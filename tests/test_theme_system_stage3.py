"""
全站主题设计系统（阶段 3：正文积木、Mermaid follow-site 联动与风格静态护栏）回归测试。

核验范围：
1. Highlight.js 与无 JS Pygments (.hl-*) 映射至双主题 --code-* 语义令牌，前台不再固定加载 github-dark.min.css；
2. Mermaid 5 处白名单/渲染链路（blocks/mermaid.py、rendering/mermaid.py、blocks/markdown.py、
   mermaid-renderer.js、mermaid-block.js、admin-mermaid-split.js、mermaid_split.html）全部支持 follow-site 与全屏弹窗同步；
3. 非破坏性 StreamField 迁移后，含旧版 theme="default" 的历史 Revision with_content_json() 反序列化与渲染正常；
4. 10 份已收敛业务 CSS 零裸 #hex 静态护栏（所有配色统一归口 tokens.css）。
"""

import re
from pathlib import Path
from uuid import uuid4

from django.test import TestCase
from wagtail.models import Page, Site

from blog.models import BlogPage
from content_blocks.blocks.markdown import CustomMarkdownBlock
from content_blocks.blocks.mermaid import CustomMermaidBlock
from content_blocks.rendering.mermaid import ALLOWED_THEMES, render_mermaid_dom
from guides.models import GuideIndexPage, GuidePage, GuideTopic
from home.models import HomePage
from portfolio.models import PortfolioIndexPage, ProjectPage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Stage3MermaidFollowSiteTests(TestCase):
    def test_mermaid_block_markdown_and_renderer_support_follow_site(self):
        """Mermaid 独立块、Markdown 围栏与服务端 DOM 契约均默认使用 follow-site"""
        self.assertIn("follow-site", ALLOWED_THEMES)

        m_block = CustomMermaidBlock()
        self.assertEqual(m_block.child_blocks["theme"].meta.default, "follow-site")

        dom_default = render_mermaid_dom("graph TD; A-->B;")
        self.assertIn('data-theme="follow-site"', dom_default)

        md_block = CustomMarkdownBlock()
        res = md_block.render_clean("```mermaid\ngraph LR\n  A --> B\n```")
        self.assertIn('data-cb-mermaid data-theme="follow-site"', res.html)

    def test_mermaid_js_assets_implement_follow_site_and_modal_sync(self):
        """前后台 Mermaid JS 与模板同步支持 follow-site、myblog:theme:change 与弹窗同步"""
        renderer_js = (
            PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/js/mermaid-renderer.js"
        ).read_text(encoding="utf-8")
        block_js = (
            PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/js/mermaid-block.js"
        ).read_text(encoding="utf-8")
        admin_js = (
            PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/js/admin-mermaid-split.js"
        ).read_text(encoding="utf-8")
        widget_tpl = (
            PROJECT_ROOT / "myblog/apps/content_blocks/templates/content_blocks/widgets/mermaid_split.html"
        ).read_text(encoding="utf-8")

        self.assertIn('"follow-site": true', renderer_js)
        self.assertIn("resolveTheme", renderer_js)
        self.assertIn("myblog:theme:change", block_js)
        self.assertIn("activeModalRefs", block_js)
        self.assertIn('"follow-site"', admin_js)
        self.assertIn("requestedTheme", admin_js)
        self.assertIn('value="follow-site"', widget_tpl)

    def test_legacy_default_mermaid_revisions_remain_compatible(self):
        """StreamField choices 迁移后，含旧版 theme='default' 的页面 Revision 仍可正常保存与水合还原"""
        home = HomePage.objects.first()
        if not home:
            root = Page.get_first_root_node() or Page.add_root(title="Root")
            home = HomePage(title="Home", slug=f"home-{uuid4().hex[:6]}")
            root.add_child(instance=home)
            home.save_revision().publish()
        if not Site.objects.filter(is_default_site=True).exists():
            Site.objects.create(hostname="localhost", port=80, root_page=home, is_default_site=True)

        blog = BlogPage(
            title="Legacy Mermaid Blog",
            slug=f"lm-blog-{uuid4().hex[:6]}",
            body=[("mermaid", {"title": "旧版图", "diagram_code": "graph TD; A-->B;", "theme": "default"})],
        )
        home.add_child(instance=blog)
        rev = blog.save_revision()
        rev.publish()

        restored_blog = rev.as_object()
        self.assertEqual(restored_blog.body[0].value["theme"], "default")

        guide_idx = GuideIndexPage(title="Guides", slug=f"lm-gi-{uuid4().hex[:6]}")
        home.add_child(instance=guide_idx)
        guide_idx.save_revision().publish()
        topic = GuideTopic.objects.create(name="LM Topic", slug=f"lm-t-{uuid4().hex[:6]}")
        guide = GuidePage(
            title="Legacy Mermaid Guide",
            slug=f"lm-guide-{uuid4().hex[:6]}",
            topic=topic,
            body=[
                ("step", {"step_number": "1", "title": "S1", "content": "<p>ok</p>"}),
                ("mermaid", {"title": "旧版指南图", "diagram_code": "graph LR; X-->Y;", "theme": "dark"}),
            ],
        )
        guide_idx.add_child(instance=guide)
        g_rev = guide.save_revision()
        g_rev.publish()
        self.assertEqual(g_rev.as_object().body[1].value["theme"], "dark")

        port_idx = PortfolioIndexPage(title="Ports", slug=f"lm-pi-{uuid4().hex[:6]}")
        home.add_child(instance=port_idx)
        port_idx.save_revision().publish()
        proj = ProjectPage(
            title="Legacy Mermaid Proj",
            slug=f"lm-proj-{uuid4().hex[:6]}",
            body=[("mermaid", {"title": "跟随站点图", "diagram_code": "graph TD; P-->Q;", "theme": "follow-site"})],
        )
        port_idx.add_child(instance=proj)
        p_rev = proj.save_revision()
        p_rev.publish()
        self.assertEqual(p_rev.as_object().body[0].value["theme"], "follow-site")


class Stage3CssTokenGuardTests(TestCase):
    def test_converged_css_files_contain_zero_bare_hex_colors(self):
        """阶段 2–3 已收敛的业务 CSS 严禁写入裸 #hex 色值，必须消费语义令牌"""
        converged_css = [
            "myblog/static/css/identity-auth.css",
            "myblog/static/comments/css/comments_widget.css",
            "myblog/static/people/css/author_directory.css",
            "myblog/apps/analytics/static/analytics/css/article-rating.css",
            "myblog/apps/content_blocks/static/content_blocks/css/code-highlighting.css",
            "myblog/apps/content_blocks/static/content_blocks/css/table.css",
            "myblog/apps/content_blocks/static/content_blocks/css/structured-blocks.css",
            "myblog/apps/content_blocks/static/content_blocks/css/embed-block.css",
            "myblog/apps/content_blocks/static/content_blocks/css/media-blocks.css",
            "myblog/apps/content_blocks/static/content_blocks/css/mermaid-block.css",
        ]
        hex_re = re.compile(r"#[0-9a-fA-F]{3,8}\b")
        for rel in converged_css:
            text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            cleaned = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            matches = hex_re.findall(cleaned)
            self.assertEqual(matches, [], f"{rel} 存在硬编码裸 hex 颜色: {matches}")
            self.assertIn("var(--", cleaned, f"{rel} 必须使用设计令牌变量")

    def test_code_highlighting_uses_dual_theme_tokens_and_templates_palette_agnostic(self):
        """code-highlighting.css 覆盖 .hljs-* 与 .hl-* 双主题令牌，业务页面模板不硬编码色板名"""
        code_css = (
            PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/css/code-highlighting.css"
        ).read_text(encoding="utf-8")
        for needle in (".hljs-keyword", ".hl-k", "var(--code-bg)", "var(--code-gutter)", "var(--code-fade-to)"):
            self.assertIn(needle, code_css)

        for rel_tpl in (
            "myblog/apps/blog/templates/blog/blog_page.html",
            "myblog/apps/guides/templates/guides/guide_page.html",
            "myblog/apps/portfolio/templates/portfolio/project_page.html",
            "myblog/apps/home/templates/home/home_page.html",
        ):
            tpl_text = (PROJECT_ROOT / rel_tpl).read_text(encoding="utf-8")
            self.assertNotIn("mist-blue", tpl_text)
            self.assertNotIn("moss-lab", tpl_text)
