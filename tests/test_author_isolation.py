"""
Phase 4: 基于 CustomBasePage 与 Wagtail 通用生命周期的作者隔离、媒体隔离与回收站隔离全量验收测试。
覆盖 BlogPage / GuidePage / ProjectPage / BlogIndexPage / GuideIndexPage / PortfolioIndexPage / HomePage。
"""

from datetime import timedelta
from io import BytesIO
import json
import os
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django

django.setup()

from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image as PILImage
from wagtail.models import Collection, Page, PageLogEntry, Revision

from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from content_lifecycle.models import RecycleBatch, RecycleEntry
from content_lifecycle.permissions import ensure_author_role_infrastructure
from content_lifecycle.recycle import RecycleService
from external_content.outbox import OutboxProcessor
from guides.models import GuideIndexPage, GuidePage, GuidePageAuthor, GuideTopic
from home.models import HomePage
from identity.account_lifecycle import AccountLifecycleService
from identity.models import AccountAuditLog, User
from media_library.access import check_media_access, restrict_asset
from wagtail.documents.forms import get_document_form
from wagtail.images.forms import get_image_form
from wagtail.search.index import insert_or_update_object
from media_library.models import (
    CustomDocument,
    CustomImage,
    MediaReferenceRecord,
    VisibilityChoices,
)
from people.models import Person
from portfolio.models import PortfolioIndexPage, ProjectPage, ProjectPagePerson


def _make_png_upload(name: str = "test.png") -> SimpleUploadedFile:
    buf = BytesIO()
    PILImage.new("RGB", (32, 32), color=(40, 120, 200)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def _make_txt_upload(name: str = "note.txt", content: bytes = b"hello doc") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="text/plain")


class AuthorIsolationPhase4Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root = Page.get_first_root_node()
        home = HomePage.objects.first()
        if home is None:
            home = HomePage(title="Phase4 Home", slug=f"p4-home-{uuid4().hex[:6]}")
            cls.root.add_child(instance=home)
        cls.home = home

        cls.infra = ensure_author_role_infrastructure()
        cls.authors_group = cls.infra["authors_group"]
        cls.workspace_col = cls.infra["workspace_collection"]
        cls.public_col = cls.infra["public_collection"]
        cls.editors_group, _ = Group.objects.get_or_create(name="Editors")
        from django.contrib.auth.models import Permission
        access_admin_perm = Permission.objects.filter(
            content_type__app_label="wagtailadmin", codename="access_admin"
        ).first()
        if access_admin_perm:
            cls.editors_group.permissions.add(access_admin_perm)

        cls.blog_index = BlogIndexPage(title="技术博客栏目", slug=f"p4-blog-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.blog_index)
        cls.blog_index.save_revision().publish()

        cls.guide_index = GuideIndexPage(title="技术专栏大厅", slug=f"p4-guides-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.guide_index)
        cls.guide_index.save_revision().publish()

        cls.portfolio_index = PortfolioIndexPage(title="作品集大厅", slug=f"p4-port-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.portfolio_index)
        cls.portfolio_index.save_revision().publish()

        cls.topic = GuideTopic.objects.create(
            name="Django隔离专题",
            slug=f"p4-topic-{uuid4().hex[:6]}",
        )

    def setUp(self):
        suffix = uuid4().hex[:6]
        self.superuser = User.objects.create_superuser(
            username=f"p4_su_{suffix}",
            email=f"su_{suffix}@example.com",
            password="Password123!",
        )
        self.editor = User.objects.create_user(
            username=f"p4_ed_{suffix}",
            password="Password123!",
            is_staff=True,
        )
        self.editor.groups.add(self.editors_group)

        self.author_a = User.objects.create_user(
            username=f"p4_auth_a_{suffix}",
            password="Password123!",
            is_staff=False,
        )
        self.author_a.groups.add(self.authors_group)

        self.author_b = User.objects.create_user(
            username=f"p4_auth_b_{suffix}",
            password="Password123!",
            is_staff=False,
        )
        self.author_b.groups.add(self.authors_group)

        self.person_a = Person.objects.create(name=f"作者A_{suffix}", linked_user=self.author_a)
        self.person_b = Person.objects.create(name=f"作者B_{suffix}", linked_user=self.author_b)

    def _create_blog(self, owner, slug_prefix="b"):
        p = BlogPage(title=f"Blog by {owner.username}", slug=f"{slug_prefix}-{uuid4().hex[:6]}", owner=owner, live=False)
        self.blog_index.add_child(instance=p)
        p.body = [("markdown", f"Content by {owner.username}")]
        p.save_revision(user=owner)
        p.refresh_from_db()
        return p

    def _create_guide(self, owner, with_step=True):
        p = GuidePage(
            title=f"Guide by {owner.username}",
            slug=f"g-{uuid4().hex[:6]}",
            owner=owner,
            topic=self.topic,
            live=False,
        )
        if with_step:
            p.body = [("step", {"step_number": 1, "title": "Step 1", "content": "Do it"})]
        self.guide_index.add_child(instance=p)
        p.save_revision(user=owner)
        p.refresh_from_db()
        return p

    def _create_project(self, owner):
        p = ProjectPage(title=f"Proj by {owner.username}", slug=f"prj-{uuid4().hex[:6]}", owner=owner, live=False)
        self.portfolio_index.add_child(instance=p)
        p.body = [("markdown", f"Project by {owner.username}")]
        p.save_revision(user=owner)
        p.refresh_from_db()
        return p

    def test_01_author_infrastructure_and_non_staff_admin_access(self):
        self.assertFalse(self.author_a.is_staff)
        self.assertTrue(self.author_a.has_perm("wagtailadmin.access_admin"))
        client = Client()
        client.force_login(self.author_a)
        res = client.get("/admin/")
        self.assertEqual(res.status_code, 200)

    def test_02_page_ownership_isolation_across_all_content_models(self):
        blog_a = self._create_blog(self.author_a)
        blog_b = self._create_blog(self.author_b)
        guide_b = self._create_guide(self.author_b)
        proj_b = self._create_project(self.author_b)

        # 1. 对象级权限检测：Author A 仅可动自己，不可动 Author B 的任何内容页，不可动任何大厅页
        for other_page in (blog_b, guide_b, proj_b):
            perms = other_page.permissions_for_user(self.author_a)
            self.assertFalse(perms.can_edit())
            self.assertFalse(perms.can_view_revisions())
            self.assertFalse(perms.can_publish())
            self.assertFalse(perms.can_unpublish())
            self.assertFalse(perms.can_delete())
            self.assertFalse(perms.can_copy())
            self.assertFalse(perms.can_move())
            self.assertFalse(perms.can_lock())
            self.assertFalse(perms.can_set_view_restrictions())
            self.assertFalse(perms.can_add_subpage())

        for hall in (self.home, self.blog_index, self.guide_index, self.portfolio_index):
            perms = hall.permissions_for_user(self.author_a)
            self.assertFalse(perms.can_edit())
            self.assertFalse(perms.can_publish())
            self.assertFalse(perms.can_delete())
            self.assertFalse(perms.can_reorder_children())
            self.assertFalse(perms.can_set_view_restrictions())
            if hall == self.home:
                self.assertFalse(perms.can_add_subpage())
            else:
                self.assertTrue(perms.can_add_subpage())

        # Own page permissions
        own_perms = blog_a.permissions_for_user(self.author_a)
        self.assertTrue(own_perms.can_edit())
        self.assertTrue(own_perms.can_view_revisions())
        self.assertTrue(own_perms.can_publish())
        self.assertTrue(own_perms.can_delete())
        self.assertTrue(own_perms.can_copy())
        self.assertFalse(own_perms.can_move())
        self.assertFalse(own_perms.can_lock())
        self.assertFalse(own_perms.can_set_view_restrictions())
        self.assertFalse(own_perms.can_add_subpage())

        # 2. HTTP 层拦截：搜索、日志、编辑、历史、版本查看、PreviewOnCreate、AddSubpage、ChooseParent
        client = Client()
        client.force_login(self.author_a)

        # Explorer 仅见自己页面
        res_exp = client.get(reverse("wagtailadmin_explore", args=[self.blog_index.id]))
        self.assertEqual(res_exp.status_code, 200)
        self.assertContains(res_exp, blog_a.title)
        self.assertNotContains(res_exp, blog_b.title)

        # Search 仅见自己页面
        insert_or_update_object(blog_a)
        insert_or_update_object(blog_b)
        res_search = client.get(reverse("wagtailadmin_pages:search"), {"q": "Blog"})
        self.assertEqual(res_search.status_code, 200)
        self.assertContains(res_search, blog_a.title)
        self.assertNotContains(res_search, blog_b.title)

        # Site history / PageLogEntry 隔离
        visible_ids = set(PageLogEntry.objects.viewable_by_user(self.author_a).values_list("page_id", flat=True))
        self.assertNotIn(blog_b.id, visible_ids)
        self.assertNotIn(guide_b.id, visible_ids)
        self.assertNotIn(proj_b.id, visible_ids)

        # 禁止编辑/删除/发布/历史/预览 Author B 的页面
        for url in [
            reverse("wagtailadmin_pages:edit", args=[blog_b.id]),
            reverse("wagtailadmin_pages:history", args=[guide_b.id]),
            reverse("wagtailadmin_pages:delete", args=[proj_b.id]),
            reverse("wagtailadmin_pages:copy", args=[blog_b.id]),
            reverse("wagtailadmin_pages:view_draft", args=[blog_b.id]),
        ]:
            res_denied = client.get(url)
            self.assertEqual(res_denied.status_code, 302, f"Expected 302 deny redirect for {url}")
            self.assertEqual(res_denied["Location"], "/admin/")
            self.assertEqual(
                client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code,
                403,
                f"Expected 403 AJAX for {url}",
            )

        # AddSubpage on BlogIndexPage 直接跳转创建 BlogPage，不露出 BlogIndexPage
        res_sub = client.get(reverse("wagtailadmin_pages:add_subpage", args=[self.blog_index.id]))
        self.assertEqual(res_sub.status_code, 302)
        self.assertIn(f"/admin/pages/add/blog/blogpage/{self.blog_index.id}/", res_sub["Location"])

        # 禁止在 BlogIndexPage 下新建 BlogIndexPage，禁止在 HomePage 或 BlogPage 下建子页
        for blocked_url in [
            reverse("wagtailadmin_pages:add", args=["blog", "blogindexpage", self.blog_index.id]),
            reverse("wagtailadmin_pages:preview_on_add", args=["blog", "blogindexpage", self.blog_index.id]),
            reverse("wagtailadmin_pages:add", args=["blog", "blogpage", self.home.id]),
            reverse("wagtailadmin_pages:preview_on_add", args=["blog", "blogpage", blog_a.id]),
            reverse("wagtailadmin_pages:choose_parent", args=["blog", "blogindexpage"]),
        ]:
            self.assertEqual(client.get(blocked_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 403)

        # 主编仍可完整编辑栏目页与所有作者页面
        ed_client = Client()
        ed_client.force_login(self.editor)
        self.assertEqual(ed_client.get(reverse("wagtailadmin_pages:edit", args=[self.blog_index.id])).status_code, 200)
        self.assertEqual(ed_client.get(reverse("wagtailadmin_pages:edit", args=[blog_b.id])).status_code, 200)

    def test_03_copy_alias_and_bulk_action_guards(self):
        blog_a = self._create_blog(self.author_a)
        blog_b = self._create_blog(self.author_b)

        client = Client()
        client.force_login(self.author_a)

        # 复制表单中不出现 alias / publish_copies，且 POST alias=on 被直接 403
        copy_url = reverse("wagtailadmin_pages:copy", args=[blog_a.id])
        res_get = client.get(copy_url)
        self.assertEqual(res_get.status_code, 200)
        self.assertNotIn('name="alias"', res_get.content.decode("utf-8"))
        res_alias = client.post(
            copy_url,
            {
                "new_title": "Alias Try",
                "new_slug": f"alias-try-{uuid4().hex[:6]}",
                "new_parent_page": self.blog_index.id,
                "alias": "on",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(res_alias.status_code, 403)
        self.assertFalse(Page.objects.filter(title="Alias Try").exists())

        # 批量发布混入他人页面：整批拒绝，blog_a 保持未发布
        bulk_pub_url = (
            reverse("wagtail_bulk_action", args=["wagtailcore", "page", "publish"])
            + f"?id={blog_a.id}&id={blog_b.id}"
        )
        res_bulk = client.post(bulk_pub_url, {}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(res_bulk.status_code, 403)
        blog_a.refresh_from_db()
        self.assertFalse(blog_a.live)

    def test_04_universal_person_auto_binding_and_impersonation_block(self):
        client = Client()
        client.force_login(self.author_a)

        # GET 新建 BlogPage / GuidePage / ProjectPage 时自动预填本人 Person
        for app_label, model_name, parent in [
            ("blog", "blogpage", self.blog_index),
            ("guides", "guidepage", self.guide_index),
            ("portfolio", "projectpage", self.portfolio_index),
        ]:
            add_url = reverse("wagtailadmin_pages:add", args=[app_label, model_name, parent.id])
            res = client.get(add_url)
            self.assertEqual(res.status_code, 200)
            html = res.content.decode("utf-8")
            self.assertIn(f'name="authors-0-person" value="{self.person_a.id}"', html)

        # 校验受限作者尝试把他人 Person 设为第一作者时被 CustomBasePageAdminForm 拒绝
        form_cls = BlogPage.get_edit_handler().get_form_class()
        blog_a = self._create_blog(self.author_a)
        bound_form = form_cls(
            data={
                "title": "Tampered Author",
                "slug": blog_a.slug,
                "authored_on": timezone.localdate().isoformat(),
                "intro-count": "0",
                "body-count": "0",
                "gallery_images-TOTAL_FORMS": "0",
                "gallery_images-INITIAL_FORMS": "0",
                "gallery_images-MIN_NUM_FORMS": "0",
                "gallery_images-MAX_NUM_FORMS": "1000",
                "authors-TOTAL_FORMS": "1",
                "authors-INITIAL_FORMS": "0",
                "authors-MIN_NUM_FORMS": "0",
                "authors-MAX_NUM_FORMS": "1000",
                "authors-0-person": str(self.person_b.id),
                "authors-0-role": "primary_author",
                "authors-0-ORDER": "0",
                "authors-0-DELETE": "",
            },
            instance=blog_a,
            parent_page=self.blog_index,
            for_user=self.author_a,
        )
        self.assertFalse(bound_form.is_valid())
        self.assertTrue(bound_form.non_field_errors())

        # 主编不受此限
        editor_form = form_cls(
            data=bound_form.data,
            instance=blog_a,
            parent_page=self.blog_index,
            for_user=self.editor,
        )
        self.assertTrue(editor_form.is_valid(), editor_form.errors)

        # 未绑定 Person 的受限作者不得冒用任何未绑定人物（例如原绑定账号被硬删后遗留的 Person）
        unbound_author = User.objects.create_user(
            username=f"p4_unbound_{uuid4().hex[:6]}",
            password="Password123!",
            is_staff=False,
        )
        unbound_author.groups.add(self.authors_group)
        orphaned_person = Person.objects.create(name="已解绑名人", linked_user=None)
        blog_u = self._create_blog(unbound_author)
        unbound_form = form_cls(
            data={
                **bound_form.data,
                "slug": blog_u.slug,
                "authors-0-person": str(orphaned_person.id),
                "authors-0-role": "co_author",
            },
            instance=blog_u,
            parent_page=self.blog_index,
            for_user=unbound_author,
        )
        self.assertFalse(unbound_form.is_valid())

    def test_05_media_isolation_replace_guard_and_auto_public_on_publish(self):
        img_a = CustomImage.objects.create(
            title="Author A Secret",
            file=_make_png_upload("a_sec.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        img_b = CustomImage.objects.create(
            title="Author B Secret",
            file=_make_png_upload("b_sec.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_b,
            visibility=VisibilityChoices.RESTRICTED,
        )
        doc_a = CustomDocument.objects.create(
            title="Author A Doc",
            file=_make_txt_upload("a_doc.txt"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        pub_img = CustomImage.objects.create(
            title="Public Stock Image",
            file=_make_png_upload("pub_stock.png"),
            collection=self.public_col,
            uploaded_by_user=self.superuser,
            visibility=VisibilityChoices.PUBLIC,
        )
        pub_hidden_img = CustomImage.objects.create(
            title="Public Col Restricted Draft",
            file=_make_png_upload("pub_hid.png"),
            collection=self.public_col,
            uploaded_by_user=self.superuser,
            visibility=VisibilityChoices.RESTRICTED,
        )

        # 1. Chooser / Index 隔离
        client = Client()
        client.force_login(self.author_a)
        res_chooser = client.get(reverse("wagtailimages_chooser:choose"))
        self.assertEqual(res_chooser.status_code, 200)
        html = res_chooser.content.decode("utf-8")
        self.assertIn("Author A Secret", html)
        self.assertIn("Public Stock Image", html)
        self.assertNotIn("Author B Secret", html)
        self.assertNotIn("Public Col Restricted Draft", html)

        # 2. Gateway 隔离（is_staff=True 的他人/无权限者也绝不可跳过）
        req_a = self.client.request().wsgi_request if False else type("Req", (), {"user": self.author_a})()
        req_b = type("Req", (), {"user": self.author_b})()
        req_ed = type("Req", (), {"user": self.editor})()
        self.assertTrue(check_media_access(img_a, req_a))
        self.assertFalse(check_media_access(img_b, req_a))
        self.assertFalse(check_media_access(pub_hidden_img, req_a))
        self.assertTrue(check_media_access(img_b, req_ed))
        self.assertEqual(client.get(f"/media-gateway/image/{img_b.public_id}/").status_code, 403)

        # 3. 禁止在引用他人 RESTRICTED 媒体时发布（跨 BlogPage / GuidePage / ProjectPage 通用生效）
        blog_a = self._create_blog(self.author_a)
        blog_a.body = [("image", {"image": img_b})]
        rev_bad = blog_a.save_revision(user=self.author_a)
        with self.assertRaises(ValidationError):
            rev_bad.publish(user=self.author_a)
        img_b.refresh_from_db()
        self.assertEqual(img_b.visibility, VisibilityChoices.RESTRICTED)

        # 4. 自身配图在草稿/未来排期时不公开，正式发布后自动公开 (BlogPage + GuidePage + ProjectPage)
        blog_a.featured_image = img_a
        blog_a.body = [("document", {"document": doc_a, "title_override": "Spec"})]
        blog_a.go_live_at = timezone.now() + timedelta(days=3)
        rev_sched = blog_a.save_revision(user=self.author_a)
        rev_sched.publish(user=self.author_a)
        img_a.refresh_from_db()
        doc_a.refresh_from_db()
        self.assertEqual(img_a.visibility, VisibilityChoices.RESTRICTED, "未来排期不应提前公开媒体")

        # 立即正式发布 BlogPage
        blog_a.go_live_at = None
        rev_live = blog_a.save_revision(user=self.author_a)
        rev_live.publish(user=self.author_a)
        img_a.refresh_from_db()
        doc_a.refresh_from_db()
        self.assertEqual(img_a.visibility, VisibilityChoices.PUBLIC)
        self.assertEqual(doc_a.visibility, VisibilityChoices.PUBLIC)
        anon = Client()
        self.assertEqual(anon.get(f"/media-gateway/image/{img_a.public_id}/").status_code, 200)

        # 同等验证 GuidePage 与 ProjectPage 发布自动公开本人图片
        img_g = CustomImage.objects.create(
            title="Guide Img",
            file=_make_png_upload("g_img.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        guide_a = self._create_guide(self.author_a, with_step=True)
        guide_a.body = [("step", {"step_number": 1, "title": "Step 1", "content": "Do it"})]
        guide_a.featured_image = img_g
        guide_a.save_revision(user=self.author_a).publish(user=self.author_a)
        img_g.refresh_from_db()
        self.assertEqual(img_g.visibility, VisibilityChoices.PUBLIC)

        img_p = CustomImage.objects.create(
            title="Proj Img",
            file=_make_png_upload("p_img.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        proj_a = self._create_project(self.author_a)
        proj_a.featured_image = img_p
        proj_a.save_revision(user=self.author_a).publish(user=self.author_a)
        img_p.refresh_from_db()
        self.assertEqual(img_p.visibility, VisibilityChoices.PUBLIC)

        # 5. EditForm 禁止替换已引用图片与文档原文件
        self.assertTrue(MediaReferenceRecord.objects.filter(asset_id=img_a.public_id).exists())
        img_form_cls = get_image_form(CustomImage)
        img_form = img_form_cls(
            data={"title": "Renamed", "collection": self.workspace_col.id, "tags": ""},
            files={"file": _make_png_upload("overwrite.png")},
            instance=img_a,
            user=self.author_a,
        )
        self.assertFalse(img_form.is_valid())
        self.assertIn("file", img_form.errors)

        doc_form_cls = get_document_form(CustomDocument)
        doc_form = doc_form_cls(
            data={"title": "Renamed Doc", "collection": self.workspace_col.id, "tags": ""},
            files={"file": _make_txt_upload("overwrite.txt")},
            instance=doc_a,
            user=self.author_a,
        )
        self.assertFalse(doc_form.is_valid())
        self.assertIn("file", doc_form.errors)

        # 6. restrict_asset 禁止非超管调用
        with self.assertRaises(PermissionDenied):
            restrict_asset("image", img_a.public_id, actor=self.author_a)
        with self.assertRaises(PermissionDenied):
            restrict_asset("image", img_a.public_id, actor=None)

    def test_06_recycle_bin_isolation_and_superuser_only_purge(self):
        blog_a = self._create_blog(self.author_a)
        blog_b = self._create_blog(self.author_b)

        batch_a = RecycleService.recycle_page_or_subtree(blog_a, actor=self.author_a)
        batch_b = RecycleService.recycle_page_or_subtree(blog_b, actor=self.author_b)

        # 打开列表时不自动清理超期批次
        RecycleBatch.objects.filter(pk=batch_b.pk).update(recycled_at=timezone.now() - timedelta(days=45))

        client_a = Client()
        client_a.force_login(self.author_a)

        # Author A 访问回收站：仅见自己批次，不见 Author B 批次，不见“彻底销毁”按钮
        res_idx = client_a.get("/admin/recycle_bin/")
        self.assertEqual(res_idx.status_code, 200)
        html_a = res_idx.content.decode("utf-8")
        self.assertIn(blog_a.title, html_a)
        self.assertNotIn(blog_b.title, html_a)
        self.assertIn("一键恢复", html_a)
        self.assertNotIn("彻底销毁", html_a)

        # 验证超期批次未被惰性销毁
        batch_b.refresh_from_db()
        self.assertEqual(batch_b.status, "recycled")

        # 原生 edit / add / delete 旁路已被剪除 (404)
        self.assertEqual(client_a.get(f"/admin/recycle_bin/edit/{batch_a.pk}/").status_code, 404)
        self.assertEqual(client_a.get("/admin/recycle_bin/new/").status_code, 404)

        # Author A 不能 inspect/restore/purge Author B 的批次
        self.assertEqual(
            client_a.get(f"/admin/recycle_bin/inspect/{batch_b.pk}/", HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code,
            403,
        )
        self.assertEqual(
            client_a.post(f"/admin/recycle_bin/{batch_b.pk}/restore/", HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code,
            403,
        )
        self.assertEqual(
            client_a.post(f"/admin/recycle_bin/{batch_a.pk}/purge/", HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code,
            403,
        )

        # 服务层同样严格拒绝非超管与无授权 actor=None
        with self.assertRaises(PermissionDenied):
            RecycleService.purge_page_or_subtree(blog_a.id, actor=self.author_a, batch_id=batch_a.batch_id)
        with self.assertRaises(PermissionDenied):
            RecycleService.purge_page_or_subtree(blog_a.id, actor=self.editor, batch_id=batch_a.batch_id)
        with self.assertRaises(PermissionDenied):
            RecycleService.purge_page_or_subtree(blog_a.id, actor=None, batch_id=batch_a.batch_id)
        with self.assertRaises(PermissionDenied):
            RecycleService.recycle_page_or_subtree(blog_a, actor=None)
        with self.assertRaises(PermissionDenied):
            RecycleService.restore_page_or_subtree(batch_a.batch_id, actor=self.author_b)

        # before_serve_page 对无 is_staff 但有 access_admin 的 Author A 展示回收提示
        # 同时验证已回收页面不可继续通过 edit / history / copy / delete / revisions_view 进入
        blog_a.refresh_from_db()
        perms_recycled = blog_a.permissions_for_user(self.author_a)
        self.assertFalse(perms_recycled.can_edit())
        self.assertFalse(perms_recycled.can_view_revisions())
        self.assertFalse(perms_recycled.can_delete())
        self.assertFalse(perms_recycled.can_copy())
        self.assertEqual(
            client_a.get(reverse("wagtailadmin_pages:edit", args=[blog_a.id]), HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code,
            403,
        )
        self.assertEqual(
            client_a.get(reverse("wagtailadmin_pages:history", args=[blog_a.id]), HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code,
            403,
        )
        from django.contrib.auth.models import AnonymousUser
        from django.http import Http404
        from django.test import RequestFactory
        from content_lifecycle.wagtail_hooks import block_recycled_pages_front_serve

        blog_a.refresh_from_db()
        rf = RequestFactory()
        req_a = rf.get("/")
        req_a.user = self.author_a
        res_front_a = block_recycled_pages_front_serve(blog_a, req_a, (), {})
        self.assertEqual(res_front_a.status_code, 404)
        self.assertIn("受控回收站", res_front_a.content.decode("utf-8"))
        req_anon = rf.get("/")
        req_anon.user = AnonymousUser()
        with self.assertRaises(Http404):
            block_recycled_pages_front_serve(blog_a, req_anon, (), {})

        # 超管可见彻底销毁按钮并成功执行 Purge
        su_client = Client()
        su_client.force_login(self.superuser)
        res_su_idx = su_client.get("/admin/recycle_bin/")
        self.assertIn("彻底销毁", res_su_idx.content.decode("utf-8"))
        res_purge = su_client.post(f"/admin/recycle_bin/{batch_b.pk}/purge/", follow=True)
        self.assertEqual(res_purge.status_code, 200)
        batch_b.refresh_from_db()
        self.assertEqual(batch_b.status, "purged")
        self.assertIn("已永久物理销毁", res_purge.content.decode("utf-8"))

    def test_07_role_revocation_and_user_hard_delete_preserves_person(self):
        blog_a = self._create_blog(self.author_a)
        blog_a.save_revision(user=self.author_a).publish(user=self.author_a)
        blog_a.refresh_from_db()
        self.assertTrue(blog_a.live)

        # 移出 Authors 组并递增 session_epoch：已发页面继续在线，继续编辑/发布立即失效
        # 同步准备：回收批次、受限媒体与未来排期版本，验证撤权后全部不可越权操作
        draft_for_sched = self._create_blog(self.author_a)
        draft_for_sched.go_live_at = timezone.now() + timedelta(days=2)
        sched_rev = draft_for_sched.save_revision(user=self.author_a, approved_go_live_at=draft_for_sched.go_live_at)
        rec_page = self._create_blog(self.author_a)
        rec_batch = RecycleService.recycle_page_or_subtree(rec_page, actor=self.author_a)
        priv_img = CustomImage.objects.create(
            title="Revoke Test Img",
            file=_make_png_upload("revoke.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )

        self.author_a.groups.remove(self.authors_group)
        AccountLifecycleService.bump_session_epoch([self.author_a.id])
        fresh_a = User.objects.get(pk=self.author_a.pk)
        blog_a.refresh_from_db()
        self.assertTrue(blog_a.live)
        self.assertFalse(blog_a.permissions_for_user(fresh_a).can_edit())
        self.assertFalse(blog_a.permissions_for_user(fresh_a).can_publish())
        with self.assertRaises(PermissionDenied):
            blog_a.validate_for_publish(user=fresh_a)

        # 撤权后不可恢复本人回收批次、不可访问原受限媒体、不可触发排期定时发布
        with self.assertRaises(PermissionDenied):
            RecycleService.restore_page_or_subtree(rec_batch.batch_id, actor=fresh_a)
        self.assertFalse(check_media_access(priv_img, type("Req", (), {"user": fresh_a})()))
        with self.assertRaises(PermissionDenied):
            sched_rev.publish()

        # 硬删作者账号：过户页面后删除 User，公开 Person 名片保留且 linked_user 置空
        del_res = AccountLifecycleService.hard_delete(
            actor=self.superuser,
            target_user_id=self.author_a.id,
            transfer_to_user_id=self.superuser.id,
            confirmed=True,
        )
        self.assertEqual(del_res["unlinked_person_id"], self.person_a.id)
        self.person_a.refresh_from_db()
        self.assertIsNone(self.person_a.linked_user)

    def test_08_draft_pruning_scoped_to_page_content_type(self):
        """验证草稿滑动窗口修剪仅作用于页面自身 Revision，绝不误删同 object_id 的非页面对象版本。"""
        from comments.models import CommunitySettings

        blog_a = self._create_blog(self.author_a)
        settings_obj = CommunitySettings.get_solo()
        other_ct = ContentType.objects.get_for_model(CommunitySettings)
        foreign_rev = Revision.objects.create(
            content_type=other_ct,
            base_content_type=other_ct,
            object_id=str(blog_a.id),
            content={"pk": settings_obj.pk},
            object_str="CommunitySettings Foreign Probe",
        )
        for i in range(23):
            blog_a.body = [("markdown", f"Draft v{i}")]
            blog_a.save_revision(user=self.author_a)

        self.assertTrue(
            Revision.objects.filter(pk=foreign_rev.pk).exists(),
            "页面草稿修剪绝不可删除同 object_id 的非页面对象历史版本！",
        )
        self.assertEqual(Revision.objects.for_instance(blog_a).count(), 20)
