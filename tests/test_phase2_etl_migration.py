"""
Phase 2 wagtailblog3 -> myblog ETL 迁移工具全量单元与幂等故障注入测试。
"""

import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from PIL import Image as PilImage
from wagtail.contrib.redirects.models import Redirect
from wagtail.models import PageViewRestriction

from analytics.models import PageAnalyticsDaily, PageAnalyticsTotal
from blog.models import BlogIndexPage, BlogPage
from comments.models import Comment, CommentReaction
from discovery.models import PublishedSearchDocument
from external_content.contracts import IdempotencyConflictError
from external_content.models import OperationResult
from media_library.models import Audio, CustomDocument, CustomImage, MediaReferenceRecord, Video, VisibilityChoices
from people.models import Person
from scripts.etl_wagtailblog3.loader import (
    TargetIsolationError,
    apply_snapshot,
    assert_target_isolation,
    dry_run_snapshot,
    reconcile_target,
)
from scripts.etl_wagtailblog3.remote_export import (
    SNAPSHOT_SCHEMA_VERSION,
    build_remote_export_command,
    build_ssh_command,
    resolve_ssh_known_hosts,
)
from scripts.etl_wagtailblog3.transformer import (
    compute_excluded_test_page_ids,
    convert_wagtail_table_to_custom_table,
    merge_page_view_counts,
    normalize_legacy_redirect_old_path,
    rewrite_markdown_content,
    strip_html_to_plain_text,
    transform_comments_with_folding,
)
from site_settings.models import SocialMediaSettings


def _make_png_bytes(color=(20, 120, 200)) -> bytes:
    buf = io.BytesIO()
    PilImage.new("RGB", (32, 24), color=color).save(buf, format="PNG")
    return buf.getvalue()


class Phase2EtlTransformerUnitTests(TestCase):
    def test_markdown_embed_rewrite_preserves_code_blocks_and_flags_relative_paths(self):
        anomalies = []
        uid = str(uuid4())
        src_md = (
            "示例图片：\n"
            '<embed embedtype="image" id="137" format="fullwidth_web" alt="架构图" src="http://old/media/images/a.jpg" />\n\n'
            "```python\nurl = 'https://example.com/images/photo1.jpg'\nhtml = '<table>'\n```\n\n"
            "行内代码 `https://example.com/images/photo2.png` 与 `<table>` 不应被改写。\n\n"
            "![local](20大报告.assets/image-1.png)\n"
        )
        out = rewrite_markdown_content(
            src_md,
            image_id_map={137: 999},
            image_uuid_map={137: uid},
            anomalies=anomalies,
            source_page_id=1172,
        )
        self.assertIn(f"![架构图|full](/media-gateway/image/{uid}/)", out)
        self.assertIn("https://example.com/images/photo1.jpg", out)
        self.assertIn("`https://example.com/images/photo2.png`", out)
        self.assertIn("`<table>`", out)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["kind"], "unmapped_local_relative_image")

    def test_table_author_bio_comments_pv_and_redirects_transforms(self):
        tbl = convert_wagtail_table_to_custom_table({
            "data": [["H1", "H2"], ["R1", "V1"]],
            "table_header_choice": "both",
            "table_caption": "weqqq",
        })
        self.assertTrue(tbl["has_header_row"])
        self.assertTrue(tbl["has_header_col"])
        self.assertEqual(tbl["caption"], "weqqq")
        rows = json.loads(tbl["table_data"])["rows"]
        self.assertTrue(rows[0][0]["header"])
        self.assertTrue(rows[1][0]["header"])
        self.assertFalse(rows[1][1]["header"])

        bio = strip_html_to_plain_text([{"type": "bio", "value": "<p>鲁迅，原名<b>周树人</b>。<a href='http://x'>百科</a></p>"}])
        self.assertEqual(bio, "鲁迅，原名 周树人 。 百科")

        excluded = compute_excluded_test_page_ids(
            [{"id": 10, "title": "技术", "path": "000100010001"}, {"id": 526, "title": "测试页", "path": "000100010002"}],
            [{"id": 100, "path": "0001000100010001"}, {"id": 1195, "path": "0001000100020001"}],
        )
        self.assertEqual(excluded, {526, 1195})

        folded, c_anoms = transform_comments_with_folding(
            [
                {"id": 23, "page_id": 100, "parent_id": None, "status": "approved", "content": "根评论"},
                {"id": 24, "page_id": 100, "parent_id": 23, "status": "deleted", "content": "已删中间层"},
                {"id": 25, "page_id": 100, "parent_id": 24, "status": "approved", "content": "折叠回复"},
            ],
            excluded_page_ids=excluded,
        )
        self.assertEqual(len(folded), 2)
        self.assertEqual(folded[1]["folded_parent_old_id"], 23)
        self.assertIsNone(folded[1]["folded_reply_to_old_id"])
        self.assertEqual(c_anoms[0]["kind"], "comment_folded_past_deleted_parent")

        merged_pv = merge_page_view_counts(
            [
                {"page_id": 100, "date": "2026-07-01", "count": 5, "unique_count": 4, "view_count_v2": 0, "unique_visitor_count_v2": 0},
                {"page_id": 100, "date": "2026-09-01", "count": 0, "unique_count": 0, "view_count_v2": 7, "unique_visitor_count_v2": 6, "engaged": 2, "scroll_90": 1, "active_reading_seconds": 120},
                {"page_id": 1195, "date": "2026-09-01", "count": 99, "unique_count": 99, "view_count_v2": 0, "unique_visitor_count_v2": 0},
            ],
            excluded_page_ids=excluded,
        )
        self.assertEqual(list(merged_pv.keys()), [100])
        self.assertEqual(sum(r["pv"] for r in merged_pv[100]), 12)
        self.assertEqual(normalize_legacy_redirect_old_path("/zh-hans/welcome/tech/"), "/welcome/tech")


class Phase2EtlEndToEndIdempotencyTests(TestCase):
    def _build_synthetic_snapshot(self, snap_dir: Path) -> None:
        png_bytes = _make_png_bytes()
        doc_bytes = b"# Guide\n"
        av_bytes = b"ID3dummy-audio-bytes"
        vid_bytes = b"\x00\x00\x00\x18ftypmp42"

        def _write_blob(kind: str, oid: int, name: str, data: bytes) -> dict:
            sha = hashlib.sha256(data).hexdigest()
            rel = f"blobs/{kind}/{oid}_{sha[:12]}_{name}"
            p = snap_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return {
                "storage_key": f"media/{kind}/{name}",
                "local_rel_path": rel,
                "byte_size": len(data),
                "sha256": sha,
                "filename": name,
            }

        snap = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "source": "synthetic-wb3",
            "site": {"site_name": "我的博客", "home_id": 3, "home_path": "00010001", "home_url_path": "/welcome/"},
            "users": [{"id": 1, "username": "wb3_root", "email": "root@example.com", "first_name": "m", "last_name": "m", "is_superuser": True, "is_staff": True, "is_active": True}],
            "groups": [],
            "page_view_restrictions": [],
            "social_links": [
                {"platform": "github", "label": "GitHub", "url": "https://github.com/anc1996"},
                {"platform": "instagram", "label": "Instagram", "url": "https://instagram.com/demo"},
            ],
            "authors": [
                {"id": 2, "name": "鲁迅", "slug": "鲁迅", "bio_raw": "<p>文学家</p>", "author_image_id": 11},
            ],
            "categories": [{"id": 3, "name": "数据库", "slug": "db"}],
            "tags": [{"id": 1, "name": "PostgreSQL", "slug": "postgresql"}],
            "images": [
                {"id": 11, "title": "头像图", "description": "desc11", "caption": "cap11", "width": 32, "height": 24, "uploaded_by_user_id": 1, "tags": ["图库标签"], "blob": _write_blob("image", 11, "av.png", png_bytes)},
                {"id": 12, "title": "栏目封面", "description": "", "caption": "栏目cap", "width": 32, "height": 24, "uploaded_by_user_id": 1, "tags": [], "blob": _write_blob("image", 12, "idx.png", png_bytes)},
            ],
            "documents": [
                {"id": 6, "title": "课件.md", "description": "boling", "uploaded_by_user_id": 1, "tags": [], "blob": _write_blob("document", 6, "guide.md", doc_bytes)},
            ],
            "media": [
                {"id": 1, "title": "音频Demo", "type": "audio", "duration": 12.0, "uploaded_by_user_id": 1, "blob": _write_blob("audio", 1, "a.mp3", av_bytes), "thumbnail_blob": _write_blob("media_thumb", 1, "athumb.png", png_bytes)},
                {"id": 2, "title": "视频Demo", "type": "video", "duration": 25.0, "uploaded_by_user_id": 1, "blob": _write_blob("video", 2, "v.mp4", vid_bytes), "thumbnail_blob": _write_blob("media_thumb", 2, "vthumb.png", png_bytes)},
            ],
            "index_pages": [
                {"id": 20, "title": "技术专栏", "slug": "tech-wb3", "path": "000100010001", "depth": 3, "url_path": "/welcome/tech-wb3/", "show_in_menus": True, "seo_title": "", "search_description": "", "live": True, "has_unpublished_changes": False, "live_revision_id": 2010, "owner_id": 1, "featured_image_id": 12, "intro": "栏目简介<script>"},
                {"id": 526, "title": "测试页", "slug": "test-branch", "path": "000100010002", "depth": 3, "url_path": "/welcome/test-branch/", "show_in_menus": False, "seo_title": "", "search_description": "", "live": True, "has_unpublished_changes": False, "live_revision_id": 5260, "owner_id": 1, "featured_image_id": None, "intro": ""},
            ],
            "blog_pages": [
                {
                    "id": 200, "title": "父文章", "slug": "parent-post", "path": "0001000100010001", "depth": 4,
                    "url_path": "/welcome/tech-wb3/parent-post/", "show_in_menus": False, "seo_title": "ParentSEO", "search_description": "Desc",
                    "live": True, "has_unpublished_changes": False, "live_revision_id": 2000, "date": "2026-08-01", "owner_id": 1, "featured_image_id": 12,
                    "intro": "父文章简介", "body_blocks": [{"type": "markdown_block", "value": {"markdown": '正文 <embed embedtype="image" id="12" format="fullwidth_web" alt="图" />'}}],
                    "author_ids": [2], "category_names": ["数据库"], "tag_names": ["PostgreSQL"],
                },
                {
                    "id": 201, "title": "子文章", "slug": "child-post", "path": "00010001000100010001", "depth": 5,
                    "url_path": "/welcome/tech-wb3/parent-post/child-post/", "show_in_menus": False, "seo_title": "", "search_description": "",
                    "live": True, "has_unpublished_changes": False, "live_revision_id": 2011, "date": "2026-08-02", "owner_id": None, "featured_image_id": None,
                    "intro": "子文章简介", "body_blocks": [
                        {"type": "rich_text", "value": '<p>参考 <a linktype="page" id="200">父文章</a></p>'},
                        {"type": "document_block", "value": {"document": 6, "title": "附件"}},
                        {"type": "audio_block", "value": {"audio": 1, "title": "音频"}},
                        {"type": "video_block", "value": {"video": 2, "title": "视频"}},
                        {"type": "mermaid_chart", "value": {"code": "flowchart LR\nA-->B"}},
                    ],
                    "author_ids": [], "category_names": [], "tag_names": [],
                },
                {
                    "id": 1195, "title": "被跳过的测试子页", "slug": "skip-me", "path": "0001000100020001", "depth": 4,
                    "url_path": "/welcome/test-branch/skip-me/", "show_in_menus": False, "seo_title": "", "search_description": "",
                    "live": True, "has_unpublished_changes": False, "live_revision_id": 11950, "date": "2026-08-03", "owner_id": 1, "featured_image_id": None,
                    "intro": "skip", "body_blocks": [], "author_ids": [], "category_names": [], "tag_names": [],
                },
            ],
            "comments": [
                {"id": 23, "page_id": 200, "author_user_id": 1, "guest_name": "", "parent_id": None, "status": "approved", "content": "根评论 🥰", "created_at": "2026-09-01T10:00:00+08:00"},
                {"id": 24, "page_id": 200, "author_user_id": 1, "guest_name": "", "parent_id": 23, "status": "deleted", "content": "已删", "created_at": "2026-09-01T10:01:00+08:00"},
                {"id": 25, "page_id": 200, "author_user_id": 1, "guest_name": "", "parent_id": 24, "status": "approved", "content": "折叠回复 $\\perp$", "created_at": "2026-09-01T10:02:00+08:00"},
            ],
            "comment_reactions": [
                {"id": 1, "comment_id": 23, "user_id": 1, "reaction": 1},
                {"id": 2, "comment_id": 25, "user_id": 1, "reaction": -1},
            ],
            "page_views": [
                {"page_id": 200, "date": "2026-07-01", "count": 5, "unique_count": 4, "view_count_v2": 0, "unique_visitor_count_v2": 0},
                {"page_id": 200, "date": "2026-09-01", "count": 0, "unique_count": 0, "view_count_v2": 7, "unique_visitor_count_v2": 6, "engaged": 3, "scroll_90": 1, "active_reading_seconds": 90},
            ],
            "redirects": [
                {"id": 1, "old_path": "/zh-hans/old-parent/", "redirect_page_id": 200, "redirect_link": "", "is_permanent": True},
                {"id": 2, "old_path": "/zh-hans/old-test/", "redirect_page_id": 1195, "redirect_link": "", "is_permanent": True},
            ],
        }
        (snap_dir / "snapshot.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

    def test_dry_run_apply_idempotent_rerun_conflict_and_reconcile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            snap_dir = tmp_path / "snap"
            media_dir = tmp_path / "media"
            snap_dir.mkdir()
            media_dir.mkdir()
            self._build_synthetic_snapshot(snap_dir)

            with override_settings(
                MEDIA_ROOT=str(media_dir),
                STORAGES={
                    **settings.STORAGES,
                    "default": {"BACKEND": "media_library.storage.ControlledFileSystemStorage"},
                },
            ):
                # 1. 目标隔离守卫禁止未授权写 myblog_dev 或使用本地文件存储
                with override_settings(DATABASES={"default": {"NAME": "myblog_dev"}}):
                    with self.assertRaises(TargetIsolationError):
                        assert_target_isolation()

                # 2. Dry-run 零写入
                dr = dry_run_snapshot(snap_dir)
                self.assertEqual(dr["counts"]["index_pages_included"], 1)
                self.assertEqual(dr["counts"]["blog_pages_included"], 2)
                self.assertEqual(dr["excluded_test_page_ids"], [526, 1195])
                self.assertEqual(BlogPage.objects.count(), 0)

                # 3. 首次正式导入
                bid = f"test_wb3_{uuid4().hex[:6]}"
                rep1 = apply_snapshot(snap_dir, batch_id=bid, _allow_test_isolation=True)
                self.assertEqual(rep1["mapped_counts"]["pages"], 3)
                self.assertEqual(BlogIndexPage.objects.count(), 1)
                self.assertEqual(BlogPage.objects.count(), 2)
                self.assertEqual(CustomImage.objects.count(), 4)  # 2 原图 + 2 缩略图
                self.assertEqual(CustomDocument.objects.count(), 1)
                self.assertEqual(Audio.objects.count(), 1)
                self.assertEqual(Video.objects.count(), 1)
                self.assertIsNotNone(Video.objects.first().poster)
                self.assertEqual(Comment.objects.count(), 2)
                self.assertEqual(CommentReaction.objects.count(), 2)
                self.assertEqual(PageAnalyticsTotal.objects.first().total_pv, 12)
                self.assertEqual(PageAnalyticsDaily.objects.count(), 2)
                self.assertEqual(SocialMediaSettings.objects.first().social_links[1].value["platform"], "custom")
                self.assertEqual(Person.objects.get(name="鲁迅").slug, "lu-xun")

                # 核验子文章在父文章之下，且富文本链接重写为新父文章 ID
                parent_post = BlogPage.objects.get(slug="parent-post")
                child_post = BlogPage.objects.get(slug="child-post")
                self.assertEqual(child_post.get_parent().id, parent_post.id)
                self.assertFalse(parent_post.get_view_restrictions().exists())
                self.assertFalse(child_post.get_view_restrictions().exists())
                child_post.hydrate_from_mongo()
                first_rt = str(child_post.body[0].value.source)
                self.assertIn(f'id="{parent_post.id}"', first_rt)
                self.assertIn("数据库", list(parent_post.tags.names()))

                # 核验前台真实 HTML 渲染（Markdown 不得退化为字面量 {'markdown': ...}，文档/音视频标题不丢失）
                parent_resp = self.client.get(parent_post.url)
                self.assertEqual(parent_resp.status_code, 200)
                parent_html = parent_resp.content.decode("utf-8")
                self.assertNotIn("{'markdown':", parent_html)
                self.assertIn("正文", parent_html)
                self.assertIn("/media-gateway/image/", parent_html)
                child_resp = self.client.get(child_post.url)
                self.assertEqual(child_resp.status_code, 200)
                child_html = child_resp.content.decode("utf-8")
                self.assertIn("附件", child_html)
                self.assertIn("音频", child_html)
                self.assertIn("视频", child_html)
                self.assertIn("mermaid", child_html)

                # 核验被引用的图片与文档均已自动公开
                self.assertEqual(CustomImage.objects.get(title="栏目封面").visibility, VisibilityChoices.PUBLIC)
                self.assertEqual(CustomDocument.objects.get(title="课件.md").visibility, VisibilityChoices.PUBLIC)
                self.assertTrue(MediaReferenceRecord.objects.filter(page_id=parent_post.id).exists())
                self.assertEqual(PublishedSearchDocument.objects.filter(page_id__in=[parent_post.id, child_post.id]).count(), 2)
                self.assertTrue(Redirect.objects.filter(old_path="/old-parent").exists())
                self.assertFalse(Redirect.objects.filter(old_path="/old-test").exists())

                # 4. 第二次重复运行：100% 幂等回放，零重复建页/翻倍 PV
                rep2 = apply_snapshot(snap_dir, batch_id=bid, _allow_test_isolation=True)
                self.assertEqual(rep2["stats"]["created"], {})
                self.assertEqual(BlogPage.objects.count(), 2)
                self.assertEqual(PageAnalyticsTotal.objects.first().total_pv, 12)
                self.assertEqual(Comment.objects.count(), 2)

                # 5. 对账核验
                rec = reconcile_target(snap_dir, batch_id=bid)
                self.assertEqual(rec["integrity"]["missing_storage_keys"], [])
                self.assertEqual(rec["integrity"]["unpromoted_referenced_images"], 0)
                self.assertEqual(rec["integrity"]["unpromoted_public_assets"], 0)
                self.assertEqual(rec["integrity"]["leaked_restricted_only_assets"], 0)
                self.assertEqual(rec["integrity"]["missing_mongo_versions"], [])
                self.assertEqual(rec["integrity"]["content_hash_mismatches"], [])
                self.assertEqual(rec["integrity"]["broken_internal_links"], [])
                self.assertEqual(rec["integrity"]["unconverted_legacy_embeds"], [])
                self.assertEqual(rec["integrity"]["residual_relative_media_links"], [])
                self.assertEqual(rec["integrity"]["residual_relative_media_links_count"], 0)
                self.assertGreater(rec["integrity"]["checked_mongo_pointers"], 0)
                self.assertEqual(rec["expected_vs_actual"]["expected_live_blogs"], {"expected": 2, "actual": 2})
                self.assertEqual(rec["expected_vs_actual"]["expected_draft_blogs"], {"expected": 0, "actual": 0})
                self.assertEqual(rec["expected_vs_actual"]["expected_restricted_blogs"], {"expected": 0, "actual": 0})
                self.assertEqual(rec["expected_vs_actual"]["pv_total_sum"]["actual"], 12)

                # 6. 同 operation_id 输入篡改触发幂等冲突保护停机
                op_row = OperationResult.objects.filter(operation_id=f"{bid}:pub_blog:200").first()
                self.assertIsNotNone(op_row)
                snap_modified = json.loads((snap_dir / "snapshot.json").read_text(encoding="utf-8"))
                snap_modified["blog_pages"][0]["title"] = "被篡改标题"
                (snap_dir / "snapshot.json").write_text(json.dumps(snap_modified, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(IdempotencyConflictError):
                    apply_snapshot(snap_dir, batch_id=bid, _allow_test_isolation=True, skip_search_rebuild=True)

    def test_mongo_prewritten_sql_rollback_recovery_is_idempotent(self):
        """模拟 Mongo 已写入但 SQL 回滚后重跑，应直接复用同 operation_id 版本而不报错"""
        from external_content.store import MongoContentStore

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            snap_dir = tmp_path / "snap"
            media_dir = tmp_path / "media"
            snap_dir.mkdir()
            media_dir.mkdir()
            self._build_synthetic_snapshot(snap_dir)

            with override_settings(
                MEDIA_ROOT=str(media_dir),
                STORAGES={
                    **settings.STORAGES,
                    "default": {"BACKEND": "media_library.storage.ControlledFileSystemStorage"},
                },
            ):
                bid = f"test_rec_{uuid4().hex[:6]}"
                # 先只跑 dry_run，再预写 1 条 Mongo 操作回执模拟半途中断
                store = MongoContentStore()
                try:
                    fake_uuid = uuid4()
                    ptr1 = store.save_field_version(
                        document_uuid=fake_uuid,
                        field_name="intro",
                        payload=[{"type": "rich_text", "value": "<p>父文章简介</p>"}],
                        model_label="blog.BlogPage",
                        operation_id=f"{bid}:mongo:200:v1_intro",
                    )
                    ptr2 = store.save_field_version(
                        document_uuid=fake_uuid,
                        field_name="intro",
                        payload=[{"type": "rich_text", "value": "<p>父文章简介</p>"}],
                        model_label="blog.BlogPage",
                        operation_id=f"{bid}:mongo:200:v1_intro",
                    )
                    self.assertEqual(ptr1.version_id, ptr2.version_id)
                finally:
                    store.close()

    def test_reconcile_detects_mongo_tampering_broken_links_and_relative_paths(self):
        """对账完整性故障注入：校验 Mongo 哈希/载荷篡改、缺失版本、站内断链、残留嵌入与本地相对图片路径。"""
        from blog.blocks import BlogBodyBlocks
        from external_content.store import MongoContentStore

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            snap_dir = tmp_path / "snap"
            media_dir = tmp_path / "media"
            snap_dir.mkdir()
            media_dir.mkdir()
            self._build_synthetic_snapshot(snap_dir)

            with override_settings(
                MEDIA_ROOT=str(media_dir),
                STORAGES={
                    **settings.STORAGES,
                    "default": {"BACKEND": "media_library.storage.ControlledFileSystemStorage"},
                },
            ):
                bid = f"test_rec_fault_{uuid4().hex[:6]}"
                apply_snapshot(snap_dir, batch_id=bid, _allow_test_isolation=True, skip_search_rebuild=True)

                baseline = reconcile_target(snap_dir, batch_id=bid)
                self.assertGreater(baseline["integrity"]["checked_mongo_pointers"], 0)
                self.assertEqual(baseline["integrity"]["missing_mongo_versions"], [])
                self.assertEqual(baseline["integrity"]["content_hash_mismatches"], [])
                self.assertEqual(baseline["integrity"]["broken_internal_links"], [])
                self.assertEqual(baseline["integrity"]["unconverted_legacy_embeds"], [])
                self.assertEqual(baseline["integrity"]["residual_relative_media_links"], [])

                parent_post = BlogPage.objects.get(slug="parent-post")
                child_post = BlogPage.objects.get(slug="child-post")
                parent_rev = parent_post.live_revision
                parent_ptrs = (
                    parent_rev.content.get("_external_field_pointers", [])
                    if isinstance(parent_rev.content, dict)
                    else json.loads(parent_rev.content).get("_external_field_pointers", [])
                )
                body_ptr = next(p for p in parent_ptrs if p["field_name"] == "body")

                store = MongoContentStore()
                try:
                    col = store._get_db()["content_field_versions"]
                    filt = {
                        "document_uuid": str(parent_post.document_uuid),
                        "field_name": "body",
                        "version_id": body_ptr["version_id"],
                    }
                    orig_doc = col.find_one(filt)
                    self.assertIsNotNone(orig_doc)

                    # 1a. 篡改 MongoDB 文档的 content_hash，必须检出 content_hash_mismatches
                    col.update_one(filt, {"$set": {"content_hash": "0" * 64}})
                    rec_hash = reconcile_target(snap_dir, batch_id=bid)
                    self.assertEqual(len(rec_hash["integrity"]["content_hash_mismatches"]), 1)
                    self.assertEqual(rec_hash["integrity"]["content_hash_mismatches"][0]["page_id"], parent_post.id)
                    self.assertEqual(rec_hash["integrity"]["content_hash_mismatches"][0]["field_name"], "body")
                    col.update_one(filt, {"$set": {"content_hash": orig_doc["content_hash"]}})

                    # 1b. 仅篡改 MongoDB 文档的 payload（保留原 content_hash），仍必须检出 content_hash_mismatches
                    col.update_one(filt, {"$set": {"payload": [{"type": "markdown", "value": "被篡改负载"}]}})
                    rec_payload = reconcile_target(snap_dir, batch_id=bid)
                    self.assertEqual(len(rec_payload["integrity"]["content_hash_mismatches"]), 1)
                    self.assertEqual(rec_payload["integrity"]["content_hash_mismatches"][0]["page_id"], parent_post.id)
                    col.update_one(filt, {"$set": {"payload": orig_doc["payload"]}})

                    # 1c. 删除 MongoDB 版本文档，必须检出 missing_mongo_versions
                    col.delete_one(filt)
                    rec_missing = reconcile_target(snap_dir, batch_id=bid)
                    self.assertEqual(len(rec_missing["integrity"]["missing_mongo_versions"]), 1)
                    self.assertEqual(rec_missing["integrity"]["missing_mongo_versions"][0]["page_id"], parent_post.id)
                    col.insert_one(orig_doc)
                finally:
                    store.close()

                # 2 & 3. 在已迁移页面正文注入断链（含属性反序 page link、缺失 asset://）、残留 embed 与本地相对路径图
                # 直接写入一致的 MongoDB 新版本并更新 live_revision 指针，绕过编辑器保存门禁模拟已存库坏链接/遗留标签
                from external_content.store import calculate_payload_hash
                from wagtail.models import Revision

                missing_asset_uid = str(uuid4())
                tampered_body_payload = [
                    {
                        "type": "rich_text",
                        "value": (
                            '<p><a id="999999" linktype="page">不存在页面</a> '
                            '<a linktype="document" id="888888">不存在文档</a></p>'
                        ),
                    },
                    {
                        "type": "markdown",
                        "value": (
                            "![图](20大报告.assets/pic.png)\n\n"
                            f"![缺失资产](asset://image/{missing_asset_uid})\n\n"
                            "![无效格式](asset://image/not-a-uuid)\n\n"
                            "[旧站文档](/media/documents/old.pdf)\n\n"
                            '<embed embedtype="image" id="12" format="fullwidth_web" />\n\n'
                            "行内代码 `![示例](ignored.assets/ok.png)` 与 `<embed embedtype=\"image\" id=\"1\" />` 不应误报。"
                        ),
                    },
                ]
                # 验证 reconcile_target 严格只读：不调用 MongoContentStore._ensure_indexes / create_index
                from pymongo.collection import Collection as MongoCollection

                store = MongoContentStore()
                try:
                    ptr = store.save_field_version(
                        document_uuid=child_post.document_uuid,
                        field_name="body",
                        payload=tampered_body_payload,
                        model_label="blog.BlogPage",
                        operation_id=f"{bid}:inject_broken_links:{uuid4().hex[:6]}",
                    )
                    live_rev = child_post.live_revision
                    rev_content = dict(live_rev.content)
                    kept_ptrs = [
                        p for p in (rev_content.get("_external_field_pointers") or [])
                        if isinstance(p, dict) and p.get("field_name") != "body"
                    ]
                    rev_content["_external_field_pointers"] = kept_ptrs + [
                        {
                            "document_uuid": str(child_post.document_uuid),
                            "field_name": "body",
                            "version_id": ptr.version_id,
                            "content_hash": calculate_payload_hash(tampered_body_payload),
                        }
                    ]
                    Revision.objects.filter(pk=live_rev.pk).update(content=rev_content)
                finally:
                    store.close()

                with patch.object(MongoCollection, "create_index", side_effect=AssertionError("reconcile_target must be read-only")):
                    rec_links = reconcile_target(snap_dir, batch_id=bid)
                self.assertEqual(rec_links["integrity"]["missing_mongo_versions"], [])
                self.assertEqual(rec_links["integrity"]["content_hash_mismatches"], [])

                broken = rec_links["integrity"]["broken_internal_links"]
                self.assertEqual(len(broken), 5)
                self.assertTrue(any(b["link_type"] == "page" and b["target_id"] == 999999 and b["page_id"] == child_post.id for b in broken))
                self.assertTrue(any(b["link_type"] == "document" and b["target_id"] == 888888 and b["page_id"] == child_post.id for b in broken))
                self.assertTrue(any(b["link_type"] == "asset:image" and b["target_id"] == missing_asset_uid and b["page_id"] == child_post.id for b in broken))
                self.assertTrue(any(b["link_type"] == "asset:image" and b["target_id"] == "not-a-uuid" and b["page_id"] == child_post.id for b in broken))
                self.assertTrue(any(b["link_type"] == "legacy_media_url" and b["target_id"] == "/media/documents/old.pdf" and b["page_id"] == child_post.id for b in broken))

                embeds = rec_links["integrity"]["unconverted_legacy_embeds"]
                self.assertEqual(len(embeds), 1)
                self.assertEqual(embeds[0]["page_id"], child_post.id)

                rel_links = rec_links["integrity"]["residual_relative_media_links"]
                self.assertEqual(len(rel_links), 1)
                self.assertEqual(rel_links[0], {"page_id": child_post.id, "path": "20大报告.assets/pic.png"})
                self.assertEqual(rec_links["integrity"]["residual_relative_media_links_count"], 1)

                # 4. 整组指针丢失（如 Revision _external_field_pointers 为空）必须明确报出 missing_expected_field_pointer
                Revision.objects.filter(pk=live_rev.pk).update(content={**rev_content, "_external_field_pointers": []})
                rec_empty_ptrs = reconcile_target(snap_dir, batch_id=bid)
                self.assertTrue(
                    any(
                        m["page_id"] == child_post.id and m["error"] == "missing_expected_field_pointer"
                        for m in rec_empty_ptrs["integrity"]["missing_mongo_versions"]
                    )
                )


class Phase2EtlP0SecurityAndProtectionTests(TestCase):
    """P0-2 安全加固测试：三端隔离、SSH 主机校验、快照版本、草稿与 PageViewRestriction 保真。"""

    def _build_base_v2_snapshot(self, snap_dir: Path) -> dict:
        png_bytes = _make_png_bytes((10, 80, 160))
        doc_bytes = b"# Secret or Public Doc\n"
        vid_bytes = b"\x00\x00\x00\x18ftypmp42"

        def _write_blob(kind: str, oid: int, name: str, data: bytes) -> dict:
            sha = hashlib.sha256(data).hexdigest()
            rel = f"blobs/{kind}/{oid}_{sha[:12]}_{name}"
            p = snap_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return {
                "storage_key": f"media/{kind}/{name}",
                "local_rel_path": rel,
                "byte_size": len(data),
                "sha256": sha,
                "filename": name,
            }

        return {
            "schema_version": 2,
            "source": "synthetic-wb3-p0",
            "site": {"site_name": "我的博客", "home_id": 3, "home_path": "00010001", "home_url_path": "/welcome/"},
            "users": [
                {"id": 1, "username": "wb3_admin", "email": "admin@example.com", "first_name": "A", "last_name": "D", "is_superuser": True, "is_staff": True, "is_active": True},
                {"id": 2, "username": "wb3_author", "email": "author@example.com", "first_name": "B", "last_name": "U", "is_superuser": False, "is_staff": False, "is_active": True},
            ],
            "groups": ["vip_readers"],
            "page_view_restrictions": [],
            "social_links": [],
            "authors": [],
            "categories": [],
            "tags": [],
            "images": [
                {"id": 101, "title": "公开文章配图", "description": "", "caption": "", "width": 32, "height": 24, "uploaded_by_user_id": 1, "tags": [], "blob": _write_blob("image", 101, "pub.png", png_bytes)},
                {"id": 102, "title": "草稿独享图", "description": "", "caption": "", "width": 32, "height": 24, "uploaded_by_user_id": 1, "tags": [], "blob": _write_blob("image", 102, "draft.png", png_bytes)},
                {"id": 103, "title": "登录受限图", "description": "", "caption": "", "width": 32, "height": 24, "uploaded_by_user_id": 1, "tags": [], "blob": _write_blob("image", 103, "login.png", png_bytes)},
                {"id": 104, "title": "分组受限图", "description": "", "caption": "", "width": 32, "height": 24, "uploaded_by_user_id": 1, "tags": [], "blob": _write_blob("image", 104, "group.png", png_bytes)},
                {"id": 105, "title": "口令受限图", "description": "", "caption": "", "width": 32, "height": 24, "uploaded_by_user_id": 1, "tags": [], "blob": _write_blob("image", 105, "pwd.png", png_bytes)},
            ],
            "documents": [
                {"id": 201, "title": "草稿独享附件.md", "description": "", "uploaded_by_user_id": 1, "tags": [], "blob": _write_blob("document", 201, "draft.md", doc_bytes)},
            ],
            "media": [
                {"id": 301, "title": "受限视频", "type": "video", "duration": 10.0, "uploaded_by_user_id": 1, "blob": _write_blob("video", 301, "sec.mp4", vid_bytes), "thumbnail_blob": _write_blob("media_thumb", 301, "sec_poster.png", png_bytes)},
            ],
            "index_pages": [
                {"id": 20, "title": "公开专栏", "slug": "pub-col", "path": "000100010001", "depth": 3, "url_path": "/welcome/pub-col/", "show_in_menus": True, "seo_title": "", "search_description": "", "live": True, "has_unpublished_changes": False, "live_revision_id": 20, "owner_id": 1, "featured_image_id": None, "intro": ""},
            ],
            "blog_pages": [
                {
                    "id": 300, "title": "公开跨作者文章", "slug": "public-cross-owner", "path": "0001000100010001", "depth": 4,
                    "url_path": "/welcome/pub-col/public-cross-owner/", "show_in_menus": False, "seo_title": "", "search_description": "",
                    "live": True, "has_unpublished_changes": False, "live_revision_id": 300, "date": "2026-08-10", "owner_id": 2, "featured_image_id": 101,
                    "intro": "公开简介", "body_blocks": [{"type": "markdown_block", "value": {"markdown": "公开内容"}}],
                    "author_ids": [], "category_names": [], "tag_names": [],
                },
                {
                    "id": 301, "title": "草稿文章", "slug": "source-draft", "path": "0001000100010002", "depth": 4,
                    "url_path": "/welcome/pub-col/source-draft/", "show_in_menus": False, "seo_title": "", "search_description": "",
                    "live": False, "has_unpublished_changes": True, "live_revision_id": None, "date": "2026-08-11", "owner_id": 1, "featured_image_id": 102,
                    "intro": "草稿简介", "body_blocks": [{"type": "document_block", "value": {"document": 201, "title": "草稿附件"}}],
                    "author_ids": [], "category_names": [], "tag_names": [],
                },
                {
                    "id": 302, "title": "登录受限文章", "slug": "login-only", "path": "0001000100010003", "depth": 4,
                    "url_path": "/welcome/pub-col/login-only/", "show_in_menus": False, "seo_title": "", "search_description": "",
                    "live": True, "has_unpublished_changes": False, "live_revision_id": 302, "date": "2026-08-12", "owner_id": 1, "featured_image_id": 103,
                    "intro": "登录可见", "body_blocks": [{"type": "video_block", "value": {"video": 301, "title": "受限视频"}}],
                    "author_ids": [], "category_names": [], "tag_names": [],
                },
                {
                    "id": 303, "title": "分组受限文章", "slug": "group-only", "path": "0001000100010004", "depth": 4,
                    "url_path": "/welcome/pub-col/group-only/", "show_in_menus": False, "seo_title": "", "search_description": "",
                    "live": True, "has_unpublished_changes": False, "live_revision_id": 303, "date": "2026-08-13", "owner_id": 1, "featured_image_id": 104,
                    "intro": "VIP分组可见", "body_blocks": [{"type": "markdown_block", "value": {"markdown": "VIP正文"}}],
                    "author_ids": [], "category_names": [], "tag_names": [],
                },
                {
                    "id": 304, "title": "口令受限文章", "slug": "password-only", "path": "0001000100010005", "depth": 4,
                    "url_path": "/welcome/pub-col/password-only/", "show_in_menus": False, "seo_title": "", "search_description": "",
                    "live": True, "has_unpublished_changes": False, "live_revision_id": 304, "date": "2026-08-14", "owner_id": 1, "featured_image_id": 105,
                    "intro": "口令可见", "body_blocks": [{"type": "markdown_block", "value": {"markdown": "口令正文"}}],
                    "author_ids": [], "category_names": [], "tag_names": [],
                },
            ],
            "comments": [],
            "comment_reactions": [],
            "page_views": [],
            "redirects": [],
        }

    def test_three_end_target_isolation_and_cli_allow_dev_removed(self):
        valid_pg = {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": "127.0.0.1",
            "PORT": "5432",
            "NAME": "myblog_staging",
        }
        valid_mongo = {"HOST": "127.0.0.1", "PORT": 27017, "DATABASE": "myblog_staging"}
        valid_storages = {
            "default": {
                "BACKEND": "media_library.storage.ProtectedRustFSStorage",
                "OPTIONS": {
                    "endpoint_url": "http://127.0.0.1:9000",
                    "bucket_name": "myblog-staging",
                    "default_acl": None,
                    "querystring_auth": False,
                },
            }
        }

        # 1. 全合法三端配置通过
        with override_settings(DATABASES={"default": valid_pg}, MONGO_SETTINGS=valid_mongo, STORAGES=valid_storages):
            info = assert_target_isolation()
            self.assertEqual(info["postgres_db"], "myblog_staging")
            self.assertEqual(info["mongo_db"], "myblog_staging")
            self.assertEqual(info["s3_bucket"], "myblog-staging")

            # 即使测试豁免参数也不能绕过非 test_* 库
            with self.assertRaises(TargetIsolationError):
                assert_target_isolation(_allow_test_isolation=True)

        # 2. PG 为 myblog_dev / wagtailblog / 空
        for bad_pg_name in ("myblog_dev", "wagtailblog", "wagtailblog3", ""):
            with override_settings(
                DATABASES={"default": {**valid_pg, "NAME": bad_pg_name}},
                MONGO_SETTINGS=valid_mongo,
                STORAGES=valid_storages,
            ):
                with self.assertRaises(TargetIsolationError):
                    assert_target_isolation()

        # 3. PG 合法但 Mongo 为 myblog_dev / wagtailblog / 空
        for bad_mongo_db in ("myblog_dev", "wagtailblog", ""):
            with override_settings(
                DATABASES={"default": valid_pg},
                MONGO_SETTINGS={**valid_mongo, "DATABASE": bad_mongo_db},
                STORAGES=valid_storages,
            ):
                with self.assertRaises(TargetIsolationError):
                    assert_target_isolation()

        # 4. PG + Mongo 合法但存储为本地文件存储或开发/空桶
        for bad_storage in (
            {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}},
            {"default": {"BACKEND": "media_library.storage.ControlledFileSystemStorage"}},
            {"default": {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": {"endpoint_url": "http://127.0.0.1:9000", "bucket_name": "myblog-staging"}}},
            {"default": {"BACKEND": "media_library.storage.ProtectedRustFSStorage", "OPTIONS": {"endpoint_url": "http://127.0.0.1:9000", "bucket_name": "myblog-staging", "default_acl": "public-read"}}},
            {"default": {"BACKEND": "media_library.storage.ProtectedRustFSStorage", "OPTIONS": {"endpoint_url": "http://127.0.0.1:9000", "bucket_name": ""}}},
            {"default": {"BACKEND": "media_library.storage.ProtectedRustFSStorage", "OPTIONS": {"endpoint_url": "http://127.0.0.1:9000", "bucket_name": "myblog-dev"}}},
            {"default": {"BACKEND": "media_library.storage.ProtectedRustFSStorage", "OPTIONS": {"endpoint_url": "http://127.0.0.1:9000", "bucket_name": "myblog-prod"}}},
            {"default": {"BACKEND": "media_library.storage.ProtectedRustFSStorage", "OPTIONS": {"endpoint_url": "", "bucket_name": "myblog-staging"}}},
        ):
            with override_settings(
                DATABASES={"default": valid_pg},
                MONGO_SETTINGS=valid_mongo,
                STORAGES=bad_storage,
            ):
                with self.assertRaises(TargetIsolationError):
                    assert_target_isolation()

        # 4.5 PG 为 staging 但 Mongo 指向不同环境库（混库拦截）
        with override_settings(
            DATABASES={"default": valid_pg},
            MONGO_SETTINGS={**valid_mongo, "DATABASE": "myblog_prod"},
            STORAGES=valid_storages,
        ):
            with self.assertRaises(TargetIsolationError):
                assert_target_isolation()

        # 4.6 测试豁免模式同样严禁连接 myblog_dev Mongo 或真实对象存储
        with override_settings(
            DATABASES={"default": {**valid_pg, "NAME": "test_myblog"}},
            MONGO_SETTINGS={**valid_mongo, "DATABASE": "myblog_dev"},
            STORAGES={"default": {"BACKEND": "media_library.storage.ControlledFileSystemStorage"}},
            MEDIA_ROOT="/tmp/test_media",
        ):
            with self.assertRaises(TargetIsolationError):
                assert_target_isolation(_allow_test_isolation=True)
        with override_settings(
            DATABASES={"default": {**valid_pg, "NAME": "test_myblog"}},
            MONGO_SETTINGS={**valid_mongo, "DATABASE": "myblog_test"},
            STORAGES=valid_storages,
        ):
            with self.assertRaises(TargetIsolationError):
                assert_target_isolation(_allow_test_isolation=True)

        # 5. CLI 不再支持 --allow-dev
        from scripts import migrate_from_wagtailblog3

        with patch.object(sys, "argv", ["migrate_from_wagtailblog3.py", "--apply", "--allow-dev"]):
            with self.assertRaises(SystemExit) as cm:
                migrate_from_wagtailblog3.main()
            self.assertEqual(cm.exception.code, 2)

    def test_remote_export_ssh_strict_host_key_checking(self):
        cmd = build_ssh_command(
            ssh_host="192.168.20.2",
            ssh_port=2222,
            ssh_user="root",
            ssh_known_hosts="/tmp/test_known_hosts",
        )
        joined = " ".join(cmd)
        self.assertIn("StrictHostKeyChecking=yes", joined)
        self.assertIn("UserKnownHostsFile=/tmp/test_known_hosts", joined)
        self.assertNotIn("StrictHostKeyChecking=no", joined)
        self.assertNotIn("/dev/null", joined)

        with self.assertRaises(ValueError):
            resolve_ssh_known_hosts("/dev/null")

        remote_cmd = build_remote_export_command(
            remote_dir="/opt/wb3",
            remote_python="/opt/py/bin/python",
        )
        self.assertIn("mktemp -d /tmp/wb3_readonly_export.XXXXXX", remote_cmd)
        self.assertIn("trap 'rm -rf \"$TMPDIR\"'", remote_cmd)

    def test_v1_snapshot_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            snap_dir = tmp_path / "snap"
            media_dir = tmp_path / "media"
            snap_dir.mkdir()
            media_dir.mkdir()
            snap = self._build_base_v2_snapshot(snap_dir)
            snap.pop("schema_version", None)
            snap.pop("page_view_restrictions", None)
            (snap_dir / "snapshot.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

            with override_settings(
                MEDIA_ROOT=str(media_dir),
                STORAGES={
                    **settings.STORAGES,
                    "default": {"BACKEND": "media_library.storage.ControlledFileSystemStorage"},
                },
            ):
                with self.assertRaises(ValueError):
                    apply_snapshot(snap_dir, batch_id=f"test_v1_{uuid4().hex[:6]}", _allow_test_isolation=True)
                self.assertEqual(BlogPage.objects.count(), 0)
                self.assertEqual(CustomImage.objects.count(), 0)

    def test_draft_and_restriction_preservation_and_reconcile_leak_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            snap_dir = tmp_path / "snap"
            media_dir = tmp_path / "media"
            snap_dir.mkdir()
            media_dir.mkdir()
            snap = self._build_base_v2_snapshot(snap_dir)
            snap["page_view_restrictions"] = [
                {"id": 11, "page_id": 302, "page_path": "0001000100010003", "page_url_path": "/welcome/pub-col/login-only/", "restriction_type": "login", "group_names": [], "has_password": False},
                {"id": 12, "page_id": 303, "page_path": "0001000100010004", "page_url_path": "/welcome/pub-col/group-only/", "restriction_type": "groups", "group_names": ["vip_readers"], "has_password": False},
                {"id": 13, "page_id": 304, "page_path": "0001000100010005", "page_url_path": "/welcome/pub-col/password-only/", "restriction_type": "password", "group_names": [], "has_password": True},
            ]
            (snap_dir / "snapshot.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

            with override_settings(
                MEDIA_ROOT=str(media_dir),
                STORAGES={
                    **settings.STORAGES,
                    "default": {"BACKEND": "media_library.storage.ControlledFileSystemStorage"},
                },
            ):
                bid = f"test_p0_{uuid4().hex[:6]}"
                Group.objects.get_or_create(name="vip_readers")
                rep = apply_snapshot(
                    snap_dir,
                    batch_id=bid,
                    group_name_map={"vip_readers": "vip_readers"},
                    _allow_test_isolation=True,
                )

                # 1. 公开跨作者文章：保持 live=True 且无任何 PageViewRestriction，跨作者媒体正常转正 PUBLIC，匿名可访问
                pub_page = BlogPage.objects.get(slug="public-cross-owner")
                self.assertTrue(pub_page.live)
                self.assertFalse(pub_page.get_view_restrictions().exists())
                self.assertEqual(self.client.get(pub_page.url).status_code, 200)
                self.assertEqual(CustomImage.objects.get(title="公开文章配图").visibility, VisibilityChoices.PUBLIC)

                # 2. 源草稿文章：保持 live=False 不发布，匿名 404，不入搜索，媒体保持 RESTRICTED
                draft_page = BlogPage.objects.get(slug="source-draft")
                self.assertFalse(draft_page.live)
                self.assertIsNone(draft_page.live_revision_id)
                self.assertEqual(self.client.get("/pub-col/source-draft/").status_code, 404)
                self.assertFalse(PublishedSearchDocument.objects.filter(page_id=draft_page.id).exists())
                self.assertEqual(CustomImage.objects.get(title="草稿独享图").visibility, VisibilityChoices.RESTRICTED)
                self.assertEqual(CustomDocument.objects.get(title="草稿独享附件.md").visibility, VisibilityChoices.RESTRICTED)

                # 3. 登录受限页：Pass 1.5 先建限制再发布，匿名访问被拦截，媒体（含视频与封面海报）保持 RESTRICTED
                login_page = BlogPage.objects.get(slug="login-only")
                self.assertTrue(login_page.live)
                self.assertTrue(login_page.get_view_restrictions().filter(restriction_type=PageViewRestriction.LOGIN).exists())
                self.assertEqual(self.client.get(login_page.url).status_code, 302)
                self.assertEqual(CustomImage.objects.get(title="登录受限图").visibility, VisibilityChoices.RESTRICTED)
                self.assertEqual(Video.objects.get(title="受限视频").visibility, VisibilityChoices.RESTRICTED)
                self.assertEqual(Video.objects.get(title="受限视频").poster.visibility, VisibilityChoices.RESTRICTED)

                # 4. 分组受限页：组已创建并关联，页面发布但媒体保持 RESTRICTED
                group_page = BlogPage.objects.get(slug="group-only")
                self.assertTrue(group_page.live)
                pvr_grp = group_page.get_view_restrictions().filter(restriction_type=PageViewRestriction.GROUPS).first()
                self.assertIsNotNone(pvr_grp)
                self.assertEqual(list(pvr_grp.groups.values_list("name", flat=True)), ["vip_readers"])
                self.assertEqual(CustomImage.objects.get(title="分组受限图").visibility, VisibilityChoices.RESTRICTED)

                # 5. 口令受限页未传密码：绝不丢弃限制直接发布，保留草稿且记录 anomaly，媒体保持 RESTRICTED
                pwd_page = BlogPage.objects.get(slug="password-only")
                self.assertFalse(pwd_page.live)
                self.assertIn(304, rep["unconfirmed_restriction_page_ids"])
                self.assertTrue(any(a["kind"] == "restriction_missing_password" for a in rep["anomalies"]))
                self.assertEqual(CustomImage.objects.get(title="口令受限图").visibility, VisibilityChoices.RESTRICTED)

                # 6. 对账准确率检查：期望 3 篇 live、2 篇 draft、3 篇 restricted，零未转正、零泄漏
                rec = reconcile_target(snap_dir, batch_id=bid)
                self.assertEqual(rec["expected_vs_actual"]["expected_live_blogs"], {"expected": 3, "actual": 3})
                self.assertEqual(rec["expected_vs_actual"]["expected_draft_blogs"], {"expected": 2, "actual": 2})
                self.assertEqual(rec["expected_vs_actual"]["expected_restricted_blogs"], {"expected": 3, "actual": 2})
                self.assertEqual(rec["expected_vs_actual"]["held_as_draft_blogs"], 1)
                self.assertEqual(rec["integrity"]["unpromoted_public_assets"], 0)
                self.assertEqual(rec["integrity"]["leaked_restricted_only_assets"], 0)

                # 模拟草稿/受限媒体被误公开，对账应精准检出 leaked_restricted_only_assets == 1
                CustomImage.objects.filter(title="草稿独享图").update(visibility=VisibilityChoices.PUBLIC)
                rec_leaked = reconcile_target(snap_dir, batch_id=bid)
                self.assertEqual(rec_leaked["integrity"]["leaked_restricted_only_assets"], 1)
                CustomImage.objects.filter(title="草稿独享图").update(visibility=VisibilityChoices.RESTRICTED)

    def test_password_supplied_and_missing_group_subtree_hold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            snap_dir = tmp_path / "snap"
            media_dir = tmp_path / "media"
            snap_dir.mkdir()
            media_dir.mkdir()
            snap = self._build_base_v2_snapshot(snap_dir)
            # 仅保留栏目(20)、分组受限子栏目(21)->子文章(303) 以及 口令受限文章(304)
            snap["index_pages"].append({
                "id": 21, "title": "缺失组栏目", "slug": "missing-grp-col", "path": "000100010003", "depth": 3,
                "url_path": "/welcome/missing-grp-col/", "show_in_menus": False, "seo_title": "", "search_description": "",
                "live": True, "has_unpublished_changes": False, "live_revision_id": 21, "owner_id": 1, "featured_image_id": None, "intro": "",
            })
            snap["blog_pages"] = [
                {
                    "id": 303, "title": "受限栏目下子文章", "slug": "child-under-missing-grp", "path": "0001000100030001", "depth": 4,
                    "url_path": "/welcome/missing-grp-col/child-under-missing-grp/", "show_in_menus": False, "seo_title": "", "search_description": "",
                    "live": True, "has_unpublished_changes": False, "live_revision_id": 303, "date": "2026-08-13", "owner_id": 1, "featured_image_id": 104,
                    "intro": "子文章", "body_blocks": [{"type": "markdown_block", "value": {"markdown": "子正文"}}],
                    "author_ids": [], "category_names": [], "tag_names": [],
                },
                snap["blog_pages"][4],  # 304 口令受限文章
            ]
            snap["page_view_restrictions"] = [
                {"id": 21, "page_id": 21, "page_path": "000100010003", "page_url_path": "/welcome/missing-grp-col/", "restriction_type": "groups", "group_names": ["nonexistent_group_not_in_snap"], "has_password": False},
                {"id": 13, "page_id": 304, "page_path": "0001000100010005", "page_url_path": "/welcome/pub-col/password-only/", "restriction_type": "password", "group_names": [], "has_password": True},
            ]
            (snap_dir / "snapshot.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

            with override_settings(
                MEDIA_ROOT=str(media_dir),
                STORAGES={
                    **settings.STORAGES,
                    "default": {"BACKEND": "media_library.storage.ControlledFileSystemStorage"},
                },
            ):
                bid = f"test_sub_{uuid4().hex[:6]}"
                rep = apply_snapshot(
                    snap_dir,
                    batch_id=bid,
                    restriction_passwords={304: "Passw0rd!2026"},
                    _allow_test_isolation=True,
                )
                # 缺失组映射的父栏目及其后代整支保留草稿
                missing_col = BlogIndexPage.objects.get(slug="missing-grp-col")
                child_under = BlogPage.objects.get(slug="child-under-missing-grp")
                self.assertFalse(missing_col.live)
                self.assertFalse(child_under.live)
                self.assertIn(21, rep["unconfirmed_restriction_page_ids"])
                self.assertIn(303, rep["unconfirmed_restriction_page_ids"])
                self.assertEqual(CustomImage.objects.get(title="分组受限图").visibility, VisibilityChoices.RESTRICTED)

                # 显式传入密码的口令受限文章：先建 PASSWORD 限制再发布，媒体保持 RESTRICTED
                pwd_page = BlogPage.objects.get(slug="password-only")
                self.assertTrue(pwd_page.live)
                pvr_pwd = pwd_page.get_view_restrictions().filter(restriction_type=PageViewRestriction.PASSWORD).first()
                self.assertIsNotNone(pvr_pwd)
                self.assertEqual(pvr_pwd.password, "Passw0rd!2026")
                self.assertEqual(CustomImage.objects.get(title="口令受限图").visibility, VisibilityChoices.RESTRICTED)

    def test_unapproved_same_name_group_draft_ancestor_and_historical_batch_guard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            snap_dir = tmp_path / "snap"
            media_dir = tmp_path / "media"
            snap_dir.mkdir()
            media_dir.mkdir()
            snap = self._build_base_v2_snapshot(snap_dir)
            # 1) 目标库已存在同名组 vip_readers，但不传 group_name_map 时绝不自动信任
            Group.objects.get_or_create(name="vip_readers")
            # 2) 新增源站 live=False 的草稿父栏目，其下挂 live=True 且无限制的子文章
            snap["index_pages"].append({
                "id": 22, "title": "草稿父栏目", "slug": "draft-parent-col", "path": "000100010004", "depth": 3,
                "url_path": "/welcome/draft-parent-col/", "show_in_menus": False, "seo_title": "", "search_description": "",
                "live": False, "has_unpublished_changes": True, "live_revision_id": None, "owner_id": 1, "featured_image_id": None, "intro": "",
            })
            snap["blog_pages"] = [
                snap["blog_pages"][3],  # 303 vip_readers 组限制页
                {
                    "id": 305, "title": "草稿栏下子文章", "slug": "live-child-under-draft-col", "path": "0001000100040001", "depth": 4,
                    "url_path": "/welcome/draft-parent-col/live-child-under-draft-col/", "show_in_menus": False, "seo_title": "", "search_description": "",
                    "live": True, "has_unpublished_changes": False, "live_revision_id": 305, "date": "2026-08-14", "owner_id": 1, "featured_image_id": 102,
                    "intro": "子文章", "body_blocks": [{"type": "markdown_block", "value": {"markdown": "草稿栏子正文"}}],
                    "author_ids": [], "category_names": [], "tag_names": [],
                },
            ]
            snap["page_view_restrictions"] = [
                {"id": 12, "page_id": 303, "page_path": "0001000100010004", "page_url_path": "/welcome/pub-col/group-only/", "restriction_type": "groups", "group_names": ["vip_readers"], "has_password": False},
            ]
            (snap_dir / "snapshot.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

            with override_settings(
                MEDIA_ROOT=str(media_dir),
                STORAGES={
                    **settings.STORAGES,
                    "default": {"BACKEND": "media_library.storage.ControlledFileSystemStorage"},
                },
            ):
                # A. 历史批次只读保护：在任何写库（含建用户/建图/建组）前拒绝写入历史批次
                OperationResult.objects.create(
                    operation_id="wb3_v1:pub_blog:1",
                    action="etl_publish_blog_page",
                    input_hash="legacy",
                    result_json={"live": True},
                )
                with self.assertRaises(TargetIsolationError):
                    apply_snapshot(snap_dir, batch_id="wb3_v1", _allow_test_isolation=True)
                self.assertEqual(CustomImage.objects.count(), 0)
                self.assertEqual(BlogPage.objects.count(), 0)

                # B. 全新批次导入：未显式核准的同名组保持草稿；草稿祖先下子文章同样保持草稿且媒体保持 RESTRICTED
                bid = f"test_p0_extra_{uuid4().hex[:6]}"
                rep = apply_snapshot(snap_dir, batch_id=bid, _allow_test_isolation=True)

                grp_page = BlogPage.objects.get(slug="group-only")
                self.assertFalse(grp_page.live)
                self.assertIn(303, rep["unconfirmed_restriction_page_ids"])
                self.assertEqual(CustomImage.objects.get(title="分组受限图").visibility, VisibilityChoices.RESTRICTED)

                draft_col = BlogIndexPage.objects.get(slug="draft-parent-col")
                child_under_draft = BlogPage.objects.get(slug="live-child-under-draft-col")
                self.assertFalse(draft_col.live)
                self.assertFalse(child_under_draft.live)
                self.assertIn(305, rep["held_draft_ancestor_page_ids"])
                self.assertTrue(any(a["kind"] == "draft_ancestor_held_as_draft" for a in rep["anomalies"]))
                self.assertEqual(CustomImage.objects.get(title="草稿独享图").visibility, VisibilityChoices.RESTRICTED)

                rec = reconcile_target(snap_dir, batch_id=bid)
                self.assertEqual(rec["integrity"]["leaked_restricted_only_assets"], 0)
                CustomImage.objects.filter(title="草稿独享图").update(visibility=VisibilityChoices.PUBLIC)
                self.assertEqual(reconcile_target(snap_dir, batch_id=bid)["integrity"]["leaked_restricted_only_assets"], 1)
