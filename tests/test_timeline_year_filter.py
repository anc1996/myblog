"""
时间线任意年份一步看全年、模板清洁与静态样式护栏测试 (Timeline Year Filter & Visual Hygiene Tests)。
"""

from datetime import date
from pathlib import Path
import re
from uuid import uuid4

from django.test import RequestFactory, TestCase
from wagtail.models import Page, Site

from blog.models import BlogPage
from discovery.timeline import timeline_view
from home.models import HomePage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TimelineYearFilterTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.home = HomePage.objects.first()
        if not self.home:
            root = Page.get_first_root_node() or Page.add_root(title="Root")
            self.home = HomePage(title="Home", slug=f"home-{uuid4().hex[:6]}")
            root.add_child(instance=self.home)
            self.home.save_revision().publish()

        if not Site.objects.filter(is_default_site=True).exists():
            Site.objects.create(
                hostname="localhost",
                port=80,
                root_page=self.home,
                is_default_site=True,
            )

        self.home.refresh_from_db()

        self.created_pages = []
        p_2024_jan = BlogPage(
            title=f"2024年初文章-{uuid4().hex[:6]}",
            slug=f"y24-jan-{uuid4().hex[:6]}",
            authored_on=date(2024, 1, 15),
        )
        p_2024_nov = BlogPage(
            title=f"2024年末文章-{uuid4().hex[:6]}",
            slug=f"y24-nov-{uuid4().hex[:6]}",
            authored_on=date(2024, 11, 20),
        )
        p_2025_feb = BlogPage(
            title=f"2025年对比文章-{uuid4().hex[:6]}",
            slug=f"y25-feb-{uuid4().hex[:6]}",
            authored_on=date(2025, 2, 10),
        )
        for p in (p_2024_jan, p_2024_nov, p_2025_feb):
            self.home.add_child(instance=p)
            p.save_revision().publish()
            self.created_pages.append(p)

        self.p_2024_jan = p_2024_jan
        self.p_2024_nov = p_2024_nov
        self.p_2025_feb = p_2025_feb

    def tearDown(self):
        for p in reversed(self.created_pages):
            try:
                p.delete(allow_physical_purge=True)
            except Exception:
                pass

    def test_year_scope_and_empty_month_show_only_target_year_articles(self):
        """访问 /timeline/?year=2024&scope=year 与 /timeline/?year=2024 均返回 200 且仅展示 2024 全年文章"""
        for url in ("/timeline/?year=2024&scope=year", "/timeline/?year=2024", "/timeline/?year=2024&month="):
            resp = timeline_view(self.factory.get(url))
            self.assertEqual(resp.status_code, 200, url)
            html = resp.content.decode("utf-8")
            self.assertIn("2024 年全年归档", html)
            self.assertIn(self.p_2024_jan.title, html)
            self.assertIn(self.p_2024_nov.title, html)
            self.assertNotIn(self.p_2025_feb.title, html)

    def test_timeline_templates_have_year_controls_and_clean_markup(self):
        """渲染页面包含全年选项、快捷年份链接、分拆 CSS，且零 📅 Emoji 与残余 Bootstrap 类"""
        resp = timeline_view(self.factory.get("/timeline/?year=2024&scope=year"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        self.assertIn("css/pages/timeline.css?v=20261127a", html)
        self.assertIn("timeline_controller.js?v=20261127a", html)
        self.assertIn("全年（全部月份）", html)
        self.assertRegex(html, r'<option value=""[^>]*selected[^>]*>全年（全部月份）</option>')
        self.assertIn('/timeline/?year=2024&amp;scope=year', html)
        self.assertIn("timeline-quick-years", html)
        self.assertIn("timeline-year-shortcut", html)
        self.assertIn("timeline-scope-switch", html)
        self.assertIn("timeline-summary-chip", html)
        self.assertIn("2024 全年", html)

        # 严格禁止 Emoji、旧 Bootstrap 工具类残留与内联 style=
        self.assertNotIn("📅", html)

        page_tpl = (PROJECT_ROOT / "myblog/templates/discovery/timeline/timeline_page.html").read_text(encoding="utf-8")
        partial_tpl = (PROJECT_ROOT / "myblog/templates/discovery/timeline/_timeline_content.html").read_text(encoding="utf-8")
        for tpl_text in (page_tpl, partial_tpl):
            self.assertNotIn("📅", tpl_text)
            self.assertNotIn("bg-white rounded border shadow-sm", tpl_text)
            self.assertNotIn("badge bg-secondary", tpl_text)
            self.assertNotIn("badge bg-primary", tpl_text)
            self.assertNotIn("badge bg-info", tpl_text)
            self.assertNotRegex(tpl_text, r"\sstyle\s*=")

    def test_timeline_controller_js_supports_empty_month_year_scope(self):
        """timeline_controller.js 支持空月份提交 scope: \"year\"，不再写死 mVal || \"1\""""
        js_text = (PROJECT_ROOT / "myblog/static/discovery/js/timeline_controller.js").read_text(encoding="utf-8")
        self.assertNotIn('mVal || "1"', js_text)
        self.assertNotIn("📅", js_text)
        self.assertIn('mVal === ""', js_text)
        self.assertIn('isYearScope ? "year" : "month"', js_text)
        self.assertIn("isYearScope ? null : mVal", js_text)
        self.assertIn("data-cal-toggle-label", js_text)

    def test_timeline_css_has_zero_bare_hex_and_zero_transition_all(self):
        """myblog/static/css/pages/timeline.css 零裸 #hex、零 transition: all 且消费语义令牌"""
        css_path = PROJECT_ROOT / "myblog/static/css/pages/timeline.css"
        self.assertTrue(css_path.exists())
        raw_css = css_path.read_text(encoding="utf-8")
        cleaned = re.sub(r"/\*.*?\*/", "", raw_css, flags=re.S)

        hex_matches = re.findall(r"#[0-9a-fA-F]{3,8}\b", cleaned)
        self.assertEqual(hex_matches, [], f"timeline.css 存在硬编码裸 hex 色值: {hex_matches}")
        self.assertNotRegex(cleaned, r"transition\s*:\s*all\b")
        self.assertIn("var(--", cleaned)
        self.assertIn("prefers-reduced-motion", cleaned)
        for cls in (
            ".timeline-header",
            ".timeline-quick-years",
            ".timeline-year-shortcut",
            ".timeline-scope-switch",
            ".timeline-summary-chip",
            ".timeline-time-travel-bar",
            ".timeline-calendar-card",
        ):
            self.assertIn(cls, cleaned)
