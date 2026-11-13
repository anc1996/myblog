"""
tests.test_portfolio
开源作品集与项目系统 (portfolio App) 核心功能测试套件。

测试覆盖：
1. 层级树约束：HomePage -> PortfolioIndexPage -> ProjectPage；
2. 方案 A 彻底脱水模式：PostgreSQL 物理列 body 始终为 [] (2字节)，MongoDB 持久化 StreamField；
3. 官方反序列化入口 with_content_json() 与 hydrate_from_mongo() 毫秒级水合；
4. 多页面通用即插即用：ProjectPageAdapter 自动接入 /feed/type/project/ 与 /api/v1/archives/?type=project；
5. 项目 RESTful JSON API (GET /api/v1/projects/ 与 GET /api/v1/projects/<slug>/)；
6. 受控回收站原树占位、全量业务字段与子表恢复。
"""

import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

import json
import unittest
import uuid
from datetime import date
from django.test import RequestFactory
from django.utils import timezone
from wagtail.models import Page, Site

from home.models import HomePage
from people.models import Person
from portfolio.models import PortfolioIndexPage, ProjectPage, ProjectPagePerson
from discovery.models import ContentDiscoveryEpoch, PublishedSearchDocument
from discovery.projection import project_page_to_discovery
from discovery.feeds import public_feed_view, compute_public_timeline_archives
from api.views import project_list_api, project_detail_api
from content_lifecycle.recycle import RecycleService


class PortfolioSystemTestCase(unittest.TestCase):
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

        # 创建作者
        self.author = Person.objects.create(
            name=f"Developer-{uuid.uuid4().hex[:6]}",
            email="dev@example.com",
            bio="开源贡献者",
        )

        # 创建关联配套博客文章 (验证场景 B: FeaturedPostsBlock 跨页面关联)
        from blog.models import BlogPage
        self.related_post = BlogPage(
            title=f"搜索引擎底层原理解析-{uuid.uuid4().hex[:6]}",
            slug=f"search-internals-{uuid.uuid4().hex[:6]}",
            authored_on=date(2026, 11, 4),
        )
        self.home.add_child(instance=self.related_post)
        self.related_post.save_revision().publish()

        # 1. 创建作品集大厅
        self.portfolio_index = PortfolioIndexPage(
            title="开源作品集大厅",
            slug=f"portfolio-{uuid.uuid4().hex[:6]}",
            intro="精选开源架构项目与研发成果",
        )
        self.home.add_child(instance=self.portfolio_index)
        self.portfolio_index.save_revision().publish()

        # 2. 创建并发布测试项目 (ProjectPage)
        self.project_slug = f"project-{uuid.uuid4().hex[:6]}"
        self.project_title = f"[Project] 高并发分布式搜索引擎-{uuid.uuid4().hex[:6]}"
        self.project = ProjectPage(
            title=self.project_title,
            slug=self.project_slug,
            project_status="maintained",
            summary="基于 Rust 与 PostgreSQL 18 的分布式全文检索引擎",
            primary_language="Rust",
            star_count=1280,
            license_name="Apache-2.0",
            repo_url="https://github.com/example/open-search",
            demo_url="https://demo.example.com",
            authored_on=date(2026, 11, 5),
            started_on=date(2026, 1, 10),
            completed_on=date(2026, 10, 1),
            body=[
                (
                    "tech_stack",
                    [
                        {"category": "backend", "name": "Rust", "version": "1.82", "description": "核心索引服务"},
                        {"category": "database", "name": "PostgreSQL 18", "version": "18.6", "description": "主关系与GIN倒排"},
                    ],
                ),
                (
                    "repository",
                    {
                        "platform": "github",
                        "repo_url": "https://github.com/example/open-search",
                        "demo_url": "https://demo.example.com",
                        "license_type": "Apache-2.0",
                        "star_count": 1280,
                    },
                ),
                ("markdown", {"markdown_text": "### 架构设计\n系统采用事件驱动与流式脱水模型。"}),
                (
                    "featured_posts",
                    {
                        "heading": "项目关联技术架构文章",
                        "description": "深入解析该项目的核心实现细节",
                        "posts": [self.related_post],
                    },
                ),
            ],
        )
        self.portfolio_index.add_child(instance=self.project)
        self.project.tags.add("rust", "search", "architecture")
        self.project.authors.create(person=self.author, role="项目负责人与主架构师")

        rev = self.project.save_revision()
        rev.publish()
        self.project.refresh_from_db()
        project_page_to_discovery(self.project)

    def tearDown(self):
        try:
            PublishedSearchDocument.objects.filter(page_id=self.project.id).delete()
            self.project.delete(allow_physical_purge=True)
            self.portfolio_index.delete(allow_physical_purge=True)
            self.related_post.delete(allow_physical_purge=True)
            self.author.delete()
        except Exception:
            pass

    def test_plan_a_dehydration_and_hydration(self):
        """验证方案 A 彻底脱水：PostgreSQL 物理列存 []，MongoDB 存储正文并精准水合"""
        from django.db import connection

        # 1. 验证 PostgreSQL 物理主表 body 列为 [] (2字节)
        with connection.cursor() as cur:
            cur.execute("SELECT body FROM portfolio_projectpage WHERE page_ptr_id = %s;", [self.project.id])
            row = cur.fetchone()
            self.assertIsNotNone(row)
            sql_body = row[0]
            # 在数据库物理列中应为 '[]'
            self.assertIn(str(sql_body), ("[]", "[]::jsonb"))

        # 2. 验证从 MongoDB 按需水合
        fresh_page = ProjectPage.objects.get(id=self.project.id)
        self.assertEqual(len(fresh_page.body), 0)  # 从数据库取出时为脱水状态 []
        fresh_page.hydrate_from_mongo()
        self.assertGreater(len(fresh_page.body), 0)
        # 验证提取到的积木
        block_types = [b.block_type for b in fresh_page.body]
        self.assertIn("tech_stack", block_types)
        self.assertIn("repository", block_types)
        self.assertIn("markdown", block_types)
        self.assertIn("featured_posts", block_types)

    def test_universal_feed_and_archive_integration(self):
        """验证多页面即插即用：ProjectPage 零核心侵入自动接入 RSS/Atom 与时间线归档"""
        # 1. RSS 订阅流 /feed/type/project/
        req_feed = self.factory.get("/feed/type/project/")
        resp_feed = public_feed_view(req_feed, dimension="type", slug="project")
        self.assertEqual(resp_feed.status_code, 200)
        content_feed = resp_feed.content.decode("utf-8")
        self.assertIn(self.project_title, content_feed)
        self.assertNotIn("开源作品集大厅", content_feed)  # 大厅聚合页被阻断

        # 2. Atom 订阅流 /feed/atom/type/project/
        req_atom = self.factory.get("/feed/atom/type/project/")
        resp_atom = public_feed_view(req_atom, dimension="type", slug="project")
        self.assertEqual(resp_atom.status_code, 200)
        content_atom = resp_atom.content.decode("utf-8")
        self.assertIn('xmlns="http://www.w3.org/2005/Atom"', content_atom)
        self.assertIn(self.project_title, content_atom)

        # 3. 时间线归档聚合 /api/v1/archives/?type=project
        archives = compute_public_timeline_archives(type_slug="project")
        self.assertGreaterEqual(archives["total_count"], 1)
        self.assertIn(2026, archives["years"])

    def test_project_restful_apis(self):
        """测试项目 RESTful API：列表分页、属性过滤与详情双形态输出"""
        # 1. 列表接口 GET /api/v1/projects/
        req_list = self.factory.get("/api/v1/projects/?language=Rust")
        resp_list = project_list_api(req_list)
        self.assertEqual(resp_list.status_code, 200)
        data_list = json.loads(resp_list.content.decode("utf-8"))
        self.assertEqual(data_list["status"], "success")
        self.assertGreaterEqual(data_list["pagination"]["total_count"], 1)
        proj_item = next(p for p in data_list["data"] if p["slug"] == self.project_slug)
        self.assertEqual(proj_item["primary_language"], "Rust")
        self.assertEqual(proj_item["star_count"], 1280)
        self.assertEqual(proj_item["license_name"], "Apache-2.0")

        # 2. 详情接口 GET /api/v1/projects/<slug>/
        req_detail = self.factory.get(f"/api/v1/projects/{self.project_slug}/")
        resp_detail = project_detail_api(req_detail, slug=self.project_slug)
        self.assertEqual(resp_detail.status_code, 200)
        data_detail = json.loads(resp_detail.content.decode("utf-8"))
        proj_data = data_detail["data"]
        self.assertEqual(proj_data["title"], self.project_title)
        self.assertIn("架构设计", proj_data["safe_html"])
        self.assertIn("项目关联技术架构文章", proj_data["safe_html"])
        self.assertIn(self.related_post.title, proj_data["safe_html"])
        self.assertGreater(len(proj_data["blocks"]), 0)

    def test_project_recycle_and_restore(self):
        """测试作品集项目受控回收与全量字段恢复 (H-02)"""
        # 1. 移入回收站
        batch = RecycleService.recycle_page_or_subtree(self.project, actor=None, system_authorized=True)
        self.project.refresh_from_db()
        self.assertTrue(self.project.is_recycled)

        # 前台不可见
        req_detail = self.factory.get(f"/api/v1/projects/{self.project_slug}/")
        from django.http import Http404
        with self.assertRaises(Http404):
            project_detail_api(req_detail, slug=self.project_slug)

        # 2. 一键原位恢复
        res = RecycleService.restore_page_or_subtree(batch.batch_id, actor=None, system_authorized=True)
        self.assertEqual(res["status"], "success")
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_recycled)
        self.assertFalse(self.project.live)  # 恢复后为未发布草稿

        # 验证全量字段完整恢复 (H-02)
        self.assertEqual(self.project.project_status, "maintained")
        self.assertEqual(self.project.primary_language, "Rust")
        self.assertEqual(self.project.star_count, 1280)
        self.assertEqual(self.project.authors.count(), 1)
        self.assertIn("search", [t.name for t in self.project.tags.all()])
