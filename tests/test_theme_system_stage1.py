"""
全站主题设计系统与昼夜跟随（阶段 1：底座与单源状态机）回归测试。

核验范围：
1. SiteThemeSettings 只读与隔离契约：空表公开 GET 零写入；保存走 on_commit 独立缓存；不递增 ContentDiscoveryEpoch；
2. base.html 样式加载顺序（tokens.css -> vendor -> myblog.css）、单源早载脚本 __myblogTheme、清理空文件 myblog.js；
3. 三态切换菜单（保留 data-theme-toggle）、Wagtail 预览条（<button> 非 <a>）、UI 桥接事件契约；
4. tokens.css 令牌完整性与 4 套（两色板 x 昼夜）核心前/背景色配对 WCAG 2.2 AA (>= 4.5:1) 对比度计算校验。
"""

import re
from pathlib import Path
from uuid import uuid4

from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from wagtail.models import Page, Site

from discovery.models import ContentDiscoveryEpoch
from home.models import HomePage
from site_settings.models import THEME_SETTINGS_CACHE_KEY, NavigationSettings, SiteThemeSettings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _hex_to_rgb(hex_str: str) -> tuple[float, float, float]:
    raw = hex_str.lstrip("#")
    return tuple(int(raw[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


def _relative_luminance(rgb: tuple[float, float, float]) -> float:
    def _channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (_channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    l1 = _relative_luminance(_hex_to_rgb(fg_hex))
    l2 = _relative_luminance(_hex_to_rgb(bg_hex))
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _parse_tokens_css(css_text: str) -> list[tuple[str, dict[str, str]]]:
    # 去掉 /* ... */ 注释，并用大括号深度追踪只提取顶层普通规则块（跳过 @media 等 at-rule 内部覆盖）
    cleaned = re.sub(r"/\*.*?\*/", "", css_text, flags=re.S)
    top_level_chunks: list[str] = []
    i = 0
    n = len(cleaned)
    while i < n:
        ch = cleaned[i]
        if ch == "@":
            brace_pos = cleaned.find("{", i)
            semi_pos = cleaned.find(";", i)
            if semi_pos != -1 and (brace_pos == -1 or semi_pos < brace_pos):
                i = semi_pos + 1
                continue
            if brace_pos != -1:
                depth = 1
                j = brace_pos + 1
                while j < n and depth > 0:
                    if cleaned[j] == "{":
                        depth += 1
                    elif cleaned[j] == "}":
                        depth -= 1
                    j += 1
                i = j
                continue
        top_level_chunks.append(ch)
        i += 1
    top_level_css = "".join(top_level_chunks)

    blocks: list[tuple[str, dict[str, str]]] = []
    for match in re.finditer(r"([^{}@]+)\{([^{}]+)\}", top_level_css):
        selector = " ".join(match.group(1).split())
        body = match.group(2)
        mapping: dict[str, str] = {}
        for var_match in re.finditer(r"(--[a-zA-Z0-9_-]+)\s*:\s*([^;]+);", body):
            mapping[var_match.group(1)] = var_match.group(2).strip()
        blocks.append((selector, mapping))
    return blocks


def _resolve_token_env(blocks: list[tuple[str, dict[str, str]]], active_selectors: set[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for selector, mapping in blocks:
        parts = [p.strip() for p in selector.split(",")]
        if any(p in active_selectors for p in parts):
            env.update(mapping)

    resolved: dict[str, str] = {}
    for key, val in env.items():
        cur = val
        for _ in range(6):
            ref = re.match(r"^var\((--[a-zA-Z0-9_-]+)\)$", cur)
            if not ref:
                break
            cur = env.get(ref.group(1), cur)
        resolved[key] = cur
    return resolved


class ThemeSettingsModelAndCacheTests(TestCase):
    def setUp(self):
        cache.delete(THEME_SETTINGS_CACHE_KEY)
        SiteThemeSettings.objects.all().delete()

    def test_empty_table_readonly_dict_never_writes_db(self):
        """空表读取 get_readonly_dict() 必须纯只读，严禁触发 INSERT/UPDATE/DELETE"""
        with CaptureQueriesContext(connection) as ctx:
            cfg = SiteThemeSettings.get_readonly_dict()

        self.assertEqual(cfg["default_palette"], "mist-blue")
        self.assertEqual(cfg["default_appearance_mode"], "system")
        self.assertEqual(SiteThemeSettings.objects.count(), 0)
        for q in ctx.captured_queries:
            sql_upper = q["sql"].lstrip().upper()
            self.assertFalse(
                sql_upper.startswith(("INSERT", "UPDATE", "DELETE")),
                f"公开只读读取不应写库，却执行了 SQL: {q['sql']}",
            )

    def test_save_updates_cache_on_commit_without_touching_discovery_epoch(self):
        """保存 SiteThemeSettings 应在 commit 后刷新独立缓存，且绝不递增 ContentDiscoveryEpoch"""
        before_epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()
        with self.captureOnCommitCallbacks(execute=True):
            setting = SiteThemeSettings.objects.create(
                default_palette="moss-lab",
                default_appearance_mode="dark",
            )

        after_epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()
        self.assertEqual(before_epoch, after_epoch)
        self.assertEqual(setting.singleton_key, 1)

        cached = cache.get(THEME_SETTINGS_CACHE_KEY)
        self.assertIsInstance(cached, dict)
        self.assertEqual(cached["default_palette"], "moss-lab")
        self.assertEqual(cached["default_appearance_mode"], "dark")
        self.assertEqual(SiteThemeSettings.get_readonly_dict()["default_palette"], "moss-lab")

        # 再次保存会递增自身 version，仍不触碰 ContentDiscoveryEpoch
        setting.default_appearance_mode = "light"
        with self.captureOnCommitCallbacks(execute=True):
            setting.save()
        self.assertEqual(setting.version, 2)
        self.assertEqual(ContentDiscoveryEpoch.get_current_epoch_readonly(), before_epoch)
        self.assertEqual(SiteThemeSettings.get_readonly_dict()["default_appearance_mode"], "light")

        # NavigationSettings 保存才会递增 ContentDiscoveryEpoch（对照组）
        nav, _ = NavigationSettings.objects.get_or_create(singleton_key=1)
        nav.save()
        self.assertGreater(ContentDiscoveryEpoch.get_current_epoch_readonly(), before_epoch)

    def test_singleton_constraint_and_invalid_value_fallback(self):
        """singleton_key 保持唯一且非法枚举值安全回退默认"""
        s = SiteThemeSettings.objects.create(
            default_palette="not-a-palette",
            default_appearance_mode="invalid",
        )
        self.assertEqual(s.default_palette, "mist-blue")
        self.assertEqual(s.default_appearance_mode, "system")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SiteThemeSettings.objects.create(singleton_key=1)


class BaseTemplateAndThemeScriptTests(TestCase):
    def setUp(self):
        cache.delete(THEME_SETTINGS_CACHE_KEY)
        SiteThemeSettings.objects.all().delete()
        self.factory = RequestFactory()
        self.home = HomePage.objects.first()
        if not self.home:
            root = Page.get_first_root_node() or Page.add_root(title="Root")
            self.home = HomePage(title="Home", slug=f"home-{uuid4().hex[:6]}")
            root.add_child(instance=self.home)
            self.home.save_revision().publish()
        self.site = Site.objects.filter(is_default_site=True).first()
        if not self.site:
            self.site = Site.objects.create(
                hostname="localhost",
                port=80,
                root_page=self.home,
                is_default_site=True,
            )

    def test_public_homepage_render_has_zero_theme_writes_and_correct_asset_order(self):
        """空表公开访问首页不写入 SiteThemeSettings，且样式顺序、单源脚本与三态菜单契约正确"""
        req = self.factory.get("/")
        with CaptureQueriesContext(connection) as ctx:
            resp = self.home.serve(req)
            resp.render()

        self.assertEqual(SiteThemeSettings.objects.count(), 0)
        for q in ctx.captured_queries:
            if "site_settings_sitethemesettings" in q["sql"].lower():
                self.assertFalse(
                    q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")),
                    f"公开访问不应写入 SiteThemeSettings: {q['sql']}",
                )

        html = resp.content.decode("utf-8")
        self.assertIn('data-palette="mist-blue"', html)
        self.assertIn('data-default-palette="mist-blue"', html)
        self.assertIn('data-default-mode="system"', html)
        self.assertIn('data-in-preview="0"', html)

        # 首屏单源脚本必须在样式表之前，且不得存在旧空文件 myblog.js
        pos_boot = html.find("window.__myblogTheme")
        pos_tokens = html.find("css/themes/tokens.css")
        pos_vendor = html.find("vendor/tomselect/tom-select.default.min.css")
        pos_myblog = html.find("css/myblog.css")
        self.assertNotEqual(pos_boot, -1)
        self.assertLess(pos_boot, pos_tokens)
        self.assertLess(pos_tokens, pos_vendor)
        self.assertLess(pos_vendor, pos_myblog)
        self.assertNotIn("js/myblog.js", html)

        # 三态菜单与兼容 data-theme-toggle
        self.assertIn("data-theme-toggle", html)
        self.assertIn('data-theme-mode-option="system"', html)
        self.assertIn('data-theme-mode-option="light"', html)
        self.assertIn('data-theme-mode-option="dark"', html)
        self.assertIn("data-theme-reset", html)
        self.assertNotIn("data-preview-theme-bar", html)

    def test_preview_panel_renders_button_only_preview_bar_and_configured_palette(self):
        """Wagtail 预览模式渲染自定义色板与仅含 <button> 的预览切换条（防 <base target='_blank'> 弹窗）"""
        with self.captureOnCommitCallbacks(execute=True):
            SiteThemeSettings.objects.create(
                default_palette="moss-lab",
                default_appearance_mode="dark",
            )

        req = self.factory.get("/")
        req.in_preview_panel = True
        resp = self.home.serve(req)
        resp.render()
        html = resp.content.decode("utf-8")

        self.assertIn('data-palette="moss-lab"', html)
        self.assertIn('data-default-mode="dark"', html)
        self.assertIn('data-in-preview="1"', html)
        self.assertIn('<base target="_blank">', html)
        self.assertIn("data-preview-theme-bar", html)
        self.assertIn('data-preview-mode-option="site"', html)
        self.assertIn('data-preview-mode-option="light"', html)
        self.assertIn('data-preview-mode-option="dark"', html)

        bar_match = re.search(r'<div class="preview-theme-bar".*?</div>', html, flags=re.S)
        self.assertIsNotNone(bar_match)
        self.assertNotIn("<a ", bar_match.group(0))

    def test_ui_components_js_delegates_to_single_source_and_binds_lifecycle_events(self):
        """ui-components.js 不重复实现主题逻辑，必须复用 __myblogTheme 并绑定实时同步事件"""
        js_text = (PROJECT_ROOT / "myblog/static/js/ui-components.js").read_text(encoding="utf-8")
        self.assertIn("window.__myblogTheme", js_text)
        self.assertNotIn('localStorage.getItem("myblog_theme_preference")', js_text)
        self.assertIn('matchMedia("(prefers-color-scheme: dark)")', js_text)
        self.assertIn('addEventListener("storage"', js_text)
        self.assertIn('addEventListener("pageshow"', js_text)
        self.assertIn("htmx:afterSwap", js_text)
        self.assertIn("myblog:theme:change", js_text)


class TokensCssAndContrastTests(TestCase):
    def test_all_four_theme_variants_satisfy_wcag_aa_contrast(self):
        """两套色板 x 昼夜共 4 种组合的语义前/背景配对均须达到 WCAG 2.2 AA (>= 4.5:1)"""
        css_text = (PROJECT_ROOT / "myblog/static/css/themes/tokens.css").read_text(encoding="utf-8")
        blocks = _parse_tokens_css(css_text)

        variants = {
            "mist-blue/light": {
                ":root",
                ':root[data-palette="mist-blue"]',
                ':root[data-palette="mist-blue"][data-theme="light"]',
            },
            "mist-blue/dark": {
                ":root",
                ':root[data-palette="mist-blue"]',
                ':root[data-theme="dark"]',
                ':root[data-palette="mist-blue"][data-theme="dark"]',
            },
            "moss-lab/light": {
                ":root",
                ':root[data-palette="mist-blue"]',
                ':root[data-palette="moss-lab"]',
                ':root[data-palette="moss-lab"][data-theme="light"]',
            },
            "moss-lab/dark": {
                ":root",
                ':root[data-palette="mist-blue"]',
                ':root[data-theme="dark"]',
                ':root[data-palette="moss-lab"][data-theme="dark"]',
            },
        }

        required_tokens = [
            "--color-bg",
            "--color-surface",
            "--color-surface-subtle",
            "--color-surface-raised",
            "--color-backdrop",
            "--color-border",
            "--color-border-strong",
            "--color-text",
            "--color-text-secondary",
            "--color-muted",
            "--color-link",
            "--color-primary",
            "--color-on-primary",
            "--color-primary-soft",
            "--color-on-primary-soft",
            "--color-success",
            "--color-on-success",
            "--color-warning",
            "--color-on-warning",
            "--color-danger",
            "--color-on-danger",
            "--code-bg",
            "--code-text",
            "--code-gutter",
            "--table-head-bg",
            "--table-head-text",
            "--cal-heat-1",
            "--cal-heat-4",
        ]

        contrast_pairs = [
            ("--color-text", "--color-bg"),
            ("--color-text", "--color-surface"),
            ("--color-text", "--color-surface-raised"),
            ("--color-text-secondary", "--color-surface"),
            ("--color-muted", "--color-surface"),
            ("--color-link", "--color-surface"),
            ("--color-on-primary", "--color-primary"),
            ("--color-on-success", "--color-success"),
            ("--color-on-warning", "--color-warning"),
            ("--color-on-danger", "--color-danger"),
            ("--table-head-text", "--table-head-bg"),
            ("--code-text", "--code-bg"),
            ("--code-gutter", "--code-bg"),
        ]

        for variant_name, selectors in variants.items():
            env = _resolve_token_env(blocks, selectors)
            for token in required_tokens:
                self.assertIn(token, env, f"{variant_name} 缺少令牌 {token}")
            for fg_key, bg_key in contrast_pairs:
                fg_val = env[fg_key]
                bg_val = env[bg_key]
                self.assertTrue(
                    fg_val.startswith("#") and bg_val.startswith("#"),
                    f"{variant_name} 令牌 {fg_key}={fg_val} / {bg_key}={bg_val} 须为可校验 hex 颜色",
                )
                ratio = _contrast_ratio(fg_val, bg_val)
                self.assertGreaterEqual(
                    ratio,
                    4.5,
                    f"{variant_name} 对比度不足 4.5:1: {fg_key}({fg_val}) on {bg_key}({bg_val}) = {ratio:.2f}:1",
                )

        # 确认包含无 JS 暗黑兜底、打印浅色兜底与 forced-colors 焦点环
        self.assertIn("@media (prefers-color-scheme: dark)", css_text)
        self.assertIn("@media print", css_text)
        self.assertIn("@media (forced-colors: active)", css_text)
