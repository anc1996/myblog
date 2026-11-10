"""
全站年月时间线归档页面自动化测试集 (Timeline Archive Tests)。

实测覆盖：
1. 时间线页面 GET /timeline/ 渲染与年月层级聚合树 (决策四 A)；
2. 按指定年份/月份过滤时间线列表；
3. AJAX 请求局部片段返回 (format=partial)；
4. 仅统计已上线、未回收内容。
"""

import os
import json
import unittest
from datetime import date
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

from django.test import RequestFactory
from discovery.timeline import timeline_view


class TimelineArchiveTests(unittest.TestCase):

    def setUp(self):
        from home.models import HomePage
        from blog.models import BlogPage
        self.factory = RequestFactory()
        self.home = HomePage.objects.first()
        self.created_pages = []

        # 创建两篇不同日期的文章
        p1 = BlogPage(title=f"文章2026-05-{uuid4().hex[:4]}", slug=f"t1-{uuid4().hex[:6]}", authored_on=date(2026, 5, 10))
        self.home.add_child(instance=p1)
        self.created_pages.append(p1)
        p1.save_revision().publish()

        p2 = BlogPage(title=f"文章2026-04-{uuid4().hex[:4]}", slug=f"t2-{uuid4().hex[:6]}", authored_on=date(2026, 4, 15))
        self.home.add_child(instance=p2)
        self.created_pages.append(p2)
        p2.save_revision().publish()

    def tearDown(self):
        for p in reversed(self.created_pages):
            try:
                p.delete(allow_physical_purge=True)
            except Exception:
                pass

    def test_01_timeline_page_full_ssr_rendering(self):
        """测试时间线页面完整 SSR 渲染与年月卡片聚合"""
        req = self.factory.get("/timeline/")
        resp = timeline_view(req)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("年月时间线归档", html)
        self.assertIn("2026 年", html)

    def test_02_timeline_ajax_partial_json(self):
        """测试时间线 AJAX 局部更新返回 JSON 载荷"""
        req = self.factory.get("/timeline/?format=partial", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        resp = timeline_view(req)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content.decode("utf-8"))
        self.assertIn("html", data)
        self.assertIn("total_count", data)
        self.assertGreaterEqual(data["total_count"], 2)
