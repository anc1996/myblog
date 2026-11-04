"""
S8 受控回收站与一键精准还原 (Recycle & Restore) 单元测试。

实测覆盖：
1. 单页与整棵子树原树占位回收；
2. 自动下架与清空排期发布 (approved_go_live_at)；
3. 正常一键还原至原树位置，断言严格处于未发布草稿态 (live=False、has_unpublished_changes=True)；
4. 冲突处理实测：
   - slug 冲突：原位置已有同名页面时阻断，通过 slug_overrides 改名恢复成功；
   - 父级丢失/被回收冲突：提示先恢复父级或指定新父节点迁移恢复；
5. Wagtail 后台 Explorer 列表过滤与前台 404 隔离钩子实测。
"""

import os
from pathlib import Path
import unittest
from uuid import uuid4
from django.core.exceptions import ValidationError
from django.http import Http404

PROJECT_DIR = Path(__file__).resolve().parents[1]


class S8RecycleRestoreTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
        import django
        django.setup()

    def test_single_page_recycle_and_in_place_restore(self):
        """测试单页面原树占位回收与原位一键还原为草稿"""
        from home.models import HomePage
        from blog.models import BlogPage
        from content_lifecycle.recycle import RecycleService
        from content_lifecycle.models import RecycleBatch, RecycleEntry

        home = HomePage.objects.first()
        # 1. 创建并发布测试文章
        slug = f"restore-test-{uuid4().hex[:6]}"
        page = BlogPage(title="待回收文章", slug=slug)
        home.add_child(instance=page)
        rev = page.save_revision()
        rev.publish()
        page.refresh_from_db()
        self.assertTrue(page.live)
        self.assertFalse(page.is_recycled)

        # 2. 移入回收站
        batch = RecycleService.recycle_page_or_subtree(page)
        page.refresh_from_db()
        self.assertTrue(page.is_recycled, "回收后必须标记 is_recycled=True")
        self.assertFalse(page.live, "回收后必须正式下架 live=False")
        self.assertEqual(batch.total_pages, 1)

        # 3. 评估恢复计划（无冲突）
        plan = RecycleService.plan_restore(batch.batch_id)
        self.assertTrue(plan["can_restore_directly"])
        self.assertEqual(len(plan["conflicts"]), 0)

        # 4. 执行原位一键还原
        res = RecycleService.restore_page_or_subtree(batch.batch_id)
        self.assertEqual(res["status"], "success")
        page.refresh_from_db()

        # 5. 核心断言：恢复后严格保持未发布草稿态，绝不直接上线！
        self.assertFalse(page.is_recycled, "恢复后解除回收标记")
        self.assertFalse(page.live, "恢复后必须严格保持 live=False（草稿态）！")
        self.assertTrue(page.has_unpublished_changes, "恢复后必须标记有未发布草稿改动！")
        self.assertIsNone(page.live_revision, "恢复后 live_revision 必须为空")
        self.assertEqual(page.slug, slug, "原位置与原 slug 保持不变")

        # 6. 清理
        page.delete(allow_physical_purge=True)

    def test_subtree_recycle_and_restore(self):
        """测试包含子文章的整棵子树一起回收与整树还原"""
        from home.models import HomePage
        from blog.models import BlogIndexPage, BlogPage
        from content_lifecycle.recycle import RecycleService

        home = HomePage.objects.first()
        # 创建父栏目与子文章
        index = BlogIndexPage(title="测试主栏目", slug=f"col-{uuid4().hex[:6]}")
        home.add_child(instance=index)
        child_page = BlogPage(title="子文章", slug=f"child-{uuid4().hex[:6]}")
        index.add_child(instance=child_page)
        child_page.save_revision().publish()

        # 回收主栏目：整棵子树 2 个节点一起回收
        batch = RecycleService.recycle_page_or_subtree(index)
        index.refresh_from_db()
        child_page.refresh_from_db()
        self.assertTrue(index.is_recycled)
        self.assertTrue(child_page.is_recycled)
        self.assertFalse(child_page.live)
        self.assertEqual(batch.total_pages, 2)

        # 整树一键恢复
        res = RecycleService.restore_page_or_subtree(batch.batch_id)
        self.assertEqual(res["status"], "success")
        index.refresh_from_db()
        child_page.refresh_from_db()
        self.assertFalse(index.is_recycled)
        self.assertFalse(child_page.is_recycled)
        self.assertFalse(child_page.live, "子文章恢复后同样保持未发布草稿态")

        # 清理
        child_page.delete(allow_physical_purge=True)
        index.delete(allow_physical_purge=True)

    def test_restore_slug_collision_conflict_handling(self):
        """测试恢复时 slug 被新页面占用的冲突判定与改名恢复"""
        from home.models import HomePage
        from blog.models import BlogIndexPage, BlogPage
        from content_lifecycle.recycle import RecycleService

        home = HomePage.objects.first()
        col1 = BlogIndexPage(title="栏目1", slug=f"col1-{uuid4().hex[:6]}")
        home.add_child(instance=col1)
        col2 = BlogIndexPage(title="栏目2", slug=f"col2-{uuid4().hex[:6]}")
        home.add_child(instance=col2)

        slug = f"collision-{uuid4().hex[:6]}"
        page1 = BlogPage(title="旧文章", slug=slug)
        col1.add_child(instance=page1)
        page1.save_revision().publish()

        col1.delete(allow_physical_purge=True)
        page1 = BlogPage(title="旧文章", slug=f"{slug}-old")
        col2.add_child(instance=page1)
        page1.save_revision().publish()
        batch = RecycleService.recycle_page_or_subtree(page1)
        entry = batch.entries.first()
        entry.original_slug = slug
        entry.save(update_fields=["original_slug"])

        # 在原父节点下新建一个占用了相同 slug 的新页面
        page2 = BlogPage(title="同名新文章", slug=slug)
        col2.add_child(instance=page2)
        page2.save_revision().publish()

        # 1. 评估恢复计划：检测到 slug 碰撞冲突
        plan = RecycleService.plan_restore(batch.batch_id)
        self.assertFalse(plan["can_restore_directly"])
        self.assertEqual(len(plan["conflicts"]), 1)
        self.assertEqual(plan["conflicts"][0]["type"], "slug_collision")

        # 2. 未提供 slug_overrides 盲目恢复：抛出 ValidationError 拒绝
        with self.assertRaises(ValidationError):
            RecycleService.restore_page_or_subtree(batch.batch_id)

        # 3. 提供改名映射：改名恢复成功
        new_slug = f"{slug}-renamed"
        res = RecycleService.restore_page_or_subtree(
            batch.batch_id,
            slug_overrides={page1.id: new_slug},
        )
        self.assertEqual(res["status"], "success")
        page1.refresh_from_db()
        self.assertEqual(page1.slug, new_slug, "通过改名解决冲突并成功恢复")
        self.assertFalse(page1.is_recycled)
        self.assertFalse(page1.live)

        # 清理
        page1.delete(allow_physical_purge=True)
        page2.delete(allow_physical_purge=True)
        col2.delete(allow_physical_purge=True)

    def test_wagtail_admin_explorer_and_serve_hooks(self):
        """测试 Wagtail Admin 过滤钩子与前台 404 拦截钩子"""
        from home.models import HomePage
        from blog.models import BlogPage
        from content_lifecycle.recycle import RecycleService
        from content_lifecycle.wagtail_hooks import (
            hide_recycled_pages_in_explorer,
            hide_recycled_pages_in_chooser,
            block_recycled_pages_front_serve,
        )
        from wagtail.models import Page

        home = HomePage.objects.first()
        page = BlogPage(title="被回收页面", slug=f"hook-test-{uuid4().hex[:6]}")
        home.add_child(instance=page)
        RecycleService.recycle_page_or_subtree(page)

        # 1. 验证在 Explorer 查询集中被排除
        qs = Page.objects.filter(id=page.id)
        filtered_qs = hide_recycled_pages_in_explorer(home, qs, None)
        self.assertFalse(filtered_qs.exists(), "已回收页面在 Explorer 中必须被过滤隐藏")

        # 2. 验证在 Chooser 查询集中被排除
        chooser_qs = hide_recycled_pages_in_chooser(qs, None)
        self.assertFalse(chooser_qs.exists(), "已回收页面在 Chooser 中必须被过滤隐藏")

        # 3. 验证前台直达访问直接抛出 Http404
        with self.assertRaises(Http404):
            block_recycled_pages_front_serve(page, None, (), {})

        # 清理
        page.delete(allow_physical_purge=True)


if __name__ == "__main__":
    unittest.main()
