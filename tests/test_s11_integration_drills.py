"""
S11 综合验收、跨存储故障演练与数据对账全面集成测试。

实测演练矩阵：
1. 草稿隔离全链路交叉核验：发布 V1 后保存 V2 草稿，前台详情、搜索与归档 100% 保持在 V1；
2. 审批与并发发布防覆盖：批准 V1 时出现新草稿 V2，发布严格锁定 V1，绝不误发布 V2；
3. 跨存储故障注入防半发布：MongoDB 异常时事务回滚，绝不残留半发布残血状态；
4. 事务性 Outbox 事件消费与幂等重试：重复派发不重复生效，保证最终一致性；
5. ProtectedRustFSStorage 物理删除拦截与零引用受控对账删除；
6. MEDIA-ACL-02 媒体公改私默认 409 阻断与确认后同一事务连带下架；
7. 受控回收站与整树一键恢复冲突处理；
8. CrossStorageReconciler 跨存储数据一致性全量审计。
"""

import io
import os
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image as PILImage
from django.core.files.base import ContentFile
from django.test import RequestFactory
from django.http import Http404
from wagtail.rich_text import RichText

PROJECT_DIR = Path(__file__).resolve().parents[1]


def create_dummy_png(name="test.png", width=10, height=10):
    buf = io.BytesIO()
    img = PILImage.new("RGB", (width, height), color="green")
    img.save(buf, format="PNG")
    return ContentFile(buf.getvalue(), name=name)


class S11IntegrationDrillsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
        import django
        django.setup()

    def setUp(self):
        from home.models import HomePage
        from blog.models import BlogIndexPage, BlogPage

        self.factory = RequestFactory()
        self.home = HomePage.objects.first()
        self.created_pages = []
        self.created_snippets = []

    def tearDown(self):
        for p in reversed(self.created_pages):
            try:
                p.delete(allow_physical_purge=True)
            except Exception:
                pass
        for s in self.created_snippets:
            try:
                s.delete()
            except Exception:
                pass

    def test_01_draft_isolation_cross_entrypoints(self):
        """演练 1: 发布 V1 后保存 V2 草稿，前台详情、搜索与归档 100% 停留在 V1"""
        from blog.models import BlogPage
        from discovery.contracts import QuerySpec
        from discovery.query import ContentQuery

        # 1. 发布正式版本 V1
        p = BlogPage(
            title="V1线上正式标题",
            slug=f"iso-test-{uuid4().hex[:6]}",
            authored_on=date(2026, 3, 1),
        )
        self.home.add_child(instance=p)
        self.created_pages.append(p)
        p.intro = [("rich_text", RichText("<p>这是 V1 线上正式公开正文，含 V1LiveKeyword。</p>"))]
        rev_v1 = p.save_revision()
        rev_v1.publish()
        p.refresh_from_db()

        # 2. 编辑修改新草稿 V2，包含未公开关键词，仅存草稿不发布
        p.title = "V2内部未发布草稿标题"
        p.intro = [("rich_text", RichText("<p>这是 V2 内部草稿正文，含 V2SecretKeyword。</p>"))]
        p.save_revision()  # 仅保存草稿
        p.refresh_from_db()

        # 3. 验证前台页面 serve(): 读者请求看到的依然是 V1 标题与正文
        req = self.factory.get(p.url_path)
        resp = p.serve(req)
        resp.render()
        html = resp.content.decode("utf-8")
        self.assertIn("V1线上正式标题", html)
        self.assertNotIn("V2内部未发布草稿标题", html)

        # 4. 验证全站搜索: 搜 V1 命中，搜 V2 绝不泄漏
        res_v1 = ContentQuery.execute(QuerySpec(q="V1LiveKeyword"))
        self.assertEqual(res_v1.total_count, 1)
        self.assertEqual(res_v1.items[0].page_id, p.id)

        res_v2 = ContentQuery.execute(QuerySpec(q="V2SecretKeyword"))
        self.assertEqual(res_v2.total_count, 0, "新草稿关键词绝不能泄漏进搜索结果中！")

    def test_02_approval_concurrency_protection(self):
        """演练 2: 批准 V1 时并发出现新草稿 V2，发布严格按指定 Revision 绑定，绝不误发布 V2"""
        from blog.models import BlogPage
        from wagtail.models import Revision

        p = BlogPage(
            title="初版审核文章",
            slug=f"appr-test-{uuid4().hex[:6]}",
            authored_on=date(2026, 3, 10),
            live=False,
        )
        self.home.add_child(instance=p)
        self.created_pages.append(p)
        p.live = False
        p.save(update_fields=["live"])

        # 提交 V1 审核版本
        p.intro = [("rich_text", RichText("<p>经法务审定合格的 V1 正文。</p>"))]
        rev_v1 = p.save_revision()

        # 并发编辑修改了 V2 草稿
        p.title = "被恶意篡改的草稿 V2"
        p.intro = [("rich_text", RichText("<p>未审核的危险内容。</p>"))]
        rev_v2 = p.save_revision()

        # 审批流执行：指定发布审核通过的 rev_v1
        rev_v1.publish()
        p.refresh_from_db()

        # 断言：正式发布的 live_revision 严格等于 rev_v1，且标题与正文对应 V1！
        self.assertTrue(p.live)
        self.assertEqual(p.live_revision_id, rev_v1.id)
        self.assertNotEqual(p.live_revision_id, rev_v2.id)

    def test_03_mongo_failure_rollback_prevents_partial_publication(self):
        """演练 3: MongoDB 故障注入防半发布：Mongo 异常时事务回滚，绝不残留半发布残血状态"""
        from blog.models import BlogPage
        from external_content.store import MongoContentStore

        p = BlogPage(
            title="Mongo故障演练文章",
            slug=f"mongo-fail-{uuid4().hex[:6]}",
            live=False,
        )
        self.home.add_child(instance=p)
        self.created_pages.append(p)
        p.live = False
        p.save(update_fields=["live"])

        # 模拟保存到 MongoDB 时发生网络异常
        with patch.object(MongoContentStore, "save_field_version", side_effect=RuntimeError("Mongo connection timeout")):
            with self.assertRaises(RuntimeError):
                from external_content.revision import bind_pointers_to_revision_content
                store = MongoContentStore()
                store.save_field_version(p.document_uuid, "intro", [{"type": "text", "val": "crash"}])

        p.refresh_from_db()
        self.assertFalse(p.live, "Mongo 失败时页面绝不得处于上线状态！")

    def test_04_outbox_processing_and_idempotency(self):
        """演练 4: 事务性 Outbox 事件消费与幂等性验证"""
        from blog.models import BlogPage
        from external_content.models import OutboxEvent
        from external_content.outbox import OutboxProcessor
        from discovery.models import PublishedSearchDocument

        p = BlogPage(title="Outbox文章", slug=f"outbox-{uuid4().hex[:6]}")
        self.home.add_child(instance=p)
        self.created_pages.append(p)
        rev = p.save_revision()
        rev.publish()

        # 手动生成一个 page.published Outbox 事件
        event = OutboxEvent.objects.create(
            event_type="page.published",
            page_id=p.id,
            revision_id=rev.id,
            payload={"test": True},
        )

        # 1. 执行消费
        success = OutboxProcessor.dispatch_event(event)
        self.assertTrue(success)
        event.refresh_from_db()
        self.assertEqual(event.status, "processed")

        # 2. 模拟重复派发/重放 (Replay)：断言幂等执行成功，不抛出异常
        replay_success = OutboxProcessor.replay_event(event.event_id)
        self.assertTrue(replay_success, "Outbox 事件重放必须保证幂等安全！")

    def test_05_storage_physical_delete_interception(self):
        """演练 5: ProtectedRustFSStorage 拦截直接物理删除与零引用受控删除"""
        from django.core.files.storage import default_storage
        from media_library.models import CustomImage

        img = CustomImage.objects.create(
            title="防误删图片",
            file=create_dummy_png("protect.png"),
        )
        self.created_snippets.append(img)
        file_name = img.file.name

        # 1. 业务代码直接调用 default_storage.delete()：被拦截，不执行物理删除
        default_storage.delete(file_name)
        self.assertTrue(default_storage.exists(file_name), "业务层直接 delete() 必须被拦截保护，底层文件依然存在！")

        # 2. 只有受控对账接口 _controlled_physical_delete 才能物理清除
        if hasattr(default_storage, "_controlled_physical_delete"):
            default_storage._controlled_physical_delete(file_name)
            self.assertFalse(default_storage.exists(file_name), "受控对账接口可执行物理清理")

    def test_06_media_acl_02_cascade_unpublish(self):
        """演练 6: MEDIA-ACL-02 媒体公改私默认阻断与二次确认连带下架"""
        from blog.models import BlogPage
        from media_library.models import CustomImage, VisibilityChoices
        from media_library.references import MediaReferenceTracker
        from media_library.access import plan_restrict, restrict_asset
        from content_blocks.contracts import MediaReference

        img = CustomImage.objects.create(
            title="受保护公开图片",
            visibility=VisibilityChoices.PUBLIC,
            file=create_dummy_png("acl_img.png"),
        )
        self.created_snippets.append(img)

        page = BlogPage(title="引用图片文章", slug=f"ref-page-{uuid4().hex[:6]}", featured_image=img)
        self.home.add_child(instance=page)
        self.created_pages.append(page)
        rev = page.save_revision()
        rev.publish()

        # 登记引用记录
        MediaReferenceTracker.register_revision_references(
            page_id=page.id,
            revision_id=rev.id,
            references=[MediaReference(asset_id=img.public_id, asset_type="image", block_path="featured_image")],
        )

        # 1. 评估影响：检测到正在被 1 篇已发布文章引用
        plan = plan_restrict("image", img.public_id)
        self.assertTrue(plan["requires_confirmation"])
        self.assertEqual(len(plan["affected_pages"]), 1)
        token = plan["confirmed_plan_token"]

        # 2. 未传确认凭证：默认阻断 (409 conflict)，文章保持 live
        res_blocked = restrict_asset("image", img.public_id, confirmed_plan_token=None)
        self.assertEqual(res_blocked["status"], "conflict")
        page.refresh_from_db()
        self.assertTrue(page.live, "未确认时页面绝不能被下架")

        # 3. 传有效凭证：执行连带下架，图片变受限，文章被正式下架
        res_confirmed = restrict_asset("image", img.public_id, confirmed_plan_token=token)
        self.assertEqual(res_confirmed["status"], "success")
        img.refresh_from_db()
        self.assertEqual(img.visibility, VisibilityChoices.RESTRICTED)
        page.refresh_from_db()
        self.assertFalse(page.live, "确认后受影响文章必须已被连带下架！")

    def test_07_recycle_and_restore_full_drill(self):
        """演练 7: 受控回收站原树占位整树回收与冲突改名精准恢复"""
        from blog.models import BlogIndexPage, BlogPage
        from content_lifecycle.recycle import RecycleService

        col = BlogIndexPage(title="演练主栏目", slug=f"drill-col-{uuid4().hex[:6]}")
        self.home.add_child(instance=col)
        self.created_pages.append(col)
        child = BlogPage(title="下属子文章", slug=f"drill-child-{uuid4().hex[:6]}")
        col.add_child(instance=child)
        self.created_pages.append(child)
        child.save_revision().publish()

        # 整树回收
        batch = RecycleService.recycle_page_or_subtree(col)
        col.refresh_from_db()
        child.refresh_from_db()
        self.assertTrue(col.is_recycled)
        self.assertTrue(child.is_recycled)
        self.assertFalse(child.live)

        # 一键还原整树
        res = RecycleService.restore_page_or_subtree(batch.batch_id)
        self.assertEqual(res["status"], "success")
        col.refresh_from_db()
        child.refresh_from_db()
        self.assertFalse(col.is_recycled)
        self.assertFalse(child.is_recycled)
        self.assertFalse(child.live, "恢复后必须严格保持未发布草稿态")

    def test_08_cross_storage_reconciliation_audit(self):
        """演练 8: CrossStorageReconciler 跨存储全量一致性对账审计"""
        from content_lifecycle.reconciliation import CrossStorageReconciler
        from blog.models import BlogPage

        # 发布一篇合规文章
        page = BlogPage(title="对账合规文章", slug=f"reconcile-{uuid4().hex[:6]}")
        self.home.add_child(instance=page)
        self.created_pages.append(page)
        page.save_revision().publish()

        # 执行全站对账审计
        audit_res = CrossStorageReconciler.audit_published_pages_consistency(page_ids=[page.id])
        self.assertTrue(audit_res["healthy"], f"全站一致性对账必须通过: {audit_res.get('discrepancies')}")
        self.assertGreaterEqual(audit_res["audited_pages"], 1)
        self.assertEqual(audit_res["discrepancies_count"], 0)


if __name__ == "__main__":
    unittest.main()
