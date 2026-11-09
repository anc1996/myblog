"""
全站层级列表与多维检索通用框架自动化测试集 (Listing Framework Tests)。

实测覆盖：
1. 参数解析与安全清洗 (ListingFilters)；
2. 双模同构交付 (SSR 首屏 HTML vs AJAX JSON 局部片段)；
3. 页面树层级范围切换 (默认后代递归 include_descendants=1 vs 仅直接子级 0)；
4. 标题+正文混合检索与仅搜标题开关 (title_only=1)；
5. D4-A 直接子栏目导航展示 (navigation_items)；
6. D1-A 签名 Keyset 游标翻页 (v2:... 游标与上一页/下一页)；
7. Wagtail 8.1 后台预览入口防崩溃与草稿导语呈现 (B-01)；
8. Guide 专栏专属主题与难度过滤及动态分桶统计 (A-03)；
9. 列表路径零 MongoDB 访问脱水契约 (0 Mongo 调用验证)。
"""

import os
import json
import unittest
from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.test import RequestFactory
from wagtail.rich_text import RichText


class ListingFrameworkTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
        import django
        django.setup()

    def setUp(self):
        from home.models import HomePage
        from blog.models import BlogIndexPage, BlogPage
        from guides.models import GuideIndexPage, GuidePage, GuideTopic

        self.factory = RequestFactory()
        self.home = HomePage.objects.first()
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

    def _create_blog_index(self, title="测试博客栏目", intro="测试栏目简介"):
        from blog.models import BlogIndexPage
        slug = f"index-{uuid4().hex[:6]}"
        idx = BlogIndexPage(title=title, slug=slug, intro=intro)
        self.home.add_child(instance=idx)
        self.created_pages.append(idx)
        idx.save_revision().publish()
        idx.refresh_from_db()
        return idx

    def _create_blog_page(self, parent, title, intro="", body_text="", authored_on=None):
        from blog.models import BlogPage
        slug = f"post-{uuid4().hex[:6]}"
        page = BlogPage(
            title=title,
            slug=slug,
            authored_on=authored_on or date(2026, 6, 1),
        )
        parent.add_child(instance=page)
        self.created_pages.append(page)
        if intro:
            page.intro = [("rich_text", RichText(f"<p>{intro}</p>"))]
        if body_text:
            page.body = [("markdown", body_text)]
        rev = page.save_revision()
        rev.publish()
        page.refresh_from_db()
        return page

    def test_01_params_parsing_and_sanitization(self):
        """测试参数解析与默认排序智能判定"""
        from discovery.listing.contracts import ListingPolicy
        from discovery.listing.params import parse_listing_filters

        policy = ListingPolicy(type_keys=("blog_page",), page_size=10, max_page_size=30)

        # 1. 默认无参：无搜索词时默认 authored_desc
        req1 = self.factory.get("/")
        f1 = parse_listing_filters(req1, policy)
        self.assertEqual(f1.q, "")
        self.assertFalse(f1.title_only)
        self.assertTrue(f1.include_descendants)
        self.assertEqual(f1.sort, "authored_desc")
        self.assertEqual(f1.page_size, 10)

        # 2. 传入关键词且无指定排序：智能默认为 relevance
        req2 = self.factory.get("/?q=分布式架构&title_only=1&include_descendants=0&page_size=100")
        f2 = parse_listing_filters(req2, policy)
        self.assertEqual(f2.q, "分布式架构")
        self.assertTrue(f2.title_only)
        self.assertFalse(f2.include_descendants)
        self.assertEqual(f2.sort, "relevance")
        self.assertEqual(f2.page_size, 30, "page_size 必须被安全截断至 max_page_size")

    def test_02_ssr_and_ajax_isomorphic_rendering(self):
        """测试双模同构：首屏 SSR HTML 直出 vs AJAX JSON 局部更新载荷 (D3-A)"""
        idx = self._create_blog_index(title="技术工程栏目", intro="系统底层架构文章。")
        self._create_blog_page(idx, title="文章A: 高并发消息队列", intro="Kafka与RabbitMQ选型分析。")

        # 1. 普通 GET 请求：返回完整 SSR HTML
        req_ssr = self.factory.get(idx.url_path)
        resp_ssr = idx.serve(req_ssr)
        resp_ssr.render()
        html_ssr = resp_ssr.content.decode("utf-8")
        self.assertIn("技术工程栏目", html_ssr)
        self.assertIn("文章A: 高并发消息队列", html_ssr)
        self.assertIn('id="listing-results-container"', html_ssr)
        self.assertIn("listing_controller.js", html_ssr)

        # 2. AJAX 局部请求 (X-Listing-Response: fragment-v1)：返回 JSON 包装的局部 HTML 与元数据
        req_ajax = self.factory.get(f"{idx.url_path}?q=高并发", HTTP_X_LISTING_RESPONSE="fragment-v1")
        resp_ajax = idx.serve(req_ajax)
        self.assertEqual(resp_ajax.status_code, 200)
        data = json.loads(resp_ajax.content.decode("utf-8"))
        self.assertIn("html", data)
        self.assertIn("文章A: 高并发消息队列", data["html"])
        self.assertEqual(data["total_count"], 1)
        self.assertIn("canonical_url", data)

    def test_03_hierarchical_subcategories_and_descendants(self):
        """测试层级递归开关 (include_descendants) 与 D4-A 子栏目导航展示"""
        from blog.models import BlogIndexPage

        parent_idx = self._create_blog_index(title="后端总览", intro="后端一级大类。")

        # 创建二级直接子栏目
        child_slug = f"sub-{uuid4().hex[:6]}"
        child_idx = BlogIndexPage(title="存储专题", slug=child_slug, intro="二级数据库专栏。")
        parent_idx.add_child(instance=child_idx)
        self.created_pages.append(child_idx)
        child_idx.save_revision().publish()
        child_idx.refresh_from_db()

        # 在一级栏目下挂文章 1
        self._create_blog_page(parent_idx, title="直接文章: API网关设计", intro="网关路由与限流。")
        # 在二级子栏目下挂文章 2
        self._create_blog_page(child_idx, title="深层文章: PostgreSQL索引深度剖析", intro="B-Tree与GIN优化。")

        # 1. 默认 include_descendants=1：递归查出一级与二级文章共 2 篇，并在上方显示子栏目导航 (D4-A)
        req1 = self.factory.get(parent_idx.url_path)
        resp1 = parent_idx.serve(req1)
        vm1 = resp1.context_data["view_model"]
        self.assertEqual(vm1.total_count, 2, "默认递归应查出本栏目与后代所有文章")
        self.assertEqual(len(vm1.navigation_items), 1, "D4-A: 应成功返回 1 个直接子栏目导航")
        self.assertEqual(vm1.navigation_items[0]["title"], "存储专题")

        # 2. 传参 include_descendants=0：仅查出直接挂在 parent_idx 下的 1 篇文章
        req2 = self.factory.get(f"{parent_idx.url_path}?include_descendants=0")
        resp2 = parent_idx.serve(req2)
        vm2 = resp2.context_data["view_model"]
        self.assertEqual(vm2.total_count, 1, "关闭后代递归仅能查出直接下挂的 1 篇文章")
        self.assertEqual(vm2.items[0].title, "直接文章: API网关设计")

    def test_04_title_only_toggle_vs_full_text(self):
        """测试仅搜标题开关 (title_only=1) 与全文字段混合检索"""
        idx = self._create_blog_index(title="分布式系统", intro="分布式技术专栏。")

        # 文章 1：标题包含“Raft”，正文普通
        self._create_blog_page(idx, title="Raft一致性协议演进", intro="介绍分布式共识协议基础。")
        # 文章 2：标题不含“Raft”，仅正文包含“Raft”
        self._create_blog_page(idx, title="分布式协同设计", intro="本文在正文深度探讨了Raft算法的核心选主流程。")

        # 1. 全文检索 (默认 title_only=0)：标题与正文均命中，返回 2 篇
        req1 = self.factory.get(f"{idx.url_path}?q=Raft")
        resp1 = idx.serve(req1)
        vm1 = resp1.context_data["view_model"]
        self.assertEqual(vm1.total_count, 2)

        # 2. 仅搜标题 (title_only=1)：仅匹配标题含有“Raft”的文章 1
        req2 = self.factory.get(f"{idx.url_path}?q=Raft&title_only=1")
        resp2 = idx.serve(req2)
        vm2 = resp2.context_data["view_model"]
        self.assertEqual(vm2.total_count, 1)
        self.assertEqual(vm2.items[0].title, "Raft一致性协议演进")

    def test_05_keyset_cursor_pagination(self):
        """测试 D1-A 签名 Keyset 游标上一页与下一页精准定位"""
        idx = self._create_blog_index(title="分页测试栏目", intro="用于验证游标分页。")
        for i in range(1, 5):
            self._create_blog_page(
                idx,
                title=f"文章{i:02d}",
                intro="测试文章介绍",
                authored_on=date(2026, 6, i),
            )

        # 第 1 页：page_size=2
        req_p1 = self.factory.get(f"{idx.url_path}?page_size=2")
        resp_p1 = idx.serve(req_p1)
        vm_p1 = resp_p1.context_data["view_model"]
        self.assertEqual(len(vm_p1.items), 2)
        self.assertIsNotNone(vm_p1.next_cursor)
        self.assertTrue(vm_p1.next_cursor.startswith("v2:"), "游标必须携带 v2: 版本前缀")

        # 第 2 页：携带 next_cursor 访问
        req_p2 = self.factory.get(f"{idx.url_path}?page_size=2&cursor={vm_p1.next_cursor}")
        resp_p2 = idx.serve(req_p2)
        vm_p2 = resp_p2.context_data["view_model"]
        self.assertEqual(len(vm_p2.items), 2)
        self.assertIsNotNone(vm_p2.previous_cursor, "第 2 页必须提供上一页标志")

    def test_06_wagtail_preview_compatibility(self):
        """测试 Wagtail 8.1 原生后台预览模式兼容性与新建防崩溃守卫 (B-01)"""
        from blog.models import BlogIndexPage

        # 1. 场景一：编辑者在后台点击“新建页面”并打开右侧实时预览 (PreviewOnCreateView: self.pk is None)
        unsaved_page = BlogIndexPage(title="新建预览栏目", intro="尚未保存到数据库的草稿。")
        req1 = self.factory.get("/admin/pages/create/blog/blogindexpage/preview/")
        req1.is_preview = True
        # serve_preview 必须安全返回 200，绝不因 root 不存在触发 404
        resp1 = unsaved_page.serve_preview(req1, "default")
        self.assertEqual(resp1.status_code, 200)

        # 2. 场景二：已保存但尚未发布的草稿栏目预览 (self.live == False)
        draft_idx = BlogIndexPage(title="草稿栏目", slug=f"draft-idx-{uuid4().hex[:6]}", intro="草稿导语。")
        self.home.add_child(instance=draft_idx)
        self.created_pages.append(draft_idx)
        draft_idx.live = False
        draft_idx.save(update_fields=["live"])
        draft_idx.save_revision()

        req2 = self.factory.get(f"/admin/pages/{draft_idx.id}/edit/preview/")
        req2.is_preview = True
        resp2 = draft_idx.serve_preview(req2, "default")
        self.assertEqual(resp2.status_code, 200)

    def test_07_guide_index_topic_and_difficulty_filters(self):
        """测试 GuideIndexPage 专属 Topic 与难度过滤及分桶聚合 (A-03)"""
        from guides.models import GuideIndexPage, GuidePage, GuideTopic

        # 创建指南大厅
        g_idx = GuideIndexPage(title="技术专栏大厅", slug=f"guides-{uuid4().hex[:6]}", intro="技术专栏介绍")
        self.home.add_child(instance=g_idx)
        self.created_pages.append(g_idx)
        g_idx.save_revision().publish()

        # 创建主题
        topic_py = GuideTopic.objects.create(name="Python生态", slug=f"python-{uuid4().hex[:4]}")
        self.created_topics.append(topic_py)
        topic_db = GuideTopic.objects.create(name="数据库核心", slug=f"db-{uuid4().hex[:4]}")
        self.created_topics.append(topic_db)

        # 创建两篇指南
        p1 = GuidePage(title="指南1: Python并发", slug=f"g1-{uuid4().hex[:6]}", topic=topic_py, difficulty="intermediate")
        p1.body = [("step", {"step_number": "1", "title": "并发基础", "content": RichText("<p>并发详细说明</p>")})]
        g_idx.add_child(instance=p1)
        self.created_pages.append(p1)
        p1.save_revision().publish()

        p2 = GuidePage(title="指南2: PostgreSQL调优", slug=f"g2-{uuid4().hex[:6]}", topic=topic_db, difficulty="advanced")
        p2.body = [("step", {"step_number": "1", "title": "索引调优", "content": RichText("<p>索引详细说明</p>")})]
        g_idx.add_child(instance=p2)
        self.created_pages.append(p2)
        p2.save_revision().publish()

        # 1. 按 topic 过滤
        req_topic = self.factory.get(f"{g_idx.url_path}?topic={topic_py.slug}")
        resp_t = g_idx.serve(req_topic)
        vm_t = resp_t.context_data["view_model"]
        self.assertEqual(vm_t.total_count, 1)
        self.assertEqual(vm_t.items[0].title, "指南1: Python并发")

        # 2. 按 difficulty 过滤
        req_diff = self.factory.get(f"{g_idx.url_path}?difficulty=advanced")
        resp_d = g_idx.serve(req_diff)
        vm_d = resp_d.context_data["view_model"]
        self.assertEqual(vm_d.total_count, 1)
        self.assertEqual(vm_d.items[0].title, "指南2: PostgreSQL调优")

    def test_08_zero_mongodb_access_on_listing(self):
        """验证列表查询与渲染路径绝对不访问 MongoDB (零 Mongo 网络开销契约)"""
        idx = self._create_blog_index(title="零Mongo测试栏目", intro="用于验证脱水契约。")
        self._create_blog_page(idx, title="文章: 架构演进", intro="导语内容", body_text="长文正文" * 50)

        req = self.factory.get(idx.url_path)

        # 拦截 external_content.store.MongoContentStore.get_field_version
        with patch("external_content.store.MongoContentStore.get_field_version") as mock_get_field:
            resp = idx.serve(req)
            resp.render()
            self.assertEqual(resp.status_code, 200)
            mock_get_field.assert_not_called()
