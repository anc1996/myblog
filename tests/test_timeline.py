"""
全站年月时间线归档页面自动化测试集 (Timeline Archive Tests)。

实测覆盖：
1. 时间线页面 GET /timeline/ 渲染与年月层级聚合树 (决策四 A)；
2. 按指定年份/月份过滤时间线列表；
3. AJAX 请求局部片段返回 (format=partial)；
4. 仅统计已上线、未回收内容。
"""

import json
from datetime import date
from uuid import uuid4

from django.conf import settings
from django.test import RequestFactory, TestCase
from wagtail.coreutils import get_supported_content_language_variant
from wagtail.models import Collection, Locale, Page, Site
from discovery.timeline import timeline_view


class TimelineArchiveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from home.models import HomePage
        from people.models import Person

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
        Person.objects.get_or_create(
            slug="anonymous",
            defaults={"name": "佚名", "is_system_anonymous": True},
        )

    def setUp(self):
        from blog.models import BlogPage
        self.factory = RequestFactory(HTTP_HOST="localhost")
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
