"""
Phase 2 统一上传体验回归测试（零迁移）：
1. RichText 粘贴/拖拽传图接口权限、防降级共享根集合、标题冲突去重、派生图异常降级不重复写库；
2. Vditor 复用 Wagtail 原生 Chooser 后的 media-resolve 接口权限隔离与 asset:// 生成；
3. Wagtail 后台编辑页 hooks 注入（CB_EDITOR_CONFIG / coordinator / richtext-paste / Vditor chooser assets）；
4. RichText 与 Markdown 上传资产接入 MediaReferenceRecord 删除保护与发布自动转正完整链路。
"""

from datetime import date
from io import BytesIO
import json
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from PIL import Image as PILImage
from wagtail.models import Collection, GroupCollectionPermission, Page
from wagtail.rich_text import RichText

from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from content_blocks.blocks.markdown import CustomMarkdownBlock
from content_blocks.widgets import VditorMarkdownWidget
from content_lifecycle.permissions import ensure_author_role_infrastructure
from guides.models import GuideIndexPage, GuidePage, GuidePageAuthor, GuideTopic
from home.models import HomePage
from media_library.models import CustomDocument, CustomImage, MediaReferenceRecord, VisibilityChoices
from people.models import Person

User = get_user_model()


def _make_png(name: str = "shot.png", size: tuple[int, int] = (48, 32)) -> SimpleUploadedFile:
    buf = BytesIO()
    PILImage.new("RGB", size, color=(24, 119, 242)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


class Phase2UnifiedUploadsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.infra = ensure_author_role_infrastructure()
        cls.authors_group = cls.infra["authors_group"]
        cls.workspace_col = cls.infra["workspace_collection"]
        cls.public_col = cls.infra["public_collection"]
        cls.root_col = Collection.get_first_root_node()

        cls.home = HomePage.objects.first()
        if cls.home is None:
            root = Page.get_first_root_node()
            cls.home = HomePage(title="P2 Home", slug=f"p2-home-{uuid4().hex[:6]}")
            root.add_child(instance=cls.home)

        cls.blog_index = BlogIndexPage(title="P2 Blog", slug=f"p2-blog-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.blog_index)
        cls.blog_index.save_revision().publish()

        cls.guide_index = GuideIndexPage(title="P2 Guide", slug=f"p2-guide-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.guide_index)
        cls.guide_index.save_revision().publish()

        cls.topic = GuideTopic.objects.create(
            name=f"P2Topic-{uuid4().hex[:6]}",
            slug=f"p2-topic-{uuid4().hex[:6]}",
        )

    def setUp(self):
        suffix = uuid4().hex[:6]
        self.superuser = User.objects.create_superuser(
            username=f"p2_su_{suffix}",
            email=f"p2_su_{suffix}@example.com",
            password="Password123!",
        )
        self.author_a = User.objects.create_user(
            username=f"p2_a_{suffix}", password="Password123!", is_active=True
        )
        self.author_b = User.objects.create_user(
            username=f"p2_b_{suffix}", password="Password123!", is_active=True
        )
        self.author_a.groups.add(self.authors_group)
        self.author_b.groups.add(self.authors_group)
        self.person_a = Person.objects.create(name=f"P2作者A_{suffix}", linked_user=self.author_a)
        self.person_b = Person.objects.create(name=f"P2作者B_{suffix}", linked_user=self.author_b)

    def test_01_richtext_image_upload_permissions_collection_guard_and_dedup(self):
        url = reverse("content_blocks_richtext_image_upload")

        # 1. 匿名与无图片权限后台用户被拒绝
        resp_anon = self.client.post(url, {"file": _make_png()})
        self.assertIn(resp_anon.status_code, (302, 403))

        plain_admin = User.objects.create_user(username=f"p2_plain_{uuid4().hex[:6]}", password="Password123!")
        plain_admin.user_permissions.add(Permission.objects.get(codename="access_admin"))
        self.client.force_login(plain_admin)
        self.assertEqual(self.client.post(url, {"file": _make_png()}).status_code, 403)

        # 2. 只有 Root 集合 add 权限、无 change 权限且无个人工作区集合的受限用户 -> 绝不降级落入共享根集合
        root_add_only_group = Group.objects.create(name=f"RootAddOnly_{uuid4().hex[:6]}")
        GroupCollectionPermission.objects.create(
            group=root_add_only_group,
            collection=self.root_col,
            permission=Permission.objects.get(content_type__app_label="wagtailimages", codename="add_image"),
        )
        root_only_user = User.objects.create_user(username=f"p2_root_only_{uuid4().hex[:6]}", password="Password123!")
        root_only_user.user_permissions.add(Permission.objects.get(codename="access_admin"))
        root_only_user.groups.add(root_add_only_group)
        self.client.force_login(root_only_user)
        resp_no_col = self.client.post(url, {"file": _make_png()})
        self.assertEqual(resp_no_col.status_code, 403)
        self.assertEqual(resp_no_col.json()["error"]["code"], "no_writable_collection")

        # 3. 受限作者上传成功 + 标题防撞名自动去重
        self.client.force_login(self.author_a)
        resp_1 = self.client.post(url, {"file": _make_png("same-shot.png"), "format": "left", "alt": "架构图"})
        self.assertEqual(resp_1.status_code, 201)
        data_1 = resp_1.json()
        self.assertTrue(data_1["ok"])
        self.assertEqual(data_1["image"]["format"], "left")
        self.assertEqual(data_1["image"]["alt"], "架构图")
        self.assertEqual(data_1["image"]["title"], "same-shot")
        self.assertTrue(data_1["preview"]["url"].startswith("/media-gateway/rendition/"))

        img_1 = CustomImage.objects.get(pk=data_1["image"]["id"], public_id=data_1["image"]["asset_id"])
        self.assertEqual(img_1.uploaded_by_user_id, self.author_a.id)
        self.assertEqual(img_1.collection_id, self.workspace_col.id)
        self.assertEqual(img_1.visibility, VisibilityChoices.RESTRICTED)

        resp_2 = self.client.post(url, {"file": _make_png("same-shot.png"), "format": "left"})
        self.assertEqual(resp_2.status_code, 201)
        data_2 = resp_2.json()
        self.assertNotEqual(data_2["image"]["id"], data_1["image"]["id"])
        self.assertTrue(data_2["image"]["title"].startswith("same-shot-"))

        # 4. 参数边界校验：非法格式 / 超长 alt / 非法图片
        self.assertEqual(self.client.post(url, {"file": _make_png(), "format": "bad_fmt"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"file": _make_png(), "alt": "x" * 2049}).status_code, 400)
        bad_file = SimpleUploadedFile("broken.png", b"not-a-real-png", content_type="image/png")
        self.assertEqual(self.client.post(url, {"file": bad_file}).status_code, 400)

        # 5. 派生图 rendition 失败时，原图仍保留并降级返回 /media-gateway/image/<uuid>/（不返 500）
        before_count = CustomImage.objects.count()
        with patch.object(CustomImage, "get_rendition", side_effect=RuntimeError("rendition failed")):
            resp_fallback = self.client.post(url, {"file": _make_png("fallback.png"), "format": "left"})
        self.assertEqual(resp_fallback.status_code, 201)
        self.assertEqual(CustomImage.objects.count(), before_count + 1)
        fb_data = resp_fallback.json()
        self.assertEqual(
            fb_data["preview"]["url"],
            f"/media-gateway/image/{fb_data['image']['asset_id']}/",
        )

    def test_02_resolve_chosen_media_permissions_and_markdown_strings(self):
        url = reverse("content_blocks_media_resolve")
        img_a = CustomImage.objects.create(
            title="Author A [Secret] Img",
            file=_make_png("a.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        doc_a = CustomDocument.objects.create(
            title="课件 [第1章]",
            file=SimpleUploadedFile("lesson.md", b"# Lesson 1", content_type="text/markdown"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        doc_pub = CustomDocument.objects.create(
            title="公开手册",
            file=SimpleUploadedFile("guide.pdf", b"%PDF-1.4\n", content_type="application/pdf"),
            collection=self.public_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.PUBLIC,
        )

        # 匿名拦截
        self.assertIn(self.client.get(url, {"kind": "image", "id": str(img_a.pk)}).status_code, (302, 403))

        # 作者 A 解析自己的受限图片与受限文档
        self.client.force_login(self.author_a)
        self.assertEqual(self.client.get(url, {"kind": "audio", "id": "1"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"kind": "image", "id": "abc"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"kind": "document", "id": "9999999"}).status_code, 404)

        r_img = self.client.get(url, {"kind": "image", "id": str(img_a.pk)})
        self.assertEqual(r_img.status_code, 200)
        self.assertIn("no-store", r_img.headers.get("Cache-Control", ""))
        img_data = r_img.json()
        self.assertEqual(img_data["asset_id"], str(img_a.public_id))
        self.assertEqual(img_data["markdown"], f"![Author A (Secret) Img](asset://image/{img_a.public_id})")

        r_doc = self.client.get(url, {"kind": "document", "id": str(doc_a.pk)})
        self.assertEqual(r_doc.status_code, 200)
        doc_data = r_doc.json()
        self.assertEqual(doc_data["asset_id"], str(doc_a.public_id))
        self.assertEqual(doc_data["markdown"], f"[课件 (第1章)](asset://document/{doc_a.public_id})")
        self.assertEqual(doc_data["preview_url"], f"/media-gateway/document/{doc_a.public_id}/")

        # 作者 B 越权解析作者 A 的受限资产 -> 403；解析公开资产 -> 200
        self.client.force_login(self.author_b)
        self.assertEqual(self.client.get(url, {"kind": "image", "id": str(img_a.pk)}).status_code, 404)
        self.assertEqual(self.client.get(url, {"kind": "document", "id": str(doc_a.pk)}).status_code, 404)
        self.assertEqual(self.client.get(url, {"kind": "document", "id": str(doc_pub.pk)}).status_code, 200)

        # 超级管理员可解析所有资产
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(url, {"kind": "image", "id": str(img_a.pk)}).status_code, 200)
        self.assertEqual(self.client.get(url, {"kind": "document", "id": str(doc_a.pk)}).status_code, 200)

    def test_03_admin_editor_hooks_and_vditor_chooser_assets(self):
        md_block = CustomMarkdownBlock()
        self.assertIsInstance(md_block.field.widget, VditorMarkdownWidget)
        widget_html = md_block.field.widget.render("body-0-value", "")
        self.assertIn(reverse("content_blocks_media_resolve"), widget_html)
        self.assertIn(reverse("wagtailimages_chooser:choose"), widget_html)
        self.assertIn(reverse("wagtaildocs_chooser:choose"), widget_html)

        media_html = str(md_block.field.widget.media)
        self.assertIn("wagtailimages/js/image-chooser-modal.js", media_html)
        self.assertIn("wagtaildocs/js/document-chooser-modal.js", media_html)
        self.assertIn("content_blocks/js/admin-upload-coordinator.js", media_html)

        # 真实请求 BlogPage 与 GuidePage 后台新建页，校验全局编辑器钩子注入（零页面硬编码）
        self.client.force_login(self.author_a)
        for add_url in (
            reverse("wagtailadmin_pages:add", args=("blog", "blogpage", self.blog_index.id)),
            reverse("wagtailadmin_pages:add", args=("guides", "guidepage", self.guide_index.id)),
        ):
            resp = self.client.get(add_url)
            self.assertEqual(resp.status_code, 200)
            html_text = resp.content.decode("utf-8")
            self.assertIn("window.CB_EDITOR_CONFIG", html_text)
            self.assertIn(reverse("content_blocks_richtext_image_upload"), html_text)
            self.assertIn("content_blocks/js/admin-upload-coordinator.js", html_text)
            self.assertIn("content_blocks/js/admin-richtext-paste.js", html_text)
            self.assertIn("content_blocks/css/admin-split-editors.css", html_text)

    def test_04_end_to_end_richtext_and_markdown_upload_lifecycle_roundtrip(self):
        self.client.force_login(self.author_a)
        rt_resp = self.client.post(
            reverse("content_blocks_richtext_image_upload"),
            {"file": _make_png("step-paste.png"), "format": "left", "alt": "步骤粘贴图"},
        )
        self.assertEqual(rt_resp.status_code, 201)
        rt_img_id = rt_resp.json()["image"]["id"]
        rt_img_uuid = rt_resp.json()["image"]["asset_id"]

        doc_a = CustomDocument.objects.create(
            title="随文压缩包",
            file=SimpleUploadedFile("pack.zip", b"PK\x03\x04demo", content_type="application/zip"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        res_doc = self.client.get(
            reverse("content_blocks_media_resolve"),
            {"kind": "document", "id": str(doc_a.pk)},
        ).json()

        guide = GuidePage(
            title="Unified Upload Roundtrip Guide",
            slug=f"uurg-{uuid4().hex[:6]}",
            owner=self.author_a,
            topic=self.topic,
            authored_on=date(2026, 11, 25),
            body=[
                (
                    "step",
                    {
                        "step_number": "1",
                        "title": "粘贴图片步骤",
                        "content": RichText(
                            f'<p><embed embedtype="image" id="{rt_img_id}" format="left" alt="步骤粘贴图"/></p>'
                        ),
                    },
                ),
                (
                    "markdown",
                    f"下载资料：{res_doc['markdown']}",
                ),
            ],
        )
        self.guide_index.add_child(instance=guide)
        GuidePageAuthor.objects.create(page=guide, person=self.person_a, role="primary_author")
        rev = guide.save_revision(user=self.author_a)

        # 草稿阶段即可登记 MediaReferenceRecord 且受删除保护
        self.assertTrue(
            MediaReferenceRecord.objects.filter(
                page_id=guide.id, revision_id=rev.id, asset_id=rt_img_uuid
            ).exists()
        )
        self.assertTrue(
            MediaReferenceRecord.objects.filter(
                page_id=guide.id, revision_id=rev.id, asset_id=doc_a.public_id
            ).exists()
        )
        rt_img = CustomImage.objects.get(pk=rt_img_id)
        with self.assertRaises(PermissionDenied), transaction.atomic():
            rt_img.delete()
        with self.assertRaises(PermissionDenied), transaction.atomic():
            doc_a.delete()

        # 正式发布后自动将本人受限图片与受限文档转为 PUBLIC
        rev.publish(user=self.author_a)
        rt_img.refresh_from_db()
        doc_a.refresh_from_db()
        self.assertEqual(rt_img.visibility, VisibilityChoices.PUBLIC)
        self.assertEqual(doc_a.visibility, VisibilityChoices.PUBLIC)
