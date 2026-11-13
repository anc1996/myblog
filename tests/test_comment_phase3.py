"""
tests.test_comment_phase3
Phase 3 双轨防刷评论、日配额、P0-6 字段级权限隔离与 Redis/PG 并发专项测试。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import os
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django

django.setup()

from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connections
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from wagtail.models import GroupPagePermission

from blog.models import BlogIndexPage, BlogPage
from captcha.service import _get_redis_client
from comments.models import (
    Comment,
    CommentDailyQuota,
    CommentGovernanceAuditLog,
    CommunitySettings,
    PageCommentSettings,
)
from comments.rate_limit import check_comment_rate_limit, get_client_ip
from comments.sanitizer import inspect_links_and_sanitize
from comments.services import (
    CommentConflictError,
    CommentQueueBudgetExceeded,
    CommentQuotaExceeded,
    CommentService,
)
from content_lifecycle.recycle import RecycleService
from home.models import HomePage
from identity.account_lifecycle import AccountLifecycleService
from identity.models import User
from wagtail.models import PageViewRestriction


class CommentPhase3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.home = HomePage.objects.first()
        cls.admin = User.objects.filter(is_superuser=True, is_active=True).first()
        if not cls.admin:
            cls.admin = User.objects.create_superuser(
                username=f"p3_admin_{uuid4().hex[:6]}",
                email="p3_admin@test.com",
                password="StrongPassword!123",
            )
        cls.blog_index = BlogIndexPage.objects.first()
        if not cls.blog_index:
            cls.blog_index = BlogIndexPage(title="P3 Blog Index", slug=f"p3-idx-{uuid4().hex[:6]}")
            cls.home.add_child(instance=cls.blog_index)
            cls.blog_index.save_revision().publish()
            cls.blog_index.refresh_from_db()

    def setUp(self):
        self.created_pages: list[BlogPage] = []
        self.created_user_ids: list[int] = []
        self.created_group_ids: list[int] = []
        self.tracked_ips: list[str] = []
        self.tracked_browser_keys: list[str] = []
        self.tracked_captcha_ids: list[str] = []

        solo = CommunitySettings._get_or_create()
        self._orig_settings = {
            "comment_mode": solo.comment_mode,
            "guest_daily_ip_cap": solo.guest_daily_ip_cap,
            "guest_daily_browser_cap": solo.guest_daily_browser_cap,
            "member_daily_cap": solo.member_daily_cap,
            "new_member_daily_cap": solo.new_member_daily_cap,
            "new_member_days_threshold": solo.new_member_days_threshold,
            "new_member_default_pending": solo.new_member_default_pending,
            "max_pending_queue_budget": solo.max_pending_queue_budget,
        }

    def tearDown(self):
        solo = CommunitySettings._get_or_create()
        for field, val in self._orig_settings.items():
            setattr(solo, field, val)
        solo.save()

        for p in reversed(self.created_pages):
            try:
                if p.id and BlogPage.objects.filter(id=p.id).exists():
                    RecycleService.recycle_page_or_subtree(p, actor=self.admin)
                    RecycleService.purge_page_or_subtree(p.id, actor=self.admin)
            except Exception:
                pass

        for uid in reversed(self.created_user_ids):
            try:
                CommentDailyQuota.objects.filter(subject_kind="user", subject_key=f"user:{uid}").delete()
                if User.objects.filter(pk=uid).exists():
                    AccountLifecycleService.hard_delete(
                        actor=self.admin,
                        target_user_id=uid,
                        transfer_to_user_id=self.admin.id,
                        confirmed=True,
                    )
            except Exception:
                pass

        for gid in reversed(self.created_group_ids):
            Group.objects.filter(id=gid).delete()

        if self.tracked_ips:
            CommentDailyQuota.objects.filter(
                subject_kind="ip",
                subject_key__in=[f"ip:{ip}" for ip in self.tracked_ips],
            ).delete()
        if self.tracked_browser_keys:
            CommentDailyQuota.objects.filter(
                subject_kind="guest_browser",
                subject_key__in=self.tracked_browser_keys,
            ).delete()

        self._cleanup_redis()

    def _cleanup_redis(self):
        try:
            r = _get_redis_client(write=True)
            if r is None:
                return
            from comments.rate_limit import _resolve_prefix
            p = _resolve_prefix()
            for uid in self.created_user_ids:
                for k in r.scan_iter(match=f"{p}:ratelimit:comment:user:{uid}:*"):
                    r.delete(k)
            for ip in self.tracked_ips:
                for pattern in (
                    f"{p}:ratelimit:comment:ip:{ip}:*",
                    f"{p}:captcha:rl:ip:{ip}",
                ):
                    for k in r.scan_iter(match=pattern):
                        r.delete(k)
            for bkey in self.tracked_browser_keys:
                for k in r.scan_iter(match=f"{p}:ratelimit:comment:browser:{bkey}:*"):
                    r.delete(k)
            for cid in self.tracked_captcha_ids:
                r.delete(f"{p}:captcha:ch:{cid}", f"{p}:captcha:img:{cid}")
        except Exception:
            pass

    def _ip(self, d: int) -> str:
        ip = f"10.244.7.{d}"
        self.tracked_ips.append(ip)
        return ip

    def _make_user(self, prefix: str, *, days_ago: int = 0, is_staff: bool = False) -> User:
        u = User.objects.create_user(
            username=f"{prefix}_{uuid4().hex[:6]}",
            password="StrongUserPass!123",
            is_staff=is_staff,
        )
        if days_ago:
            u.date_joined = timezone.now() - timedelta(days=days_ago)
            u.save(update_fields=["date_joined"])
        self.created_user_ids.append(u.id)
        return u

    def _make_blog(self, *, owner=None) -> BlogPage:
        p = BlogPage(
            title=f"Phase3测试文章-{uuid4().hex[:6]}",
            slug=f"p3-blog-{uuid4().hex[:6]}",
            authored_on=date(2026, 11, 12),
            owner=owner or self.admin,
            body=[("markdown", "Phase 3 评论防刷测试正文。")],
        )
        self.blog_index.add_child(instance=p)
        p.save_revision(user=owner or self.admin).publish(user=self.admin)
        p.refresh_from_db()
        self.created_pages.append(p)
        return p

    def _issue_captcha(self, client: Client, scope: str, ip: str, code: str = "K7M9") -> str:
        with patch("captcha.service.generate_captcha_code", return_value=code):
            resp = client.post(
                reverse("api_v1:captcha_challenge_issue"),
                data={"scope": scope},
                REMOTE_ADDR=ip,
            )
        self.assertEqual(resp.status_code, 200, resp.content.decode("utf-8"))
        cid = resp.json()["captcha_id"]
        self.tracked_captcha_ids.append(cid)
        return cid

    def test_community_settings_modes_and_tightening(self):
        """验证全站 comment_mode（closed / members_only / controlled_guests）与单篇收紧规则"""
        page = self._make_blog()
        member = self._make_user("p3_mem", days_ago=15)
        ip = self._ip(11)
        guest_client = Client()

        # 1. 默认 members_only：前台详情页提示登录，API 返回 requires_login=True，游客 POST 403
        ssr_resp = guest_client.get(page.url)
        self.assertEqual(ssr_resp.status_code, 200)
        ssr_html = ssr_resp.content.decode("utf-8")
        self.assertIn('id="comment-login-prompt"', ssr_html)
        self.assertNotIn('id="comment-post-form"', ssr_html)

        list_resp = guest_client.get(f"/api/v1/pages/{page.id}/comments/")
        self.assertEqual(list_resp.status_code, 200)
        cs = list_resp.json()["comment_settings"]
        self.assertEqual(cs["comment_mode"], "members_only")
        self.assertFalse(cs["effective_allow_guests"])
        self.assertTrue(cs["requires_login"])

        self.assertEqual(
            guest_client.post(
                f"/api/v1/pages/{page.id}/comments/post/",
                {"guest_name": "访客", "content": "默认模式下不应通过"},
                REMOTE_ADDR=ip,
            ).status_code,
            403,
        )

        # 2. 全局切到 controlled_guests，但该文章未勾选 allow_guest_comments -> 依然禁止游客
        solo = CommunitySettings._get_or_create()
        solo.comment_mode = "controlled_guests"
        solo.save()
        self.assertIn('id="comment-login-prompt"', guest_client.get(page.url).content.decode("utf-8"))
        self.assertEqual(
            guest_client.post(
                f"/api/v1/pages/{page.id}/comments/post/",
                {"guest_name": "访客", "content": "单篇未放开访客"},
                REMOTE_ADDR=ip,
            ).status_code,
            403,
        )

        # 3. 管理员开启单篇 allow_guest_comments=True -> SSR 显示访客表单 + 图片验证码组件
        CommentService.update_page_comment_settings(
            page_id=page.id,
            actor=self.admin,
            allow_comments=True,
            allow_guest_comments=True,
        )
        guest_ssr = guest_client.get(page.url).content.decode("utf-8")
        self.assertIn('id="comment-post-form"', guest_ssr)
        self.assertIn('data-captcha-scope="guest_comment"', guest_ssr)
        self.assertIn('data-defer-load="false"', guest_ssr)

        # 4. 全局切回 closed -> 同时阻断游客和登录会员，保留可读历史评论
        c_hist = CommentService.post_comment(
            page=page,
            author=member,
            raw_content="关停前历史评论",
            auto_approve=True,
            ip_address=ip,
        )
        solo.comment_mode = "closed"
        solo.save()
        self.assertEqual(
            guest_client.post(
                f"/api/v1/pages/{page.id}/comments/post/",
                {"guest_name": "访客", "content": "全站暂停时禁止"},
                REMOTE_ADDR=ip,
            ).status_code,
            403,
        )
        member_client = Client()
        member_client.force_login(member)
        self.assertEqual(
            member_client.post(
                f"/api/v1/pages/{page.id}/comments/post/",
                {"content": "全站暂停时会员也禁止"},
                REMOTE_ADDR=ip,
            ).status_code,
            403,
        )
        closed_list = guest_client.get(f"/api/v1/pages/{page.id}/comments/")
        self.assertEqual(closed_list.status_code, 200)
        self.assertFalse(closed_list.json()["comment_settings"]["allow_comments"])
        self.assertEqual(closed_list.json()["items"][0]["id"], c_hist.id)

        # 5. 全局切回 members_only -> 原先勾选过 allow_guest_comments 的文章立刻停收访客
        solo.comment_mode = "members_only"
        solo.save()
        self.assertEqual(
            guest_client.post(
                f"/api/v1/pages/{page.id}/comments/post/",
                {"guest_name": "访客", "content": "全局收紧后立即停收"},
                REMOTE_ADDR=ip,
            ).status_code,
            403,
        )

    def test_page_settings_p0_6_authorization_and_ui_isolation(self):
        """验证 P0-6：作者只能改自己页的基础开关且严禁修改 allow_guest_comments；主编与超管可修改"""
        author_group = Group.objects.create(name=f"p3_authors_{uuid4().hex[:6]}")
        editor_group = Group.objects.create(name=f"p3_editors_{uuid4().hex[:6]}")
        mod_group = Group.objects.create(name=f"p3_mods_{uuid4().hex[:6]}")
        self.created_group_ids.extend([author_group.id, editor_group.id, mod_group.id])

        access_admin = Permission.objects.get(content_type__app_label="wagtailadmin", codename="access_admin")
        add_page_perm = Permission.objects.get(content_type__app_label="wagtailcore", codename="add_page")
        change_page_perm = Permission.objects.get(content_type__app_label="wagtailcore", codename="change_page")
        publish_page_perm = Permission.objects.get(content_type__app_label="wagtailcore", codename="publish_page")
        moderate_perm = Permission.objects.get(content_type__app_label="comments", codename="moderate_comment")

        author_group.permissions.add(access_admin)
        editor_group.permissions.add(access_admin)
        mod_group.permissions.add(access_admin, moderate_perm)

        GroupPagePermission.objects.create(group=author_group, page=self.blog_index, permission=add_page_perm)
        GroupPagePermission.objects.create(group=author_group, page=self.blog_index, permission=publish_page_perm)
        GroupPagePermission.objects.create(group=editor_group, page=self.blog_index, permission=add_page_perm)
        GroupPagePermission.objects.create(group=editor_group, page=self.blog_index, permission=change_page_perm)
        GroupPagePermission.objects.create(group=editor_group, page=self.blog_index, permission=publish_page_perm)

        author_a = self._make_user("p3_autha", days_ago=10)
        author_b = self._make_user("p3_authb", days_ago=10)
        editor_u = self._make_user("p3_editor", days_ago=10)
        mod_only = self._make_user("p3_mod", days_ago=10)
        author_a.groups.add(author_group)
        author_b.groups.add(author_group)
        editor_u.groups.add(editor_group)
        mod_only.groups.add(mod_group)

        page_a = self._make_blog(owner=author_a)
        page_b = self._make_blog(owner=author_b)

        # 1. author_a 编辑自己的 page_a：UI 隐藏 allow_guest_comments，可保存基础设置，伪造 POST allow_guest_comments 返回 403
        client_a = Client()
        client_a.force_login(author_a)
        edit_a = client_a.get(f"/admin/pages/{page_a.id}/edit/")
        self.assertEqual(edit_a.status_code, 200)
        edit_a_html = edit_a.content.decode("utf-8")
        self.assertIn('data-comment-field="allow_comments"', edit_a_html)
        self.assertNotIn('data-comment-field="allow_guest_comments"', edit_a_html)
        self.assertNotIn("允许未登录访客在本页发表受控留言", edit_a_html)

        ok_save = client_a.post(
            f"/admin/comments/page-settings/{page_a.id}/",
            {
                "allow_comments": "true",
                "close_reason": "作者自定义公告",
                "auto_moderate_new": "true",
            },
        )
        self.assertEqual(ok_save.status_code, 200)
        self.assertFalse(ok_save.json()["allow_guest_comments"])

        forged = client_a.post(
            f"/admin/comments/page-settings/{page_a.id}/",
            {
                "allow_comments": "true",
                "allow_guest_comments": "true",
                "close_reason": "",
                "auto_moderate_new": "false",
            },
        )
        self.assertEqual(forged.status_code, 403)
        self.assertFalse(PageCommentSettings.objects.get(page=page_a).allow_guest_comments)

        # 2. author_a 越权访问或修改 author_b 的 page_b -> 403
        self.assertEqual(client_a.get(f"/admin/comments/page-settings/{page_b.id}/").status_code, 403)
        self.assertEqual(
            client_a.post(
                f"/admin/comments/page-settings/{page_b.id}/",
                {"allow_comments": "false"},
            ).status_code,
            403,
        )
        self.assertEqual(client_a.get(f"/admin/comments/page-comments/{page_b.id}/").status_code, 403)
        # author_a 对自己的 page_comments 可查看，但 can_moderate 为 False
        own_comms = client_a.get(f"/admin/comments/page-comments/{page_a.id}/")
        self.assertEqual(own_comms.status_code, 200)
        self.assertFalse(own_comms.json()["can_moderate"])

        # 3. 仅有 moderate_comment 的审核员也不能改 allow_guest_comments -> 403
        client_mod = Client()
        client_mod.force_login(mod_only)
        self.assertEqual(client_mod.get(f"/admin/comments/page-settings/{page_a.id}/").status_code, 403)
        self.assertEqual(
            client_mod.post(
                f"/admin/comments/page-settings/{page_a.id}/",
                {"allow_comments": "false"},
            ).status_code,
            403,
        )
        self.assertEqual(
            client_mod.post(
                f"/admin/comments/page-settings/{page_a.id}/",
                {"allow_comments": "true", "allow_guest_comments": "true"},
            ).status_code,
            403,
        )
        mod_panel_comments = client_mod.get(f"/admin/comments/page-comments/{page_a.id}/")
        self.assertEqual(mod_panel_comments.status_code, 200)
        self.assertTrue(mod_panel_comments.json()["can_moderate"])

        # 4. 主编 (Editors) UI 展示 allow_guest_comments，且可修改成功
        client_editor = Client()
        client_editor.force_login(editor_u)
        edit_editor = client_editor.get(f"/admin/pages/{page_a.id}/edit/")
        self.assertEqual(edit_editor.status_code, 200)
        edit_editor_html = edit_editor.content.decode("utf-8")
        self.assertIn('data-comment-field="allow_guest_comments"', edit_editor_html)
        self.assertIn("允许未登录访客在本页发表受控留言", edit_editor_html)
        editor_post = client_editor.post(
            f"/admin/comments/page-settings/{page_a.id}/",
            {
                "allow_comments": "true",
                "allow_guest_comments": "true",
                "close_reason": "",
                "auto_moderate_new": "false",
            },
        )
        self.assertEqual(editor_post.status_code, 200)
        self.assertTrue(PageCommentSettings.objects.get(page=page_a).allow_guest_comments)

    def test_guest_captcha_before_tx_non_replayable_and_content_rules(self):
        """验证访客验证码先于 SQL 事务核销（SQL 回滚不可重放）、禁外链、500 字上限及不可绕过待审"""
        page = self._make_blog()
        solo = CommunitySettings._get_or_create()
        solo.comment_mode = "controlled_guests"
        solo.save()
        PageCommentSettings.objects.create(page=page, allow_comments=True, allow_guest_comments=True)

        ip = self._ip(21)
        guest_client = Client()

        # 1. 无绑定 Cookie 的裸 POST 被拒 (400)
        naked = guest_client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {"guest_name": "访客", "content": "无 Cookie 提交"},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(naked.status_code, 400)
        self.assertEqual(naked.json()["code"], "captcha_binding_missing")

        # 2. 输错一次验证码立即作废，不能拿同一个 captcha_id 再次猜对
        cid1 = self._issue_captcha(guest_client, "guest_comment", ip, code="W9P4")
        wrong = guest_client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {"guest_name": "访客", "content": "首次输错", "captcha_id": cid1, "captcha_code": "XXXX"},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(wrong.status_code, 400)
        replay_after_wrong = guest_client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {"guest_name": "访客", "content": "拿旧挑战重试", "captcha_id": cid1, "captcha_code": "W9P4"},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(replay_after_wrong.status_code, 400)

        # 3. 验证码正确但内容含外链 -> SQL 回滚 400，且该验证码已核销绝不能重放
        cid2 = self._issue_captcha(guest_client, "guest_comment", ip, code="M4N7")
        with_link = guest_client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {
                "guest_name": "访客",
                "content": "请看广告 https://spam.example.com/offer",
                "captcha_id": cid2,
                "captcha_code": "M4N7",
            },
            REMOTE_ADDR=ip,
        )
        self.assertEqual(with_link.status_code, 400)
        self.assertEqual(Comment.objects.filter(page=page).count(), 0)

        replay_after_sql_rollback = guest_client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {
                "guest_name": "访客",
                "content": "去掉链接重放刚才成功的验证码",
                "captcha_id": cid2,
                "captcha_code": "M4N7",
            },
            REMOTE_ADDR=ip,
        )
        self.assertEqual(replay_after_sql_rollback.status_code, 400)
        self.assertEqual(Comment.objects.filter(page=page).count(), 0)

        # 4. 超出 500 字符被拒
        # 注意：guest_client 刚才在 step 3 已通过验证码消费了本浏览器 60s max 1 的写频率槽位，此处先验证同浏览器 429 拦截，
        # 再使用独立浏览器会话验证 500 字符上限。
        cid3 = self._issue_captcha(guest_client, "guest_comment", ip, code="R5T8")
        rate_limited_second = guest_client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {
                "guest_name": "访客",
                "content": "同一浏览器 60 秒内第二次有效提交应被限流",
                "captcha_id": cid3,
                "captcha_code": "R5T8",
            },
            REMOTE_ADDR=ip,
        )
        self.assertEqual(rate_limited_second.status_code, 429)

        guest_client_len = Client()
        cid4 = self._issue_captcha(guest_client_len, "guest_comment", ip, code="H2J6")
        too_long = guest_client_len.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {
                "guest_name": "访客",
                "content": "字" * 501,
                "captcha_id": cid4,
                "captcha_code": "H2J6",
            },
            REMOTE_ADDR=ip,
        )
        self.assertEqual(too_long.status_code, 400)

        # 5. 服务层强传 auto_approve=True 也绝不能跳过访客人工待审
        svc_guest = CommentService.post_comment(
            page=page,
            author=None,
            guest_name="服务层访客",
            raw_content="合法短留言",
            ip_address=ip,
            auto_approve=True,
        )
        self.assertEqual(svc_guest.status, "pending")
        self.assertFalse(svc_guest.is_publicly_visible)
        self.assertIn("guest_submission", svc_guest.latest_version.risk_reasons)

    def test_member_tiers_and_member_captcha_trigger(self):
        """验证新会员 (<7天) 与可信会员 (>=7天) 内容约束、高频触发 member_comment 验证码与各接口限流"""
        page = self._make_blog()
        new_member = self._make_user("p3_new", days_ago=1)
        trusted_member = self._make_user("p3_old", days_ago=30)
        ip = self._ip(31)

        # 新会员：1000 字上限 + 最多 1 个外链 (1个外链转待审，2个外链拒绝；edit 同受约束)
        with self.assertRaises(ValidationError):
            CommentService.post_comment(page=page, author=new_member, raw_content="N" * 1001, ip_address=ip)
        with self.assertRaises(ValidationError):
            CommentService.post_comment(
                page=page,
                author=new_member,
                raw_content="双链 [A](https://a.example.com) [B](https://b.example.com)",
                ip_address=ip,
            )
        one_link = CommentService.post_comment(
            page=page,
            author=new_member,
            raw_content="单链参考 [Doc](https://docs.example.com)",
            auto_approve=True,
            ip_address=ip,
        )
        self.assertEqual(one_link.status, "pending")
        with self.assertRaises(ValidationError):
            CommentService.edit_comment(one_link.id, new_member, "E" * 1001, ip_address=ip)

        # 可信会员：允许 2000 字与最多 2 个外链（转待审），3 个外链被拒
        c_two_links = CommentService.post_comment(
            page=page,
            author=trusted_member,
            raw_content="两个链接 [A](https://a.example.com) [B](https://b.example.com)",
            auto_approve=True,
            ip_address=ip,
        )
        self.assertEqual(c_two_links.status, "pending")
        with self.assertRaises(ValidationError):
            CommentService.post_comment(
                page=page,
                author=trusted_member,
                raw_content="三链 https://a.example.com https://b.example.com https://c.example.com",
                ip_address=ip,
            )

        # 高频触发 member_comment 验证码与 60s max 2 限流
        client = Client()
        client.force_login(new_member)
        r1 = client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {"content": "新会员第一条正常无链评论"},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(r1.status_code, 201)

        # 60s 内第 2 条无验证码 -> 400 captcha_required (member_comment)
        r2_no_cap = client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {"content": "新会员 60 秒内第二条"},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(r2_no_cap.status_code, 400)
        self.assertEqual(r2_no_cap.json()["code"], "captcha_required")
        self.assertEqual(r2_no_cap.json()["captcha_scope"], "member_comment")

        cid = self._issue_captcha(client, "member_comment", ip, code="Q6W2")
        r2_ok = client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {"content": "带验证码的第二条", "captcha_id": cid, "captcha_code": "Q6W2"},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(r2_ok.status_code, 201)

        # 60s 内第 3 条超过 new_member 2/60s -> 429 rate_limited
        cid2 = self._issue_captcha(client, "member_comment", ip, code="Z8X4")
        r3_limited = client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {"content": "超限第三条", "captcha_id": cid2, "captcha_code": "Z8X4"},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(r3_limited.status_code, 429)
        self.assertIn("Retry-After", r3_limited.headers)

        # Redis 不可用 Fail-Closed -> 503
        with patch("comments.rate_limit._get_redis_client", return_value=None):
            self.assertEqual(
                client.post(
                    f"/api/v1/pages/{page.id}/comments/post/",
                    {"content": "断网探测"},
                    REMOTE_ADDR=ip,
                ).status_code,
                503,
            )
            self.assertEqual(
                client.post(
                    f"/api/v1/comments/{one_link.id}/edit/",
                    {"content": "断网编辑探测"},
                    REMOTE_ADDR=ip,
                ).status_code,
                503,
            )
            self.assertEqual(
                client.post(
                    f"/api/v1/comments/{one_link.id}/react/",
                    {"reaction": "1"},
                    REMOTE_ADDR=ip,
                ).status_code,
                503,
            )

    def test_redis_lua_rate_limit_concurrency(self):
        """验证 Redis Lua 单次脚本在高并发下严格放行不超过上限"""
        new_member = self._make_user("p3_rlconc", days_ago=1)
        ip = self._ip(41)
        factory = RequestFactory()

        def _worker():
            connections.close_all()
            try:
                req = factory.post("/", REMOTE_ADDR=ip)
                allowed, _, _ = check_comment_rate_limit(req, new_member, action="post")
                return allowed
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: _worker(), range(6)))
        self.assertEqual(sum(1 for ok in results if ok), 2, f"NewMember 60s限流应严格只放行2次: {results}")

    def test_pg_daily_quota_concurrency_and_pending_queue_budget(self):
        """验证 PostgreSQL 持久日配额并发不超发、删除不退额、以及待审队列预算熔断"""
        page = self._make_blog()
        solo = CommunitySettings._get_or_create()
        solo.comment_mode = "controlled_guests"
        solo.new_member_daily_cap = 3
        solo.guest_daily_browser_cap = 2
        solo.guest_daily_ip_cap = 3
        solo.save()
        PageCommentSettings.objects.create(page=page, allow_comments=True, allow_guest_comments=True)

        conc_member = self._make_user("p3_pgconc", days_ago=1)
        ip = self._ip(51)

        def _post_worker(idx: int):
            connections.close_all()
            try:
                CommentService.post_comment(
                    page=page,
                    author=conc_member,
                    raw_content=f"并发配额评论 #{idx}",
                    ip_address=ip,
                )
                return "ok"
            except CommentQuotaExceeded:
                return "quota_exceeded"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(_post_worker, range(8)))
        self.assertEqual(outcomes.count("ok"), 3, outcomes)
        self.assertEqual(outcomes.count("quota_exceeded"), 5, outcomes)
        quota_row = CommentDailyQuota.objects.get(
            subject_kind="user",
            subject_key=f"user:{conc_member.id}",
            quota_date=timezone.localdate(),
            action="post_comment",
        )
        self.assertEqual(quota_row.used_count, 3)

        # 软删除评论不退还今日配额
        first_comment = Comment.objects.filter(page=page, author=conc_member).first()
        CommentService.soft_delete_comment(first_comment.id, actor=conc_member)
        with self.assertRaises(CommentQuotaExceeded):
            CommentService.post_comment(
                page=page,
                author=conc_member,
                raw_content="删评后不应退额",
                ip_address=ip,
            )

        # 访客更换浏览器 Cookie（模拟脚本丢 Cookie）仍受单 IP 日配额硬边界阻断
        for i in range(3):
            bkey = f"guest:rot_{uuid4().hex[:10]}"
            self.tracked_browser_keys.append(bkey)
            CommentService.post_comment(
                page=page,
                author=None,
                guest_name=f"访客{i}",
                raw_content=f"同 IP 访客第 {i + 1} 条",
                ip_address=ip,
                guest_browser_key=bkey,
                require_guest_browser=True,
            )
        bkey_over = f"guest:rot_{uuid4().hex[:10]}"
        self.tracked_browser_keys.append(bkey_over)
        with self.assertRaises(CommentQuotaExceeded):
            CommentService.post_comment(
                page=page,
                author=None,
                guest_name="访客超限",
                raw_content="同 IP 访客第 4 条应被拦截",
                ip_address=ip,
                guest_browser_key=bkey_over,
                require_guest_browser=True,
            )

        # 待审队列熔断：设置预算 == 当前待审数量，访客被 429 熔断而会员不受影响
        current_pending = Comment.objects.filter(pending_version__isnull=False).count()
        solo.max_pending_queue_budget = current_pending
        solo.save()
        ip2 = self._ip(52)
        bkey2 = f"guest:bud_{uuid4().hex[:10]}"
        self.tracked_browser_keys.append(bkey2)
        with self.assertRaises(CommentQueueBudgetExceeded):
            CommentService.post_comment(
                page=page,
                author=None,
                guest_name="预算超限访客",
                raw_content="待审满载应被熔断",
                ip_address=ip2,
                guest_browser_key=bkey2,
                require_guest_browser=True,
            )
        other_member = self._make_user("p3_othermem", days_ago=1)
        m_ok = CommentService.post_comment(
            page=page,
            author=other_member,
            raw_content="会员不受匿名待审队列预算熔断",
            ip_address=ip2,
        )
        self.assertIsNotNone(m_ok.id)

    def test_idempotency_receipt_atomic_409_moderate_cas_and_lift_audit(self):
        """验证 CommentMutationReceipt 并发幂等不双扣额、异载荷 409、reject CAS 及解封审计日志"""
        page = self._make_blog()
        member = self._make_user("p3_idem", days_ago=1)
        ip = self._ip(61)
        req_key = f"idem-{uuid4().hex[:12]}"

        # 1. 4 线程携带相同 request_key 与相同载荷并发提交 -> 返回同一个评论且仅扣 1 次日配额
        def _idem_worker():
            connections.close_all()
            try:
                return CommentService.post_comment(
                    page=page,
                    author=member,
                    raw_content="幂等并发正文",
                    ip_address=ip,
                    request_key=req_key,
                ).id
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=4) as pool:
            cids = list(pool.map(lambda _: _idem_worker(), range(4)))
        self.assertEqual(len(set(cids)), 1)
        quota = CommentDailyQuota.objects.get(
            subject_kind="user",
            subject_key=f"user:{member.id}",
            quota_date=timezone.localdate(),
            action="post_comment",
        )
        self.assertEqual(quota.used_count, 1)

        # 2. 相同 request_key 但内容不同 -> 服务层抛 CommentConflictError，API 返回 409
        with self.assertRaises(CommentConflictError):
            CommentService.post_comment(
                page=page,
                author=member,
                raw_content="篡改后的不同内容",
                ip_address=ip,
                request_key=req_key,
            )
        c_client = Client()
        c_client.force_login(member)
        conflict_resp = c_client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {"content": "API 不同载荷重放", "request_key": req_key},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(conflict_resp.status_code, 409)

        # 3. 审核批准后 pending_version 已清空，再拿旧 pending_version_id 调 reject -> 必须报冲突
        comment = Comment.objects.get(id=cids[0])
        old_ver_id = comment.pending_version_id
        CommentService.moderate_comment(
            comment_id=comment.id,
            action="approve",
            reviewer=self.admin,
            expected_version_id=old_ver_id,
            require_expected_version=True,
        )
        with self.assertRaises(ValidationError):
            CommentService.moderate_comment(
                comment_id=comment.id,
                action="reject",
                reviewer=self.admin,
                expected_version_id=old_ver_id,
                require_expected_version=True,
            )

        # 4. restrict_subject / lift_restriction 鉴权与审计日志
        target_key = f"user:{member.id}"
        with self.assertRaises(PermissionDenied):
            CommentService.restrict_subject(kind="user", subject_key=target_key, actor=member)
        CommentService.restrict_subject(kind="user", subject_key=target_key, hours=24, reason="测试禁言", actor=self.admin)
        with self.assertRaises(PermissionDenied):
            CommentService.lift_restriction(kind="user", subject_key=target_key, actor=member)
        self.assertTrue(CommentService.lift_restriction(kind="user", subject_key=target_key, actor=self.admin, reason="测试解封"))

        logs = list(
            CommentGovernanceAuditLog.objects.filter(subject_key=target_key).order_by("created_at").values_list("action", flat=True)
        )
        self.assertEqual(logs[-2:], ["restrict", "lift_restriction"])

    def test_alias_inherits_source_page_view_restrictions(self):
        """验证 Blocker #1：无密码别名页访问受密码保护的源页时，必须被拒绝并禁止公共缓存"""
        source = self._make_blog()
        CommentService.post_comment(
            page=source,
            author=self.admin,
            raw_content="受保护源页面的机密评论",
            auto_approve=True,
        )
        alias = source.create_alias(
            parent=self.blog_index,
            update_slug=f"alias-sec-{uuid4().hex[:6]}",
        )
        self.created_pages.append(alias)
        PageViewRestriction.objects.create(
            page=source,
            restriction_type=PageViewRestriction.PASSWORD,
            password="secret-password-123",
        )
        self.assertFalse(alias.view_restrictions.exists())
        self.assertTrue(source.get_view_restrictions().exists())

        guest = Client()
        # 未验证访问别名页的评论列表 -> 必须 403
        self.assertEqual(guest.get(f"/api/v1/pages/{alias.id}/comments/").status_code, 403)
        member = self._make_user("p3_alias_u", days_ago=10)
        mclient = Client()
        mclient.force_login(member)
        self.assertEqual(mclient.post(f"/api/v1/pages/{alias.id}/comments/post/", {"content": "越权发评"}).status_code, 403)

    def test_deleted_or_hidden_comment_cannot_be_resurrected_by_moderate(self):
        """验证 Blocker #2：已软删除或已隐藏的评论，不得被旧待审版本或 approve 请求复活"""
        page = self._make_blog()
        member = self._make_user("p3_resurrect", days_ago=10)

        c_del = CommentService.post_comment(page=page, author=member, raw_content="待审后将自删")
        del_ver_id = c_del.pending_version_id
        CommentService.soft_delete_comment(c_del.id, actor=member)
        c_del.refresh_from_db()
        self.assertIsNone(c_del.pending_version_id)
        with self.assertRaises(ValidationError):
            CommentService.moderate_comment(c_del.id, action="approve", reviewer=self.admin)
        with self.assertRaises(ValidationError):
            CommentService.moderate_comment(
                c_del.id, action="approve", reviewer=self.admin, expected_version_id=del_ver_id
            )
        c_del.refresh_from_db()
        self.assertEqual(c_del.status, "deleted")
        self.assertFalse(c_del.is_publicly_visible)

        c_hide = CommentService.post_comment(page=page, author=member, raw_content="待审后将隐藏")
        hide_ver_id = c_hide.pending_version_id
        CommentService.moderate_comment(c_hide.id, action="hide", reviewer=self.admin)
        c_hide.refresh_from_db()
        self.assertIsNone(c_hide.pending_version_id)
        with self.assertRaises(ValidationError):
            CommentService.moderate_comment(c_hide.id, action="approve", reviewer=self.admin)
        with self.assertRaises(ValidationError):
            CommentService.moderate_comment(
                c_hide.id, action="approve", reviewer=self.admin, expected_version_id=hide_ver_id
            )
        c_hide.refresh_from_db()
        self.assertEqual(c_hide.status, "rejected")
        self.assertFalse(c_hide.is_publicly_visible)

    def test_concurrent_guest_pending_queue_budget_hard_cap(self):
        """验证 P1 #4：6 个不同 IP/浏览器并发抢占剩余 2 个待审预算名额时严格只放行 2 个"""
        page = self._make_blog()
        PageCommentSettings.objects.create(page=page, allow_comments=True, allow_guest_comments=True)
        solo = CommunitySettings._get_or_create()
        current_pending = Comment.objects.filter(pending_version__isnull=False).count()
        solo.comment_mode = "controlled_guests"
        solo.max_pending_queue_budget = current_pending + 2
        solo.guest_daily_ip_cap = 10
        solo.guest_daily_browser_cap = 10
        solo.save()

        barrier = threading.Barrier(6)
        ips = [self._ip(70 + i) for i in range(6)]
        bkeys = [f"guest:conc_bud_{i}_{uuid4().hex[:8]}" for i in range(6)]
        self.tracked_browser_keys.extend(bkeys)

        def _guest_worker(idx: int):
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                CommentService.post_comment(
                    page=page,
                    author=None,
                    guest_name=f"并发访客{idx}",
                    raw_content=f"并发抢预算 #{idx}",
                    ip_address=ips[idx],
                    guest_browser_key=bkeys[idx],
                    require_guest_browser=True,
                )
                return "ok"
            except CommentQueueBudgetExceeded:
                return "budget_exceeded"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(_guest_worker, range(6)))
        self.assertEqual(outcomes.count("ok"), 2, outcomes)
        self.assertEqual(outcomes.count("budget_exceeded"), 4, outcomes)

    def test_concurrent_member_captcha_burst_html_link_and_xff_rightmost(self):
        """验证 P1 #7/#8 & P2 #10：并发首发高频验证码不穿透、HTML实体链接必拦、<a>不重复计数、XFF从右向左取IP"""
        page = self._make_blog()
        new_member = self._make_user("p3_burst", days_ago=1)
        trusted = self._make_user("p3_xff", days_ago=20)
        ip = self._ip(81)

        # 1. X-Forwarded-For 从右向左跳过可信代理，抵御左端伪造
        factory = RequestFactory()
        xff_req = factory.get(
            "/",
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="1.1.1.1, 198.51.100.42, 127.0.0.1",
        )
        self.assertEqual(get_client_ip(xff_req), "198.51.100.42")

        # 2. 原生 HTML <a> 标签不与裸 URL 重复计数（新会员允许 1 个外链）
        _, link_cnt = inspect_links_and_sanitize('<a href="https://docs.example.com">官方文档</a>')
        self.assertEqual(link_cnt, 1)
        c_html_link = CommentService.post_comment(
            page=page,
            author=trusted,
            raw_content='<a href="https://docs.example.com">官方文档</a>',
            ip_address=ip,
        )
        self.assertEqual(c_html_link.status, "pending")

        # 3. HTML 实体编码绕过必须触发 member_comment 验证码
        client = Client()
        client.force_login(new_member)
        ent_res = client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {"content": "混淆链接 [点我](&#104;ttps://evil.example.com)"},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(ent_res.status_code, 400)
        self.assertEqual(ent_res.json()["code"], "captcha_required")

        # 4. 5 线程无验证码并发首发 -> 严格 1 个成功、4 个被 captcha_required 拦截
        conc_member = self._make_user("p3_conc_burst", days_ago=1)
        ip_conc = self._ip(82)
        barrier = threading.Barrier(5)

        def _http_worker(idx: int):
            connections.close_all()
            try:
                c = Client()
                c.force_login(conc_member)
                barrier.wait(timeout=5)
                r = c.post(
                    f"/api/v1/pages/{page.id}/comments/post/",
                    {"content": f"并发无验证码评论 #{idx}"},
                    REMOTE_ADDR=ip_conc,
                )
                return r.status_code, r.json().get("code")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=5) as pool:
            res_list = list(pool.map(_http_worker, range(5)))
        statuses = [s for s, _ in res_list]
        codes = [c for _, c in res_list]
        self.assertEqual(statuses.count(201), 1, res_list)
        self.assertEqual(statuses.count(400), 4, res_list)
        self.assertEqual(codes.count("captcha_required"), 4, res_list)

    def test_http_end_to_end_idempotency_replay_and_concurrent_hard_delete_lock_order(self):
        """验证 P1 #9 HTTP 重放不二次触发限流/验证码 & P1 #6 硬删与审核/软删并发无死锁"""
        page = self._make_blog()
        member = self._make_user("p3_httpidem", days_ago=1)
        ip = self._ip(91)
        client = Client()
        client.force_login(member)

        req_key = f"http-idem-{uuid4().hex[:10]}"
        r1 = client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {"content": "网络抖动端到端幂等测试", "request_key": req_key},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(r1.status_code, 201)
        cid = r1.json()["data"]["id"]

        # 同一 request_key + 同一载荷立即重试（不带验证码）：应直接命中幂等回放并返回原 ID，不额外消耗日配额
        r2 = client.post(
            f"/api/v1/pages/{page.id}/comments/post/",
            {"content": "网络抖动端到端幂等测试", "request_key": req_key},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(r2.status_code, 201)
        self.assertTrue(r2.json().get("idempotent_replay"))
        self.assertEqual(r2.json()["data"]["id"], cid)
        self.assertEqual(
            CommentDailyQuota.objects.get(
                subject_kind="user",
                subject_key=f"user:{member.id}",
                quota_date=timezone.localdate(),
                action="post_comment",
            ).used_count,
            1,
        )

        # 并发硬删目标账号 vs admin 软删评论：验证统一 User -> Comment 锁序无死锁
        victim = self._make_user("p3_victim", days_ago=10)
        c_admin = CommentService.post_comment(page=page, author=self.admin, raw_content="待删评论", ip_address=ip)
        c_victim = CommentService.post_comment(
            page=page, author=victim, raw_content="注销用户评论", ip_address=ip, auto_approve=True
        )
        CommentService.react_comment(c_victim.id, self.admin, 1)

        barrier = threading.Barrier(2)
        errors = []

        def _del_user():
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                AccountLifecycleService.hard_delete(
                    actor=self.admin,
                    target_user_id=victim.id,
                    transfer_to_user_id=self.admin.id,
                    confirmed=True,
                )
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        def _soft_del():
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                CommentService.soft_delete_comment(c_admin.id, actor=self.admin)
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_del_user)
            f2 = pool.submit(_soft_del)
            f1.result(timeout=15)
            f2.result(timeout=15)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
