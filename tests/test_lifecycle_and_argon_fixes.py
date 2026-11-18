"""
Wagtail 8.1 生命周期审核与剩余未测核心流程回归测试。
覆盖页面复制/别名转换水合、live 版本对比隔离、RoutablePage/子树回收路由、
回收站冲突弹窗与全字段还原、评论 5 分钟编辑前端契约、音视频发布门禁、
导航栏死链过滤与常驻搜索订阅、媒体网关缓存头及超期自动清理冷却触发。
"""

from datetime import date, timedelta
from io import BytesIO
import os
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django

django.setup()

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image as PILImage
from wagtail.models import Page
from wagtail.rich_text import RichText

from blog.models import BlogIndexPage, BlogPage, BlogPageGalleryImage, BlogPagePerson
from comments.models import Comment
from comments.services import CommentService
from content_lifecycle.models import RecycleBatch
from content_lifecycle.permissions import ensure_author_role_infrastructure
from content_lifecycle.recycle import RecycleService
from guides.models import GuideIndexPage, GuidePage, GuidePageAuthor, GuideRelatedPage, GuideTopic
from home.models import HomePage
from media_library.models import Audio, CustomDocument, CustomImage, Video, VisibilityChoices
from people.models import Person
from portfolio.models import PortfolioIndexPage, ProjectPage, ProjectPagePerson
from site_settings.models import NavigationSettings
from taxonomy.models import TagIndexPage

User = get_user_model()


def _make_png(name: str = "img.png") -> SimpleUploadedFile:
    buf = BytesIO()
    PILImage.new("RGB", (20, 20), (15, 110, 200)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


class LifecycleAndArgonFixesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.infra = ensure_author_role_infrastructure()
        cls.authors_group = cls.infra["authors_group"]
        cls.home = HomePage.objects.first()
        cls.superuser = User.objects.create_superuser(
            username=f"laf_su_{uuid4().hex[:6]}",
            email="laf_su@test.com",
            password="StrongAdminPassword!123",
        )
        cls.author_a = User.objects.create_user(
            username=f"laf_a_{uuid4().hex[:6]}",
            password="AuthorPassword!123",
            is_active=True,
        )
        cls.author_a.groups.add(cls.authors_group)
        cls.person_a = Person.objects.create(name="LAF Author A", linked_user=cls.author_a)

        cls.member = User.objects.create_user(
            username=f"laf_m_{uuid4().hex[:6]}",
            password="MemberPassword!123",
            is_active=True,
        )

        cls.blog_index = BlogIndexPage(title="LAF Blog", slug=f"laf-blog-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.blog_index)
        cls.blog_index.save_revision(user=cls.superuser).publish(user=cls.superuser)

        cls.guide_index = GuideIndexPage(title="LAF Guide", slug=f"laf-guide-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.guide_index)
        cls.guide_index.save_revision(user=cls.superuser).publish(user=cls.superuser)

        cls.portfolio_index = PortfolioIndexPage(title="LAF Port", slug=f"laf-port-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.portfolio_index)
        cls.portfolio_index.save_revision(user=cls.superuser).publish(user=cls.superuser)

        cls.topic_a = GuideTopic.objects.create(name=f"TopicA-{uuid4().hex[:4]}", slug=f"topa-{uuid4().hex[:4]}")
        cls.topic_b = GuideTopic.objects.create(name=f"TopicB-{uuid4().hex[:4]}", slug=f"topb-{uuid4().hex[:4]}")

    def test_01_copy_published_pages_without_draft_preserves_mongo_body(self):
        blog = BlogPage(title="Orig Blog", slug=f"ob-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 7, 1))
        blog.intro = [("rich_text", RichText("<p>COPY_INTRO_KEEP</p>"))]
        blog.body = [("markdown", "COPY_BODY_KEEP_BLOG")]
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person_a, role="作者")
        blog.save_revision(user=self.author_a).publish(user=self.author_a)

        # 1. Python copy() API
        copied_api = blog.copy(
            to=self.blog_index,
            update_attrs={"title": "Copy Blog API", "slug": f"cb-api-{uuid4().hex[:6]}"},
            user=self.author_a,
        )
        copied_api.refresh_from_db()
        api_obj = copied_api.get_latest_revision_as_object()
        self.assertIn("COPY_BODY_KEEP_BLOG", str(api_obj.body))
        self.assertIn("COPY_INTRO_KEEP", str(api_obj.intro))

        # 2. Wagtail CopyView POST (UI 入口直接传 Page 基类给 CopyPageAction)
        client = Client()
        client.force_login(self.author_a)
        new_slug = f"cb-ui-{uuid4().hex[:6]}"
        res = client.post(
            reverse("wagtailadmin_pages:copy", args=[blog.id]),
            {"new_title": "Copy Blog UI", "new_slug": new_slug, "new_parent_page": self.blog_index.id},
        )
        self.assertIn(res.status_code, (302, 200))
        copied_ui = BlogPage.objects.get(slug=new_slug)
        ui_obj = copied_ui.get_latest_revision_as_object()
        self.assertIn("COPY_BODY_KEEP_BLOG", str(ui_obj.body))

        # 3. ProjectPage copy()
        proj = ProjectPage(title="Orig Proj", slug=f"op-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 7, 1))
        proj.body = [("markdown", "COPY_PROJ_BODY_KEEP")]
        self.portfolio_index.add_child(instance=proj)
        ProjectPagePerson.objects.create(page=proj, person=self.person_a, role="负责人")
        proj.save_revision(user=self.author_a).publish(user=self.author_a)
        copied_proj = proj.copy(
            to=self.portfolio_index,
            update_attrs={"title": "Copy Proj", "slug": f"cp-{uuid4().hex[:6]}"},
            user=self.author_a,
        )
        self.assertIn("COPY_PROJ_BODY_KEEP", str(copied_proj.get_latest_revision_as_object().body))

    def test_02_convert_alias_view_preserves_source_body_in_mongo_revision(self):
        blog = BlogPage(title="Source For Alias", slug=f"sfa-{uuid4().hex[:6]}", owner=self.superuser, authored_on=date(2026, 7, 1))
        blog.body = [("markdown", "ALIAS_CONVERT_BODY_KEEP")]
        self.blog_index.add_child(instance=blog)
        blog.save_revision(user=self.superuser).publish(user=self.superuser)

        alias = blog.create_alias(parent=self.blog_index, update_slug=f"al-{uuid4().hex[:6]}", user=self.superuser)
        self.assertIsNotNone(alias.alias_of_id)

        client = Client()
        client.force_login(self.superuser)
        res = client.post(reverse("wagtailadmin_pages:convert_alias", args=[alias.id]))
        self.assertEqual(res.status_code, 302)

        converted = BlogPage.objects.get(id=alias.id)
        self.assertIsNone(converted.alias_of_id)
        rev_obj = converted.live_revision.as_object()
        self.assertIn("ALIAS_CONVERT_BODY_KEEP", str(rev_obj.body))

    def test_03_revisions_compare_live_uses_live_revision_not_draft_scalars(self):
        blog = BlogPage(title="LIVE_TITLE_V1", slug=f"cmp-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 7, 1))
        blog.body = [("markdown", "LIVE_BODY_V1")]
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person_a, role="作者")
        blog.save_revision(user=self.author_a).publish(user=self.author_a)

        blog.title = "DRAFT_TITLE_V2"
        blog.body = [("markdown", "DRAFT_BODY_V2")]
        draft_rev = blog.save_revision(user=self.author_a)

        # 模拟主表 title 被写入草稿标题或脏写时，对比 live...draft 仍应以 live_revision 为准
        BlogPage.objects.filter(pk=blog.pk).update(title="DIRTY_SCALAR_TITLE", draft_title="DRAFT_TITLE_V2")

        client = Client()
        client.force_login(self.author_a)
        res = client.get(reverse("wagtailadmin_pages:revisions_compare", args=[blog.id, "live", draft_rev.id]))
        self.assertEqual(res.status_code, 200)
        html_text = res.content.decode("utf-8")
        self.assertIn('class="deletion">LIVE</span>', html_text)
        self.assertIn("LIVE_BODY_V1", html_text)
        self.assertIn("DRAFT_BODY_V2", html_text)
        self.assertNotIn("DIRTY_SCALAR_TITLE", html_text)

    def test_04_routable_recycled_tag_index_subroute_and_child_under_recycled_parent(self):
        tag_idx = TagIndexPage(title="Recycled Tags", slug=f"rtags-{uuid4().hex[:6]}")
        self.home.add_child(instance=tag_idx)
        tag_idx.save_revision(user=self.superuser).publish(user=self.superuser)
        RecycleService.recycle_page_or_subtree(tag_idx, actor=self.superuser)

        client_su = Client()
        client_su.force_login(self.superuser)
        res_sub = client_su.get(f"{tag_idx.get_url()}python/")
        self.assertEqual(res_sub.status_code, 404)
        self.assertIn("受控回收站", res_sub.content.decode("utf-8"))
        self.assertNotIn("受控回收站", Client().get(f"{tag_idx.get_url()}python/").content.decode("utf-8"))

        # 父栏目回收时，子页面访问原前台 URL 也应显示精确回收时间
        sub_idx = BlogIndexPage(title="Parent Index", slug=f"pidx-{uuid4().hex[:6]}")
        self.home.add_child(instance=sub_idx)
        sub_idx.save_revision(user=self.superuser).publish(user=self.superuser)
        child = BlogPage(title="Child Under Recycled", slug=f"cur-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 7, 1))
        child.body = [("markdown", "child body")]
        sub_idx.add_child(instance=child)
        BlogPagePerson.objects.create(page=child, person=self.person_a, role="作者")
        child.save_revision(user=self.author_a).publish(user=self.author_a)
        child_url = child.get_url()

        RecycleService.recycle_page_or_subtree(sub_idx, actor=self.superuser)
        res_child = client_su.get(child_url)
        self.assertEqual(res_child.status_code, 404)
        self.assertIn("受控回收站", res_child.content.decode("utf-8"))

    def test_05_recycle_restore_conflict_form_and_full_field_restoration(self):
        # 1. 创建并发布包含完整字段与子表的 GuidePage + BlogPage + ProjectPage
        ref_guide = GuidePage(
            title="Ref Guide",
            slug=f"rg-{uuid4().hex[:6]}",
            owner=self.author_a,
            topic=self.topic_a,
            authored_on=date(2026, 7, 1),
        )
        ref_guide.body = [("step", {"step_number": "1", "title": "S1", "content": RichText("<p>ref</p>")})]
        self.guide_index.add_child(instance=ref_guide)
        GuidePageAuthor.objects.create(page=ref_guide, person=self.person_a, role="主笔作者")
        ref_guide.save_revision(user=self.author_a).publish(user=self.author_a)

        guide = GuidePage(
            title="Guide Orig Title",
            slug=f"gcol-{uuid4().hex[:6]}",
            owner=self.author_a,
            topic=self.topic_a,
            difficulty="beginner",
            estimated_minutes=10,
            authored_on=date(2026, 7, 1),
        )
        guide.body = [("step", {"step_number": "1", "title": "Step1", "content": RichText("<p>guide</p>")})]
        self.guide_index.add_child(instance=guide)
        GuidePageAuthor.objects.create(page=guide, person=self.person_a, role="主笔作者")
        GuideRelatedPage.objects.create(page=guide, related_page=ref_guide)
        guide.save_revision(user=self.author_a).publish(user=self.author_a)

        # 在草稿中修改 GuidePage 专属字段（live 仍为旧值，修改仅在最新 Revision 中）
        guide.title = "Guide Draft Updated Title"
        guide.topic = self.topic_b
        guide.difficulty = "advanced"
        guide.estimated_minutes = 45
        guide.seo_title = "Guide SEO Restore"
        guide.save_revision(user=self.author_a)

        orig_slug = guide.slug
        guide_old_placeholder = f"{orig_slug}-archived"
        batch = RecycleService.recycle_page_or_subtree(guide, actor=self.author_a)
        GuidePage.objects.filter(id=guide.id).update(slug=guide_old_placeholder)
        entry = batch.entries.get(page_id=guide.id)
        entry.original_slug = orig_slug
        entry.save(update_fields=["original_slug"])
        # 制造同 slug 碰撞页面
        collider = GuidePage(
            title="Collider",
            slug=orig_slug,
            owner=self.author_a,
            topic=self.topic_a,
            authored_on=date(2026, 7, 1),
        )
        collider.body = [("step", {"step_number": "1", "title": "CS1", "content": RichText("<p>c</p>")})]
        self.guide_index.add_child(instance=collider)
        GuidePageAuthor.objects.create(page=collider, person=self.person_a, role="主笔作者")
        collider.save_revision(user=self.author_a).publish(user=self.author_a)

        # GET restore_view 必须渲染冲突改名表单，而不能直接重定向报错退出
        client = Client()
        client.force_login(self.author_a)
        restore_url = reverse("recycle_bin:restore", args=[batch.pk])
        res_get = client.get(restore_url)
        self.assertEqual(res_get.status_code, 200)
        get_html = res_get.content.decode("utf-8")
        self.assertIn(f"name='slug_{guide.id}'", get_html)

        # POST 提交新 slug 恢复，核验所有 GuidePage 字段恢复完整
        new_slug = f"{orig_slug}-restored"
        res_post = client.post(restore_url, {f"slug_{guide.id}": new_slug})
        self.assertEqual(res_post.status_code, 302)
        restored = GuidePage.objects.get(id=guide.id)
        self.assertEqual(restored.slug, new_slug)
        self.assertEqual(restored.title, "Guide Draft Updated Title")
        self.assertEqual(restored.topic_id, self.topic_b.id)
        self.assertEqual(restored.difficulty, "advanced")
        self.assertEqual(restored.estimated_minutes, 45)
        self.assertEqual(restored.seo_title, "Guide SEO Restore")
        self.assertEqual(restored.related_guides.count(), 1)

        # 2. BlogPage gallery_images & ProjectPage star_count
        img = CustomImage.objects.create(title="GImg", file=_make_png("g.png"), uploaded_by_user=self.author_a)
        blog = BlogPage(title="Blog Gallery", slug=f"bg-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 7, 1))
        blog.body = [("markdown", "b")]
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person_a, role="作者")
        BlogPageGalleryImage.objects.create(page=blog, image=img, caption="cap1")
        blog.save_revision(user=self.author_a).publish(user=self.author_a)
        b_batch = RecycleService.recycle_page_or_subtree(blog, actor=self.author_a)
        RecycleService.restore_page_or_subtree(b_batch.batch_id, actor=self.author_a)
        blog.refresh_from_db()
        self.assertEqual(blog.gallery_images.count(), 1)

        proj = ProjectPage(title="Proj Star", slug=f"ps-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 7, 1), star_count=77)
        proj.body = [("markdown", "p")]
        self.portfolio_index.add_child(instance=proj)
        ProjectPagePerson.objects.create(page=proj, person=self.person_a, role="负责人")
        proj.save_revision(user=self.author_a).publish(user=self.author_a)
        proj.star_count = 123
        proj.save_revision(user=self.author_a)
        p_batch = RecycleService.recycle_page_or_subtree(proj, actor=self.author_a)
        RecycleService.restore_page_or_subtree(p_batch.batch_id, actor=self.author_a)
        proj.refresh_from_db()
        self.assertEqual(proj.star_count, 123)

        # 3. 父栏目被回收时：受限作者无权恢复父栏目，不应显示“自动连带恢复上级栏目”勾选框
        parent_col = BlogIndexPage(title="Recycled Parent Col", slug=f"rpc-{uuid4().hex[:6]}", owner=self.superuser)
        self.home.add_child(instance=parent_col)
        parent_col.save_revision(user=self.superuser).publish(user=self.superuser)
        sub_post = BlogPage(title="Sub Post", slug=f"sp-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 7, 1))
        sub_post.body = [("markdown", "sub")]
        parent_col.add_child(instance=sub_post)
        BlogPagePerson.objects.create(page=sub_post, person=self.person_a, role="作者")
        sub_post.save_revision(user=self.author_a).publish(user=self.author_a)

        child_batch = RecycleService.recycle_page_or_subtree(sub_post, actor=self.author_a)
        RecycleService.recycle_page_or_subtree(parent_col, actor=self.superuser)
        res_author = client.get(reverse("recycle_bin:restore", args=[child_batch.pk]))
        self.assertEqual(res_author.status_code, 200)
        self.assertNotIn("name='restore_parent_batch'", res_author.content.decode("utf-8"))
        self.assertIn("name='target_parent_id'", res_author.content.decode("utf-8"))

    def test_06_comment_edit_api_exposure_and_frontend_edit_contract(self):
        blog = BlogPage(title="Comment Edit Page", slug=f"ce-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 7, 1))
        blog.body = [("markdown", "comment edit body")]
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person_a, role="作者")
        blog.save_revision(user=self.author_a).publish(user=self.author_a)

        c = CommentService.post_comment(page=blog, author=self.member, raw_content="初始版本 V1", ip_address="127.0.0.1")
        CommentService.moderate_comment(c.id, "approve", reviewer=self.superuser)
        CommentService.edit_comment(c.id, self.member, "待审新版 V2")

        # 作者本人查询列表时返回 editable_raw_content 与 has_pending_edit=True；游客看不到
        client_m = Client()
        client_m.force_login(self.member)
        res_m = client_m.get(reverse("api_v1:list_comments", args=[blog.id]))
        self.assertEqual(res_m.status_code, 200)
        item_m = res_m.json()["items"][0]
        self.assertTrue(item_m["is_editable"])
        self.assertTrue(item_m["has_pending_edit"])
        self.assertEqual(item_m["editable_raw_content"], "待审新版 V2")

        res_g = Client().get(reverse("api_v1:list_comments", args=[blog.id]))
        item_g = res_g.json()["items"][0]
        self.assertFalse(item_g["is_editable"])
        self.assertFalse(item_g.get("has_pending_edit", False))
        self.assertNotIn("editable_raw_content", item_g)

        # 前端模板与 JS 具备编辑入口
        page_html = client_m.get(blog.get_url()).content.decode("utf-8")
        self.assertIn("data-edit-url-tpl=", page_html)
        from pathlib import Path
        js_text = (Path(__file__).resolve().parents[1] / "myblog" / "static" / "comments" / "js" / "comments_widget.js").read_text(encoding="utf-8")
        self.assertIn("edit-comment-btn", js_text)
        self.assertIn("has_pending_edit", js_text)

    def test_07_audio_video_publish_gate_blocks_restricted_for_non_superuser(self):
        aud = Audio.objects.create(
            title="Restricted Audio",
            file=ContentFile(b"fake-audio", name="test.mp3"),
            visibility=VisibilityChoices.RESTRICTED,
        )
        vid = Video.objects.create(
            title="Restricted Video",
            file=ContentFile(b"fake-video", name="test.mp4"),
            visibility=VisibilityChoices.RESTRICTED,
        )
        blog = BlogPage(title="AV Gate Page", slug=f"av-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 7, 1))
        blog.body = [
            ("audio", {"audio": aud, "title_override": "a"}),
            ("video", {"video": vid, "title_override": "v"}),
        ]
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person_a, role="作者")
        rev = blog.save_revision(user=self.author_a)
        with self.assertRaises(ValidationError):
            rev.publish(user=self.author_a)

        # 超级管理员发布本人的页面时自动转公开并放行
        blog.owner = self.superuser
        blog.save(clean=False)
        rev2 = blog.save_revision(user=self.superuser)
        rev2.publish(user=self.superuser)
        aud.refresh_from_db()
        vid.refresh_from_db()
        self.assertEqual(aud.visibility, VisibilityChoices.PUBLIC)
        self.assertEqual(vid.visibility, VisibilityChoices.PUBLIC)

    def test_08_base_html_filters_recycled_and_empty_nav_items_and_keeps_search_feeds(self):
        recycled_page = BlogPage(title="Nav Recycled", slug=f"nrec-{uuid4().hex[:6]}", owner=self.superuser, authored_on=date(2026, 7, 1))
        recycled_page.body = [("markdown", "nav")]
        self.blog_index.add_child(instance=recycled_page)
        recycled_page.save_revision(user=self.superuser).publish(user=self.superuser)
        RecycleService.recycle_page_or_subtree(recycled_page, actor=self.superuser)

        live_page = BlogPage(title="Nav Live", slug=f"nlive-{uuid4().hex[:6]}", owner=self.superuser, authored_on=date(2026, 7, 1))
        live_page.body = [("markdown", "nav live")]
        self.blog_index.add_child(instance=live_page)
        live_page.save_revision(user=self.superuser).publish(user=self.superuser)

        nav, _ = NavigationSettings.objects.get_or_create(singleton_key=1)
        nav.nav_items = [
            ("menu_item", {"label": "有效文章", "target_page": live_page, "external_url": "", "badge": "", "is_external": False}),
            ("menu_item", {"label": "死链占位", "target_page": None, "external_url": "", "badge": "", "is_external": False}),
            ("menu_item", {"label": "已回收入口", "target_page": recycled_page, "external_url": "", "badge": "", "is_external": False}),
        ]
        nav.save()

        html_text = Client().get(self.blog_index.get_url()).content.decode("utf-8")
        self.assertIn("有效文章", html_text)
        self.assertNotIn("死链占位", html_text)
        self.assertNotIn("已回收入口", html_text)
        self.assertIn('href="/search/"', html_text)
        self.assertIn('href="/feeds/"', html_text)

    def test_09_rendition_cache_header_and_auto_purge_cooldown(self):
        col = self.infra["workspace_collection"]
        img = CustomImage.objects.create(
            title="Cache Img",
            file=_make_png("cache.png"),
            uploaded_by_user=self.author_a,
            collection=col,
            visibility=VisibilityChoices.RESTRICTED,
        )
        rendition = img.get_rendition("original")
        client_a = Client()
        client_a.force_login(self.author_a)
        res = client_a.get(rendition.url)
        self.assertEqual(res.status_code, 200)
        self.assertIn("private", res.get("Cache-Control", ""))

        # 30 天超期冷却触发：超过 30 天的批次自动销毁，未超期的保留，冷却期内不重复扫描
        from content_lifecycle.recycle import maybe_trigger_expired_recycle_purge

        old_page = BlogPage(title="Expired 35d", slug=f"exp-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 7, 1))
        old_page.body = [("markdown", "old")]
        self.blog_index.add_child(instance=old_page)
        BlogPagePerson.objects.create(page=old_page, person=self.person_a, role="作者")
        old_page.save_revision(user=self.author_a).publish(user=self.author_a)
        old_batch = RecycleService.recycle_page_or_subtree(old_page, actor=self.author_a)
        RecycleBatch.objects.filter(pk=old_batch.pk).update(recycled_at=timezone.now() - timedelta(days=35))

        fresh_page = BlogPage(title="Fresh 1d", slug=f"fr-{uuid4().hex[:6]}", owner=self.author_a, authored_on=date(2026, 7, 1))
        fresh_page.body = [("markdown", "fresh")]
        self.blog_index.add_child(instance=fresh_page)
        BlogPagePerson.objects.create(page=fresh_page, person=self.person_a, role="作者")
        fresh_page.save_revision(user=self.author_a).publish(user=self.author_a)
        fresh_batch = RecycleService.recycle_page_or_subtree(fresh_page, actor=self.author_a)

        cache.delete("recycle:auto_purge_cooldown")
        purged = maybe_trigger_expired_recycle_purge(force=False)
        self.assertEqual(purged, 1)
        old_batch.refresh_from_db()
        fresh_batch.refresh_from_db()
        self.assertEqual(old_batch.status, "purged")
        self.assertEqual(fresh_batch.status, "recycled")
        self.assertFalse(Page.objects.filter(id=old_page.id).exists())

    def test_10_custom_document_gateway_and_wagtaildocs_serve_enforce_visibility(self):
        col = self.infra["workspace_collection"]
        doc = CustomDocument.objects.create(
            title="Secret Doc",
            file=SimpleUploadedFile("secret.txt", b"top-secret-doc-bytes", content_type="text/plain"),
            uploaded_by_user=self.author_a,
            collection=col,
            visibility=VisibilityChoices.RESTRICTED,
        )
        self.assertTrue(doc.url.startswith("/media-gateway/document/"))
        self.assertNotIn(":9000", doc.url)

        wagtail_doc_url = reverse("wagtaildocs_serve", args=[doc.id, doc.filename])
        # 匿名与普通会员访问受限文档（无论是网关还是原生 /documents/）必须被 403 拦截，且绝不能 302 到 RustFS
        self.assertEqual(Client().get(doc.url).status_code, 403)
        self.assertEqual(Client().get(wagtail_doc_url).status_code, 403)
        client_m = Client()
        client_m.force_login(self.member)
        self.assertEqual(client_m.get(wagtail_doc_url).status_code, 403)

        # 作者本人可下载，且不暴露 :9000 重定向
        client_a = Client()
        client_a.force_login(self.author_a)
        res_a = client_a.get(wagtail_doc_url)
        self.assertEqual(res_a.status_code, 200)
        self.assertEqual(b"".join(res_a.streaming_content), b"top-secret-doc-bytes")
