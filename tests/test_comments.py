"""
tests.test_comments
全站通用评论系统核心单元测试。

实测覆盖：
1. 通用 Page 挂载 (C-01 & C-02)：BlogPage 与 GuidePage 均可通用评论；HomePage/栏目页自动阻断；
2. 扁平二级反嵌套 (C-05)：回复深层子回复时 parent 恒归一化指向根评论，replied_to_user 精准记录；
3. 版本级审核与防盲批 CAS (C-03)：编辑生成不可变新版本；审批绑定 expected_version_id；
4. is_publicly_visible 状态跃迁计数 (C-04)：已生效评论编辑并审批新版本时，reply_count 绝不重复累加；
5. 点赞/点踩互斥与非负原子约束 (C-05)：互斥切换、作者自评阻断、F() 原子更新；
6. 防垃圾与风控 (C-08)：蜜罐静默拦截、nh3 严格白名单与强制 nofollow ugc 注入；
7. 评论 RESTful JSON API 全流程实测。
"""

import os
import unittest
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

from django.core.exceptions import ValidationError, PermissionDenied
from django.test import Client, RequestFactory
from django.urls import reverse
from wagtail.models import Page
from home.models import HomePage
from blog.models import BlogPage, BlogIndexPage
from guides.models import GuidePage, GuideTopic, GuideIndexPage
from identity.models import User
from captcha.service import _get_redis_client
from comments.models import (
    Comment,
    CommentDailyQuota,
    CommentVersion,
    CommentReaction,
    CommunitySettings,
    PageCommentSettings,
)
from comments.services import CommentService
from comments.access import get_accessible_comment_page
from comments.sanitizer import sanitize_comment_html


class CommentsSystemTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = Client()
        cls.factory = RequestFactory()
        cls.home = HomePage.objects.first()

        # 创建测试用户
        cls.admin_user = User.objects.filter(is_superuser=True).first()
        cls.user_a, _ = User.objects.get_or_create(username="commenter_alice", defaults={"email": "alice@test.com"})
        cls.user_b, _ = User.objects.get_or_create(username="commenter_bob", defaults={"email": "bob@test.com"})
        cls.user_c, _ = User.objects.get_or_create(username="commenter_charlie", defaults={"email": "charlie@test.com"})

        # 1. 创建并发布一篇 BlogPage
        cls.blog_page = BlogPage(title=f"评论测试文章-{uuid4().hex[:6]}", slug=f"comm-blog-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.blog_page)
        cls.blog_page.save_revision().publish()
        cls.blog_page.refresh_from_db()

        # 2. 创建并发布一篇 GuidePage (证明全站多 Page 通用复用)
        cls.topic = GuideTopic.objects.create(name=f"评论主题-{uuid4().hex[:6]}", slug=f"topic-{uuid4().hex[:6]}")
        cls.guide_index = GuideIndexPage(title="专栏大厅", slug=f"guide-hall-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.guide_index)
        cls.guide_page = GuidePage(
            title=f"评论测试指南-{uuid4().hex[:6]}",
            slug=f"comm-guide-{uuid4().hex[:6]}",
            topic=cls.topic,
            body=[("step", {"step_number": "1", "title": "步骤1", "content": "内容"})],
        )
        cls.guide_index.add_child(instance=cls.guide_page)
        cls.guide_page.save_revision().publish()
        cls.guide_page.refresh_from_db()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.blog_page.delete(allow_physical_purge=True)
            cls.guide_page.delete(allow_physical_purge=True)
            cls.guide_index.delete(allow_physical_purge=True)
            cls.topic.delete()
        except Exception:
            pass

    def setUp(self):
        self._cleanup_comment_state()

    def tearDown(self):
        self._cleanup_comment_state()

    def _cleanup_comment_state(self):
        CommentDailyQuota.objects.filter(
            subject_key__in=[
                f"user:{self.user_a.id}",
                f"user:{self.user_b.id}",
                f"user:{self.user_c.id}",
                "ip:127.0.0.1",
                "ip:10.243.1.7",
            ]
        ).delete()
        try:
            r = _get_redis_client(write=True)
            if r is not None:
                for pattern in (
                    f"myblog:ratelimit:comment:user:{self.user_a.id}:*",
                    f"myblog:ratelimit:comment:user:{self.user_b.id}:*",
                    f"myblog:ratelimit:comment:user:{self.user_c.id}:*",
                    "myblog:ratelimit:comment:ip:127.0.0.1:*",
                    "myblog:ratelimit:comment:ip:10.243.1.7:*",
                    "myblog:captcha:rl:ip:10.243.1.7",
                ):
                    for key in r.scan_iter(match=pattern):
                        r.delete(key)
        except Exception:
            pass

    def test_01_universal_page_binding_and_policy_guards(self):
        """测试全站通用 Page 挂载与准入守卫 (C-01 & C-02)"""
        req = self.factory.get("/")

        # 1. BlogPage 允许评论
        target, is_restricted = get_accessible_comment_page(req, self.blog_page.id)
        self.assertEqual(target.id, self.blog_page.id)

        # 2. GuidePage 同样零成本允许评论（证明全站多 Page 通用复用）
        target_g, _ = get_accessible_comment_page(req, self.guide_page.id)
        self.assertEqual(target_g.id, self.guide_page.id)

        # 3. 根首页 HomePage 明确禁止评论
        with self.assertRaises(PermissionDenied):
            get_accessible_comment_page(req, self.home.id)

        # 4. 栏目页 GuideIndexPage 明确禁止评论
        with self.assertRaises(PermissionDenied):
            get_accessible_comment_page(req, self.guide_index.id)

        # 5. 树根 Root (depth=1) 明确禁止评论
        with self.assertRaises(PermissionDenied):
            get_accessible_comment_page(req, 1)

    def test_02_flat_two_level_threading_and_replied_user(self):
        """测试扁平两级反嵌套：回复子回复时恒指向根评论并自动提取被回复人 (C-05)"""
        # 1. Alice 发表根评论
        root = CommentService.post_comment(
            page=self.blog_page,
            author=self.user_a,
            raw_content="这是 Alice 发表的根评论。",
            auto_approve=True,
        )
        self.assertTrue(root.is_root)
        self.assertEqual(root.reply_count, 0)

        # 2. Bob 回复根评论 -> parent=root
        reply1 = CommentService.post_comment(
            page=self.blog_page,
            author=self.user_b,
            raw_content="Bob 回复 Alice 的根评论。",
            parent_id=root.id,
            auto_approve=True,
        )
        self.assertEqual(reply1.parent_id, root.id)
        self.assertIsNone(reply1.replied_to_user)
        root.refresh_from_db()
        self.assertEqual(root.reply_count, 1)

        # 3. Charlie 在前端点击回复 Bob (reply1) -> 核心反嵌套契约：parent 恒指向 root，replied_to_user=Bob
        reply2 = CommentService.post_comment(
            page=self.blog_page,
            author=self.user_c,
            raw_content="Charlie 回复 Bob 的子回复。",
            parent_id=root.id,
            reply_to_comment_id=reply1.id,
            auto_approve=True,
        )
        self.assertEqual(reply2.parent_id, root.id, "即使回复子回复，parent 也必须恒指向顶级根评论！")
        self.assertEqual(reply2.replied_to_user_id, self.user_b.id, "必须精准记录被回复的用户 Bob")
        root.refresh_from_db()
        self.assertEqual(root.reply_count, 2)

        # 4. 尝试跨页面关联父评论 -> 必须被拒绝
        with self.assertRaises(ValidationError):
            CommentService.post_comment(
                page=self.guide_page,
                author=self.user_a,
                raw_content="尝试跨页面回复",
                parent_id=root.id,
            )

        # 5. 尝试将已是回复的评论作为 parent -> 必须阻断递归嵌套
        with self.assertRaises(ValidationError):
            CommentService.post_comment(
                page=self.blog_page,
                author=self.user_a,
                raw_content="尝试三级嵌套",
                parent_id=reply1.id,
            )

    def test_03_versioned_moderation_cas_and_reply_count_stability(self):
        """测试版本审核防盲批 CAS (C-03) 与已上线评论编辑审批时 reply_count 绝不累加 (C-04)"""
        # 1. 提交待审根评论与待审子回复
        root = CommentService.post_comment(self.blog_page, self.user_a, "根评论初始版本", auto_approve=True)
        sub = CommentService.post_comment(self.blog_page, self.user_b, "子回复 V1 待审核", parent_id=root.id, auto_approve=False)

        self.assertFalse(sub.is_publicly_visible)
        root.refresh_from_db()
        self.assertEqual(root.reply_count, 0, "待审核的子回复绝不计入 reply_count")

        # 2. 批准子回复 V1
        CommentService.moderate_comment(sub.id, action="approve", reviewer=self.admin_user, expected_version_id=sub.pending_version_id)
        sub.refresh_from_db()
        root.refresh_from_db()
        self.assertTrue(sub.is_publicly_visible)
        self.assertEqual(root.reply_count, 1, "子回复首次批准上线，reply_count +1")

        # 3. 作者 Bob 在 5 分钟内修改子回复 -> 产生待审版本 V2
        v2 = CommentService.edit_comment(sub.id, self.user_b, "子回复修改为 V2 内容")
        sub.refresh_from_db()
        root.refresh_from_db()
        self.assertEqual(sub.pending_version_id, v2.id)
        self.assertTrue(sub.is_publicly_visible, "V2 待审期间，旧 V1 依然在前台保持可见")
        self.assertEqual(root.reply_count, 1, "V2 待审期间，reply_count 保持为 1")

        # 4. CAS 防盲批校验：若传入错误的旧版本 ID 执行审批 -> 必须被拒绝！
        with self.assertRaises(ValidationError):
            CommentService.moderate_comment(sub.id, action="approve", reviewer=self.admin_user, expected_version_id=99999)

        # 5. C-04 核心计数保护：批准 V2 替换 V1 -> reply_count 绝对不能变成 2！
        CommentService.moderate_comment(sub.id, action="approve", reviewer=self.admin_user, expected_version_id=v2.id)
        sub.refresh_from_db()
        root.refresh_from_db()
        self.assertEqual(sub.approved_version_id, v2.id, "生效版本已成功无缝切换为 V2")
        self.assertEqual(root.reply_count, 1, "已上线子回复更新版本通过审批后，reply_count 绝不能翻倍！")

        # 6. 软删除子回复 -> reply_count 原子 -1
        CommentService.soft_delete_comment(sub.id, self.user_b)
        sub.refresh_from_db()
        root.refresh_from_db()
        self.assertFalse(sub.is_publicly_visible)
        self.assertEqual(root.reply_count, 0, "子回复软删除后 reply_count 原子减 1")

    def test_04_reactions_mutex_and_non_negative_counters(self):
        """测试点赞/点踩互斥切换与非负保护 (C-05)"""
        c = CommentService.post_comment(self.blog_page, self.user_a, "点赞测试评论", auto_approve=True)

        # 1. 作者自赞自踩被阻断
        with self.assertRaises(ValidationError):
            CommentService.react_comment(c.id, self.user_a, target_reaction=1)

        # 2. Bob 点赞 (+1 赞, 0 踩)
        res1 = CommentService.react_comment(c.id, self.user_b, target_reaction=1)
        self.assertEqual(res1["like_count"], 1)
        self.assertEqual(res1["dislike_count"], 0)

        # 3. Bob 重复点赞 -> 幂等返回
        res_repeat = CommentService.react_comment(c.id, self.user_b, target_reaction=1)
        self.assertEqual(res_repeat["like_count"], 1)

        # 4. Bob 切换为点踩 -> 互斥翻转 (-1 赞, +1 踩)
        res2 = CommentService.react_comment(c.id, self.user_b, target_reaction=-1)
        self.assertEqual(res2["like_count"], 0)
        self.assertEqual(res2["dislike_count"], 1)

        # 5. Bob 取消互动 -> 清除
        res3 = CommentService.react_comment(c.id, self.user_b, target_reaction=0)
        self.assertEqual(res3["like_count"], 0)
        self.assertEqual(res3["dislike_count"], 0)

    def test_05_anti_spam_honeypot_and_xss_sanitization(self):
        """测试防垃圾风控：蜜罐静默拦截与 nh3 评论白名单 (C-08)"""
        # 1. Markdown 净化：剔除 img, 危险 script, 强制注入 nofollow ugc
        import textwrap
        dirty = textwrap.dedent("""
        这是正常讨论内容。
        <script>alert('xss');</script>
        <img src="http://evil.com/track.png" />
        访问我的个人网站 [点击这里](http://example.com/blog)。
        """).strip()
        clean_html = sanitize_comment_html(dirty)
        self.assertNotIn("<script>", clean_html)
        self.assertNotIn("<img", clean_html, "评论绝对禁止内嵌 img 追踪像素")
        self.assertIn('rel="nofollow ugc noopener noreferrer"', clean_html, "外链必须强制注入 nofollow ugc")
        self.assertIn('href="http://example.com/blog"', clean_html)

        # 2. API 蜜罐拦截测试
        self.client.force_login(self.user_a)
        post_url = f"/api/v1/pages/{self.blog_page.id}/comments/post/"
        res_honeypot = self.client.post(post_url, {
            "content": "机器人提交的垃圾广告",
            "contact_website": "http://spambot.com",  # 触碰蜜罐
        })
        self.assertEqual(res_honeypot.status_code, 200)
        # 验证数据库中绝对没有写入该垃圾评论
        self.assertFalse(Comment.objects.filter(raw_content__contains="垃圾广告").exists() if hasattr(Comment, "raw_content") else CommentVersion.objects.filter(raw_content__contains="垃圾广告").exists())

    def test_06_restful_json_api_end_to_end(self):
        """测试全套 RESTful JSON API 读写闭环"""
        self.client.force_login(self.user_a)

        # 1. POST 发表根评论
        post_res = self.client.post(f"/api/v1/pages/{self.blog_page.id}/comments/post/", {
            "content": "**API 自动化端到端测试评论**",
        })
        self.assertEqual(post_res.status_code, 201)
        cid = post_res.json()["data"]["id"]

        # 审核通过它
        CommentService.moderate_comment(cid, action="approve", reviewer=self.admin_user)

        # 2. GET 根评论列表
        get_res = self.client.get(f"/api/v1/pages/{self.blog_page.id}/comments/")
        self.assertEqual(get_res.status_code, 200)
        data = get_res.json()
        self.assertEqual(data["status"], "success")
        found = next((item for item in data["items"] if item["id"] == cid), None)
        self.assertIsNotNone(found)
        self.assertIn("API 自动化端到端测试评论", found["safe_html"])

        # 3. POST 点赞
        self.client.force_login(self.user_b)
        react_res = self.client.post(f"/api/v1/comments/{cid}/react/", {"reaction": "1"})
        self.assertEqual(react_res.status_code, 200)
        self.assertEqual(react_res.json()["data"]["like_count"], 1)

    def test_07_guest_comment_submission(self):
        """测试未登录访客（Guest）发表评论全流程与流控"""
        guest_client = Client()
        ip = "10.243.1.7"

        # 1. 默认全站 members_only 模式下未登录访客被拒 -> 403
        res_no_name = guest_client.post(f"/api/v1/pages/{self.blog_page.id}/comments/post/", {
            "content": "未填昵称的留言",
        }, REMOTE_ADDR=ip)
        self.assertEqual(res_no_name.status_code, 403)

        community = CommunitySettings._get_or_create()
        prev_mode = community.comment_mode
        try:
            community.comment_mode = "controlled_guests"
            community.save(update_fields=["comment_mode"])
            PageCommentSettings.objects.update_or_create(
                page=self.blog_page,
                defaults={"allow_comments": True, "allow_guest_comments": True},
            )

            # 2. 开启受控访客后，裸 POST 缺少绑定 Cookie 与验证码 -> 400
            res_naked = guest_client.post(
                f"/api/v1/pages/{self.blog_page.id}/comments/post/",
                {"guest_name": "访客小红", "content": "裸请求尝试"},
                REMOTE_ADDR=ip,
            )
            self.assertEqual(res_naked.status_code, 400)

            # 3. 先签发图片验证码取得 HttpOnly 绑定 Cookie，再缺昵称提交 -> 400
            with patch("captcha.service.generate_captcha_code", return_value="G7K2"):
                issue_res = guest_client.post(
                    reverse("api_v1:captcha_challenge_issue"),
                    data={"scope": "guest_comment"},
                    REMOTE_ADDR=ip,
                )
            self.assertEqual(issue_res.status_code, 200)
            cid = issue_res.json()["captcha_id"]
            res_missing_name = guest_client.post(
                f"/api/v1/pages/{self.blog_page.id}/comments/post/",
                {"content": "忘记填昵称", "captcha_id": cid, "captcha_code": "G7K2"},
                REMOTE_ADDR=ip,
            )
            self.assertEqual(res_missing_name.status_code, 400)

            # 4. 提供 guest_name 与有效验证码正常发表 -> 201 且强制待审
            res_guest = guest_client.post(
                f"/api/v1/pages/{self.blog_page.id}/comments/post/",
                {
                    "guest_name": "访客小红",
                    "content": "这是一条来自访客小红的受控讨论",
                    "captcha_id": cid,
                    "captcha_code": "G7K2",
                },
                REMOTE_ADDR=ip,
            )
            self.assertEqual(res_guest.status_code, 201)
            gc_id = res_guest.json()["data"]["id"]
            gc = Comment.objects.get(id=gc_id)
            self.assertIsNone(gc.author)
            self.assertEqual(gc.status, "pending")
            self.assertFalse(gc.is_publicly_visible)
        finally:
            community.comment_mode = prev_mode
            community.save(update_fields=["comment_mode"])
            PageCommentSettings.objects.filter(page=self.blog_page).delete()


if __name__ == "__main__":
    unittest.main()
