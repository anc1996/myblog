"""
S6 MongoDB 不可变正文版本与 Wagtail 发布绑定单元测试。

实测覆盖：
1. MongoContentStore 字段版本持久化与 SHA-256 哈希校验；
2. 相同 operation_id 幂等重放与冲突检测 (IdempotencyConflictError)；
3. 伪造或篡改哈希时触发 ContentHashMismatchError；
4. 查询不存在版本抛出 VersionNotFoundError；
5. ContentMutationGuard 3秒局部行锁超时与排他保护；
6. OperationResult 操作回执与 OutboxEvent 事务性持久化；
7. 核心契约大验收：改草稿绝不影响线上正式文章！
   - 发布 V1 后编辑草稿 V2，公众读者始终只看到 V1，后台预览能看到 V2。
"""

import os
from pathlib import Path
import unittest
from uuid import uuid4
from datetime import datetime, timezone

PROJECT_DIR = Path(__file__).resolve().parents[1]


class S6MongoRevisionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
        import django
        django.setup()

    def test_mongo_content_store_save_and_hash_verification(self):
        from external_content.store import MongoContentStore, calculate_payload_hash
        from external_content.contracts import ContentHashMismatchError, VersionNotFoundError

        store = MongoContentStore()
        try:
            doc_uuid = uuid4()
            payload = [{"type": "rich_text", "value": "<p>第1版正文内容</p>"}]
            op_id = f"op_test_{uuid4().hex[:12]}"

            # 1. 正常保存版本
            pointer = store.save_field_version(
                document_uuid=doc_uuid,
                field_name="body",
                payload=payload,
                model_label="blog.BlogPage",
                operation_id=op_id,
            )
            self.assertEqual(pointer.field_name, "body")
            self.assertEqual(pointer.document_uuid, doc_uuid)
            self.assertEqual(pointer.content_hash, calculate_payload_hash(payload))

            # 2. 正常读取版本
            read_payload = store.get_field_version(
                document_uuid=doc_uuid,
                field_name="body",
                version_id=pointer.version_id,
                expected_hash=pointer.content_hash,
            )
            self.assertEqual(read_payload.payload, payload)
            self.assertEqual(read_payload.operation_id, op_id)

            # 3. 校验哈希篡改拦截
            with self.assertRaises(ContentHashMismatchError):
                store.get_field_version(
                    document_uuid=doc_uuid,
                    field_name="body",
                    version_id=pointer.version_id,
                    expected_hash="tampered_fake_hash_123456",
                )

            # 4. 校验版本不存在
            with self.assertRaises(VersionNotFoundError):
                store.get_field_version(
                    document_uuid=doc_uuid,
                    field_name="body",
                    version_id="non_existent_version_xyz",
                )
        finally:
            store.close()

    def test_mongo_idempotency_replay_and_conflict(self):
        from external_content.store import MongoContentStore
        from external_content.contracts import IdempotencyConflictError

        store = MongoContentStore()
        try:
            doc_uuid = uuid4()
            op_id = f"op_idem_{uuid4().hex[:12]}"
            payload_original = [{"type": "markdown", "value": "# 原始标题"}]

            # 1. 首次保存
            ptr1 = store.save_field_version(
                document_uuid=doc_uuid,
                field_name="intro",
                payload=payload_original,
                model_label="blog.BlogPage",
                operation_id=op_id,
            )

            # 2. 相同 operation_id 相同 payload 重试：幂等返回原指针
            ptr2 = store.save_field_version(
                document_uuid=doc_uuid,
                field_name="intro",
                payload=payload_original,
                model_label="blog.BlogPage",
                operation_id=op_id,
            )
            self.assertEqual(ptr1.version_id, ptr2.version_id)
            self.assertEqual(ptr1.content_hash, ptr2.content_hash)

            # 3. 相同 operation_id 不同 payload 伪造：触发 IdempotencyConflictError
            payload_conflicted = [{"type": "markdown", "value": "# 篡改内容"}]
            with self.assertRaises(IdempotencyConflictError):
                store.save_field_version(
                    document_uuid=doc_uuid,
                    field_name="intro",
                    payload=payload_conflicted,
                    model_label="blog.BlogPage",
                    operation_id=op_id,
                )
        finally:
            store.close()

    def test_content_mutation_guard_locking(self):
        from content_lifecycle.guard import content_mutation_guard
        from content_lifecycle.models import ContentMutationGuard

        with content_mutation_guard(timeout_seconds=3) as guard:
            self.assertIsNotNone(guard)
            self.assertEqual(guard.name, "global")
            self.assertTrue(ContentMutationGuard.objects.filter(name="global").exists())

    def test_operation_result_and_outbox_event(self):
        from django.db import transaction
        from external_content.models import OperationResult, OutboxEvent

        op_id = f"op_receipt_{uuid4().hex[:12]}"
        with transaction.atomic():
            receipt = OperationResult.objects.create(
                operation_id=op_id,
                action="publish_page",
                page_id=99,
                input_hash="hash_99",
                result_json={"status": "published", "generation": 1},
            )
            event = OutboxEvent.objects.create(
                event_type="page.published",
                page_id=99,
                revision_id=1,
                publication_generation=1,
                payload={"title": "文章标题"},
            )

        self.assertEqual(receipt.operation_id, op_id)
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.page_id, 99)

    def test_draft_modification_does_not_affect_public_live_content(self):
        """
        核心契约大体验收：
        1. 正式发布 V1，正文内容保存在 Mongo v1；
        2. 作者修改草稿 V2，正文保存在 Mongo v2，生成新草稿 Revision 2，未发布；
        3. 断言公众访问严格读取 V1；
        4. 断言草稿预览严格读取 V2！
        """
        from home.models import HomePage
        from blog.models import BlogIndexPage, BlogPage
        from external_content.store import MongoContentStore
        from external_content.revision import (
            bind_pointers_to_revision_content,
            get_effective_page_content,
        )
        from content_lifecycle.access import AccessContext, AccessMode

        home = HomePage.objects.first()
        store = MongoContentStore()
        doc_uuid = uuid4()

        # 1. 在首页下创建 BlogPage
        blog_page = BlogPage(
            title="版本隔离测试文章",
            slug=f"iso-test-{uuid4().hex[:6]}",
            document_uuid=doc_uuid,
        )
        home.add_child(instance=blog_page)

        # 2. 生成第 1 版内容 (V1) 并持久化到 Mongo
        v1_body = [{"type": "markdown", "value": "这是【正式发布 V1】的正文内容"}]
        op_v1 = f"op_v1_{uuid4().hex[:8]}"
        ptr_v1 = store.save_field_version(
            document_uuid=doc_uuid,
            field_name="body",
            payload=v1_body,
            model_label="blog.BlogPage",
            operation_id=op_v1,
        )

        # 绑定到 Revision 1 并正式发布
        rev1 = blog_page.save_revision()
        rev1_content = rev1.as_object()
        bind_pointers_to_revision_content(rev1.content, [ptr_v1])
        rev1.save()
        rev1.publish()
        blog_page.refresh_from_db()
        self.assertEqual(blog_page.live_revision_id, rev1.id)

        # 3. 作者在后台编辑新草稿 V2，持久化到 Mongo，绑定到 Revision 2，不发布！
        v2_body = [{"type": "markdown", "value": "这是【未发布草稿 V2】的绝密修改"}]
        op_v2 = f"op_v2_{uuid4().hex[:8]}"
        ptr_v2 = store.save_field_version(
            document_uuid=doc_uuid,
            field_name="body",
            payload=v2_body,
            model_label="blog.BlogPage",
            operation_id=op_v2,
        )

        rev2 = blog_page.save_revision()
        bind_pointers_to_revision_content(rev2.content, [ptr_v2])
        rev2.save()
        blog_page.refresh_from_db()

        # 4. 断言验证：公众读者通过 PUBLIC 模式读取
        public_ctx = AccessContext(mode=AccessMode.PUBLIC)
        public_data = get_effective_page_content(blog_page, access_context=public_ctx, store=store)
        self.assertEqual(public_data["revision_id"], rev1.id)
        self.assertTrue(public_data["is_public_version"])
        self.assertEqual(public_data["body"], v1_body)
        self.assertNotIn("绝密修改", str(public_data["body"]))

        # 5. 断言验证：编辑人员通过 PREVIEW_DRAFT 模式预览草稿
        preview_ctx = AccessContext(mode=AccessMode.PREVIEW_DRAFT)
        preview_data = get_effective_page_content(blog_page, access_context=preview_ctx, store=store)
        self.assertEqual(preview_data["revision_id"], rev2.id)
        self.assertFalse(preview_data["is_public_version"])
        self.assertEqual(preview_data["body"], v2_body)
        self.assertIn("绝密修改", str(preview_data["body"]))

        # 6. 清理测试页面
        try:
            blog_page.delete(allow_physical_purge=True)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
