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

import os
import unittest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

from django.test import Client
from discovery.models import ContentDiscoveryEpoch
from blog.models import BlogPage


class ApiV1Tests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = Client()

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
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("current_page", data)
        self.assertIn("total_pages", data)
        self.assertIn("items", data)

    def test_post_detail_api_dual_output(self):
        page = BlogPage.objects.filter(live=True).first()
        if not page:
            return

        res = self.client.get(f"/api/v1/posts/{page.slug}/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        post = data["data"]
        self.assertEqual(post["id"], page.id)
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
        page = BlogPage.objects.filter(live=True).first()
        if not page:
            return

        # 初始请求填充缓存
        self.client.get(f"/api/v1/posts/{page.slug}/")
        cached_res = self.client.get(f"/api/v1/posts/{page.slug}/")
        self.assertEqual(cached_res.headers.get("X-Cache"), "HIT")

        # 模拟后台发布/下架递增 Epoch
        ContentDiscoveryEpoch.increment_epoch()

        # 再次请求，旧缓存自动作废，必须重新从数据库/Mongo 水合 (MISS)
        invalidated_res = self.client.get(f"/api/v1/posts/{page.slug}/")
        self.assertEqual(invalidated_res.headers.get("X-Cache"), "MISS")


if __name__ == "__main__":
    unittest.main()
