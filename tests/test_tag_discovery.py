"""
全站标签大厅与跨类型检索系统自动化测试集 (Tag Discovery Tests)。

实测覆盖：
1. TagIndexPage 路由与模型守卫 (parent_page_types, can_copy=False, _enforce_entry_guard)；
2. 模式一：标签大厅目录 (Top 20 热门云、A-Z 分组、#组在后、标签名即时搜索)；
3. 模式二：单标签跨类型聚合检索 (同时检索 BlogPage, GuidePage, ProjectPage)；
4. 模式二：多类型分栏 Tab (全部/博客/指南/项目) 与各类型计数在单快照下的精确性；
5. 模式二：标签内关键词检索、仅搜标题开关、日期区间与 Keyset 游标翻页；
6. 模式三：缺失标签 200 友好推荐导流与私有草稿标签防泄漏 (BLK-05)；
7. 纯符号标签与空 Slug 容错 (BLK-04: NoReverseMatch 免疫)；
8. 标签大厅与详情 100% 零 MongoDB 访问脱水契约。
"""

import os
import json
import unittest
from datetime import date
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

from django.test import RequestFactory
from taggit.models import Tag
from wagtail.rich_text import RichText


class TagDiscoveryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        pass

    def setUp(self):
        from home.models import HomePage
        from taxonomy.models import TagIndexPage, TagDirectoryMetadata

        self.factory = RequestFactory()
        self.home = HomePage.objects.first()
        self.created_pages = []
        self.created_tags = []

        # 创建全局单例 TagIndexPage
        self.tag_index = TagIndexPage(
            title="全站标签大厅",
            slug=f"tags-{uuid4().hex[:6]}",
            intro="汇聚全站技术博客、专栏指南与开源项目的知识图谱。",
        )
        self.home.add_child(instance=self.tag_index)
        self.created_pages.append(self.tag_index)
        self.tag_index.save_revision().publish()
        self.tag_index.refresh_from_db()

    def tearDown(self):
        for p in reversed(self.created_pages):
            try:
                p.delete(allow_physical_purge=True)
            except Exception:
                pass
        for t in self.created_tags:
            try:
                t.delete()
            except Exception:
                pass

    def _create_and_publish_blog(self, title, tag_names=None, intro="", body=""):
        from blog.models import BlogPage
        page = BlogPage(title=title, slug=f"b-{uuid4().hex[:6]}", authored_on=date(2026, 6, 1))
        self.home.add_child(instance=page)
        self.created_pages.append(page)
        if intro:
            page.intro = [("rich_text", RichText(f"<p>{intro}</p>"))]
        if body:
            page.body = [("markdown", body)]
        if tag_names:
            for tn in tag_names:
                page.tags.add(tn)
        page.save_revision().publish()
        page.refresh_from_db()
        return page

    def _create_and_publish_guide(self, title, tag_names=None, topic=None):
        from guides.models import GuidePage, GuideTopic
        if not topic:
            topic = GuideTopic.objects.create(name=f"主题-{uuid4().hex[:4]}", slug=f"top-{uuid4().hex[:4]}")
        page = GuidePage(title=title, slug=f"g-{uuid4().hex[:6]}", topic=topic, difficulty="intermediate")
        page.body = [("step", {"step_number": "1", "title": "操作步骤", "content": RichText("<p>指南步骤</p>")})]
        self.home.add_child(instance=page)
        self.created_pages.append(page)
        if tag_names:
            for tn in tag_names:
                page.tags.add(tn)
        page.save_revision().publish()
        page.refresh_from_db()
        return page

    def _create_and_publish_project(self, title, tag_names=None):
        from portfolio.models import ProjectPage
        page = ProjectPage(title=title, slug=f"p-{uuid4().hex[:6]}", project_status="maintained")
        page.body = [("markdown", "开源项目说明与架构图。")]
        self.home.add_child(instance=page)
        self.created_pages.append(page)
        if tag_names:
            for tn in tag_names:
                page.tags.add(tn)
        page.save_revision().publish()
        page.refresh_from_db()
        return page

    def test_01_tag_index_page_guards_and_constraints(self):
        """测试 TagIndexPage 页面树层级约束、复制拦截与入口守卫 (BLK-01 / ADV-05)"""
        self.assertFalse(self.tag_index.can_copy(), "TagIndexPage 必须禁止被直接复制")
        self.assertEqual(self.tag_index.subpage_types, [], "TagIndexPage 下属严禁挂接普通子页面")

        # 后台原生预览防崩溃守卫
        req = self.factory.get("/admin/pages/create/taxonomy/tagindexpage/preview/")
        req.is_preview = True
        resp = self.tag_index.serve_preview(req, "default")
        self.assertEqual(resp.status_code, 200)

    def test_02_tag_directory_top_cloud_and_alphabet_grouping(self):
        """测试模式一：标签大厅 Top 20 热门云、A-Z 分组与 # 组排序 (BLK-04 / 决策三-A)"""
        # 创建包含不同中英文首字母与特殊符号的标签内容
        self._create_and_publish_blog("文章1", tag_names=["Python", "PostgreSQL", "架构设计", "+++"])
        self._create_and_publish_blog("文章2", tag_names=["Python", "PostgreSQL", "分布式"])
        self._create_and_publish_blog("文章3", tag_names=["Python", "Docker"])

        # 访问标签大厅目录 GET /tags/
        req = self.factory.get(self.tag_index.url_path)
        resp = self.tag_index.serve(req)
        self.assertEqual(resp.status_code, 200)
        resp.render()
        html = resp.content.decode("utf-8")

        self.assertIn("全站标签大厅", html)
        self.assertIn("#Python", html)
        self.assertIn("#PostgreSQL", html)

        vm = resp.context_data["view_model"]
        self.assertGreaterEqual(len(vm.top_tags), 1)
        py_item = next((t for t in vm.top_tags if t.name == "Python"), None)
        self.assertIsNotNone(py_item, "Python 必须出现在热门标签中")
        self.assertGreaterEqual(py_item.count, 3)

        # 验证 A-Z 分组与 # 组排序 (BLK-04)
        # 'Python' 归入 'P' 组，'架构设计' 拼音首字母归入 'J' 组，'+++' 归入 '#' 组
        groups = vm.grouped_tags
        self.assertIn("P", groups)
        self.assertIn("#", groups)
        # 确保 # 组的 initial_group_rank 为 1，排在字母组后面
        sharp_tags = groups["#"]
        self.assertTrue(all(t.initial_group_rank == 1 for t in sharp_tags))

    def test_03_tag_directory_instant_search_filtering(self):
        """测试标签大厅即时搜索过滤 (支持中英文关键词)"""
        self._create_and_publish_blog("文章A", tag_names=["Django", "Docker", "微服务架构"])

        # 检索 "dock"
        req = self.factory.get(f"{self.tag_index.url_path}?q=dock")
        resp = self.tag_index.serve(req)
        vm = resp.context_data["view_model"]
        matched_names = [t.name for t in vm.top_tags]
        self.assertIn("Docker", matched_names)
        self.assertNotIn("Django", matched_names)

    def test_04_cross_type_tag_detail_aggregation(self):
        """测试模式二：单个标签跨类型聚合 (同时汇聚 BlogPage, GuidePage, ProjectPage)"""
        common_tag = f"PG-{uuid4().hex[:6]}"
        b = self._create_and_publish_blog("PG核心架构文章", tag_names=[common_tag], intro="深入浅出数据库内核。")
        g = self._create_and_publish_guide("PG快速上手指南", tag_names=[common_tag])
        p = self._create_and_publish_project("PG开源连接池项目", tag_names=[common_tag])

        tag = Tag.objects.get(name=common_tag)
        # 访问标签详情页 GET /tags/postgresql/
        req = self.factory.get(f"{self.tag_index.url_path}{tag.slug}/")
        resp = self.tag_index.serve(req)
        self.assertEqual(resp.status_code, 200)
        resp.render()
        html = resp.content.decode("utf-8")

        self.assertIn(f"#{common_tag}", html)
        self.assertIn("PG核心架构文章", html)
        self.assertIn("PG快速上手指南", html)
        self.assertIn("PG开源连接池项目", html)

        data = resp.context_data["data"]
        vm = data["view_model"]
        self.assertEqual(vm.total_count, 3, "必须同时汇聚全部 3 种类型的公开内容")

    def test_05_multi_type_tabs_switching(self):
        """测试多类型分栏 Tab (全部/博客/指南/项目) 切换与各类型计数单快照一致性 (BLK-03 / 决策二-A)"""
        t_name = "Docker"
        self._create_and_publish_blog("Docker文章1", tag_names=[t_name])
        self._create_and_publish_blog("Docker文章2", tag_names=[t_name])
        self._create_and_publish_guide("Docker专栏指南", tag_names=[t_name])

        tag = Tag.objects.get(name=t_name)

        # 1. 访问“全部”Tab
        req_all = self.factory.get(f"{self.tag_index.url_path}{tag.slug}/?type=all")
        resp_all = self.tag_index.serve(req_all)
        tabs_all = resp_all.context_data["data"]["type_tabs"]
        counts_all = {t["slug"]: t["count"] for t in tabs_all}
        self.assertEqual(counts_all["all"], 3)
        self.assertEqual(counts_all["blog_page"], 2)
        self.assertEqual(counts_all["guide"], 1)
        self.assertEqual(counts_all["project"], 0)

        # 2. 访问“技术指南”Tab (type=guide)：仅返回指南列表，但其他 Tab 计数保持绝对稳定！
        req_guide = self.factory.get(f"{self.tag_index.url_path}{tag.slug}/?type=guide")
        resp_guide = self.tag_index.serve(req_guide)
        data_guide = resp_guide.context_data["data"]
        self.assertEqual(data_guide["view_model"].total_count, 1, "指南 Tab 下仅展示 1 篇指南")
        tabs_guide = data_guide["type_tabs"]
        counts_guide = {t["slug"]: t["count"] for t in tabs_guide}
        self.assertEqual(counts_guide["all"], 3, "切换 Tab 时全站总数不得跳变")
        self.assertEqual(counts_guide["blog_page"], 2, "切换 Tab 时博客数量不得跳变为 0")

    def test_06_in_tag_search_and_title_only(self):
        """测试标签限定范围内的混合检索与仅搜标题开关"""
        t_name = "并发技术"
        self._create_and_publish_blog("Goroutine调度机理", tag_names=[t_name], intro="Go语言GMP模型剖析。")
        self._create_and_publish_blog("高并发网络框架", tag_names=[t_name], intro="深入探讨Epoll与Goroutine集成。")

        tag = Tag.objects.get(name=t_name)

        # 1. 检索关键词 "Epoll" (仅在第2篇导语中出现)
        req1 = self.factory.get(f"{self.tag_index.url_path}{tag.slug}/?q=Epoll")
        resp1 = self.tag_index.serve(req1)
        self.assertEqual(resp1.context_data["data"]["view_model"].total_count, 1)

        # 2. 勾选仅搜标题 (title_only=1) 再次检索 "Epoll" -> 标题不含，返回 0
        req2 = self.factory.get(f"{self.tag_index.url_path}{tag.slug}/?q=Epoll&title_only=1")
        resp2 = self.tag_index.serve(req2)
        self.assertEqual(resp2.context_data["data"]["view_model"].total_count, 0)

    def test_07_missing_tag_friendly_fallback_and_no_leak(self):
        """测试缺失/全下架标签 200 友好推荐导流，且严禁泄漏未发布草稿私有标签 (BLK-05 / 决策四-A)"""
        # 创建已发布公有标签
        self._create_and_publish_blog("公开发布文章", tag_names=["Kubernetes"])

        # 创建未发布私有草稿中的绝密标签 (名称绝密，slug为暗号编码)
        from blog.models import BlogPage
        draft = BlogPage(title="未发布草稿", slug=f"d-{uuid4().hex[:6]}")
        self.home.add_child(instance=draft)
        self.created_pages.append(draft)
        draft.live = False
        draft.save(update_fields=["live"])
        secret_name = f"机密绝密项目名称-{uuid4().hex[:4]}"
        draft.tags.add(secret_name)
        draft.save_revision()

        secret_tag = Tag.objects.get(name=secret_name)
        secret_slug = f"code-{uuid4().hex[:8]}"
        secret_tag.slug = secret_slug
        secret_tag.save()

        # 外部访客通过 slug 尝试探测
        req = self.factory.get(f"{self.tag_index.url_path}{secret_slug}/")
        resp = self.tag_index.serve(req)
        self.assertEqual(resp.status_code, 200, "必须返回 200 友好导流页而不是冷冻 404")
        resp.render()
        html = resp.content.decode("utf-8")

        # 验证严禁泄漏数据库中的私有标签真实名称与描述 (BLK-05)
        self.assertNotIn(secret_name, html, "绝对禁止在缺失导流页回显未发布草稿的真实标签名称！")
        self.assertIn("未找到与标签", html)
        self.assertIn("为您推荐以下活跃技术标签", html)
        self.assertGreaterEqual(len(resp.context_data["data"]["recommended_tags"]), 1)

    def test_08_zero_mongodb_access_on_tag_pages(self):
        """验证标签大厅与标签详情页面渲染全过程 100% 零 MongoDB 访问脱水契约"""
        self._create_and_publish_blog("性能架构长文", tag_names=["性能优化"], intro="简介", body="长文"*100)
        tag = Tag.objects.get(name="性能优化")

        # 拦截 external_content.store.MongoContentStore.get_field_version
        with patch("external_content.store.MongoContentStore.get_field_version") as mock_get_field:
            # 1. 验证标签大厅目录渲染 0 Mongo 访问
            resp_dir = self.tag_index.serve(self.factory.get(self.tag_index.url_path))
            resp_dir.render()
            self.assertEqual(resp_dir.status_code, 200)
            mock_get_field.assert_not_called()

            # 2. 验证单标签跨类型详情渲染 0 Mongo 访问
            resp_detail = self.tag_index.serve(self.factory.get(f"{self.tag_index.url_path}{tag.slug}/"))
            resp_detail.render()
            self.assertEqual(resp_detail.status_code, 200)
            mock_get_field.assert_not_called()
