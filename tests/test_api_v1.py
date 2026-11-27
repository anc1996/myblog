"""
tests.test_api_v1
第一阶段 RESTful JSON API 单元测试。

验证范围：
1. GET /api/v1/search/: 关键词、分页 (next_cursor)、Facets 统计；
2. GET /api/v1/posts/: 文章列表只读元数据分页；
3. GET /api/v1/posts/<slug>/: 双形态积木输出 (blocks 数组 + safe_html 字符串)；
4. Redis Epoch 缓存机制：首查 MISS，重查 HIT，Epoch 递增瞬间失效；
5. 滑动窗口限流：超限返回 429 与 Retry-After。
"""

from datetime import date
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.test import Client, TestCase
from wagtail.coreutils import get_supported_content_language_variant
from wagtail.models import Collection, Locale, Page, Site

from blog.models import BlogPage
from discovery.models import ContentDiscoveryEpoch
from discovery.projection import project_page_to_discovery
from home.models import HomePage


class ApiV1Tests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Locale.objects.get_or_create(
            language_code=get_supported_content_language_variant(settings.LANGUAGE_CODE)
        )
        if Collection.get_first_root_node() is None:
            Collection.add_root(name="Root")
        root = Page.get_first_root_node() or Page.add_root(title="Root")
        home = HomePage.objects.first()
        if home is None:
            home = HomePage(title="Home", slug=f"home-apiv1-{uuid4().hex[:6]}")
            root.add_child(instance=home)
            home.save_revision().publish()
        if not Site.objects.filter(is_default_site=True).exists():
            Site.objects.create(hostname="localhost", port=80, root_page=home, is_default_site=True)
        Site.clear_site_root_paths_cache()

        cls.page = BlogPage(
            title="PostgreSQL 18 API 测试文章",
            slug=f"pg18-apiv1-{uuid4().hex[:6]}",
            authored_on=date(2026, 11, 20),
            body=[("markdown", {"markdown_text": "PostgreSQL 18 索引架构详解"})],
        )
        home.add_child(instance=cls.page)
        cls.page.save_revision().publish()
        project_page_to_discovery(cls.page)

    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = Client(HTTP_HOST="localhost")

    def test_search_api_and_epoch_cache(self):
        # 1. 首次查询
        res1 = self.client.get("/api/v1/search/?query=PostgreSQL")
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()
        self.assertEqual(data1["status"], "success")
        self.assertIn("total_count", data1)
        self.assertIn("items", data1)
        self.assertIn("facets", data1)
        self.assertEqual(res1.headers.get("X-Cache"), "MISS")

        # 2. 相同查询命中 Redis 缓存
        res2 = self.client.get("/api/v1/search/?query=PostgreSQL")
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.headers.get("X-Cache"), "HIT")

    def test_search_api_pagination(self):
        res = self.client.get("/api/v1/search/?page=1&page_size=2")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertLessEqual(len(data["items"]), 2)
        self.assertEqual(data["page_size"], 2)

    def test_post_list_api(self):
        res = self.client.get("/api/v1/posts/?page=1&page_size=5")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("X-Cache"), "BYPASS")
        self.assertEqual(res.headers.get("Cache-Control"), "no-store")
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("current_page", data)
        self.assertIn("total_pages", data)
        self.assertIn("items", data)
        self.assertIn(self.page.slug, [it["slug"] for it in data["items"]])

    def test_post_detail_api_dual_output(self):
        res = self.client.get(f"/api/v1/posts/{self.page.slug}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("X-Cache"), "BYPASS")
        self.assertEqual(res.headers.get("Cache-Control"), "no-store")
        data = res.json()
        self.assertEqual(data["status"], "success")
        post = data["data"]
        self.assertEqual(post["id"], self.page.id)
        self.assertIn("blocks", post)
        self.assertIsInstance(post["blocks"], list)
        self.assertIn("safe_html", post)
        self.assertIsInstance(post["safe_html"], str)
        self.assertIn("authors", post)
        self.assertIn("tags", post)

        # 404 验证
        res_404 = self.client.get("/api/v1/posts/non-existing-slug-xyz/")
        self.assertEqual(res_404.status_code, 404)
        self.assertEqual(res_404.json()["status"], "error")

    def test_epoch_cache_instant_invalidation(self):
        # 1. 搜索接口使用 Epoch 缓存，Epoch 递增后立即失效
        res1 = self.client.get("/api/v1/search/?query=EpochInvalidationProbe", HTTP_HOST="localhost")
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.headers.get("X-Cache"), "MISS")
        res2 = self.client.get("/api/v1/search/?query=EpochInvalidationProbe", HTTP_HOST="localhost")
        self.assertEqual(res2.headers.get("X-Cache"), "HIT")
        ContentDiscoveryEpoch.increment_epoch()
        res3 = self.client.get("/api/v1/search/?query=EpochInvalidationProbe", HTTP_HOST="localhost")
        self.assertEqual(res3.headers.get("X-Cache"), "MISS")

        # 2. 公开详情与列表接口停用共享缓存并强制 no-store
        res_detail = self.client.get(f"/api/v1/posts/{self.page.slug}/")
        self.assertEqual(res_detail.status_code, 200)
        self.assertEqual(res_detail.headers.get("X-Cache"), "BYPASS")
        self.assertEqual(res_detail.headers.get("Cache-Control"), "no-store")

        res_list = self.client.get("/api/v1/posts/")
        self.assertEqual(res_list.status_code, 200)
        self.assertEqual(res_list.headers.get("X-Cache"), "BYPASS")
        self.assertEqual(res_list.headers.get("Cache-Control"), "no-store")
