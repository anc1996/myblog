"""
全站共享详情壳层 (detail_shell.html)、单次服务端 TOC 提取器与公共 UI 组件回归测试。
"""

from datetime import date
from pathlib import Path
from uuid import uuid4

from django.http import Http404
from django.test import RequestFactory, TestCase
from wagtail.models import Site
from wagtail.rich_text import RichText

from blog.models import BlogIndexPage, BlogPage
from content_blocks.rendering.detail import build_detail_presentation
from content_lifecycle.recycle import RecycleService
from guides.models import GuideIndexPage, GuidePage, GuideTopic
from home.models import HomePage
from portfolio.models import PortfolioIndexPage, ProjectPage
from taxonomy.models import TagIndexPage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DetailShellAndTocTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.home = HomePage.objects.first()
        if not self.home:
            from wagtail.models import Page
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

        self.created_pages = []
        self.created_topics = []

    def tearDown(self):
        for p in reversed(self.created_pages):
            try:
                p.delete(allow_physical_purge=True)
            except Exception:
                pass
        for t in self.created_topics:
            try:
                t.delete()
            except Exception:
                pass

    def test_template_hygiene_no_inline_style_blocks(self):
        """详情页与首页模板不得保留模板级 <style> 块，统一使用共享设计令牌 CSS"""
        targets = [
            PROJECT_ROOT / "myblog/apps/blog/templates/blog/blog_page.html",
            PROJECT_ROOT / "myblog/apps/guides/templates/guides/guide_page.html",
            PROJECT_ROOT / "myblog/apps/portfolio/templates/portfolio/project_page.html",
            PROJECT_ROOT / "myblog/apps/home/templates/home/home_page.html",
            PROJECT_ROOT / "myblog/templates/detail/detail_shell.html",
        ]
        for path in targets:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("<style", text.lower(), f"{path} 不应包含内联 <style> 块")

    def test_single_pass_toc_ignores_code_and_callout_headings_and_resolves_tags(self):
        """测试单遍 TOC 提取跳过代码块/提示卡无关标题，并按 TagIndexPage 反解标签路由"""
        tag_index = TagIndexPage.objects.live().filter(is_recycled=False).first()
        if not tag_index:
            tag_index = TagIndexPage(title="全站标签", slug=f"tags-{uuid4().hex[:6]}")
            self.home.add_child(instance=tag_index)
            tag_index.save_revision().publish()
            self.created_pages.append(tag_index)

        blog = BlogPage(
            title="详情页共享壳层测试文章",
            slug=f"detail-toc-{uuid4().hex[:6]}",
            authored_on=date(2026, 11, 22),
            body=[
                ("markdown", "## 第一章 总体架构\n正文内容。\n\n```python\n# <h2>代码块内伪标题</h2>\nprint(1)\n```\n\n### 1.1 子章节"),
                ("rich_text", RichText("<h2>第二章 富文本章节</h2><p>段落</p>")),
                ("tip", {"tip_type": "warning", "title": "提示卡标题不应进目录", "content": RichText("<p>提示内容</p>")}),
                ("mermaid", {"title": "架构图", "diagram_code": "graph TD;\nA-->B;", "theme": "default"}),
            ],
        )
        self.home.add_child(instance=blog)
        blog.tags.add("Wagtail")
        blog.save_revision().publish()
        self.created_pages.append(blog)

        req = self.factory.get(blog.url_path)
        resp = blog.serve(req)
        resp.render()
        html_text = resp.content.decode("utf-8")

        # 1. TOC 存在且渲染桌面侧栏与移动抽屉
        self.assertIn("has-toc", html_text)
        self.assertIn("detail-toc-aside", html_text)
        self.assertIn("detail-toc-dialog", html_text)
        self.assertIn("第一章 总体架构", html_text)
        self.assertIn("1.1 子章节", html_text)
        self.assertIn("第二章 富文本章节", html_text)
        self.assertNotIn("代码块内伪标题", html_text.split("detail-toc-scroll")[1].split("</aside>")[0])
        self.assertNotIn("提示卡标题不应进目录", html_text.split("detail-toc-scroll")[1].split("</aside>")[0])

        # 2. 每个积木均有受控根容器 data-cb-block
        self.assertIn('data-cb-block="markdown"', html_text)
        self.assertIn('data-cb-block="rich_text"', html_text)
        self.assertIn('data-cb-block="mermaid"', html_text)

        # 3. 评论区仅挂载一次
        self.assertEqual(html_text.count('id="comments-section"'), 1)

        # 4. 标签链接通过 TagIndexPage 反解，而不是跳去 /search/?tag=
        expected_tag_url = f"{tag_index.url.rstrip('/')}/{tag_index.reverse_subpage('detail', args=['wagtail']).lstrip('/')}"
        self.assertIn(expected_tag_url, html_text)
        self.assertNotIn('/search/?tag=', html_text)

    def test_no_toc_page_omits_sidebar_and_tag_fallback_without_tag_index(self):
        """无标题正文不渲染空侧栏；全站无可用 TagIndexPage 时标签安全降级为纯文本徽章"""
        # 暂时确保当前站点没有活跃 TagIndexPage
        existing_tags_pages = list(TagIndexPage.objects.live().filter(is_recycled=False))
        for tp in existing_tags_pages:
            tp.unpublish()

        try:
            blog = BlogPage(
                title="纯段落无标题文章",
                slug=f"no-toc-{uuid4().hex[:6]}",
                body=[("rich_text", RichText("<p>只有正文，没有任何标题。</p>"))],
            )
            self.home.add_child(instance=blog)
            blog.tags.add("PlainTag")
            blog.save_revision().publish()
            self.created_pages.append(blog)

            req = self.factory.get(blog.url_path)
            resp = blog.serve(req)
            resp.render()
            html_text = resp.content.decode("utf-8")

            self.assertIn("no-toc", html_text)
            self.assertNotIn("detail-toc-aside", html_text)
            self.assertNotIn("detail-toc-mobile-bar", html_text)
            self.assertIn('<span class="tag-pill">#PlainTag</span>', html_text)
        finally:
            for tp in existing_tags_pages:
                tp.save_revision().publish()

    def test_guide_step_compat_and_preview_target_self(self):
        """GuidePage 兼容 #step-<编号> 锚点（含重复编号去重），预览态目录链接显式 target="_self" """
        topic = GuideTopic.objects.create(name=f"Topic-{uuid4().hex[:6]}", slug=f"t-{uuid4().hex[:6]}")
        self.created_topics.append(topic)
        guide = GuidePage(
            title="指南共享壳层测试",
            slug=f"guide-shell-{uuid4().hex[:6]}",
            topic=topic,
            body=[
                ("step", {"step_number": "1", "title": "安装环境", "content": RichText("<p>第一步</p>")}),
                ("step", {"step_number": "1", "title": "重复编号步骤", "content": RichText("<p>第二步</p>")}),
                ("markdown", "## 进阶说明\n内容"),
            ],
        )
        self.home.add_child(instance=guide)
        guide.save_revision().publish()
        self.created_pages.append(guide)

        req = self.factory.get(guide.url_path)
        req.in_preview_panel = True
        detail = build_detail_presentation(guide, req)
        toc_ids = [item["id"] for item in detail["toc_items"]]
        self.assertIn("step-1", toc_ids)
        self.assertIn("step-1-2", toc_ids)
        self.assertEqual(len(toc_ids), 3)
        self.assertIn('id="step-1"', str(detail["rendered_body_html"]))
        self.assertIn('id="step-1-2"', str(detail["rendered_body_html"]))

        resp = guide.serve(req)
        resp.render()
        html_text = resp.content.decode("utf-8")
        self.assertIn('href="#step-1"', html_text)
        self.assertIn('target="_self"', html_text)
        self.assertIn('href="#page-top"', html_text)
        self.assertIn('href="#article-start"', html_text)

    def test_four_level_tree_toc_hierarchy_and_skip_level(self):
        """测试二/三/四级（及兼容 h1 四级）树形目录构建、跳级挂载、快捷定位锚点与无JS兜底"""
        blog = BlogPage(
            title="四级树形目录测试",
            slug=f"tree-toc-{uuid4().hex[:6]}",
            authored_on=date(2026, 11, 22),
            body=[
                (
                    "markdown",
                    "## 一级章节\n"
                    "### 二级小节\n"
                    "#### 三级子节\n"
                    "## 跳级章节\n"
                    "#### 直接跳到四级标题\n",
                ),
            ],
        )
        self.home.add_child(instance=blog)
        blog.save_revision().publish()
        self.created_pages.append(blog)

        req = self.factory.get(blog.url_path)
        detail = build_detail_presentation(blog, req)
        tree = detail["toc_tree"]
        self.assertEqual(len(tree), 2)
        self.assertEqual(tree[0]["text"], "一级章节")
        self.assertEqual(tree[0]["depth"], 0)
        self.assertEqual(tree[0]["children"][0]["text"], "二级小节")
        self.assertEqual(tree[0]["children"][0]["depth"], 1)
        self.assertEqual(tree[0]["children"][0]["children"][0]["text"], "三级子节")
        self.assertEqual(tree[0]["children"][0]["children"][0]["depth"], 2)
        # 跳级不补空层：h4 直接挂到 h2 下
        self.assertEqual(tree[1]["text"], "跳级章节")
        self.assertEqual(tree[1]["children"][0]["text"], "直接跳到四级标题")

        resp = blog.serve(req)
        resp.render()
        html_text = resp.content.decode("utf-8")
        self.assertIn('data-toc-branch-toggle', html_text)
        self.assertIn('data-toc-sublist', html_text)
        self.assertIn('id="page-top"', html_text)
        self.assertIn('id="article-start"', html_text)
        self.assertIn('detail-toc-nojs-details', html_text)

    def test_detail_body_section_asset_selectors_and_portfolio_blocks_no_dl(self):
        """回归防线：公共代码高亮/图片灯箱脚本与样式必须包含 .detail-body-section；ProjectPage 积木不输出 <dl>"""
        hl_js = (PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/js/code-highlighting.js").read_text(encoding="utf-8")
        img_js = (PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/js/content-images.js").read_text(encoding="utf-8")
        img_css = (PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/css/content-images.css").read_text(encoding="utf-8")
        self.assertIn(".detail-body-section pre > code", hl_js)
        self.assertIn(".detail-body-section img", img_js)
        self.assertIn(".detail-body-section::after", img_css)

        proj_idx = PortfolioIndexPage(title="作品集回归", slug=f"port-reg-{uuid4().hex[:6]}")
        self.home.add_child(instance=proj_idx)
        proj_idx.save_revision().publish()
        self.created_pages.append(proj_idx)

        related = BlogPage(title="配套解析博文", slug=f"rel-blog-{uuid4().hex[:6]}")
        self.home.add_child(instance=related)
        related.save_revision().publish()
        self.created_pages.append(related)

        project = ProjectPage(
            title="作品积木渲染检查",
            slug=f"proj-clean-{uuid4().hex[:6]}",
            body=[
                ("tech_stack", [{"category": "backend", "name": "Django", "version": "6.1", "description": "Web 框架"}]),
                ("repository", {"platform": "github", "repo_url": "https://github.com/example/demo", "license_type": "MIT", "star_count": 42}),
                ("featured_posts", {"heading": "关联博文", "description": "推荐阅读", "posts": [related]}),
                ("markdown", "## 架构细节\n正文内容"),
            ],
        )
        proj_idx.add_child(instance=project)
        project.save_revision().publish()
        self.created_pages.append(project)

        req = self.factory.get(project.url_path)
        resp = project.serve(req)
        resp.render()
        html_text = resp.content.decode("utf-8")
        self.assertNotIn("<dl", html_text.lower())
        self.assertIn("portfolio-tech-item", html_text)
        self.assertIn("portfolio-repo-card", html_text)
        self.assertIn("featured-posts-widget", html_text)
        self.assertIn("配套解析博文", html_text)
        # 关联卡片内小标题不混入主 TOC 目录
        toc_fragment = html_text.split("detail-toc-tree")[1].split("</aside>")[0]
        self.assertIn("架构细节", toc_fragment)
        self.assertNotIn("关联博文", toc_fragment)
        self.assertNotIn("开源仓库与构建信息", toc_fragment)

    def test_project_page_detail_shell_and_recycled_isolation(self):
        """ProjectPage 接入共享壳层且保持回收站 404 隔离"""
        proj_idx = PortfolioIndexPage(title="作品集", slug=f"port-{uuid4().hex[:6]}")
        self.home.add_child(instance=proj_idx)
        proj_idx.save_revision().publish()
        self.created_pages.append(proj_idx)

        project = ProjectPage(
            title="项目详情共享壳层",
            slug=f"proj-shell-{uuid4().hex[:6]}",
            primary_language="Python",
            body=[("markdown", "## 项目背景\n高性能架构")],
        )
        proj_idx.add_child(instance=project)
        project.save_revision().publish()
        self.created_pages.append(project)

        req = self.factory.get(project.url_path)
        resp = project.serve(req)
        resp.render()
        html_text = resp.content.decode("utf-8")
        self.assertIn("detail-toc-aside", html_text)
        self.assertIn("项目背景", html_text)
        self.assertIn('data-theme-toggle', html_text)

        # 回收后仍严格 404
        RecycleService.recycle_page_or_subtree(project, system_authorized=True)
        project.refresh_from_db()
        with self.assertRaises(Http404):
            project.serve(req)

    def test_homepage_carousel_and_listing_ui_hooks(self):
        """首页渲染 Splide 轮播容器，列表筛选栏包含 Tom Select 与 Flatpickr 挂载钩子"""
        blog_idx = BlogIndexPage(title="技术专栏", slug=f"col-{uuid4().hex[:6]}")
        self.home.add_child(instance=blog_idx)
        blog_idx.save_revision().publish()
        self.created_pages.append(blog_idx)

        post = BlogPage(title="轮播展示文章", slug=f"car-{uuid4().hex[:6]}")
        blog_idx.add_child(instance=post)
        post.save_revision().publish()
        self.created_pages.append(post)

        home_req = self.factory.get("/")
        home_resp = self.home.serve(home_req)
        home_resp.render()
        home_html = home_resp.content.decode("utf-8")
        self.assertIn("data-ui-carousel", home_html)
        self.assertIn("splide__slide", home_html)

        idx_req = self.factory.get(blog_idx.url_path)
        idx_resp = blog_idx.serve(idx_req)
        idx_resp.render()
        idx_html = idx_resp.content.decode("utf-8")
        self.assertIn('id="listing-filter-form"', idx_html)
        self.assertIn('data-ui-select', idx_html)
        self.assertIn('data-ui-date="single"', idx_html)
