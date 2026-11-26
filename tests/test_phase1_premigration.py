"""
Phase 1 生产迁移前置改造全量回归测试：
1. C-04=B: CustomImage.caption 与 CustomDocument.description 可选元数据与受保护表单；
2. A-02=A: BlogIndexPage 封面图与 RichTextField intro（迁移清洗、引用提取、自动公开、适配器纯文本、前台渲染）；
3. D-06 样式 A: 搜索/列表/标签/作者/首页左侧封面缩略图（无图不留白）+ 详情页标题背景横幅图；
4. D-03: HTML lang="zh-Hans"、seo_title、canonical、Open Graph、Article JSON-LD；
5. D-01 & D-02: /zh-hans/、/archive/、/feed/rss/、/tags/?tag=、/blog/authors/<pk>/ 单跳 301 重定向。
"""

import importlib
import io
import json
import re
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image as PilImage
from wagtail.documents.forms import get_document_form
from wagtail.images.forms import get_image_form
from wagtail.models import Page, Site

from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from content_blocks.blocks.media import CustomDocumentBlock, CustomImageBlock
from discovery.projection import project_page_to_discovery
from guides.models import GuideIndexPage, GuidePage, GuideTopic
from home.models import HomePage
from media_library.models import (
    CustomDocument,
    CustomImage,
    MediaReferenceRecord,
    VisibilityChoices,
)
from people.models import Person
from portfolio.models import PortfolioIndexPage, ProjectPage
from taxonomy.models import TagIndexPage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _make_png_file(name: str = "cover.png", color=(30, 80, 160)) -> SimpleUploadedFile:
    buf = io.BytesIO()
    PilImage.new("RGB", (64, 48), color=color).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def _make_doc_file(name: str = "notes.md", content: bytes = b"# Hello\n") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="text/markdown")


def _ensure_site_and_home():
    home = HomePage.objects.first()
    if not home:
        root = Page.get_first_root_node() or Page.add_root(title="Root")
        home = HomePage(title="Home", slug=f"home-{uuid4().hex[:6]}")
        root.add_child(instance=home)
        home.save_revision().publish()
    site = Site.objects.filter(is_default_site=True).first()
    if not site:
        site = Site.objects.create(hostname="localhost", port=80, root_page=home, is_default_site=True)
    elif site.root_page_id != home.id:
        site.root_page = home
        site.save()
    return site, home


class Phase1MediaMetadataTests(TestCase):
    """C-04=B: 媒体库可选 caption / description 扩展与受保护表单验证"""

    def test_optional_caption_and_description_fields_and_forms(self):
        User = get_user_model()
        user = User.objects.create_superuser(username=f"adm_{uuid4().hex[:6]}", email="a@ex.com", password="Pass12345!")

        self.assertIn("caption", CustomImage.admin_form_fields)
        self.assertIn("description", CustomDocument.admin_form_fields)

        img = CustomImage.objects.create(
            title="示例图片",
            file=_make_png_file("c04.png"),
            uploaded_by_user=user,
            caption="库级图注",
            description="库级描述",
        )
        doc = CustomDocument.objects.create(
            title="示例文档",
            file=_make_doc_file("c04.md"),
            uploaded_by_user=user,
            description="库级文档说明",
        )
        self.assertEqual(img.caption, "库级图注")
        self.assertEqual(doc.description, "库级文档说明")

        # 无引用时允许只传 title 留空 caption/description
        ImageFormCls = get_image_form(CustomImage)
        img_form = ImageFormCls(
            data={"title": "更新标题", "collection": img.collection_id, "caption": "新图注"},
            instance=img,
        )
        self.assertTrue(img_form.is_valid(), img_form.errors)
        saved_img = img_form.save()
        self.assertEqual(saved_img.caption, "新图注")

        DocFormCls = get_document_form(CustomDocument)
        doc_form = DocFormCls(
            data={"title": "更新文档", "collection": doc.collection_id, "description": "新文档说明"},
            instance=doc,
        )
        self.assertTrue(doc_form.is_valid(), doc_form.errors)
        saved_doc = doc_form.save()
        self.assertEqual(saved_doc.description, "新文档说明")

        # 正文块保持使用块级字段，不回退读取库级 metadata
        img_html = CustomImageBlock().render_clean({"image": img, "caption": ""}).html
        self.assertNotIn("库级图注", img_html)
        doc_html = CustomDocumentBlock().render_clean({"document": doc, "description": ""}).html
        self.assertNotIn("库级文档说明", doc_html)


class Phase1BlogIndexUpgradeTests(TestCase):
    """A-02=A: BlogIndexPage 封面图、RichTextField intro、引用采集与数据迁移清洗"""

    def test_migration_0006_plain_text_conversion_is_safe_and_idempotent(self):
        mig = importlib.import_module("blog.migrations.0006_blogindexpage_featured_image_and_more")
        converted = mig._plain_text_to_richtext_html("第一行<script>alert(1)</script>\n第二行\n\n第二段")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", converted)
        self.assertIn("<br/>", converted)
        self.assertEqual(converted.count("<p>"), 2)
        already = "<p>已有富文本</p>"
        self.assertEqual(mig._plain_text_to_richtext_html(already), already)

    def test_blog_index_featured_image_and_richtext_intro_lifecycle_and_render(self):
        User = get_user_model()
        owner = User.objects.create_superuser(username=f"own_{uuid4().hex[:6]}", email="o@ex.com", password="Pass12345!")
        other = User.objects.create_user(username=f"oth_{uuid4().hex[:6]}", password="Pass12345!")
        _, home = _ensure_site_and_home()

        cover = CustomImage.objects.create(
            title="栏目封面",
            file=_make_png_file("col_cover.png"),
            uploaded_by_user=owner,
            visibility=VisibilityChoices.RESTRICTED,
        )
        intro_img = CustomImage.objects.create(
            title="简介插图",
            file=_make_png_file("intro_img.png"),
            uploaded_by_user=owner,
            visibility=VisibilityChoices.RESTRICTED,
        )
        other_restricted = CustomImage.objects.create(
            title="他人受限图",
            file=_make_png_file("other.png"),
            uploaded_by_user=other,
            visibility=VisibilityChoices.RESTRICTED,
        )

        col = BlogIndexPage(
            title="系统架构专栏",
            slug=f"arch-col-{uuid4().hex[:6]}",
            owner=owner,
            featured_image=cover,
            intro=f'<p>欢迎来到<strong>架构专栏</strong></p><embed embedtype="image" format="left" id="{intro_img.id}" alt="插图"/>',
        )
        home.add_child(instance=col)
        rev = col.save_revision(user=owner)

        refs = {(r.asset_type, str(r.asset_id)) for r in col.collect_media_references()}
        self.assertIn(("image", str(cover.public_id)), refs)
        self.assertIn(("image", str(intro_img.public_id)), refs)
        self.assertTrue(
            MediaReferenceRecord.objects.filter(page_id=col.id, revision_id=rev.id, asset_id=cover.public_id).exists()
        )
        self.assertTrue(
            MediaReferenceRecord.objects.filter(page_id=col.id, revision_id=rev.id, asset_id=intro_img.public_id).exists()
        )

        rev.publish(user=owner)
        cover.refresh_from_db()
        intro_img.refresh_from_db()
        self.assertEqual(cover.visibility, VisibilityChoices.PUBLIC)
        self.assertEqual(intro_img.visibility, VisibilityChoices.PUBLIC)

        # 适配器必须产出纯文本摘要与 cover_public_id
        doc = project_page_to_discovery(col)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.extra_data.get("cover_public_id"), str(cover.public_id))
        self.assertNotIn("<p>", doc.summary)
        self.assertNotIn("<embed", doc.summary)
        self.assertIn("欢迎来到 架构专栏", doc.summary)

        # 前台栏目页展示封面、净化后的富文本，且首页栏目摘要不带 HTML 标签
        resp = self.client.get(col.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn(f"/media-gateway/image/{cover.public_id}/", html)
        self.assertIn("<strong>架构专栏</strong>", html)
        self.assertIn("listing-page-cover", html)

        home_resp = self.client.get(home.url)
        self.assertEqual(home_resp.status_code, 200)
        home_html = home_resp.content.decode("utf-8")
        self.assertNotIn("&lt;p&gt;", home_html)
        self.assertNotIn("<embed", home_html)

        # 引用他人受限图片应拦截发布
        col.featured_image = other_restricted
        bad_rev = col.save_revision(user=owner)
        with self.assertRaises(ValidationError):
            col.validate_for_publish(user=owner, revision=bad_rev)


class Phase1CoverUiSeoAndRedirectsTests(TestCase):
    """D-06 样式 A + D-03 SEO + D-01/D-02 301 重定向"""

    def test_left_cover_cards_detail_hero_banner_and_seo_tags(self):
        User = get_user_model()
        owner = User.objects.create_superuser(username=f"seo_{uuid4().hex[:6]}", email="s@ex.com", password="Pass12345!")
        _, home = _ensure_site_and_home()

        tag_index = TagIndexPage.objects.first()
        if not tag_index:
            tag_index = TagIndexPage(title="全站标签", slug="tags")
            home.add_child(instance=tag_index)
            tag_index.save_revision().publish()

        person = Person.objects.create(name="测试作者", slug=f"tester-{uuid4().hex[:6]}")
        cover = CustomImage.objects.create(
            title="文章封面",
            file=_make_png_file("post_cover.png"),
            uploaded_by_user=owner,
            visibility=VisibilityChoices.PUBLIC,
        )

        col = BlogIndexPage(title="技术", slug=f"tech-{uuid4().hex[:6]}")
        home.add_child(instance=col)
        col.save_revision().publish()

        with_cover = BlogPage(
            title="带封面文章",
            slug=f"with-cover-{uuid4().hex[:6]}",
            seo_title="定制SEO标题",
            search_description="用于SEO的页面描述",
            featured_image=cover,
            body=[("markdown", {"markdown": "## 第一节\n\n正文内容"})],
        )
        with_cover.tags.add("PostgreSQL")
        col.add_child(instance=with_cover)
        BlogPagePerson.objects.create(page=with_cover, person=person, role="primary_author")
        with_cover.save_revision(user=owner).publish(user=owner)

        no_cover = BlogPage(
            title="纯文字文章",
            slug=f"no-cover-{uuid4().hex[:6]}",
            body=[("markdown", {"markdown": "纯文字无封面"})],
        )
        no_cover.tags.add("PostgreSQL")
        col.add_child(instance=no_cover)
        no_cover.save_revision(user=owner).publish(user=owner)

        # 同步验证 GuidePage 与 ProjectPage 封面也能进入发现卡片
        g_idx = GuideIndexPage(title="指南", slug=f"guides-{uuid4().hex[:6]}")
        home.add_child(instance=g_idx)
        g_idx.save_revision().publish()
        topic = GuideTopic.objects.create(name="数据库", slug=f"db-{uuid4().hex[:6]}")
        guide = GuidePage(
            title="带封面指南",
            slug=f"guide-cov-{uuid4().hex[:6]}",
            topic=topic,
            featured_image=cover,
            body=[("step", {"step_number": "1", "title": "步骤1", "content": "<p>ok</p>"})],
        )
        g_idx.add_child(instance=guide)
        guide.save_revision(user=owner).publish(user=owner)

        p_idx = PortfolioIndexPage(title="项目", slug=f"port-{uuid4().hex[:6]}")
        home.add_child(instance=p_idx)
        p_idx.save_revision().publish()
        proj = ProjectPage(
            title="带封面项目",
            slug=f"proj-cov-{uuid4().hex[:6]}",
            featured_image=cover,
            body=[("markdown", {"markdown": "项目内容"})],
        )
        p_idx.add_child(instance=proj)
        proj.save_revision(user=owner).publish(user=owner)

        # 1. 详情页：样式 A 标题背景横幅图 + SEO 标签
        detail_resp = self.client.get(with_cover.url)
        self.assertEqual(detail_resp.status_code, 200)
        d_html = detail_resp.content.decode("utf-8")
        self.assertIn('<html lang="zh-Hans"', d_html)
        self.assertIn("<title>", d_html)
        self.assertIn("定制SEO标题", d_html)
        self.assertIn('<link rel="canonical"', d_html)
        self.assertIn('property="og:image"', d_html)
        self.assertIn("用于SEO的页面描述", d_html)
        self.assertIn("detail-hero-banner", d_html)
        self.assertIn("detail-hero-bg", d_html)
        ld_match = re.search(r'<script type="application/ld\+json">(.*?)</script>', d_html, re.S)
        self.assertIsNotNone(ld_match)
        ld_data = json.loads(ld_match.group(1))
        self.assertEqual(ld_data["@type"], "Article")
        self.assertEqual(ld_data["headline"], "带封面文章")

        # 无封面文章不输出横幅图，保持纯文字头部
        plain_resp = self.client.get(no_cover.url)
        self.assertEqual(plain_resp.status_code, 200)
        p_html = plain_resp.content.decode("utf-8")
        self.assertIn("detail-header-plain", p_html)
        self.assertNotIn("detail-hero-banner", p_html)

        # 2. 栏目列表页 / 搜索页 / 标签页 / 作者页 / 首页：左侧缩略图（仅限有封面条目）
        for route in (
            col.url,
            "/search/",
            f"{tag_index.url}postgresql/",
            f"/authors/{person.slug}/",
            home.url,
        ):
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 200, route)
            html = resp.content.decode("utf-8")
            self.assertIn(f"/media-gateway/image/{cover.public_id}/", html, route)

        # 无封面搜索时不渲染 media 容器
        no_cov_search = self.client.get("/search/?query=纯文字无封面")
        self.assertEqual(no_cov_search.status_code, 200)
        self.assertNotIn("search-result-media", no_cov_search.content.decode("utf-8"))

        # 3. 301 重定向验证 (D-01 / D-02)
        r1 = self.client.get("/zh-hans/")
        self.assertEqual(r1.status_code, 301)
        self.assertEqual(r1["Location"], "/")

        r2 = self.client.get("/zh-hans/search/?query=abc")
        self.assertEqual(r2.status_code, 301)
        self.assertEqual(r2["Location"], "/search/?query=abc")

        # 防开放重定向：/zh-hans//evil.com 不得跳外链
        r_open = self.client.get("/zh-hans//evil.com")
        self.assertEqual(r_open.status_code, 301)
        self.assertEqual(r_open["Location"], "/evil.com")

        r3 = self.client.get("/zh-hans/archive/?year=2025")
        self.assertEqual(r3.status_code, 301)
        self.assertEqual(r3["Location"], "/timeline/?year=2025")

        r4 = self.client.get("/archive/?year=2025")
        self.assertEqual(r4.status_code, 301)
        self.assertEqual(r4["Location"], "/timeline/?year=2025")

        r5 = self.client.get("/feed/rss/")
        self.assertEqual(r5.status_code, 301)
        self.assertEqual(r5["Location"], "/feeds/")

        r6 = self.client.get(f"{tag_index.url}?tag=PostgreSQL")
        self.assertEqual(r6.status_code, 301)
        self.assertEqual(r6["Location"], f"{tag_index.url}postgresql/")

        r7 = self.client.get(f"/blog/authors/{person.id}/")
        self.assertEqual(r7.status_code, 301)
        self.assertEqual(r7["Location"], f"/authors/{person.slug}/")

        r8 = self.client.get("/blog/authors/999999/")
        self.assertEqual(r8.status_code, 404)

    def test_modified_templates_and_css_respect_style_redlines(self):
        for rel_css in ("myblog/static/css/detail-layout.css", "myblog/static/css/myblog.css"):
            text = (PROJECT_ROOT / rel_css).read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"#[0-9a-fA-F]{3,8}\b", text),
                f"{rel_css} 不得写死裸 #hex 色值",
            )
        for rel_tpl in (
            "myblog/templates/base.html",
            "myblog/templates/detail/detail_shell.html",
            "myblog/templates/discovery/listing/_listing_card.html",
            "myblog/apps/search/templates/search/partials/_search_content.html",
            "myblog/apps/people/templates/people/partials/_author_results.html",
            "myblog/apps/blog/templates/blog/blog_index_page.html",
        ):
            text = (PROJECT_ROOT / rel_tpl).read_text(encoding="utf-8")
            self.assertNotIn(" style=", text, f"{rel_tpl} 不得新增内联 style=")
