"""
栏目页头部统一 3 预设模板 + 垂直焦点保护 + 长导读折叠 + 剩余 Emoji 清零回归测试。
"""

import io
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image as PilImage
from wagtail.models import Page, Site

from analytics.models import AnalyticsSettings, PageAnalyticsTotal
from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from guides.models import GuideIndexPage, GuideTopic
from home.models import HomePage
from media_library.models import CustomImage, VisibilityChoices
from people.models import Person
from portfolio.models import PortfolioIndexPage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _make_png_file(name: str = "header.png", size=(400, 300), color=(40, 90, 150)) -> SimpleUploadedFile:
    buf = io.BytesIO()
    PilImage.new("RGB", size, color=color).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


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


class ListingHeaderLayoutsTests(TestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.owner = User.objects.create_superuser(
            username=f"hdr_{uuid4().hex[:6]}",
            email="hdr@example.com",
            password="Pass12345!",
        )
        self.site, self.home = _ensure_site_and_home()

    def _make_image(self, title: str, focal_y: int | None = None, height: int = 300) -> CustomImage:
        kwargs = {}
        if focal_y is not None:
            kwargs = {
                "focal_point_x": 200,
                "focal_point_y": focal_y,
                "focal_point_width": 40,
                "focal_point_height": 40,
            }
        return CustomImage.objects.create(
            title=title,
            file=_make_png_file(f"{uuid4().hex[:6]}.png", size=(400, height)),
            uploaded_by_user=self.owner,
            visibility=VisibilityChoices.PUBLIC,
            **kwargs,
        )

    def test_index_models_inherit_default_header_layout_and_panel(self):
        """三类栏目模型均继承 header_layout 默认 editorial_split，且注册后台面板"""
        for model_cls in (BlogIndexPage, GuideIndexPage, PortfolioIndexPage):
            field = model_cls._meta.get_field("header_layout")
            self.assertEqual(field.default, "editorial_split")
            panel_fields = [getattr(p, "field_name", None) for p in model_cls.content_panels]
            self.assertIn("header_layout", panel_fields)

        b_idx = BlogIndexPage(title="默认博客栏目", slug=f"b-{uuid4().hex[:6]}")
        g_idx = GuideIndexPage(title="默认指南栏目", slug=f"g-{uuid4().hex[:6]}")
        p_idx = PortfolioIndexPage(title="默认作品栏目", slug=f"p-{uuid4().hex[:6]}")
        self.assertEqual(b_idx.header_layout, "editorial_split")
        self.assertEqual(g_idx.header_layout, "editorial_split")
        self.assertEqual(p_idx.header_layout, "editorial_split")

        # 非法枚举值必须安全回退为 editorial_split
        b_idx.header_layout = "invalid_preset"
        self.assertEqual(b_idx.get_header_presentation()["layout"], "editorial_split")

    def test_editorial_split_with_and_without_cover(self):
        """editorial_split 有图时渲染左右双栏与默认上偏焦点类，无图时不渲染空图框"""
        cover = self._make_image("人物封面")
        with_cover = BlogIndexPage(
            title="带图左文右图栏目",
            slug=f"split-cov-{uuid4().hex[:6]}",
            owner=self.owner,
            featured_image=cover,
            header_layout=BlogIndexPage.HEADER_LAYOUT_EDITORIAL_SPLIT,
            intro="<p>简短栏目导读。</p>",
        )
        self.home.add_child(instance=with_cover)
        with_cover.save_revision(user=self.owner).publish(user=self.owner)

        resp_cov = self.client.get(with_cover.url)
        self.assertEqual(resp_cov.status_code, 200)
        html_cov = resp_cov.content.decode("utf-8")
        self.assertIn("listing-header--editorial_split has-cover", html_cov)
        self.assertIn("listing-header-cover", html_cov)
        self.assertIn(f"/media-gateway/image/{cover.public_id}/", html_cov)
        self.assertIn("is-focal-upper", html_cov)
        self.assertNotIn(" style=", html_cov)

        no_cover = BlogIndexPage(
            title="无图左文右图栏目",
            slug=f"split-nocov-{uuid4().hex[:6]}",
            owner=self.owner,
            header_layout=BlogIndexPage.HEADER_LAYOUT_EDITORIAL_SPLIT,
            intro="<p>纯文本刊头。</p>",
        )
        self.home.add_child(instance=no_cover)
        no_cover.save_revision(user=self.owner).publish(user=self.owner)

        resp_nocov = self.client.get(no_cover.url)
        self.assertEqual(resp_nocov.status_code, 200)
        html_nocov = resp_nocov.content.decode("utf-8")
        self.assertIn("listing-header--editorial_split", html_nocov)
        self.assertNotIn("listing-header--editorial_split has-cover", html_nocov)
        self.assertNotIn("listing-header-cover", html_nocov)

    def test_compact_doc_hides_header_cover_even_when_featured_image_exists(self):
        """compact_doc 即使配置了封面图，也不在顶部渲染封面容器"""
        cover = self._make_image("公文封面")
        col = BlogIndexPage(
            title="公文精简文档栏目",
            slug=f"compact-{uuid4().hex[:6]}",
            owner=self.owner,
            featured_image=cover,
            header_layout=BlogIndexPage.HEADER_LAYOUT_COMPACT_DOC,
            intro="<p>精简文档导读。</p>",
        )
        self.home.add_child(instance=col)
        col.save_revision(user=self.owner).publish(user=self.owner)

        resp = self.client.get(col.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("listing-header--compact_doc", html)
        self.assertNotIn("listing-header-cover", html)
        self.assertNotIn("listing-page-cover", html)
        self.assertNotIn(" style=", html)

    def test_visual_banner_focal_point_mapping_without_inline_styles(self):
        """visual_banner 读取 featured_image 焦点框并映射正确焦点类，零内联 style="""
        cases = [
            (30, "is-focal-top"),
            (105, "is-focal-upper"),
            (150, "is-focal-center"),
            (210, "is-focal-lower"),
            (270, "is-focal-bottom"),
        ]
        for focal_y, expected_cls in cases:
            cover = self._make_image(f"横幅-{expected_cls}", focal_y=focal_y, height=300)
            col = BlogIndexPage(
                title=f"画报横幅-{expected_cls}",
                slug=f"banner-{expected_cls}-{uuid4().hex[:4]}",
                owner=self.owner,
                featured_image=cover,
                header_layout=BlogIndexPage.HEADER_LAYOUT_VISUAL_BANNER,
                intro="<p>宽幅横幅专题。</p>",
            )
            self.home.add_child(instance=col)
            col.save_revision(user=self.owner).publish(user=self.owner)

            resp = self.client.get(col.url)
            self.assertEqual(resp.status_code, 200)
            html = resp.content.decode("utf-8")
            self.assertIn("listing-header--visual_banner has-cover", html)
            self.assertIn("listing-header-cover", html)
            self.assertIn(expected_cls, html)
            self.assertNotIn(" style=", html)

    def test_long_intro_collapsible_across_listing_pages(self):
        """长导读（超 180 字、含列表或标题、多段落）在三类栏目均触发自动折叠标记与切换按钮"""
        long_text = "这是一段用于测试长导读自动折叠功能的栏目介绍文字，确保在内容较多时不会挤占首屏文章列表区域。" * 5
        long_blog = BlogIndexPage(
            title="长导读博客栏目",
            slug=f"long-b-{uuid4().hex[:6]}",
            owner=self.owner,
            header_layout=BlogIndexPage.HEADER_LAYOUT_COMPACT_DOC,
            intro=f"<p>{long_text}</p>",
        )
        self.home.add_child(instance=long_blog)
        long_blog.save_revision(user=self.owner).publish(user=self.owner)

        list_blog = BlogIndexPage(
            title="列表导读博客栏目",
            slug=f"list-b-{uuid4().hex[:6]}",
            owner=self.owner,
            intro="<p>导读要点：</p><ul><li>第一条</li><li>第二条</li></ul>",
        )
        self.home.add_child(instance=list_blog)
        list_blog.save_revision(user=self.owner).publish(user=self.owner)

        short_blog = BlogIndexPage(
            title="短导读博客栏目",
            slug=f"short-b-{uuid4().hex[:6]}",
            owner=self.owner,
            intro="<p>一句简短导读。</p>",
        )
        self.home.add_child(instance=short_blog)
        short_blog.save_revision(user=self.owner).publish(user=self.owner)

        topic = GuideTopic.objects.create(name="系统架构主题", slug=f"arch-{uuid4().hex[:6]}")
        g_idx = GuideIndexPage(
            title="指南多段导读栏目",
            slug=f"long-g-{uuid4().hex[:6]}",
            featured_topic=topic,
            header_layout=GuideIndexPage.HEADER_LAYOUT_COMPACT_DOC,
            intro="第一段指南导读。\n\n第二段指南导读。",
        )
        self.home.add_child(instance=g_idx)
        g_idx.save_revision(user=self.owner).publish(user=self.owner)

        p_idx = PortfolioIndexPage(
            title="作品长导读栏目",
            slug=f"long-p-{uuid4().hex[:6]}",
            header_layout=PortfolioIndexPage.HEADER_LAYOUT_COMPACT_DOC,
            intro=long_text,
        )
        self.home.add_child(instance=p_idx)
        p_idx.save_revision(user=self.owner).publish(user=self.owner)

        for page_obj in (long_blog, list_blog, g_idx, p_idx):
            html = self.client.get(page_obj.url).content.decode("utf-8")
            self.assertIn("data-intro-collapsible", html)
            self.assertIn("data-intro-toggle", html)
            self.assertIn("展开完整导读", html)

        g_html = self.client.get(g_idx.url).content.decode("utf-8")
        self.assertIn("系统架构主题", g_html)

        short_html = self.client.get(short_blog.url).content.decode("utf-8")
        self.assertNotIn("data-intro-collapsible", short_html)
        self.assertNotIn("data-intro-toggle", short_html)

    def test_remaining_three_emoji_sites_replaced_with_svg_icons(self):
        """详情页统计条、作者月历卡与作者结果列表均已清零 👁️ / 📅 并改用单色 SVG 图标"""
        person = Person.objects.create(name="图标核验作者", slug=f"icon-au-{uuid4().hex[:6]}")
        col = BlogIndexPage(title="图标核验栏目", slug=f"icon-col-{uuid4().hex[:6]}", owner=self.owner)
        self.home.add_child(instance=col)
        col.save_revision(user=self.owner).publish(user=self.owner)

        post = BlogPage(
            title="图标核验文章",
            slug=f"icon-post-{uuid4().hex[:6]}",
            body=[("markdown", {"markdown": "正文内容"})],
        )
        col.add_child(instance=post)
        BlogPagePerson.objects.create(page=post, person=person, role="primary_author")
        post.save_revision(user=self.owner).publish(user=self.owner)

        ana_cfg = AnalyticsSettings.get_for_site_readonly(self.site)
        ana_cfg.public_pv_display_enabled = True
        ana_cfg.save()
        PageAnalyticsTotal.objects.create(site=self.site, page=post, total_pv=88)

        detail_html = self.client.get(post.url).content.decode("utf-8")
        self.assertIn("detail-stat-pill", detail_html)
        self.assertIn("author-heatmap-title", detail_html)
        self.assertIn('class="ui-icon"', detail_html)
        self.assertNotIn("👁", detail_html)
        self.assertNotIn("📅", detail_html)

        author_html = self.client.get(f"/authors/{person.slug}/").content.decode("utf-8")
        self.assertIn("88 次阅读", author_html)
        self.assertIn('class="ui-icon"', author_html)
        self.assertNotIn("👁", author_html)
        self.assertNotIn("📅", author_html)

        # 静态模板文本核验：确认目标模板与样式无残留 Emoji 与内联 style=
        header_tpl = (PROJECT_ROOT / "myblog/templates/discovery/listing/_listing_header.html").read_text(encoding="utf-8")
        self.assertNotIn(" style=", header_tpl)
        for rel_path in (
            "myblog/apps/analytics/templatetags/analytics_tags.py",
            "myblog/templates/detail/_detail_author_card.html",
            "myblog/apps/people/templates/people/partials/_author_results.html",
        ):
            src = (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
            self.assertNotIn("👁", src, rel_path)
            self.assertNotIn("📅", src, rel_path)
