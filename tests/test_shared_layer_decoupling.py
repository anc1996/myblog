"""
共享层零页面硬编码架构守卫与通用注册表 API 回归测试。

落实 AGENTS.md「共享层零页面硬编码」铁律：
1. AST 静态校验：shared apps 严禁 import blog/guides/portfolio 模型或直接引用具体页面类名；
2. 行为校验：Sitemap、HomePage、Analytics 运营与安全搜索词、API 注册表通用路由均通过 Registry 正常工作。
"""

import ast
from pathlib import Path
from uuid import uuid4

from django.core.cache import cache
from django.test import TestCase
from wagtail.models import Page, Site

from blog.models import BlogIndexPage, BlogPage
from guides.models import GuideIndexPage, GuidePage, GuideTopic
from portfolio.models import PortfolioIndexPage, ProjectPage
from content_lifecycle.recycle import RecycleService
from discovery.registry import list_registered_page_models
from discovery.seo import PublicOnlySitemap
from home.models import HomePage

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SHARED_APPS = (
    "analytics",
    "api",
    "captcha",
    "comments",
    "content_blocks",
    "content_lifecycle",
    "discovery",
    "external_content",
    "home",
    "identity",
    "markdown_import",
    "media_library",
    "people",
    "search",
    "site_settings",
    "taxonomy",
)

FORBIDDEN_PAGE_APPS = {"blog", "guides", "portfolio"}
FORBIDDEN_PAGE_SYMBOLS = {
    "BlogPage",
    "BlogIndexPage",
    "GuidePage",
    "GuideIndexPage",
    "ProjectPage",
    "PortfolioIndexPage",
}


class SharedLayerDecouplingAstGuardTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_shared_apps_never_import_or_reference_concrete_page_models(self):
        """所有共享应用严禁直接导入 blog/guides/portfolio 模块或引用具体 Page 类名"""
        violations: list[str] = []
        for app_name in SHARED_APPS:
            app_dir = PROJECT_ROOT / "myblog" / "apps" / app_name
            for py_file in app_dir.rglob("*.py"):
                if "migrations" in py_file.parts or "__pycache__" in py_file.parts:
                    continue
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
                rel = py_file.relative_to(PROJECT_ROOT).as_posix()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            top = alias.name.split(".")[0]
                            if top in FORBIDDEN_PAGE_APPS:
                                violations.append(f"{rel}:{node.lineno} import {alias.name}")
                    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                        top = node.module.split(".")[0]
                        if top in FORBIDDEN_PAGE_APPS:
                            violations.append(f"{rel}:{node.lineno} from {node.module} import ...")
                    elif isinstance(node, ast.Name) and node.id in FORBIDDEN_PAGE_SYMBOLS:
                        violations.append(f"{rel}:{node.lineno} references {node.id}")
                    elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_PAGE_SYMBOLS:
                        violations.append(f"{rel}:{node.lineno} attribute .{node.attr}")

        self.assertEqual(violations, [], f"共享层检测到硬编码业务页面依赖：\n" + "\n".join(violations))

    def test_comments_hooks_never_hardcode_concrete_page_model_names(self):
        """评论后台钩子不得以小写模型名字符串硬编码过滤 blogpage/guidepage/projectpage"""
        hooks_src = (PROJECT_ROOT / "myblog/apps/comments/wagtail_hooks.py").read_text(encoding="utf-8")
        for bad in ('"blogpage"', '"guidepage"', '"projectpage"', "'blogpage'", "'guidepage'", "'projectpage'"):
            self.assertNotIn(bad, hooks_src)

    def test_registered_page_models_and_sitemap_decoupled(self):
        """TypeAdapter 注册表动态解析全部已注册页面模型，且 Sitemap 自动排除回收页"""
        models = list_registered_page_models()
        self.assertIn(BlogPage, models)
        self.assertIn(BlogIndexPage, models)

        home = HomePage.objects.first()
        if not home:
            root = Page.get_first_root_node() or Page.add_root(title="Root")
            home = HomePage(title="Home", slug=f"home-{uuid4().hex[:6]}")
            root.add_child(instance=home)
            home.save_revision().publish()
        if not Site.objects.filter(is_default_site=True).exists():
            Site.objects.create(hostname="localhost", port=80, root_page=home, is_default_site=True)

        live_post = BlogPage(title="Sitemap Live", slug=f"sm-live-{uuid4().hex[:6]}")
        home.add_child(instance=live_post)
        live_post.save_revision().publish()

        recycled_post = BlogPage(title="Sitemap Recycled", slug=f"sm-rec-{uuid4().hex[:6]}")
        home.add_child(instance=recycled_post)
        recycled_post.save_revision().publish()
        RecycleService.recycle_page_or_subtree(recycled_post, system_authorized=True)

        sitemap_ids = set(PublicOnlySitemap().items().values_list("id", flat=True))
        self.assertIn(live_post.id, sitemap_ids)
        self.assertNotIn(recycled_post.id, sitemap_ids)

    def test_generic_content_registry_api_endpoints(self):
        """通用 /api/v1/content/<type_slug>/ 与详情接口可无缝服务已注册页面类型"""
        from django.test import Client

        client = Client(HTTP_HOST="localhost")
        home = HomePage.objects.first()
        if not home:
            root = Page.get_first_root_node() or Page.add_root(title="Root")
            home = HomePage(title="Home", slug=f"home-{uuid4().hex[:6]}")
            root.add_child(instance=home)
            home.save_revision().publish()
        if not Site.objects.filter(is_default_site=True).exists():
            Site.objects.create(hostname="localhost", port=80, root_page=home, is_default_site=True)

        post = BlogPage(
            title="通用接口文章",
            slug=f"gen-api-{uuid4().hex[:6]}",
            body=[("markdown", {"markdown": "hello generic api"})],
        )
        home.add_child(instance=post)
        post.save_revision().publish()

        resp_list = client.get("/api/v1/content/blog/")
        self.assertEqual(resp_list.status_code, 200)
        slugs = [it["slug"] for it in resp_list.json()["items"]]
        self.assertIn(post.slug, slugs)

        resp_detail = client.get(f"/api/v1/content/blog/{post.slug}/")
        self.assertEqual(resp_detail.status_code, 200)
        self.assertEqual(resp_detail.json()["data"]["id"], post.id)
        self.assertIn("hello generic api", resp_detail.json()["data"]["safe_html"])

        resp_unknown = client.get("/api/v1/content/not-exists-type/")
        self.assertEqual(resp_unknown.status_code, 404)

    def test_legacy_typed_api_endpoints_backward_compatible(self):
        """通过注册表重构后的 /api/v1/{posts,guides,projects}/ 保持原有 JSON 契约兼容"""
        from django.test import Client

        client = Client(HTTP_HOST="localhost")
        home = HomePage.objects.first()
        if not home:
            root = Page.get_first_root_node() or Page.add_root(title="Root")
            home = HomePage(title="Home", slug=f"home-{uuid4().hex[:6]}")
            root.add_child(instance=home)
            home.save_revision().publish()
        if not Site.objects.filter(is_default_site=True).exists():
            Site.objects.create(hostname="localhost", port=80, root_page=home, is_default_site=True)

        post = BlogPage(title="Compat Post", slug=f"cp-{uuid4().hex[:6]}", body=[("markdown", {"markdown": "post body"})])
        home.add_child(instance=post)
        post.save_revision().publish()

        g_idx = GuideIndexPage(title="Guides", slug=f"gi-{uuid4().hex[:6]}")
        home.add_child(instance=g_idx)
        g_idx.save_revision().publish()
        topic = GuideTopic.objects.create(name="CompatTopic", slug=f"ct-{uuid4().hex[:6]}")
        guide = GuidePage(
            title="Compat Guide",
            slug=f"cg-{uuid4().hex[:6]}",
            topic=topic,
            body=[("step", {"step_number": "1", "title": "S1", "content": "<p>ok</p>"})],
        )
        g_idx.add_child(instance=guide)
        guide.save_revision().publish()

        p_idx = PortfolioIndexPage(title="Ports", slug=f"pi-{uuid4().hex[:6]}")
        home.add_child(instance=p_idx)
        p_idx.save_revision().publish()
        proj = ProjectPage(
            title="Compat Proj",
            slug=f"cproj-{uuid4().hex[:6]}",
            primary_language="Python",
            body=[("markdown", {"markdown": "proj body"})],
        )
        p_idx.add_child(instance=proj)
        proj.save_revision().publish()

        r_posts = client.get("/api/v1/posts/")
        self.assertEqual(r_posts.status_code, 200)
        self.assertIn(post.slug, [x["slug"] for x in r_posts.json()["items"]])
        self.assertEqual(client.get(f"/api/v1/posts/{post.slug}/").status_code, 200)

        r_guides = client.get("/api/v1/guides/")
        self.assertEqual(r_guides.status_code, 200)
        self.assertIn(guide.slug, [x["slug"] for x in r_guides.json()["items"]])
        self.assertEqual(client.get(f"/api/v1/guides/{guide.slug}/").status_code, 200)

        r_projs = client.get("/api/v1/projects/")
        self.assertEqual(r_projs.status_code, 200)
        self.assertIn(proj.slug, [x["slug"] for x in r_projs.json()["data"]])
        self.assertEqual(client.get(f"/api/v1/projects/{proj.slug}/").status_code, 200)
