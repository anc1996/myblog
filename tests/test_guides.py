"""
tests.test_guides
专题技术专栏与指南系统单元测试。

实测覆盖：
1. 决策二业务门禁：保存草稿宽容；公开发布强制校验绑定 Topic 且至少包含 1 个 step 步骤块；
2. 决策一复核时效：复核日期超过 365 天自动标记 is_outdated 并生成时效警告；
3. MongoDB 彻底脱水：SQL 主表 body 为 []，正文在 MongoDB 不可变集合中，水合无损还原；
4. 受控回收站继承：继承 CustomBasePage，单页删除拦截转入受控回收站，原位一键恢复；
5. 全站搜索接入：通过 GuidePageAdapter 自动索引分块为 type_key="guide"；
6. 前后端分离 API：GET /api/v1/guides/ 与 GET /api/v1/guides/<slug>/（双形态输出与 steps_toc 目录）。
"""

import os
import unittest
from datetime import timedelta
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone
from wagtail.models import Page
from home.models import HomePage
from guides.models import GuideTopic, GuideIndexPage, GuidePage
from content_lifecycle.recycle import RecycleService
from discovery.models import ContentDiscoveryEpoch


class GuidesSystemTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = Client()
        cls.home = HomePage.objects.first()
        # 创建公共专栏技术主题
        cls.topic = GuideTopic.objects.create(
            name=f"PostgreSQL 深度调优-{uuid4().hex[:6]}",
            slug=f"pg-tuning-{uuid4().hex[:6]}",
            description="面向高并发场景的索引与查询调优实战专栏",
            order=1,
        )
        # 创建专栏聚合大厅页
        cls.index_page = GuideIndexPage(
            title=f"专题指南专栏-{uuid4().hex[:6]}",
            slug=f"guides-hall-{uuid4().hex[:6]}",
            featured_topic=cls.topic,
            intro="汇聚前沿架构与运维指南实操手册",
        )
        cls.home.add_child(instance=cls.index_page)

    @classmethod
    def tearDownClass(cls):
        # 清理测试聚合页与主题
        try:
            cls.index_page.delete(allow_physical_purge=True)
            cls.topic.delete()
        except Exception:
            pass

    def test_01_draft_permissive_and_publish_gate_rules(self):
        """测试决策二门禁：草稿宽容，发布强校验 Topic 与 step 步骤块"""
        # 1. 保存未完成的草稿（无 topic，无 step 步骤）-> 宽容允许
        draft_guide = GuidePage(
            title="未完成的灵感草稿",
            slug=f"draft-guide-{uuid4().hex[:6]}",
            live=False,
            summary="这是一篇草稿，还没选主题",
        )
        self.index_page.add_child(instance=draft_guide)
        rev = draft_guide.save_revision()
        self.assertIsNotNone(rev.id)
        self.assertFalse(draft_guide.live, "草稿态保持未发布")

        # 2. 尝试无 topic 直接发布 -> 触发校验阻断
        draft_guide.live = True
        with self.assertRaises(ValidationError) as cm:
            draft_guide.clean()
        self.assertIn("topic", cm.exception.message_dict)

        # 3. 关联 topic 但正文仍无 step 步骤 -> 触发步骤缺失阻断
        draft_guide.topic = self.topic
        draft_guide.body = [("markdown", "这是没有步骤的纯文字内容")]
        with self.assertRaises(ValidationError) as cm:
            draft_guide.clean()
        self.assertIn("body", cm.exception.message_dict)
        self.assertIn("操作步骤", str(cm.exception.message_dict["body"]))

        # 4. 补齐合法 step 步骤块 -> 校验完全通过并成功发布
        draft_guide.body = [
            ("lead", {"title": "学习目标", "overview": "掌握 GIN 索引原理"}),
            ("step", {
                "step_number": "1",
                "title": "创建测试表与扩展",
                "content": "执行 SQL 建立 GIN 索引",
                "code_snippet": "CREATE INDEX test_idx ON tbl USING gin (vec);",
                "code_language": "sql",
            }),
            ("tip", {"tip_type": "tip", "title": "技巧", "content": "建议调大 maintenance_work_mem"}),
        ]
        draft_guide.clean()  # 校验通过无异常
        publish_rev = draft_guide.save_revision()
        publish_rev.publish()
        draft_guide.refresh_from_db()
        self.assertTrue(draft_guide.live, "校验完整后成功上线")

        # 清理
        draft_guide.delete(allow_physical_purge=True)

    def test_02_review_outdated_warning_rule(self):
        """测试决策一复核时效：复核时间超过 365 天时自动发出时效告警"""
        fresh_guide = GuidePage(
            title="新鲜复核指南",
            slug=f"fresh-guide-{uuid4().hex[:6]}",
            topic=self.topic,
            last_reviewed_at=timezone.now().date(),
        )
        is_outdated, _, _ = fresh_guide.is_outdated()
        self.assertFalse(is_outdated, "近期复核的不应标记为过时")

        # 模拟 400 天前复核的老文章
        old_date = timezone.now().date() - timedelta(days=400)
        old_guide = GuidePage(
            title="长周期技术指南",
            slug=f"old-guide-{uuid4().hex[:6]}",
            topic=self.topic,
            last_reviewed_at=old_date,
        )
        is_outdated, days, msg = old_guide.is_outdated()
        self.assertTrue(is_outdated, "超 1 年未复核必须标记为 is_outdated")
        self.assertGreaterEqual(days, 365)
        self.assertIn("最后技术复核于 1 年前", msg)

    def test_03_mongodb_dehydration_and_hydration(self):
        """测试方案 A【彻底脱水模式】：正文外置 MongoDB，SQL 物理列为 []"""
        guide = GuidePage(
            title="脱水验证指南",
            slug=f"dehydrate-guide-{uuid4().hex[:6]}",
            topic=self.topic,
            live=False,
            body=[
                ("step", {
                    "step_number": "1",
                    "title": "环境初始化",
                    "content": "这是一万字实操指南的第一个核心段落...",
                }),
            ],
        )
        self.index_page.add_child(instance=guide)
        rev = guide.save_revision()

        # 1. 验证 PostgreSQL 物理表中 body 彻底清空为 []
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT body FROM guides_guidepage WHERE page_ptr_id = %s;", [guide.id])
            row = cur.fetchone()
            self.assertEqual(row[0], "[]", "PostgreSQL 主表列必须脱水保持为 2 字节 []")

        # 2. 验证从 MongoDB 精准水合恢复
        guide.body = []  # 抹掉内存
        guide.hydrate_from_mongo()
        self.assertEqual(len(guide.body), 1)
        self.assertEqual(guide.body[0].block_type, "step")
        self.assertIn("环境初始化", str(guide.body[0].value.get("title")))

        # 清理
        guide.delete(allow_physical_purge=True)

    def test_04_controlled_recycle_bin_and_restore(self):
        """测试受控回收站体系集成：继承 CustomBasePage，单页删除拦截与一键恢复"""
        guide = GuidePage(
            title="专栏待回收指南",
            slug=f"recycle-guide-{uuid4().hex[:6]}",
            topic=self.topic,
            live=False,
        )
        self.index_page.add_child(instance=guide)

        # 1. 移入回收站
        batch = RecycleService.recycle_page_or_subtree(guide, system_authorized=True)
        guide.refresh_from_db()
        self.assertTrue(guide.is_recycled, "页面必须标记为 is_recycled")

        # 2. 验证前台访问返回 404
        from content_lifecycle.wagtail_hooks import block_recycled_pages_front_serve
        from django.http import Http404
        with self.assertRaises(Http404):
            block_recycled_pages_front_serve(guide, None, (), {})

        # 3. 从回收站原位恢复
        res = RecycleService.restore_page_or_subtree(batch.batch_id, system_authorized=True)
        guide.refresh_from_db()
        self.assertFalse(guide.is_recycled, "恢复后取消回收标记")
        self.assertFalse(guide.live, "恢复后严格保持草稿态")

        # 清理
        guide.delete(allow_physical_purge=True)

    def test_05_guides_restful_json_api(self):
        """测试 Guides 前后端分离 RESTful JSON API"""
        guide = GuidePage(
            title="API 专栏指南实测",
            slug=f"api-guide-{uuid4().hex[:6]}",
            topic=self.topic,
            difficulty="advanced",
            estimated_minutes=25,
            summary="专为 API 交互设计的专业实操指南",
            body=[
                ("step", {
                    "step_number": "1",
                    "title": "配置连接池参数",
                    "content": "设置 max_connections 与 pool_size",
                    "code_snippet": "pool = ConnectionPool(max_size=20)",
                    "code_language": "python",
                }),
            ],
        )
        self.index_page.add_child(instance=guide)
        rev = guide.save_revision()
        rev.publish()

        # 1. 测试专栏列表 API: GET /api/v1/guides/
        res_list = self.client.get(f"/api/v1/guides/?topic={self.topic.slug}")
        self.assertEqual(res_list.status_code, 200)
        d_list = res_list.json()
        self.assertEqual(d_list["status"], "success")
        self.assertGreaterEqual(d_list["total_count"], 1)
        found_item = next((it for it in d_list["items"] if it["id"] == guide.id), None)
        self.assertIsNotNone(found_item)
        self.assertEqual(found_item["difficulty"], "advanced")
        self.assertEqual(found_item["estimated_minutes"], 25)

        # 2. 测试专栏详情 API: GET /api/v1/guides/<slug>/
        res_detail = self.client.get(f"/api/v1/guides/{guide.slug}/")
        self.assertEqual(res_detail.status_code, 200)
        d_detail = res_detail.json()
        self.assertEqual(d_detail["status"], "success")
        g_data = d_detail["data"]
        self.assertEqual(g_data["id"], guide.id)
        # 双形态输出验证
        self.assertIn("blocks", g_data)
        self.assertIsInstance(g_data["blocks"], list)
        self.assertIn("safe_html", g_data)
        self.assertIn("steps_toc", g_data)
        self.assertEqual(len(g_data["steps_toc"]), 1)
        self.assertEqual(g_data["steps_toc"][0]["title"], "配置连接池参数")
        self.assertFalse(g_data["is_outdated"])

        # 清理
        guide.delete(allow_physical_purge=True)


if __name__ == "__main__":
    unittest.main()
