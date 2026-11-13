"""
2026-11-13 真实浏览器全流程验收问题回归测试。
覆盖编辑页无草稿水合、Redis 前缀隔离、回收页前台提示、待审评论搜索、
回收站表格操作列、指南摘要纯文本与缺失标签链接、头像脱敏、媒体网关渲染与 favicon。
"""

from datetime import date
from io import BytesIO
import os
import re
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django

django.setup()

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image as PILImage
from wagtail.rich_text import RichText

from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from comments.models import Comment
from comments.services import CommentService
from content_lifecycle.permissions import ensure_author_role_infrastructure
from content_lifecycle.recycle import RecycleService
from guides.models import GuideIndexPage, GuidePage, GuidePageAuthor, GuideTopic
from home.models import HomePage
from media_library.models import CustomImage, VisibilityChoices
from people.models import Person
from portfolio.models import PortfolioIndexPage, ProjectPage, ProjectPagePerson
from taxonomy.models import TagIndexPage

User = get_user_model()


def _make_png(name: str = "img.png") -> SimpleUploadedFile:
    buf = BytesIO()
    PILImage.new("RGB", (24, 24), (30, 120, 210)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


class BrowserAuditFixesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.infra = ensure_author_role_infrastructure()
        cls.authors_group = cls.infra["authors_group"]
        cls.home = HomePage.objects.first()
        cls.superuser = User.objects.create_superuser(
            username=f"baf_su_{uuid4().hex[:6]}",
            email="baf_su@test.com",
            password="StrongAdminPassword!123",
        )
        cls.author_a = User.objects.create_user(
            username=f"baf_a_{uuid4().hex[:6]}",
            password="AuthorPassword!123",
            is_active=True,
        )
        cls.author_a.groups.add(cls.authors_group)
        cls.person_a = Person.objects.create(name="Author A", linked_user=cls.author_a)

        cls.author_b = User.objects.create_user(
            username=f"baf_b_{uuid4().hex[:6]}",
            password="AuthorPassword!123",
            is_active=True,
        )
        cls.author_b.groups.add(cls.authors_group)
        cls.person_b = Person.objects.create(name="Author B", linked_user=cls.author_b)

        cls.member = User.objects.create_user(
            username=f"baf_m_{uuid4().hex[:6]}",
            password="MemberPassword!123",
            is_active=True,
        )

        cls.blog_index = BlogIndexPage(title="BAF Blog", slug=f"baf-blog-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.blog_index)
        cls.blog_index.save_revision(user=cls.superuser).publish(user=cls.superuser)

        cls.guide_index = GuideIndexPage(title="BAF Guide", slug=f"baf-guide-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.guide_index)
        cls.guide_index.save_revision(user=cls.superuser).publish(user=cls.superuser)

        cls.portfolio_index = PortfolioIndexPage(title="BAF Portfolio", slug=f"baf-port-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.portfolio_index)
        cls.portfolio_index.save_revision(user=cls.superuser).publish(user=cls.superuser)

        cls.tag_index = TagIndexPage(title="BAF Tags", slug=f"baf-tags-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.tag_index)
        cls.tag_index.save_revision(user=cls.superuser).publish(user=cls.superuser)

        cls.topic = GuideTopic.objects.create(name=f"Topic-{uuid4().hex[:4]}", slug=f"top-{uuid4().hex[:4]}")

    def test_01_published_pages_without_draft_hydrate_on_admin_edit_and_preview(self):
        blog = BlogPage(title="Published Blog", slug=f"pb-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 6, 1))
        blog.intro = [("rich_text", RichText("<p>BLOG_INTRO_TOKEN_XYZ</p>"))]
        blog.body = [("markdown", "BLOG_BODY_TOKEN_XYZ")]
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person_a, role="作者")
        blog.save_revision(user=self.author_a).publish(user=self.author_a)
        blog.refresh_from_db()
        self.assertTrue(blog.live)
        self.assertFalse(blog.has_unpublished_changes)

        guide = GuidePage(
            title="Published Guide",
            slug=f"pg-{uuid4().hex[:6]}",
            owner=self.author_a,
            topic=self.topic,
            authored_on=date(2026, 6, 1),
        )
        guide.body = [("step", {"step_number": "1", "title": "STEP_TITLE_XYZ", "content": RichText("<p>GUIDE_STEP_TOKEN_XYZ</p>")})]
        self.guide_index.add_child(instance=guide)
        GuidePageAuthor.objects.create(page=guide, person=self.person_a, role="作者")
        guide.save_revision(user=self.author_a).publish(user=self.author_a)
        guide.refresh_from_db()
        self.assertFalse(guide.has_unpublished_changes)

        project = ProjectPage(title="Published Project", slug=f"pp-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 6, 1))
        project.body = [("markdown", "PROJECT_BODY_TOKEN_XYZ")]
        self.portfolio_index.add_child(instance=project)
        ProjectPagePerson.objects.create(page=project, person=self.person_a, role="负责人")
        project.save_revision(user=self.author_a).publish(user=self.author_a)
        project.refresh_from_db()
        self.assertFalse(project.has_unpublished_changes)

        client = Client()
        client.force_login(self.author_a)

        res_blog = client.get(reverse("wagtailadmin_pages:edit", args=[blog.id]))
        self.assertEqual(res_blog.status_code, 200)
        html_blog = res_blog.content.decode("utf-8")
        self.assertIn("BLOG_BODY_TOKEN_XYZ", html_blog)
        self.assertNotIn('name="body-count" value="0"', html_blog)

        res_guide = client.get(reverse("wagtailadmin_pages:edit", args=[guide.id]))
        self.assertEqual(res_guide.status_code, 200)
        html_guide = res_guide.content.decode("utf-8")
        self.assertIn("GUIDE_STEP_TOKEN_XYZ", html_guide)
        self.assertNotIn('name="body-count" value="0"', html_guide)

        res_proj = client.get(reverse("wagtailadmin_pages:edit", args=[project.id]))
        self.assertEqual(res_proj.status_code, 200)
        html_proj = res_proj.content.decode("utf-8")
        self.assertIn("PROJECT_BODY_TOKEN_XYZ", html_proj)
        self.assertNotIn('name="body-count" value="0"', html_proj)

        # 无草稿已发布页面直接点“查看草稿”与“编辑预览”也必须保留 MongoDB 正文
        res_view_draft = client.get(reverse("wagtailadmin_pages:view_draft", args=[blog.id]))
        self.assertEqual(res_view_draft.status_code, 200)
        self.assertIn("BLOG_BODY_TOKEN_XYZ", res_view_draft.content.decode("utf-8"))

        res_preview_proj = client.get(reverse("wagtailadmin_pages:view_draft", args=[project.id]))
        self.assertEqual(res_preview_proj.status_code, 200)
        self.assertIn("PROJECT_BODY_TOKEN_XYZ", res_preview_proj.content.decode("utf-8"))

        rev_id = project.live_revision_id
        res_cmp = client.get(reverse("wagtailadmin_pages:revisions_compare", args=[project.id, "live", rev_id]))
        self.assertEqual(res_cmp.status_code, 200)

    def test_02_redis_keys_honor_configured_prefix(self):
        from api.limiter import RateLimiter
        from captcha.service import _ch_key, _img_key, _active_key
        from comments.rate_limit import _resolve_prefix as comments_prefix
        from identity.credentials import _resolve_prefix as identity_prefix

        with override_settings(
            REDIS_KEY_PREFIX="myblog:isolated",
            CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "KEY_PREFIX": "myblog:isolated"}},
        ):
            self.assertTrue(_ch_key("abc").startswith("myblog:isolated:captcha:"))
            self.assertTrue(_img_key("abc").startswith("myblog:isolated:captcha:"))
            self.assertTrue(_active_key("abc").startswith("myblog:isolated:captcha:"))
            self.assertEqual(comments_prefix(), "myblog:isolated")
            self.assertEqual(identity_prefix(), "myblog:isolated")
            self.assertTrue(RateLimiter.build_redis_key("api", "127.0.0.1").startswith("myblog:isolated:ratelimit:"))

    def test_03_recycled_page_front_url_shows_notice_for_owner_and_superuser_only(self):
        blog = BlogPage(title="Recycled Secret Title", slug=f"rec-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 6, 1))
        blog.body = [("markdown", "RECYCLE_NOTICE_BODY")]
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person_a, role="作者")
        blog.save_revision(user=self.author_a).publish(user=self.author_a)
        live_url = blog.get_url()

        RecycleService.recycle_page_or_subtree(blog, actor=self.author_a)

        # 1. 作者 A 访问原前台 URL：404 状态但渲染受控回收站提示页
        client_a = Client()
        client_a.force_login(self.author_a)
        res_a = client_a.get(live_url)
        self.assertEqual(res_a.status_code, 404)
        self.assertIn("受控回收站", res_a.content.decode("utf-8"))
        self.assertIn("/admin/recycle_bin/", res_a.content.decode("utf-8"))

        # 2. 超级管理员访问：同样显示受控回收站提示页
        client_su = Client()
        client_su.force_login(self.superuser)
        res_su = client_su.get(live_url)
        self.assertEqual(res_su.status_code, 404)
        self.assertIn("受控回收站", res_su.content.decode("utf-8"))

        # 3. 匿名游客、普通会员、其他受限作者 B 访问：仅普通 404，不泄露回收站信息与标题
        for cl in (Client(),):
            res = cl.get(live_url)
            self.assertEqual(res.status_code, 404)
            self.assertNotIn("受控回收站", res.content.decode("utf-8"))

        client_m = Client()
        client_m.force_login(self.member)
        res_m = client_m.get(live_url)
        self.assertEqual(res_m.status_code, 404)
        self.assertNotIn("受控回收站", res_m.content.decode("utf-8"))

        client_b = Client()
        client_b.force_login(self.author_b)
        res_b = client_b.get(live_url)
        self.assertEqual(res_b.status_code, 404)
        self.assertNotIn("受控回收站", res_b.content.decode("utf-8"))
        self.assertNotIn("Recycled Secret Title", res_b.content.decode("utf-8"))

    def test_04_comment_admin_search_matches_pending_comment_content(self):
        blog = BlogPage(title="Comment Search Page", slug=f"cs-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 6, 1))
        blog.body = [("markdown", "Body")]
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person_a, role="作者")
        blog.save_revision(user=self.author_a).publish(user=self.author_a)

        token = f"PENDING_SEARCH_KW_{uuid4().hex[:8]}"
        c = CommentService.post_comment(
            page=blog,
            author=self.member,
            raw_content=f"待审评论关键词 {token}",
            ip_address="127.0.0.1",
        )
        self.assertEqual(c.status, "pending")

        client_su = Client()
        client_su.force_login(self.superuser)
        res = client_su.get(f"/admin/comments/?q={token}")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode("utf-8")
        self.assertIn(token, body)
        self.assertNotIn("No 评论管理 match your query", body)

    def test_05_recycle_bin_actions_rendered_inside_table_cells(self):
        blog = BlogPage(title="Recycle Table Check", slug=f"rtc-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 6, 1))
        blog.body = [("markdown", "Recycle table body")]
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person_a, role="作者")
        blog.save_revision(user=self.author_a).publish(user=self.author_a)
        batch = RecycleService.recycle_page_or_subtree(blog, actor=self.author_a)

        client_su = Client()
        client_su.force_login(self.superuser)
        res = client_su.get("/admin/recycle_bin/")
        self.assertEqual(res.status_code, 200)
        html_text = res.content.decode("utf-8")

        restore_url = reverse("recycle_bin:restore", args=[batch.pk])
        purge_url = reverse("recycle_bin:purge", args=[batch.pk])
        # 按钮必须位于 <tbody>...<td>...</td>...</tbody> 内，而不是被抛出 <table> 外
        listing_match = re.search(r'<table class="listing"[^>]*>.*?<tbody[^>]*>(.*?)</tbody>', html_text, re.DOTALL)
        self.assertIsNotNone(listing_match)
        tbody_html = listing_match.group(1)
        self.assertIn(f'<a href="{restore_url}"', tbody_html)
        self.assertIn(f'<a href="{purge_url}"', tbody_html)
        self.assertRegex(tbody_html, rf"(?s)<td[^>]*>.*?{re.escape(restore_url)}.*?{re.escape(purge_url)}.*?</td>")

    def test_06_guide_tag_summary_is_plain_text_and_missing_tag_links_are_absolute(self):
        tag_name = f"tag_{uuid4().hex[:6]}"
        guide = GuidePage(
            title="Guide Plain Summary",
            slug=f"gps-{uuid4().hex[:6]}",
            owner=self.author_a,
            topic=self.topic,
            authored_on=date(2026, 6, 1),
        )
        guide.body = [(
            "step",
            {
                "step_number": "1",
                "title": "指南步骤一",
                "content": RichText("<p data-block-key=\"sk7n3\">GUIDE_PLAIN_TOKEN 指南操作说明</p>"),
            },
        )]
        self.guide_index.add_child(instance=guide)
        GuidePageAuthor.objects.create(page=guide, person=self.person_a, role="作者")
        guide.tags.add(tag_name)
        guide.save_revision(user=self.author_a).publish(user=self.author_a)

        client = Client()
        tag_detail_url = f"{self.tag_index.get_url()}{tag_name}/"
        res_detail = client.get(tag_detail_url)
        self.assertEqual(res_detail.status_code, 200)
        detail_html = res_detail.content.decode("utf-8")
        self.assertIn("GUIDE_PLAIN_TOKEN 指南操作说明", detail_html)
        self.assertNotIn("data-block-key", detail_html)
        self.assertNotIn("&lt;p", detail_html)

        from content_blocks.blocks.structured import CustomFaqBlock, CustomLeadBlock, CustomTipBlock
        self.assertEqual(
            CustomLeadBlock().extract_text({"title": "T", "prerequisites": RichText("<p>P1</p>"), "overview": RichText("<p>O1</p>")}).plain_text,
            "T\nP1\nO1",
        )
        self.assertEqual(
            CustomTipBlock().extract_text({"tip_type": "tip", "title": "Tip", "content": RichText("<p>C1</p>")}).plain_text,
            "Tip\nC1",
        )
        self.assertEqual(
            CustomFaqBlock().extract_text({"question": "Q1", "answer": RichText("<p>A1</p>")}).plain_text,
            "Q1\nA1",
        )

        missing_url = f"{self.tag_index.get_url()}nonexistent_{uuid4().hex[:6]}/"
        res_missing = client.get(missing_url)
        self.assertEqual(res_missing.status_code, 200)
        missing_html = res_missing.content.decode("utf-8")
        self.assertIn(f'href="{self.tag_index.get_url()}{tag_name}/"', missing_html)

    def test_07_avatar_corrupt_error_redacts_internal_repr_and_media_rendition_uses_gateway(self):
        from identity.avatars import sanitize_and_encode_avatar

        bad = SimpleUploadedFile("bad_corrupt.png", b"not-a-valid-png", content_type="image/png")
        with self.assertRaises(ValidationError) as ctx:
            sanitize_and_encode_avatar(bad)
        err_msg = str(ctx.exception)
        self.assertNotIn("BytesIO", err_msg)
        self.assertNotIn("0x", err_msg)

        col = self.infra["workspace_collection"]
        img = CustomImage.objects.create(
            title="Gateway Image",
            file=_make_png("gw.png"),
            uploaded_by_user=self.author_a,
            collection=col,
            visibility=VisibilityChoices.RESTRICTED,
        )
        rendition = img.get_rendition("original")
        self.assertTrue(rendition.url.startswith("/media-gateway/rendition/"))
        self.assertNotIn(":9000", rendition.url)

        client_a = Client()
        client_a.force_login(self.author_a)
        res_edit = client_a.get(reverse("wagtailimages:edit", args=[img.id]))
        self.assertEqual(res_edit.status_code, 200)
        self.assertIn(rendition.url, res_edit.content.decode("utf-8"))
        self.assertNotIn(":9000/", res_edit.content.decode("utf-8"))

        res_gw_a = client_a.get(rendition.url)
        self.assertEqual(res_gw_a.status_code, 200)
        self.assertGreater(len(b"".join(res_gw_a.streaming_content)), 0)

        client_b = Client()
        client_b.force_login(self.author_b)
        self.assertEqual(client_b.get(rendition.url).status_code, 403)
        self.assertEqual(Client().get(rendition.url).status_code, 403)

    def test_08_base_template_has_favicon_link(self):
        client = Client()
        res = client.get(self.blog_index.get_url())
        self.assertEqual(res.status_code, 200)
        self.assertIn('rel="icon"', res.content.decode("utf-8"))

    def test_09_comment_widget_js_renders_inline_card_error_on_react_failure(self):
        from pathlib import Path
        js_path = Path(__file__).resolve().parents[1] / "myblog" / "static" / "comments" / "js" / "comments_widget.js"
        js_text = js_path.read_text(encoding="utf-8")
        self.assertIn("showCardFeedback", js_text)
        self.assertIn(".comment-card-inline-alert", js_text)
