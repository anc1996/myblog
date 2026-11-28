"""
第 8 步（P1-3）：前端旧样式残余清理、UI 控件去 Emoji、后台内联样式收敛与 404/500 中文化回归测试。
"""

from pathlib import Path
import re
from uuid import uuid4

from django.conf import settings
from django.template import Context, Template
from django.test import TestCase, override_settings
from wagtail.coreutils import get_supported_content_language_variant
from wagtail.models import Collection, Locale, Page, Site

from home.models import HomePage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


class Step8UiCleanupAndErrorPagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Locale.objects.get_or_create(
            language_code=get_supported_content_language_variant(settings.LANGUAGE_CODE)
        )
        if Collection.get_first_root_node() is None:
            Collection.add_root(name="Root")
        root = Page.get_first_root_node() or Page.add_root(title="Root")
        cls.home = HomePage.objects.first()
        if cls.home is None:
            slug = "home" if not root.get_children().filter(slug="home").exists() else f"home-{uuid4().hex[:6]}"
            cls.home = HomePage(title="Home", slug=slug)
            root.add_child(instance=cls.home)
            cls.home.save_revision().publish()
        if not Site.objects.filter(hostname="localhost").exists():
            is_default = not Site.objects.filter(is_default_site=True).exists()
            Site.objects.create(hostname="localhost", port=80, root_page=cls.home, is_default_site=is_default)
        Site.clear_site_root_paths_cache()

    def test_404_page_localized_and_uses_site_shell(self):
        with override_settings(DEBUG=False, ALLOWED_HOSTS=["localhost", "testserver"]):
            resp = self.client.get("/nonexistent-page-step8-probe/", HTTP_HOST="localhost")
        self.assertEqual(resp.status_code, 404)
        html = resp.content.decode("utf-8")
        self.assertIn("页面未找到", html)
        self.assertIn("返回首页", html)
        self.assertIn("站内搜索", html)
        self.assertIn("css/themes/tokens.css", html)
        self.assertNotIn("Page not found", html)

    def test_500_template_standalone_zero_db_and_dark_mode_ready(self):
        path = PROJECT_ROOT / "myblog/templates/500.html"
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("{%", raw)
        self.assertNotIn("{{", raw)
        with self.assertNumQueries(0):
            rendered = Template(raw).render(Context())
        self.assertIn("服务器暂时出现异常", rendered)
        self.assertIn("返回首页", rendered)
        self.assertIn("prefers-color-scheme: dark", rendered)
        self.assertNotIn("Internal server error", rendered)

    def test_frontend_css_zero_raw_hex(self):
        for rel in (
            "myblog/static/css/myblog.css",
            "myblog/static/people/css/author_directory.css",
            "myblog/static/comments/css/comments_widget.css",
        ):
            css_text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            self.assertEqual(HEX_RE.findall(css_text), [], msg=f"{rel} 存在裸 #hex 色值")

    def test_cleaned_templates_have_no_bootstrap_emojis_or_static_inline_styles(self):
        cleaned_templates = (
            "myblog/templates/404.html",
            "myblog/templates/detail/_detail_meta.html",
            "myblog/templates/discovery/listing/_listing_filters.html",
            "myblog/templates/discovery/listing/_listing_pagination.html",
            "myblog/templates/discovery/listing/_listing_results.html",
            "myblog/templates/comments/partials/_comment_section.html",
            "myblog/templates/comments/admin/comment_index.html",
            "myblog/templates/comments/admin/page_comment_governance_panel.html",
            "myblog/templates/identity/admin/unified_avatar_panel.html",
            "myblog/apps/people/templates/people/author_detail.html",
            "myblog/apps/people/templates/people/partials/_author_directory.html",
            "myblog/apps/people/templates/people/partials/_author_missing_inner.html",
            "myblog/apps/people/templates/people/partials/_author_results.html",
            "myblog/apps/search/templates/search/partials/_search_content.html",
            "myblog/apps/content_blocks/templates/content_blocks/embeds/chooser.html",
            "myblog/apps/markdown_import/templates/markdown_import/admin_tokens.html",
            "myblog/apps/analytics/templates/analytics/admin/alerts.html",
            "myblog/apps/analytics/templates/analytics/admin/curation.html",
            "myblog/apps/analytics/templates/analytics/admin/dashboard.html",
            "myblog/apps/analytics/templates/analytics/admin/operations.html",
            "myblog/apps/analytics/templates/analytics/admin/page_drawer.html",
            "myblog/apps/analytics/templates/analytics/admin/rankings.html",
        )
        legacy_bootstrap_re = re.compile(
            r'(?<![\w-])(bg-white|bg-light|d-flex|justify-content-[\w-]+|badge\s+bg-[\w-]+|'
            r'btn-outline-secondary|btn-outline-primary|form-control|form-select|'
            r'alert-warning|alert-info|text-muted|text-secondary|text-primary)(?![\w-])'
        )
        ui_emojis = ("👤", "📡", "⚛", "🔍", "📂", "⚠️", "📌", "⚙️", "💬", "🔒")
        inline_style_re = re.compile(r'style="([^"]*)"')

        for rel in cleaned_templates:
            text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            self.assertIsNone(legacy_bootstrap_re.search(text), msg=f"{rel} 残留 Bootstrap 类名")
            for em in ui_emojis:
                self.assertNotIn(em, text, msg=f"{rel} 残留 UI Emoji: {em}")
            for m in inline_style_re.finditer(text):
                style_val = m.group(1).strip()
                self.assertRegex(
                    style_val,
                    r"^width:\s*\{\{[^}]+\}\}%;$",
                    msg=f"{rel} 存在非动态进度条静态内联 style={style_val!r}",
                )
