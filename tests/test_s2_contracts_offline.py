"""
S2 公共接口与数据契约离线测试（零数据库/网络 IO 守卫）。

在不连接数据库、不运行迁移、不访问外部网络的前提下，验证：
1. content_blocks.contracts: BlockContract 协议、MediaReference 与返回数据类；
2. external_content.contracts: 字段版本指针、负载结构与异常分级体系；
3. content_lifecycle.access: 访问模式、上下文边界与可见性判断逻辑；
4. discovery.contracts: ContentQuery 参数规范（含 SEARCH-MODE-02 与游标重置契约）与 TypeAdapter 注册协议；
5. 路径断言守卫：所有四个公共模块严格位于本项目工作区之内。
"""

import os
import sys
from pathlib import Path
import unittest
from uuid import uuid4
from datetime import date, datetime

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
from myblog.bootstrap import configure_app_imports
configure_app_imports()


class S2ContractsOfflineTests(unittest.TestCase):
    """S2 四大公共契约模块纯内存数据结构与接口测试"""

    def test_content_blocks_contracts(self):
        from content_blocks.contracts import (
            BlockContract,
            MediaReference,
            BlockValidationResult,
            BlockRenderResult,
            BlockTextExtraction,
        )

        # 1. 验证数据类实例化
        ref_id = uuid4()
        ref = MediaReference(asset_id=ref_id, asset_type="image", block_path="body.0.image")
        self.assertEqual(ref.asset_id, ref_id)
        self.assertEqual(ref.asset_type, "image")

        val = BlockValidationResult(is_valid=True)
        self.assertTrue(val.is_valid)
        self.assertEqual(val.errors, [])

        render = BlockRenderResult(html="<p>Test</p>", is_safe=True)
        self.assertTrue(render.is_safe)

        extraction = BlockTextExtraction(plain_text="Hello Wagtail", logical_segments=["Hello", "Wagtail"])
        self.assertEqual(extraction.plain_text, "Hello Wagtail")

        # 2. 验证协议运行时校验 (Protocol)
        class DummyBlock:
            type_name = "dummy"
            schema_version = 1

            def validate(self, value, context=None):
                return BlockValidationResult(is_valid=True)

            def render(self, value, context=None):
                return BlockRenderResult(html="<div></div>")

            def extract_text(self, value):
                return BlockTextExtraction(plain_text="text")

            def extract_media_refs(self, value, path=""):
                return []

        dummy = DummyBlock()
        self.assertTrue(isinstance(dummy, BlockContract))

    def test_external_content_contracts(self):
        from external_content.contracts import (
            FieldVersionPointer,
            FieldVersionPayload,
            ExternalContentError,
            VersionNotFoundError,
            ContentHashMismatchError,
            SchemaVersionUnsupportedError,
            IdempotencyConflictError,
        )

        # 1. 验证异常继承层次
        self.assertTrue(issubclass(VersionNotFoundError, ExternalContentError))
        self.assertTrue(issubclass(ContentHashMismatchError, ExternalContentError))
        self.assertTrue(issubclass(SchemaVersionUnsupportedError, ExternalContentError))
        self.assertTrue(issubclass(IdempotencyConflictError, ExternalContentError))

        # 2. 验证版本指针
        doc_id = uuid4()
        pointer = FieldVersionPointer(
            document_uuid=doc_id,
            field_name="body",
            version_id="ver_001",
            schema_version=1,
            content_hash="sha256_dummy_hash",
        )
        self.assertEqual(pointer.field_name, "body")
        self.assertEqual(pointer.document_uuid, doc_id)

        # 3. 验证完整存储负载
        payload = FieldVersionPayload(
            version_id="ver_001",
            document_uuid=doc_id,
            model_label="blog.BlogPage",
            field_name="body",
            schema_version=1,
            block_schema_versions={"markdown": 1, "image": 1},
            content_hash="sha256_dummy_hash",
            payload=[{"type": "markdown", "value": "# Title"}],
            created_at=datetime.now(),
            operation_id="op_save_001",
        )
        self.assertEqual(payload.model_label, "blog.BlogPage")

    def test_content_lifecycle_access(self):
        from content_lifecycle.access import AccessMode, AccessContext, PublicationStatus

        # 1. 验证枚举
        self.assertEqual(AccessMode.PUBLIC.value, "public")
        self.assertEqual(AccessMode.PREVIEW_DRAFT.value, "preview_draft")

        # 2. 验证上下文属性
        public_ctx = AccessContext(mode=AccessMode.PUBLIC)
        self.assertTrue(public_ctx.is_public_reader)

        draft_ctx = AccessContext(mode=AccessMode.PREVIEW_DRAFT)
        self.assertFalse(draft_ctx.is_public_reader)

        # 3. 验证页面发布可见性判定
        pub_live = PublicationStatus(page_id=1, live=True, live_revision_id=10, has_unpublished_changes=False, is_recycled=False)
        self.assertTrue(pub_live.is_publicly_visible)

        pub_recycled = PublicationStatus(page_id=1, live=True, live_revision_id=10, has_unpublished_changes=False, is_recycled=True)
        self.assertFalse(pub_recycled.is_publicly_visible)

        pub_draft = PublicationStatus(page_id=1, live=False, live_revision_id=None, has_unpublished_changes=True, is_recycled=False)
        self.assertFalse(pub_draft.is_publicly_visible)

    def test_discovery_contracts(self):
        from discovery.contracts import (
            QuerySpec,
            SearchResultItem,
            QueryResult,
            TypeAdapterContract,
        )

        # 1. 验证 QuerySpec 默认值（含 SEARCH-MODE-02 与分页参数）
        spec = QuerySpec(q="Django 数据库")
        self.assertEqual(spec.page_size, 20)
        self.assertEqual(spec.expand, "auto")
        self.assertEqual(spec.secondary, "none")  # 默认不自动展开 B
        self.assertEqual(spec.sort, "relevance")

        # 2. 验证单项与总结果结构
        item = SearchResultItem(
            page_id=10,
            effective_revision_id=25,
            type_key="blog",
            title="Django 数据库笔记",
            url="/blog/django-notes/",
            authored_on=date(2026, 11, 4),
            summary="简介摘要",
            match_mode="A",
            score_key=1000,
        )
        self.assertEqual(item.type_key, "blog")
        self.assertEqual(item.match_mode, "A")

        res = QueryResult(
            items=[item],
            total_count=1,
            match_policy="A_only",
            corpus_epoch=2,
            cursor_reset=False,
        )
        self.assertEqual(res.total_count, 1)
        self.assertFalse(res.cursor_reset)

        # 3. 验证 TypeAdapter 协议
        class DummyTypeAdapter:
            type_key = "blog"
            model_label = "blog.BlogPage"
            schema_version = 1
            extractor_version = 1
            allowed_filters = ["category", "author"]
            weight_profile = {"title": 1.0, "body": 0.4}

            def extract(self, published_snapshot):
                return {"title": "Test"}

        adapter = DummyTypeAdapter()
        self.assertTrue(isinstance(adapter, TypeAdapterContract))

    def test_import_paths_are_strictly_within_workspace(self):
        """路径守卫断言：确保 4 个模块均加载自本项目 myblog/apps/ 目录"""
        import content_blocks
        import external_content
        import content_lifecycle
        import discovery

        for mod in [content_blocks, external_content, content_lifecycle, discovery]:
            self.assertTrue(
                Path(mod.__file__).resolve().is_relative_to((PROJECT_DIR / "myblog" / "apps").resolve()),
                f"{mod} is not loaded from myblog/apps directory",
            )


if __name__ == "__main__":
    unittest.main()
