"""
tests.test_comment_governance
RFC-020 全站多页面评论管理与多维治理系统专项测试集。

验证内容：
1. D5-B & B-01: Page 永久清空 (Purge) 级联物理销毁关联评论及其版本、反应，DO_NOTHING 避免 CheckConstraint 报错；
2. C-06 防误删: 普通 comment.delete() 抛出 PermissionDenied；
3. D6-A: 作者自删根评论占位条机制（保留二级回复可读与分页），未公开根直接隐藏；
4. D7-A: 编辑评论生成待审 V2 期间前台稳固展示 V1，获批后无缝切换 V2；
5. CAS 防盲批: approve/reject 预期版本不匹配时抛出 ValidationError 冲突；
6. D2: PageCommentSettings 页面级评论开关与关评公告；
7. D4-A: CommentGovernanceSubject 24 小时主体禁言与解除、批量隐藏；
8. B-05 & SEC-01: 外链超限人工待审门禁、长度超限拒绝、URL 防绕过与 inspect 原文 XSS 修复。
"""

import os
import json
import unittest
from uuid import uuid4
from datetime import timedelta

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

from django.core.exceptions import ValidationError, PermissionDenied
from django.test import Client, RequestFactory
from django.utils import timezone
from wagtail.models import Page
from home.models import HomePage
from blog.models import BlogPage, BlogIndexPage
from identity.models import User
from comments.models import (
    Comment,
    CommentVersion,
    CommentReaction,
    PageCommentSettings,
    CommentGovernanceSubject,
)
from comments.services import CommentService
from comments.sanitizer import (
    validate_comment_length,
    sanitize_comment_html,
    inspect_links_and_sanitize,
)
from comments.wagtail_hooks import comment_viewset
from content_lifecycle.recycle import RecycleService


class CommentGovernanceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = Client()
        cls.factory = RequestFactory()
        cls.home = HomePage.objects.first()

        cls.admin_user = User.objects.filter(is_superuser=True).first()
        if not cls.admin_user:
            cls.admin_user = User.objects.create_superuser(
                username=f"admin_gov_{uuid4().hex[:6]}",
                email="admin_gov@test.com",
                password="password123",
            )

        cls.normal_user = User.objects.filter(username="gov_author").first()
        if not cls.normal_user:
            cls.normal_user = User.objects.create_user(
                username="gov_author",
                email="gov_author@test.com",
                password="password123",
            )

        cls.reader_user = User.objects.filter(username="gov_reader").first()
        if not cls.reader_user:
            cls.reader_user = User.objects.create_user(
                username="gov_reader",
                email="gov_reader@test.com",
                password="password123",
            )

        cls.blog_index = BlogIndexPage.objects.first()
        if not cls.blog_index:
            cls.blog_index = BlogIndexPage(title="Gov Blog Index", slug=f"gov-index-{uuid4().hex[:6]}")
            cls.home.add_child(instance=cls.blog_index)
            cls.blog_index.save_revision().publish()

    def setUp(self):
        # 为每组测试创建独立的 BlogPage
        self.test_page = BlogPage(
            title=f"Gov Test Article {uuid4().hex[:6]}",
            slug=f"gov-article-{uuid4().hex[:6]}",
            intro="Intro for governance tests",
        )
        self.blog_index.add_child(instance=self.test_page)
        self.test_page.save_revision().publish()

    def tearDown(self):
        # 清理测试页面
        try:
            if self.test_page.id and Page.objects.filter(id=self.test_page.id).exists():
                RecycleService.recycle_page_or_subtree(self.test_page, actor=self.admin_user)
                RecycleService.purge_page_or_subtree(self.test_page.id, actor=self.admin_user)
        except Exception:
            pass

    def test_ordinary_comment_delete_blocked(self):
        """验证 C-06：直接调用 Comment.delete() 必须被拒绝"""
        comment = CommentService.post_comment(
            page=self.test_page,
            author=self.normal_user,
            raw_content="普通物理删除防御测试",
            auto_approve=True,
        )
        with self.assertRaises(PermissionDenied):
            comment.delete()

    def test_purge_cascades_to_comments_and_versions(self):
        """验证 D5-B & B-01：Page Purge 时关联评论及其版本、反应被级联物理清空"""
        c1 = CommentService.post_comment(
            page=self.test_page,
            author=self.normal_user,
            raw_content="将随页面彻底清空的根评论",
            auto_approve=True,
        )
        c2 = CommentService.post_comment(
            page=self.test_page,
            author=self.reader_user,
            raw_content="将随页面彻底清空的回复",
            parent_id=c1.id,
            auto_approve=True,
        )
        CommentService.react_comment(c1.id, self.reader_user, 1)

        pid = self.test_page.id
        self.assertTrue(Comment.objects.filter(page_id=pid).exists())
        self.assertTrue(CommentVersion.objects.filter(comment_id__in=[c1.id, c2.id]).exists())
        self.assertTrue(CommentReaction.objects.filter(comment_id=c1.id).exists())

        # 先移入回收站
        RecycleService.recycle_page_or_subtree(self.test_page, actor=self.admin_user)
        # 执行页面受控永久销毁 (Purge)
        res = RecycleService.purge_page_or_subtree(pid, actor=self.admin_user)
        self.assertEqual(res["status"], "success")

        # 断言：Page、Comment、CommentVersion、CommentReaction 全为 0，无孤儿数据
        self.assertFalse(Page.objects.filter(id=pid).exists())
        self.assertEqual(Comment.objects.filter(page_id=pid).count(), 0)
        self.assertEqual(CommentVersion.objects.filter(comment_id__in=[c1.id, c2.id]).count(), 0)
        self.assertEqual(CommentReaction.objects.filter(comment_id=c1.id).count(), 0)

    def test_author_self_delete_root_placeholder(self):
        """验证 D6-A：作者自删有子回复的根评论，原位转为占位条且合规回复完好"""
        root = CommentService.post_comment(
            page=self.test_page,
            author=self.normal_user,
            raw_content="原作者将要自删的精彩根讨论",
            auto_approve=True,
        )
        reply = CommentService.post_comment(
            page=self.test_page,
            author=self.reader_user,
            raw_content="读者的优质二级回复",
            parent_id=root.id,
            auto_approve=True,
        )
        root.refresh_from_db()
        self.assertEqual(root.reply_count, 1)

        # 作者自删根评论
        CommentService.soft_delete_comment(root.id, self.normal_user, reason="author")
        root.refresh_from_db()
        self.assertEqual(root.status, "deleted")
        self.assertEqual(root.deletion_reason, "author")
        self.assertFalse(root.is_publicly_visible)

        # 调用前台 API: list_comments_api
        resp = self.client.get(f"/api/v1/pages/{self.test_page.id}/comments/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["kind"], "author_deleted")
        self.assertEqual(item["message"], "该评论已由作者自行删除")
        self.assertEqual(item["reply_count"], 1)

        # 调用前台 API: list_replies_api，二级回复必须完好存在
        reply_resp = self.client.get(f"/api/v1/comments/{root.id}/replies/")
        self.assertEqual(reply_resp.status_code, 200)
        reply_data = reply_resp.json()
        self.assertEqual(len(reply_data["items"]), 1)
        self.assertIn("读者的优质二级回复", reply_data["items"][0]["safe_html"])

    def test_public_view_keeps_v1_while_v2_pending(self):
        """验证 D7-A：编辑待审期间前台继续展示 V1，审核通过后平滑切换 V2"""
        c = CommentService.post_comment(
            page=self.test_page,
            author=self.normal_user,
            raw_content="第一版正文 V1 内容",
            auto_approve=True,
        )
        v1_id = c.approved_version_id

        # 前台访问：显示 V1
        resp1 = self.client.get(f"/api/v1/pages/{self.test_page.id}/comments/")
        self.assertIn("第一版正文 V1 内容", resp1.json()["items"][0]["safe_html"])

        # 作者提交修改为 V2
        v2 = CommentService.edit_comment(
            comment_id=c.id,
            author=self.normal_user,
            new_raw_content="第二版修改 V2 内容（尚未获批）",
            expected_version_id=v1_id,
        )
        c.refresh_from_db()
        self.assertEqual(c.pending_version_id, v2.id)
        self.assertEqual(c.approved_version_id, v1_id)

        # 前台访问：仍然稳固展示 V1，绝不泄漏未批的 V2！
        resp2 = self.client.get(f"/api/v1/pages/{self.test_page.id}/comments/")
        self.assertIn("第一版正文 V1 内容", resp2.json()["items"][0]["safe_html"])
        self.assertNotIn("第二版修改 V2 内容", resp2.json()["items"][0]["safe_html"])

        # 管理员批准 V2
        CommentService.moderate_comment(
            comment_id=c.id,
            action="approve",
            reviewer=self.admin_user,
            expected_version_id=v2.id,
        )
        c.refresh_from_db()
        self.assertEqual(c.approved_version_id, v2.id)
        self.assertIsNone(c.pending_version_id)

        # 前台访问：已正式平滑切换为 V2
        resp3 = self.client.get(f"/api/v1/pages/{self.test_page.id}/comments/")
        self.assertIn("第二版修改 V2 内容", resp3.json()["items"][0]["safe_html"])

    def test_cas_version_validation(self):
        """验证 CAS 防盲批：预期版本号不匹配时拒绝审批或编辑"""
        c = CommentService.post_comment(
            page=self.test_page,
            author=self.normal_user,
            raw_content="CAS 待审测试稿",
            auto_approve=False,
        )
        real_ver_id = c.pending_version_id
        wrong_ver_id = real_ver_id + 9999

        # 尝试使用错误的预期版本批准 -> 冲突拦截
        with self.assertRaises(ValidationError) as ctx:
            CommentService.moderate_comment(
                comment_id=c.id,
                action="approve",
                reviewer=self.admin_user,
                expected_version_id=wrong_ver_id,
            )
        self.assertIn("审核冲突", str(ctx.exception))

        # 尝试使用过期的 expected_generation 编辑 -> 冲突拦截
        with self.assertRaises(ValidationError) as ctx2:
            CommentService.edit_comment(
                comment_id=c.id,
                author=self.normal_user,
                new_raw_content="新内容",
                expected_generation=c.state_generation + 5,
            )
        self.assertIn("编辑版本冲突", str(ctx2.exception))

    def test_page_comment_settings_toggle(self):
        """验证 D2：单篇 PageCommentSettings 关评生效"""
        # 关闭评论互动
        PageCommentSettings.objects.create(
            page=self.test_page,
            allow_comments=False,
            close_reason="系统维护暂关评论",
        )

        # 读者发评被拒绝
        with self.assertRaises(ValidationError) as ctx:
            CommentService.post_comment(
                page=self.test_page,
                author=self.normal_user,
                raw_content="尝试在已关评文章发评",
            )
        self.assertIn("已关闭评论互动", str(ctx.exception))
        self.assertIn("系统维护暂关评论", str(ctx.exception))

    def test_community_governance_subject_restriction(self):
        """验证 D4-A：治理主体 24 小时限制与提前解除"""
        user_key = f"user:{self.normal_user.id}"

        # 1. 对该作者实施 24 小时治理限制
        sub = CommentService.restrict_subject(
            kind="user",
            subject_key=user_key,
            hours=24,
            reason="发布违规广告内容",
            actor=self.admin_user,
        )
        self.assertTrue(CommentService.is_subject_restricted(author=self.normal_user))

        # 2. 受限期间发评被拦截
        with self.assertRaises(ValidationError) as ctx:
            CommentService.post_comment(
                page=self.test_page,
                author=self.normal_user,
                raw_content="广告测试",
            )
        self.assertIn("限制发表评论", str(ctx.exception))

        # 3. 管理员提前解除限制
        unbanned = CommentService.lift_restriction(kind="user", subject_key=user_key, actor=self.admin_user)
        self.assertTrue(unbanned)
        self.assertFalse(CommentService.is_subject_restricted(author=self.normal_user))

        # 4. 解封后可正常发评
        c = CommentService.post_comment(
            page=self.test_page,
            author=self.normal_user,
            raw_content="解封后正常发表的内容",
            auto_approve=True,
        )
        self.assertEqual(c.status, "active")

    def test_link_and_length_sanitization(self):
        """验证 B-05：外链数 >= 2 自动强制待审；长度超限 2000 拒绝"""
        # 1. 2 个外链 -> auto_approve 被强制关闭进入待审
        raw_two_links = "推荐参考 [网站A](https://evil-a.com) 和 [网站B](https://evil-b.com) 获取详情"
        c = CommentService.post_comment(
            page=self.test_page,
            author=self.normal_user,
            raw_content=raw_two_links,
            auto_approve=True,  # 即使声明自动审批也被门禁打断
        )
        self.assertEqual(c.status, "pending")
        self.assertFalse(c.is_publicly_visible)
        self.assertIn("external_links_threshold", c.latest_version.risk_reasons)

        # 2. 超出 2000 字符限制 -> 拒绝静默截断并报错
        oversized = "A" * 2001
        with self.assertRaises(ValidationError) as ctx:
            validate_comment_length(oversized, 2000)
        self.assertIn("超出限制", str(ctx.exception))

        # 3. 防御反斜杠与伪造主机绕过 (B-05)
        safe_html, ext_count = inspect_links_and_sanitize('访问 [危险链接](//\\evil.com/payload)')
        self.assertNotIn("//\\evil.com", safe_html)

    def test_xss_protection_in_pending_raw_content(self):
        """验证 SEC-01：待审原文中的 HTML/JavaScript 恶意标签必须转义"""
        c = CommentService.post_comment(
            page=self.test_page,
            author=self.normal_user,
            raw_content="<script>alert('xss_attack')</script>",
            auto_approve=False,
        )
        output_html = comment_viewset.pending_raw_content(c)
        self.assertNotIn("<script>", output_html)
        self.assertIn("&lt;script&gt;alert(&#x27;xss_attack&#x27;)&lt;/script&gt;", output_html)

    def test_toggle_pin_comment(self):
        """验证置顶切换"""
        c = CommentService.post_comment(
            page=self.test_page,
            author=self.normal_user,
            raw_content="置顶测试评论",
            auto_approve=True,
        )
        self.assertFalse(c.is_pinned)

        # 置顶
        CommentService.toggle_pin_comment(c.id, self.admin_user)
        c.refresh_from_db()
        self.assertTrue(c.is_pinned)

        # 取消置顶
        CommentService.toggle_pin_comment(c.id, self.admin_user)
        c.refresh_from_db()
        self.assertFalse(c.is_pinned)

    def test_batch_hide_comments(self):
        """验证批量隐藏违规评论"""
        c1 = CommentService.post_comment(page=self.test_page, author=self.normal_user, raw_content="违规水帖1", auto_approve=True)
        c2 = CommentService.post_comment(page=self.test_page, author=self.normal_user, raw_content="违规水帖2", auto_approve=True)

        res = CommentService.batch_hide_comments(actor=self.admin_user, comment_ids=[c1.id, c2.id], reason="广告治理")
        self.assertEqual(res["success"], 2)
        self.assertEqual(res["conflict"], 0)
        self.assertEqual(res["failed"], 0)

        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual(c1.status, "rejected")
        self.assertFalse(c1.is_publicly_visible)
        self.assertEqual(c2.status, "rejected")
        self.assertFalse(c2.is_publicly_visible)
