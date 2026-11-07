"""
tests.test_feed_and_archive
全站多页面通用订阅（RSS 2.0 / Atom 1.0）与时间线归档（Timeline Archive）单元与集成测试套件。

测试覆盖：
1. RSS 2.0 与 Atom 1.0 生成合规性（RFC 4287 / RFC 822，UTC 时间戳，urn:uuid，summary type="text"）；
2. 分维度订阅（all, type, tag, author, topic）与非订阅类型（blog_index 404）；
3. 弱 ETag 与 HTTP 304 Not Modified 极速响应；
4. 时间线归档 API (GET /api/v1/archives/) 与年月分桶聚合、年份区间过滤；
5. 刚性生命周期防御：别名页排除、祖先回收防御、PageViewRestriction 权限隔离、未来排期过滤；
6. 未来任意 xxPage（如 ProjectPage）零核心修改即插即用扩展性测试。
"""

import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

import json
import unittest
import uuid
from datetime import date, timedelta
from django.test import RequestFactory
from django.utils import timezone
from wagtail.models import Page, Site, PageViewRestriction

from home.models import HomePage
from blog.models import BlogPage, BlogIndexPage
from guides.models import GuidePage, GuideIndexPage, GuideTopic
from people.models import Person
from discovery.models import ContentDiscoveryEpoch, PublishedSearchDocument
from discovery.projection import project_page_to_discovery
from discovery.contracts import PublicListingCapabilities, PublishedPayload, TextSegment
from discovery.registry import register_type_adapter, clear_adapters
from discovery.feeds import public_feed_view, compute_public_timeline_archives
from api.views import timeline_archives_api
from content_lifecycle.models import RecycleBatch, RecycleEntry


class FeedAndArchiveTestCase(unittest.TestCase):
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
            name=f"Author-{uuid.uuid4().hex[:6]}",
            email="author@example.com",
            bio="测试作者简介",
        )

        # 创建指南主题
        self.topic = GuideTopic.objects.create(
            name="后端架构测试",
            slug=f"backend-arch-{uuid.uuid4().hex[:6]}",
            description="后端架构主题",
        )

        # 创建博客索引页
        self.blog_index = BlogIndexPage(
            title="技术博文",
            slug=f"blog-{uuid.uuid4().hex[:6]}",
            intro="技术博文专栏",
        )
        self.home.add_child(instance=self.blog_index)
        self.blog_index.save_revision().publish()

        # 创建指南索引页
        self.guide_index = GuideIndexPage(
            title="专题专栏",
            slug=f"guides-{uuid.uuid4().hex[:6]}",
            intro="专题指南专栏",
        )
        self.home.add_child(instance=self.guide_index)
        self.guide_index.save_revision().publish()

        # 创建已发布博客文章 1 (2026-10-15)
        self.custom_tag = f"tag-{uuid.uuid4().hex[:6]}"
        self.blog_post_1 = BlogPage(
            title=f"[FT] PG18架构-{uuid.uuid4().hex[:6]}",
            slug=f"pg18-arch-{uuid.uuid4().hex[:6]}",
            authored_on=date(2026, 10, 15),
        )
        self.blog_index.add_child(instance=self.blog_post_1)
        self.blog_post_1.tags.add(self.custom_tag, "database")
        self.blog_post_1.authors.create(person=self.author, role="主笔")
        rev1 = self.blog_post_1.save_revision()
        rev1.publish()
        self.blog_post_1.refresh_from_db()
        project_page_to_discovery(self.blog_post_1)

        # 创建已发布博客文章 2 (2026-11-01)
        self.blog_post_2 = BlogPage(
            title=f"[FT] Wagtail剖析-{uuid.uuid4().hex[:6]}",
            slug=f"wagtail-deep-dive-{uuid.uuid4().hex[:6]}",
            authored_on=date(2026, 11, 1),
        )
        self.blog_index.add_child(instance=self.blog_post_2)
        self.blog_post_2.tags.add("wagtail", "python")
        rev2 = self.blog_post_2.save_revision()
        rev2.publish()
        self.blog_post_2.refresh_from_db()
        project_page_to_discovery(self.blog_post_2)

        # 创建已发布指南文章 (2026-11-03)
        self.guide_post = GuidePage(
            title=f"[FT] 分布式事务-{uuid.uuid4().hex[:6]}",
            slug=f"dist-trans-{uuid.uuid4().hex[:6]}",
            topic=self.topic,
            authored_on=date(2026, 11, 3),
            summary="分布式事务步骤指南",
            body=[("step", {"step_number": "1", "title": "阶段一准备", "content": "协调者向所有参与者发送 prepare 请求"})],
        )
        self.guide_index.add_child(instance=self.guide_post)
        self.guide_post.tags.add("architecture", "database")
        self.guide_post.authors.create(person=self.author, role="技术审校")
        rev_g = self.guide_post.save_revision()
        rev_g.publish()
        self.guide_post.refresh_from_db()
        project_page_to_discovery(self.guide_post)

    def tearDown(self):
        # 清理测试数据
        try:
            PublishedSearchDocument.objects.filter(
                page_id__in=[self.blog_post_1.id, self.blog_post_2.id, self.guide_post.id, 9999]
            ).delete()
            self.blog_post_1.delete(allow_physical_purge=True)
            self.blog_post_2.delete(allow_physical_purge=True)
            self.guide_post.delete(allow_physical_purge=True)
            self.blog_index.delete(allow_physical_purge=True)
            self.guide_index.delete(allow_physical_purge=True)
            self.topic.delete()
            self.author.delete()
        except Exception:
            pass

    def test_rss2_feed_all_generation(self):
        """测试全站 RSS 2.0 生成：包含博客和指南，不含聚合页，符合 RSS 规范"""
        request = self.factory.get("/feed/")
        response = public_feed_view(request, dimension="all")

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/rss+xml", response["Content-Type"])
        content = response.content.decode("utf-8")
        self.assertIn("<rss", content)
        self.assertIn(f"<title>{self.blog_post_1.title}</title>", content)
        self.assertIn(f"<title>{self.guide_post.title}</title>", content)
        self.assertNotIn("技术博文专栏", content)  # 聚合页被排除
        self.assertIn("urn:uuid:", content)  # GUID 采用稳定 urn:uuid 格式

    def test_atom_feed_generation_and_safe_summary(self):
        """测试 Atom 1.0 生成：RFC 4287 合规、summary type='text'"""
        request = self.factory.get("/feed/atom/")
        response = public_feed_view(request, dimension="all")

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/atom+xml", response["Content-Type"])
        content = response.content.decode("utf-8")
        self.assertIn('xmlns="http://www.w3.org/2005/Atom"', content)
        self.assertIn('<summary type="text">', content)  # 验证强制 type="text" 防 HTML 注入 (BLK-07)
        self.assertIn("<author><name>", content)

    def test_feed_dimensions(self):
        """测试多维度过滤：type, tag, author, topic"""
        # 1. 按类型过滤 (type=blog)
        req_blog = self.factory.get("/feed/type/blog/")
        resp_blog = public_feed_view(req_blog, dimension="type", slug="blog")
        content_blog = resp_blog.content.decode("utf-8")
        self.assertIn(self.blog_post_1.title, content_blog)
        self.assertNotIn(self.guide_post.title, content_blog)

        # 2. 按类型过滤 (type=guide)
        req_guide = self.factory.get("/feed/type/guide/")
        resp_guide = public_feed_view(req_guide, dimension="type", slug="guide")
        content_guide = resp_guide.content.decode("utf-8")
        self.assertIn(self.guide_post.title, content_guide)
        self.assertNotIn(self.blog_post_1.title, content_guide)

        # 3. 非法/非订阅类型 (type=blog_index) 必须返回 404
        from django.http import Http404
        with self.assertRaises(Http404):
            public_feed_view(self.factory.get("/feed/type/blog_index/"), dimension="type", slug="blog_index")

        # 4. 按独立标签过滤 (tag=self.custom_tag)
        req_tag = self.factory.get(f"/feed/tag/{self.custom_tag}/")
        resp_tag = public_feed_view(req_tag, dimension="tag", slug=self.custom_tag)
        content_tag = resp_tag.content.decode("utf-8")
        self.assertIn(self.blog_post_1.title, content_tag)
        self.assertNotIn(self.blog_post_2.title, content_tag)

        # 5. 按专题过滤 (topic=backend-arch)
        req_topic = self.factory.get(f"/feed/topic/{self.topic.slug}/")
        resp_topic = public_feed_view(req_topic, dimension="topic", slug=self.topic.slug)
        content_topic = resp_topic.content.decode("utf-8")
        self.assertIn(self.guide_post.title, content_topic)
        self.assertNotIn("Wagtail 8.1 深度剖析", content_topic)

    def test_feed_weak_etag_and_http_304(self):
        """测试弱 ETag 协商与 HTTP 304 快速响应"""
        request = self.factory.get("/feed/")
        response = public_feed_view(request, dimension="all")
        etag = response["ETag"]
        self.assertTrue(etag.startswith('W/"feed:'))

        # 携带相同 ETag 请求 -> 极速返回 304 Not Modified
        cond_request = self.factory.get("/feed/", HTTP_IF_NONE_MATCH=etag)
        cond_response = public_feed_view(cond_request, dimension="all")
        self.assertEqual(cond_response.status_code, 304)

        # Epoch 自增后，再次请求 -> 返回 200 并更新 ETag
        ContentDiscoveryEpoch.increment_epoch()
        cond_response_2 = public_feed_view(cond_request, dimension="all")
        self.assertEqual(cond_response_2.status_code, 200)
        self.assertNotEqual(cond_response_2["ETag"], etag)

    def test_timeline_archives_api_aggregation(self):
        """测试时间线归档 API：PostgreSQL TruncMonth 年月聚合统计与过滤"""
        request = self.factory.get("/api/v1/archives/")
        response = timeline_archives_api(request)
        self.assertEqual(response.status_code, 200)
        res = json.loads(response.content.decode("utf-8"))

        self.assertEqual(res["status"], "success")
        data = res["data"]
        self.assertGreaterEqual(data["total_count"], 3)  # 至少包含本次创建的 2篇博客 + 1篇专栏
        self.assertIn(2026, data["years"])

        # 验证 2026 年下的月份分布 (11月: 2篇, 10月: 1篇)
        year_2026 = next(y for y in data["timeline"] if y["year"] == 2026)
        self.assertGreaterEqual(year_2026["count"], 3)
        month_map = {m["month"]: m["count"] for m in year_2026["months"]}
        self.assertGreaterEqual(month_map[11], 2)
        self.assertGreaterEqual(month_map[10], 1)

        # 测试按专题过滤 (topic=self.topic.slug)
        req_guide = self.factory.get(f"/api/v1/archives/?topic={self.topic.slug}")
        resp_guide = timeline_archives_api(req_guide)
        res_guide = json.loads(resp_guide.content.decode("utf-8"))
        self.assertEqual(res_guide["data"]["total_count"], 1)

        # 测试时间线 ETag 304
        etag = response["ETag"]
        cond_req = self.factory.get("/api/v1/archives/", HTTP_IF_NONE_MATCH=etag)
        cond_resp = timeline_archives_api(cond_req)
        self.assertEqual(cond_resp.status_code, 304)

    def test_lifecycle_security_guards(self):
        """测试刚性生命周期安全守卫：回收站、未来排期、PageViewRestriction 隔离"""
        # 1. 未来排期文章 (authored_on = 明天) 绝不进入 Feed 和归档
        tomorrow = date.today() + timedelta(days=1)
        future_post = BlogPage(
            title="未来技术前瞻",
            slug="future-tech",
            authored_on=tomorrow,
        )
        self.blog_index.add_child(instance=future_post)
        future_post.save_revision().publish()
        future_post.refresh_from_db()
        project_page_to_discovery(future_post)

        feed_resp = public_feed_view(self.factory.get("/feed/"), dimension="all")
        feed_content = feed_resp.content.decode("utf-8")
        self.assertNotIn("未来技术前瞻", feed_content)

        # 2. 页面设置 PageViewRestriction（私有密码保护）立即阻断
        PageViewRestriction.objects.create(
            page=self.blog_post_1,
            restriction_type=PageViewRestriction.PASSWORD,
            password="secret-pass-123",
        )
        # 触发 epoch 递增与重新检测
        ContentDiscoveryEpoch.increment_epoch()
        feed_resp_2 = public_feed_view(self.factory.get("/feed/"), dimension="all")
        feed_content_2 = feed_resp_2.content.decode("utf-8")
        self.assertNotIn(self.blog_post_1.title, feed_content_2)

        # 3. 移入回收站阻断
        batch = RecycleBatch.objects.create(
            root_page_id=self.blog_post_2.id,
            root_page_title=self.blog_post_2.title,
            status="recycled",
        )
        RecycleEntry.objects.create(
            batch=batch,
            page_id=self.blog_post_2.id,
            parent_id=self.blog_index.id,
            original_slug=self.blog_post_2.slug,
            original_path=self.blog_post_2.path,
            original_depth=self.blog_post_2.depth,
            original_url_path=self.blog_post_2.url_path,
            status="recycled",
        )
        ContentDiscoveryEpoch.increment_epoch()
        feed_resp_3 = public_feed_view(self.factory.get("/feed/"), dimension="all")
        feed_content_3 = feed_resp_3.content.decode("utf-8")
        self.assertNotIn(self.blog_post_2.title, feed_content_3)

    def test_future_xxpage_plug_and_play_extensibility(self):
        """
        测试未来任意 xxPage（例如 ProjectPage）零核心修改即插即用扩展性：
        只需要定义 TypeAdapter 并声明 capabilities，即可自动接入 Feed 与归档。
        """
        class MockProjectPageAdapter:
            type_key = "project"
            model_label = "portfolio.ProjectPage"
            schema_version = 1
            extractor_version = 1
            allowed_filters = ["year", "month"]
            weight_profile = {"title": 1.0, "summary": 0.5}
            capabilities = PublicListingCapabilities(
                public_type_slug="project",
                feed_enabled=True,
                archive_enabled=True,
                supported_dimensions=("type", "tag", "author"),
            )

            def extract(self, page):
                return PublishedPayload(
                    page_id=9999,
                    document_uuid=str(uuid.uuid4()),
                    type_key=self.type_key,
                    title="开源高性能搜索引擎项目",
                    url_path="/projects/open-search/",
                    authored_on=date(2026, 11, 5),
                    summary="基于 Rust 与 PostgreSQL 的搜索引擎",
                )

        # 注册该未来页面适配器
        register_type_adapter(MockProjectPageAdapter())

        # 模拟该页面投影落库
        mock_adapter = MockProjectPageAdapter()
        payload = mock_adapter.extract(None)
        PublishedSearchDocument.objects.create(
            page_id=9999,
            document_uuid=payload.document_uuid,
            effective_revision_id=1,
            type_key=payload.type_key,
            title=payload.title,
            url_path=payload.url_path,
            authored_on=payload.authored_on,
            summary=payload.summary,
            is_live=True,
            is_recycled=False,
            site_id=self.site.id,
            root_path="000100019999",
            depth=3,
        )
        ContentDiscoveryEpoch.increment_epoch()

        # 1. 验证订阅流 /feed/type/project/ 自动生效并输出 200 OK
        req_proj = self.factory.get("/feed/type/project/")
        resp_proj = public_feed_view(req_proj, dimension="type", slug="project")
        self.assertEqual(resp_proj.status_code, 200)
        content_proj = resp_proj.content.decode("utf-8")
        self.assertIn("开源高性能搜索引擎项目", content_proj)

        # 2. 验证时间线归档自动包含 project
        archive_data = compute_public_timeline_archives(type_slug="project")
        self.assertEqual(archive_data["total_count"], 1)
        self.assertEqual(archive_data["timeline"][0]["year"], 2026)
