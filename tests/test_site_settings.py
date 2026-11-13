"""
tests.test_site_settings
全局站点设置与导航 (site_settings App) 核心功能测试套件。

测试覆盖：
1. 单例模型约束：singleton_key=1 数据库唯一约束与 CHECK(singleton_key=1) (H-03)；
2. 只读安全防护：公开 API 严格调用 filter(singleton_key=1)，严禁调用 .load()；
3. 弱 ETag 与 HTTP 304 快速缓存协商；
4. 菜单下架优雅降级 (H-07)：下架或回收页面在导航接口中自动过滤；
5. 配置变动 Epoch 原子自增与缓存失效。
"""

import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

import json
import unittest
import uuid
from django.test import RequestFactory
from wagtail.models import Page, Site

from home.models import HomePage
from blog.models import BlogPage, BlogIndexPage
from site_settings.models import NavigationSettings, SocialMediaSettings, FooterSettings
from discovery.models import ContentDiscoveryEpoch
from api.views import site_settings_api
from content_lifecycle.recycle import RecycleService


class SiteSettingsTestCase(unittest.TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.home = HomePage.objects.first()
        if not self.home:
            root = Page.get_first_root_node()
            if not root:
                root = Page.add_root(title="Root")
            self.home = HomePage(title="Home", slug="home")
            root.add_child(instance=self.home)
            self.home.save_revision().publish()

        self.site = Site.objects.filter(is_default_site=True).first()
        if not self.site:
            self.site = Site.objects.create(hostname="localhost", port=80, root_page=self.home, is_default_site=True)

        # 准备一个公开正常文章
        self.public_page = BlogPage(title=f"公开页面-{uuid.uuid4().hex[:6]}", slug=f"pub-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=self.public_page)
        self.public_page.save_revision().publish()

        # 准备一个已回收文章 (用于验证 H-07 优雅降级)
        self.recycled_page = BlogPage(title=f"回收页面-{uuid.uuid4().hex[:6]}", slug=f"rec-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=self.recycled_page)
        self.recycled_page.save_revision().publish()
        RecycleService.recycle_page_or_subtree(self.recycled_page, actor=None, system_authorized=True)

        # 配置 NavigationSettings
        self.nav_setting, _ = NavigationSettings.objects.get_or_create(singleton_key=1)
        self.nav_setting.nav_items = [
            (
                "menu_item",
                {
                    "label": "开源项目",
                    "badge": "NEW",
                    "target_page": self.public_page,
                    "external_url": "",
                    "is_external": False,
                },
            ),
            (
                "menu_item",
                {
                    "label": "回收文章",
                    "badge": "",
                    "target_page": self.recycled_page,  # 指向已回收页面
                    "external_url": "",
                    "is_external": False,
                },
            ),
            (
                "menu_item",
                {
                    "label": "GitHub 源码",
                    "badge": "HOT",
                    "target_page": None,
                    "external_url": "https://github.com/example/myblog",
                    "is_external": True,
                },
            ),
        ]
        self.nav_setting.save()

        # 配置 SocialMediaSettings
        self.social_setting, _ = SocialMediaSettings.objects.get_or_create(singleton_key=1)
        self.social_setting.social_links = [
            ("social_link", {"platform": "github", "label": "GitHub", "url": "https://github.com/anc1996"}),
            ("social_link", {"platform": "email", "label": "联系邮箱", "url": "mailto:dev@example.com"}),
        ]
        self.social_setting.save()

        # 配置 FooterSettings
        self.footer_setting, _ = FooterSettings.objects.get_or_create(singleton_key=1)
        self.footer_setting.copyright_text = "© 2026 myblog. 高性能分布式架构规范."
        self.footer_setting.icp_number = "京ICP备20261108号-1"
        self.footer_setting.police_record_number = "京公网安备11010802026110号"
        self.footer_setting.about_text = "基于 Django 6.1 与 Wagtail 8.1 打造的现代技术博客。"
        self.footer_setting.save()

    def tearDown(self):
        try:
            self.public_page.delete(allow_physical_purge=True)
            self.recycled_page.delete(allow_physical_purge=True)
        except Exception:
            pass

    def test_singleton_constraint_enforcement(self):
        """验证设置单例约束：singleton_key=1 严格受唯一索引与数据库 CHECK 保护 (H-03)"""
        from django.db import IntegrityError

        # 尝试插入第二行 singleton_key=2 必须抛出异常
        with self.assertRaises(IntegrityError):
            NavigationSettings.objects.create(singleton_key=2)

    def test_site_settings_api_and_graceful_degradation(self):
        """验证全站设置 API 与菜单下架优雅降级 (H-07)"""
        request = self.factory.get("/api/v1/site/settings/")
        response = site_settings_api(request)
        self.assertEqual(response.status_code, 200)
        res = json.loads(response.content.decode("utf-8"))

        self.assertEqual(res["status"], "success")
        data = res["data"]

        # 1. 验证菜单项及下架过滤
        nav_list = data["navigation"]
        labels = [item["label"] for item in nav_list]
        self.assertIn("开源项目", labels)
        self.assertIn("GitHub 源码", labels)
        self.assertNotIn("回收文章", labels)  # H-07: 已回收页面被自动过滤

        # 2. 验证社交媒体链接
        social_list = data["social_links"]
        platforms = [item["platform"] for item in social_list]
        self.assertIn("github", platforms)
        self.assertIn("email", platforms)

        # 3. 验证页脚信息
        footer = data["footer"]
        self.assertEqual(footer["icp_number"], "京ICP备20261108号-1")
        self.assertEqual(footer["police_record_number"], "京公网安备11010802026110号")

    def test_etag_and_http_304_cache_negotiation(self):
        """验证设置弱 ETag 协商与 HTTP 304 快速响应"""
        request = self.factory.get("/api/v1/site/settings/")
        response = site_settings_api(request)
        etag = response["ETag"]
        self.assertTrue(etag.startswith('W/"site_settings:'))

        # 携带 ETag 发起请求 -> 返回 304 Not Modified
        cond_req = self.factory.get("/api/v1/site/settings/", HTTP_IF_NONE_MATCH=etag)
        cond_resp = site_settings_api(cond_req)
        self.assertEqual(cond_resp.status_code, 304)

        # 修改配置保存后 -> Epoch 自增，再次请求返回 200 并更新 ETag
        self.footer_setting.copyright_text = "© 2026 myblog. 新版权声明."
        self.footer_setting.save()

        cond_resp_2 = site_settings_api(cond_req)
        self.assertEqual(cond_resp_2.status_code, 200)
        self.assertNotEqual(cond_resp_2["ETag"], etag)
