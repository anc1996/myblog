"""
Phase 1 媒体生命周期与结构化卡片富文本回归测试（零迁移）：
1. CustomLeadBlock / CustomStepBlock / CustomTipBlock / CustomFaqBlock 内嵌图片、文档、受控媒体 embed 的渲染、引用追踪与纯文本提取；
2. Revision 保存后 MediaReferenceRecord 自动登记与 pre_delete 删除保护（活动页、历史版本、回收站均禁删）；
3. 正式发布仅公开 live_revision_id 中属于页面 owner 的受限媒体，不提前公开最新草稿里的新媒体；
4. 公开页引用不会自动转正的受限媒体（含超管代发作者公开页场景）被 validate_for_publish 阻断，受访问限制页面允许保留受限态；
5. CustomRendition 网关交付与缓存头收敛（public, max-age=300, must-revalidate vs private, max-age=300）。
"""

from datetime import date
from io import BytesIO
import os
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django

django.setup()

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client, TestCase
from PIL import Image as PILImage
from wagtail.models import Page, PageViewRestriction
from wagtail.rich_text import RichText

from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from content_blocks.blocks.structured import (
    CustomFaqBlock,
    CustomLeadBlock,
    CustomStepBlock,
    CustomTipBlock,
)
from content_lifecycle.permissions import ensure_author_role_infrastructure
from content_lifecycle.recycle import RecycleService
from guides.models import GuideIndexPage, GuidePage, GuidePageAuthor, GuideTopic
from home.models import HomePage
from identity.models import User
from media_library.access import restrict_asset
from media_library.models import (
    CustomDocument,
    CustomImage,
    MediaReferenceRecord,
    VisibilityChoices,
)
from people.models import Person


def _make_png(name: str = "p1.png") -> SimpleUploadedFile:
    buf = BytesIO()
    PILImage.new("RGB", (24, 24), color=(30, 144, 255)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def _make_doc(name: str = "p1.txt", body: bytes = b"phase1-doc") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, body, content_type="text/plain")


class Phase1MediaLifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.infra = ensure_author_role_infrastructure()
        cls.authors_group = cls.infra["authors_group"]
        cls.workspace_col = cls.infra["workspace_collection"]
        cls.public_col = cls.infra["public_collection"]
        cls.home = HomePage.objects.first()
        if cls.home is None:
            root = Page.get_first_root_node()
            cls.home = HomePage(title="Phase1 Home", slug=f"p1-home-{uuid4().hex[:6]}")
            root.add_child(instance=cls.home)

        cls.blog_index = BlogIndexPage(title="Phase1 Blog", slug=f"p1-blog-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.blog_index)
        cls.blog_index.save_revision().publish()

        cls.guide_index = GuideIndexPage(title="Phase1 Guide", slug=f"p1-guide-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.guide_index)
        cls.guide_index.save_revision().publish()

        cls.topic = GuideTopic.objects.create(
            name=f"Phase1Topic-{uuid4().hex[:6]}",
            slug=f"p1-topic-{uuid4().hex[:6]}",
        )

    def setUp(self):
        suffix = uuid4().hex[:6]
        self.superuser = User.objects.create_superuser(
            username=f"p1_su_{suffix}",
            email=f"p1_su_{suffix}@example.com",
            password="Password123!",
        )
        self.author_a = User.objects.create_user(
            username=f"p1_a_{suffix}",
            password="Password123!",
            is_active=True,
        )
        self.author_b = User.objects.create_user(
            username=f"p1_b_{suffix}",
            password="Password123!",
            is_active=True,
        )
        self.author_a.groups.add(self.authors_group)
        self.author_b.groups.add(self.authors_group)
        self.person_a = Person.objects.create(name=f"P1作者A_{suffix}", linked_user=self.author_a)
        self.person_b = Person.objects.create(name=f"P1作者B_{suffix}", linked_user=self.author_b)

    def test_01_structured_cards_nested_richtext_render_refs_and_text(self):
        img_lead_pre = CustomImage.objects.create(
            title="Lead Pre Img", file=_make_png("lp.png"), collection=self.public_col, visibility=VisibilityChoices.PUBLIC
        )
        doc_lead_ov = CustomDocument.objects.create(
            title="Lead Ov Doc", file=_make_doc("lo.txt"), collection=self.public_col, visibility=VisibilityChoices.PUBLIC
        )
        img_step_inline = CustomImage.objects.create(
            title="Step Inline Img", file=_make_png("si.png"), collection=self.public_col, visibility=VisibilityChoices.PUBLIC
        )
        img_step_main = CustomImage.objects.create(
            title="Step Main Img", file=_make_png("sm.png"), collection=self.public_col, visibility=VisibilityChoices.PUBLIC
        )
        doc_tip = CustomDocument.objects.create(
            title="Tip Doc", file=_make_doc("tip.txt"), collection=self.public_col, visibility=VisibilityChoices.PUBLIC
        )
        img_faq = CustomImage.objects.create(
            title="Faq Img", file=_make_png("faq.png"), collection=self.public_col, visibility=VisibilityChoices.PUBLIC
        )

        bili_url = "https://www.bilibili.com/video/BV1xx411c7mD"

        lead_block = CustomLeadBlock()
        lead_val = {
            "title": "导读标题",
            "prerequisites": RichText(
                f'<p>前置条件<embed embedtype="image" id="{img_lead_pre.id}" format="fullwidth" alt="pre"/></p>'
                f'<embed embedtype="media" url="{bili_url}" title="导读视频"/>'
                '<script>alert(1)</script>'
            ),
            "overview": RichText(
                f'<p>概要说明<a linktype="document" id="{doc_lead_ov.id}">下载清单</a></p>'
            ),
        }
        lead_res = lead_block.render_clean(lead_val)
        self.assertTrue(lead_res.is_safe)
        self.assertNotIn("<script>", lead_res.html)
        self.assertIn("/media-gateway/rendition/", lead_res.html)
        self.assertIn('alt="pre"', lead_res.html)
        self.assertIn(f"/media-gateway/document/{doc_lead_ov.public_id}/", lead_res.html)
        self.assertIn(f'data-embed-url="{bili_url}"', lead_res.html)
        self.assertIn('data-embed-title="导读视频"', lead_res.html)
        lead_refs = lead_block.extract_media_refs(lead_val, path="body.0")
        self.assertEqual(
            [(r.asset_id, r.asset_type, r.block_path) for r in lead_refs],
            [
                (str(img_lead_pre.public_id), "image", "body.0.prerequisites"),
                (str(doc_lead_ov.public_id), "document", "body.0.overview"),
            ],
        )
        lead_text = lead_block.extract_text(lead_val).plain_text
        self.assertIn("导读标题", lead_text)
        self.assertIn("前置条件", lead_text)
        self.assertIn("概要说明", lead_text)

        step_block = CustomStepBlock()
        step_val = {
            "step_number": "1",
            "title": "步骤一",
            "content": RichText(
                f'<p>步骤正文<embed embedtype="image" id="{img_step_inline.id}" format="fullwidth" alt="step-in"/></p>'
                f'<embed embedtype="media" url="{bili_url}" title="步骤视频"/>'
            ),
            "image": img_step_main,
            "image_caption": "步骤独立配图",
        }
        step_res = step_block.render_clean(step_val)
        self.assertIn('alt="step-in"', step_res.html)
        self.assertIn("/media-gateway/rendition/", step_res.html)
        self.assertIn(f"/media-gateway/image/{img_step_main.public_id}/", step_res.html)
        self.assertIn('data-embed-title="步骤视频"', step_res.html)
        step_refs = step_block.extract_media_refs(step_val, path="body.1")
        self.assertEqual(
            [(r.asset_id, r.asset_type, r.block_path) for r in step_refs],
            [
                (str(img_step_inline.public_id), "image", "body.1.content"),
                (str(img_step_main.public_id), "image", "body.1.image"),
            ],
        )
        step_text = step_block.extract_text(step_val).plain_text
        self.assertIn("步骤一", step_text)
        self.assertIn("步骤正文", step_text)
        self.assertIn("步骤独立配图", step_text)

        tip_block = CustomTipBlock()
        tip_val = {
            "tip_type": "warning",
            "title": "注意",
            "content": RichText(
                f'<p>提示文档<a linktype="document" id="{doc_tip.id}">查看文档</a></p>'
                f'<embed embedtype="media" url="{bili_url}" title="提示视频"/>'
            ),
        }
        tip_res = tip_block.render_clean(tip_val)
        self.assertIn(f"/media-gateway/document/{doc_tip.public_id}/", tip_res.html)
        self.assertIn('data-embed-title="提示视频"', tip_res.html)
        tip_refs = tip_block.extract_media_refs(tip_val, path="body.2")
        self.assertEqual(
            [(r.asset_id, r.asset_type, r.block_path) for r in tip_refs],
            [(str(doc_tip.public_id), "document", "body.2.content")],
        )
        self.assertIn("提示文档", tip_block.extract_text(tip_val).plain_text)

        faq_block = CustomFaqBlock()
        faq_val = {
            "question": "如何排错？",
            "answer": RichText(
                f'<p>参考图<embed embedtype="image" id="{img_faq.id}" format="fullwidth" alt="faq"/></p>'
                f'<embed embedtype="media" url="{bili_url}" title="排错视频"/>'
            ),
        }
        faq_res = faq_block.render_clean(faq_val)
        self.assertIn('alt="faq"', faq_res.html)
        self.assertIn("/media-gateway/rendition/", faq_res.html)
        self.assertIn('data-embed-title="排错视频"', faq_res.html)
        faq_refs = faq_block.extract_media_refs(faq_val, path="body.3")
        self.assertEqual(
            [(r.asset_id, r.asset_type, r.block_path) for r in faq_refs],
            [(str(img_faq.public_id), "image", "body.3.answer")],
        )
        faq_text = faq_block.extract_text(faq_val).plain_text
        self.assertIn("如何排错", faq_text)
        self.assertIn("参考图", faq_text)

    def test_02_revision_registers_nested_card_refs_and_protects_across_history_and_recycle(self):
        img_hist = CustomImage.objects.create(
            title="Hist Card Img",
            file=_make_png("hist.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        doc_live = CustomDocument.objects.create(
            title="Live Card Doc",
            file=_make_doc("live.txt"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )

        guide = GuidePage(
            title="Nested Ref Guide",
            slug=f"nrg-{uuid4().hex[:6]}",
            owner=self.author_a,
            topic=self.topic,
            authored_on=date(2026, 11, 25),
            body=[
                (
                    "lead",
                    {
                        "title": "导读",
                        "prerequisites": RichText(f'<p><embed embedtype="image" id="{img_hist.id}" format="fullwidth"/></p>'),
                        "overview": RichText("<p>概述</p>"),
                    },
                ),
                (
                    "step",
                    {
                        "step_number": "1",
                        "title": "步骤1",
                        "content": RichText(f'<p><a linktype="document" id="{doc_live.id}">附件</a></p>'),
                    },
                ),
            ],
        )
        self.guide_index.add_child(instance=guide)
        GuidePageAuthor.objects.create(page=guide, person=self.person_a, role="primary_author")
        rev1 = guide.save_revision(user=self.author_a)
        rev1.publish(user=self.author_a)

        self.assertTrue(
            MediaReferenceRecord.objects.filter(
                page_id=guide.id, revision_id=rev1.id, asset_id=img_hist.public_id, block_path="body.0.prerequisites"
            ).exists()
        )
        self.assertTrue(
            MediaReferenceRecord.objects.filter(
                page_id=guide.id, revision_id=rev1.id, asset_id=doc_live.public_id, block_path="body.1.content"
            ).exists()
        )

        # 活动页面引用时禁删
        with self.assertRaises(PermissionDenied), transaction.atomic():
            img_hist.delete()
        with self.assertRaises(PermissionDenied), transaction.atomic():
            doc_live.delete()

        # 改为新版本移除 img_hist，但历史版本 rev1 仍引用 img_hist -> 历史版本仍禁删
        guide.body = [
            (
                "step",
                {
                    "step_number": "1",
                    "title": "步骤1-更新",
                    "content": RichText(f'<p><a linktype="document" id="{doc_live.id}">附件</a></p>'),
                },
            ),
        ]
        rev2 = guide.save_revision(user=self.author_a)
        rev2.publish(user=self.author_a)
        with self.assertRaises(PermissionDenied), transaction.atomic():
            img_hist.delete()

        # 移入回收站后，img_hist 与 doc_live 依然均禁删
        batch = RecycleService.recycle_page_or_subtree(guide, actor=self.author_a)
        with self.assertRaises(PermissionDenied), transaction.atomic():
            img_hist.delete()
        with self.assertRaises(PermissionDenied), transaction.atomic():
            doc_live.delete()

        # 彻底物理销毁页面后，引用清零才允许删除
        RecycleService.purge_page_or_subtree(guide.id, actor=self.superuser, batch_id=batch.batch_id)
        img_hist.delete()
        doc_live.delete()

    def test_03_publish_only_promotes_live_revision_owner_media_not_newer_draft(self):
        img_rev1 = CustomImage.objects.create(
            title="Rev1 Owner Img",
            file=_make_png("r1.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        doc_rev1 = CustomDocument.objects.create(
            title="Rev1 Owner Doc",
            file=_make_doc("r1.txt"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        img_draft2 = CustomImage.objects.create(
            title="Draft2 New Img",
            file=_make_png("d2.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )

        guide = GuidePage(
            title="Revision Scope Promote Guide",
            slug=f"rspg-{uuid4().hex[:6]}",
            owner=self.author_a,
            topic=self.topic,
            authored_on=date(2026, 11, 25),
            body=[
                (
                    "step",
                    {
                        "step_number": "1",
                        "title": "Step 1",
                        "content": RichText(
                            f'<p><embed embedtype="image" id="{img_rev1.id}" format="fullwidth"/>'
                            f'<a linktype="document" id="{doc_rev1.id}">d</a></p>'
                        ),
                    },
                )
            ],
        )
        self.guide_index.add_child(instance=guide)
        GuidePageAuthor.objects.create(page=guide, person=self.person_a, role="primary_author")
        rev1 = guide.save_revision(user=self.author_a)

        # 在发布 rev1 前先保存一份更新的草稿 rev2（包含 img_draft2）
        guide.body = [
            (
                "step",
                {
                    "step_number": "1",
                    "title": "Step 1",
                    "content": RichText(f'<p><embed embedtype="image" id="{img_draft2.id}" format="fullwidth"/></p>'),
                },
            )
        ]
        guide.save_revision(user=self.author_a)

        # 仅批准发布 rev1：应只公开 rev1 的图/文档，绝不提前公开草稿 rev2 的 img_draft2
        rev1.publish(user=self.author_a)
        img_rev1.refresh_from_db()
        doc_rev1.refresh_from_db()
        img_draft2.refresh_from_db()
        self.assertEqual(img_rev1.visibility, VisibilityChoices.PUBLIC)
        self.assertEqual(doc_rev1.visibility, VisibilityChoices.PUBLIC)
        self.assertEqual(img_draft2.visibility, VisibilityChoices.RESTRICTED)

    def test_04_public_page_gate_blocks_non_promotable_restricted_media_while_restricted_page_allows(self):
        img_other = CustomImage.objects.create(
            title="Author B Restricted Img",
            file=_make_png("b_img.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_b,
            visibility=VisibilityChoices.RESTRICTED,
        )
        doc_su = CustomDocument.objects.create(
            title="Superuser Restricted Doc",
            file=_make_doc("su_doc.txt"),
            collection=self.workspace_col,
            uploaded_by_user=self.superuser,
            visibility=VisibilityChoices.RESTRICTED,
        )

        # 1. 超管代发 author_a 的公开页：引用 superuser 或 author_b 上传的受限图/文档
        # 因为 owner_id=author_a != uploaded_by_user_id，发布后不会自动转正 -> 必须被 validate_for_publish 阻断
        blog_a = BlogPage(
            title="Author A Public Page",
            slug=f"pub-a-{uuid4().hex[:6]}",
            owner=self.author_a,
            authored_on=date(2026, 11, 25),
            body=[("image", {"image": img_other})],
        )
        self.blog_index.add_child(instance=blog_a)
        BlogPagePerson.objects.create(page=blog_a, person=self.person_a, role="primary_author")
        rev_bad_img = blog_a.save_revision(user=self.author_a)
        with self.assertRaises(ValidationError):
            rev_bad_img.publish(user=self.superuser)
        img_other.refresh_from_db()
        self.assertEqual(img_other.visibility, VisibilityChoices.RESTRICTED)

        blog_a.body = [("document", {"document": doc_su})]
        rev_bad_doc = blog_a.save_revision(user=self.author_a)
        with self.assertRaises(ValidationError):
            rev_bad_doc.publish(user=self.superuser)
        doc_su.refresh_from_db()
        self.assertEqual(doc_su.visibility, VisibilityChoices.RESTRICTED)

        # 2. 超管发自己 owner=superuser 的公开页，引用他人 (author_b) 的受限图 -> 不会自动转正，同样阻断
        blog_su = BlogPage(
            title="SU Public Page",
            slug=f"pub-su-{uuid4().hex[:6]}",
            owner=self.superuser,
            authored_on=date(2026, 11, 25),
            body=[("image", {"image": img_other})],
        )
        self.blog_index.add_child(instance=blog_su)
        rev_su_bad = blog_su.save_revision(user=self.superuser)
        with self.assertRaises(ValidationError):
            rev_su_bad.publish(user=self.superuser)

        # 3. 当页面存在 view_restrictions 时（受访问限制页面）：
        # 超管可发布且保持受限图不自动公开；即使引用本 owner 自己的受限图也不会被错误转正为 PUBLIC
        img_a_priv = CustomImage.objects.create(
            title="Author A Private Page Img",
            file=_make_png("a_priv.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        private_blog = BlogPage(
            title="Private Restricted Page",
            slug=f"priv-a-{uuid4().hex[:6]}",
            owner=self.author_a,
            authored_on=date(2026, 11, 25),
            body=[
                ("image", {"image": img_a_priv}),
                ("image", {"image": img_other}),
            ],
        )
        self.blog_index.add_child(instance=private_blog)
        BlogPagePerson.objects.create(page=private_blog, person=self.person_a, role="primary_author")
        PageViewRestriction.objects.create(
            page=private_blog,
            restriction_type=PageViewRestriction.LOGIN,
        )
        rev_priv = private_blog.save_revision(user=self.author_a)
        rev_priv.publish(user=self.superuser)
        private_blog.refresh_from_db()
        img_a_priv.refresh_from_db()
        img_other.refresh_from_db()
        self.assertTrue(private_blog.live)
        self.assertEqual(img_a_priv.visibility, VisibilityChoices.RESTRICTED)
        self.assertEqual(img_other.visibility, VisibilityChoices.RESTRICTED)

    def test_05_rendition_gateway_cache_headers_and_restrict_transition(self):
        img = CustomImage.objects.create(
            title="Rendition Cache Img",
            file=_make_png("rend.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.PUBLIC,
        )
        rendition = img.get_rendition("fill-16x16")
        self.assertTrue(rendition.url.startswith("/media-gateway/rendition/"))

        anon = Client()
        res_pub = anon.get(rendition.url)
        self.assertEqual(res_pub.status_code, 200)
        self.assertEqual(res_pub.headers.get("Cache-Control"), "public, max-age=300, must-revalidate")

        # 转为受限后：匿名 403，作者与超管 200 且缓存头为 private, max-age=300
        res_restrict = restrict_asset("image", img.public_id, actor=self.superuser)
        self.assertEqual(res_restrict["status"], "success")

        self.assertEqual(anon.get(rendition.url).status_code, 403)

        client_b = Client()
        client_b.force_login(self.author_b)
        self.assertEqual(client_b.get(rendition.url).status_code, 403)

        client_a = Client()
        client_a.force_login(self.author_a)
        res_a = client_a.get(rendition.url)
        self.assertEqual(res_a.status_code, 200)
        self.assertEqual(res_a.headers.get("Cache-Control"), "private, max-age=300")

        client_su = Client()
        client_su.force_login(self.superuser)
        res_su = client_su.get(rendition.url)
        self.assertEqual(res_su.status_code, 200)
        self.assertEqual(res_su.headers.get("Cache-Control"), "private, max-age=300")

    def test_06_validate_for_publish_uses_target_revision_not_stale_page_fields(self):
        """before_publish_page 传入旧 DB 实例 + 已清理的新 revision 时，不应被旧 DB 残留受限引用误拦截。"""
        img_other = CustomImage.objects.create(
            title="Stale DB Restricted Img",
            file=_make_png("stale.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_b,
            visibility=VisibilityChoices.RESTRICTED,
        )
        blog = BlogPage(
            title="Clean Rev Publish",
            slug=f"clean-rev-{uuid4().hex[:6]}",
            owner=self.author_a,
            authored_on=date(2026, 11, 25),
            body=[("image", {"image": img_other})],
        )
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person_a, role="primary_author")
        # 模拟页面 DB 行上仍残留他人受限图，但新保存的 revision 已移除该图
        blog_db_stale = BlogPage.objects.get(pk=blog.pk)

        blog.body = [("rich_text", RichText("<p>已清理受限图后的干净版本</p>"))]
        rev_clean = blog.save_revision(user=self.author_a)

        # 用旧 DB 实例 + 干净 rev_clean 校验，必须放行
        blog_db_stale.validate_for_publish(user=self.author_a, revision=rev_clean)
        rev_clean.publish(user=self.author_a)
        blog.refresh_from_db()
        self.assertTrue(blog.live)
        img_other.refresh_from_db()
        self.assertEqual(img_other.visibility, VisibilityChoices.RESTRICTED)

    def test_07_legacy_revision_without_media_reference_record_promotes_on_publish(self):
        """模拟尚未登记 MediaReferenceRecord 的历史 Revision 发布时，signals.py 自动补采并转正本人受限资产。"""
        img_owner = CustomImage.objects.create(
            title="Legacy Unregistered Owner Img",
            file=_make_png("leg_own.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        blog = BlogPage(
            title="Legacy Unindexed Rev",
            slug=f"leg-rev-{uuid4().hex[:6]}",
            owner=self.author_a,
            authored_on=date(2026, 11, 25),
            body=[("image", {"image": img_owner})],
        )
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person_a, role="primary_author")
        rev = blog.save_revision(user=self.author_a)

        # 人为清空 MediaReferenceRecord，模拟迁移前历史 Revision
        MediaReferenceRecord.objects.filter(page_id=blog.id, revision_id=rev.id).delete()
        self.assertFalse(MediaReferenceRecord.objects.filter(page_id=blog.id, revision_id=rev.id).exists())

        rev.publish(user=self.author_a)
        img_owner.refresh_from_db()
        self.assertEqual(img_owner.visibility, VisibilityChoices.PUBLIC)
        self.assertTrue(
            MediaReferenceRecord.objects.filter(
                page_id=blog.id, revision_id=rev.id, asset_id=img_owner.public_id
            ).exists()
        )

        # 部分漏记（表内已有 img_owner，但当年漏记嵌套卡片内的 doc_part）发布时必须增量补记并保护删除
        doc_part = CustomDocument.objects.create(
            title="Partial Missing Doc",
            file=_make_doc("part.txt"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_a,
            visibility=VisibilityChoices.RESTRICTED,
        )
        blog.body = [
            ("image", {"image": img_owner}),
            ("tip", {"tip_type": "tip", "content": RichText(f'<p><a linktype="document" id="{doc_part.id}">附件</a></p>')}),
        ]
        rev_part = blog.save_revision(user=self.author_a)
        MediaReferenceRecord.objects.filter(
            page_id=blog.id, revision_id=rev_part.id, asset_id=doc_part.public_id
        ).delete()
        rev_part.publish(user=self.author_a)
        doc_part.refresh_from_db()
        self.assertEqual(doc_part.visibility, VisibilityChoices.PUBLIC)
        self.assertTrue(
            MediaReferenceRecord.objects.filter(
                page_id=blog.id, revision_id=rev_part.id, asset_id=doc_part.public_id
            ).exists()
        )
        with self.assertRaises(PermissionDenied), transaction.atomic():
            doc_part.delete()

    def test_08_direct_gateway_urls_captured_no_title_leak_and_alias_closure(self):
        """直写 /media-gateway/ 链接被捕获；报错不泄露他人受限标题；存在公开别名时私密源页也阻断未转正资产。"""
        secret_title = f"绝密私有标题_{uuid4().hex[:8]}"
        img_secret = CustomImage.objects.create(
            title=secret_title,
            file=_make_png("sec.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_b,
            visibility=VisibilityChoices.RESTRICTED,
        )
        doc_secret = CustomDocument.objects.create(
            title=f"绝密文档_{uuid4().hex[:8]}",
            file=_make_doc("sec.txt"),
            collection=self.workspace_col,
            uploaded_by_user=self.author_b,
            visibility=VisibilityChoices.RESTRICTED,
        )

        # 1. Markdown 直写 /media-gateway/image/<uuid>/ 与 /media-gateway/document/<uuid>/ 必须被提取且阻断 + 报错不回显他人标题
        blog_direct = BlogPage(
            title="Direct Gateway Escaping Test",
            slug=f"gw-esc-{uuid4().hex[:6]}",
            owner=self.author_a,
            authored_on=date(2026, 11, 25),
            body=[
                (
                    "markdown",
                    f"![图](/media-gateway/image/{img_secret.public_id}/) [文](/media-gateway/document/{doc_secret.public_id}/)",
                )
            ],
        )
        self.blog_index.add_child(instance=blog_direct)
        BlogPagePerson.objects.create(page=blog_direct, person=self.person_a, role="primary_author")
        rev_dir = blog_direct.save_revision(user=self.author_a)
        self.assertTrue(
            MediaReferenceRecord.objects.filter(
                page_id=blog_direct.id, revision_id=rev_dir.id, asset_id=img_secret.public_id
            ).exists()
        )
        self.assertTrue(
            MediaReferenceRecord.objects.filter(
                page_id=blog_direct.id, revision_id=rev_dir.id, asset_id=doc_secret.public_id
            ).exists()
        )
        with self.assertRaises(ValidationError) as ctx:
            rev_dir.publish(user=self.author_a)
        err_text = " ".join(ctx.exception.messages)
        self.assertNotIn(secret_title, err_text)
        self.assertNotIn(doc_secret.title, err_text)

        # 1b. Draftail 富文本内直写 /media-gateway/document/<uuid>/ 同样必须被捕获阻断
        blog_rt_gw = BlogPage(
            title="RichText Gateway Escaping Test",
            slug=f"rt-gw-{uuid4().hex[:6]}",
            owner=self.author_a,
            authored_on=date(2026, 11, 25),
            body=[
                (
                    "rich_text",
                    RichText(f'<p><a href="/media-gateway/document/{doc_secret.public_id}/">下载</a></p>'),
                )
            ],
        )
        self.blog_index.add_child(instance=blog_rt_gw)
        BlogPagePerson.objects.create(page=blog_rt_gw, person=self.person_a, role="primary_author")
        rev_rt_gw = blog_rt_gw.save_revision(user=self.author_a)
        self.assertTrue(
            MediaReferenceRecord.objects.filter(
                page_id=blog_rt_gw.id, revision_id=rev_rt_gw.id, asset_id=doc_secret.public_id
            ).exists()
        )
        with self.assertRaises(ValidationError) as ctx_rt:
            rev_rt_gw.publish(user=self.author_a)
        self.assertNotIn(doc_secret.title, " ".join(ctx_rt.exception.messages))

        # 2. 祖先栏目存在 PageViewRestriction 时，子页面继承访问限制（无公开别名时允许超管发受限图且不转正）；
        #    一旦在公开父栏目下存在无限制 Alias 别名，Wagtail 别名不继承源页祖先限制 -> 必须阻断发布！
        restricted_index = BlogIndexPage(title="Restricted Index", slug=f"rest-idx-{uuid4().hex[:6]}")
        self.home.add_child(instance=restricted_index)
        restricted_index.save_revision().publish()
        PageViewRestriction.objects.create(page=restricted_index, restriction_type=PageViewRestriction.LOGIN)

        child_private = BlogPage(
            title="Inherited Private Child",
            slug=f"inh-priv-{uuid4().hex[:6]}",
            owner=self.author_a,
            authored_on=date(2026, 11, 25),
            body=[("image", {"image": img_secret})],
        )
        restricted_index.add_child(instance=child_private)
        BlogPagePerson.objects.create(page=child_private, person=self.person_a, role="primary_author")
        self.assertTrue(child_private.get_view_restrictions().exists())
        rev_priv = child_private.save_revision(user=self.author_a)
        rev_priv.publish(user=self.superuser)
        img_secret.refresh_from_db()
        self.assertEqual(img_secret.visibility, VisibilityChoices.RESTRICTED)

        alias_under_public = child_private.create_alias(
            parent=self.blog_index,
            update_slug=f"alias-inh-{uuid4().hex[:6]}",
        )
        self.assertFalse(alias_under_public.get_view_restrictions().exists())
        rev_priv2 = child_private.save_revision(user=self.author_a)
        with self.assertRaises(ValidationError):
            rev_priv2.publish(user=self.superuser)

        # 3. 当最新草稿含他人受限图、而目标发布的是历史干净 Revision 时，不得被误杀
        clean_blog = BlogPage(
            title="Clean Old Rev Test",
            slug=f"clean-old-{uuid4().hex[:6]}",
            owner=self.author_a,
            authored_on=date(2026, 11, 25),
            body=[("rich_text", RichText("<p>干净的历史版本</p>"))],
        )
        self.blog_index.add_child(instance=clean_blog)
        BlogPagePerson.objects.create(page=clean_blog, person=self.person_a, role="primary_author")
        rev_clean = clean_blog.save_revision(user=self.author_a)
        clean_blog.body = [("image", {"image": img_secret})]
        clean_blog.save_revision(user=self.author_a)
        rev_clean.publish(user=self.author_a)
        clean_blog.refresh_from_db()
        self.assertEqual(clean_blog.live_revision_id, rev_clean.id)
