"""
Phase 2 wagtailblog3 -> myblog ETL 迁移工具全量单元与幂等故障注入测试。
"""

import hashlib
import io
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.test import TestCase, override_settings
from PIL import Image as PilImage
from wagtail.contrib.redirects.models import Redirect

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
            "source": "synthetic-wb3",
            "site": {"site_name": "我的博客", "home_id": 3, "home_path": "00010001", "home_url_path": "/welcome/"},
            "users": [{"id": 1, "username": "wb3_root", "email": "root@example.com", "first_name": "m", "last_name": "m", "is_superuser": True, "is_staff": True, "is_active": True}],
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
                {"id": 20, "title": "技术专栏", "slug": "tech-wb3", "path": "000100010001", "depth": 3, "url_path": "/welcome/tech-wb3/", "show_in_menus": True, "seo_title": "", "search_description": "", "live": True, "owner_id": 1, "featured_image_id": 12, "intro": "栏目简介<script>"},
                {"id": 526, "title": "测试页", "slug": "test-branch", "path": "000100010002", "depth": 3, "url_path": "/welcome/test-branch/", "show_in_menus": False, "seo_title": "", "search_description": "", "live": True, "owner_id": 1, "featured_image_id": None, "intro": ""},
            ],
            "blog_pages": [
                {
                    "id": 200, "title": "父文章", "slug": "parent-post", "path": "0001000100010001", "depth": 4,
                    "url_path": "/welcome/tech-wb3/parent-post/", "show_in_menus": False, "seo_title": "ParentSEO", "search_description": "Desc",
                    "live": True, "date": "2026-08-01", "owner_id": 1, "featured_image_id": 12,
                    "intro": "父文章简介", "body_blocks": [{"type": "markdown_block", "value": {"markdown": '正文 <embed embedtype="image" id="12" format="fullwidth_web" alt="图" />'}}],
                    "author_ids": [2], "category_names": ["数据库"], "tag_names": ["PostgreSQL"],
                },
                {
                    "id": 201, "title": "子文章", "slug": "child-post", "path": "00010001000100010001", "depth": 5,
                    "url_path": "/welcome/tech-wb3/parent-post/child-post/", "show_in_menus": False, "seo_title": "", "search_description": "",
                    "live": True, "date": "2026-08-02", "owner_id": None, "featured_image_id": None,
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
                    "live": True, "date": "2026-08-03", "owner_id": 1, "featured_image_id": None,
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
                # 1. 目标隔离守卫禁止未授权写 myblog_dev
                with override_settings(DATABASES={"default": {"NAME": "myblog_dev"}}):
                    with self.assertRaises(TargetIsolationError):
                        assert_target_isolation(allow_dev=False)

                # 2. Dry-run 零写入
                dr = dry_run_snapshot(snap_dir)
                self.assertEqual(dr["counts"]["index_pages_included"], 1)
                self.assertEqual(dr["counts"]["blog_pages_included"], 2)
                self.assertEqual(dr["excluded_test_page_ids"], [526, 1195])
                self.assertEqual(BlogPage.objects.count(), 0)

                # 3. 首次正式导入
                bid = f"test_wb3_{uuid4().hex[:6]}"
                rep1 = apply_snapshot(snap_dir, batch_id=bid, allow_dev=True)
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
                child_post.hydrate_from_mongo()
                first_rt = str(child_post.body[0].value.source)
                self.assertIn(f'id="{parent_post.id}"', first_rt)
                self.assertIn("数据库", list(parent_post.tags.names()))

                # 核验前台真实 HTML 渲染（Markdown 不得退化为字面量 {'markdown': ...}，文档/音视频标题不丢失）
                parent_html = self.client.get(parent_post.url).content.decode("utf-8")
                self.assertNotIn("{'markdown':", parent_html)
                self.assertIn("正文", parent_html)
                self.assertIn("/media-gateway/image/", parent_html)
                child_html = self.client.get(child_post.url).content.decode("utf-8")
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
                rep2 = apply_snapshot(snap_dir, batch_id=bid, allow_dev=True)
                self.assertEqual(rep2["stats"]["created"], {})
                self.assertEqual(BlogPage.objects.count(), 2)
                self.assertEqual(PageAnalyticsTotal.objects.first().total_pv, 12)
                self.assertEqual(Comment.objects.count(), 2)

                # 5. 对账核验
                rec = reconcile_target(snap_dir, batch_id=bid)
                self.assertEqual(rec["integrity"]["missing_storage_keys"], [])
                self.assertEqual(rec["integrity"]["unpromoted_referenced_images"], 0)
                self.assertEqual(rec["expected_vs_actual"]["pv_total_sum"]["actual"], 12)

                # 6. 同 operation_id 输入篡改触发幂等冲突保护停机
                op_row = OperationResult.objects.filter(operation_id=f"{bid}:pub_blog:200").first()
                self.assertIsNotNone(op_row)
                snap_modified = json.loads((snap_dir / "snapshot.json").read_text(encoding="utf-8"))
                snap_modified["blog_pages"][0]["title"] = "被篡改标题"
                (snap_dir / "snapshot.json").write_text(json.dumps(snap_modified, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(IdempotencyConflictError):
                    apply_snapshot(snap_dir, batch_id=bid, allow_dev=True, skip_search_rebuild=True)

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
