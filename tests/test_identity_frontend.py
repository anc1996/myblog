"""
前台会员中心端到端集成测试 (tests/test_identity_frontend.py)。

覆盖验证：
1. 未配置验证码 Provider 时注册 POST 严格 Fail-Closed 返回 503 且绝不建账号；
2. 配置测试桩验证码后注册成功（is_staff=False, is_superuser=False）、蜜罐拦截、
   个人中心越权字段（is_staff=True / is_superuser=True / recovery_hint 等）被忽略且不回显核密线索；
3. 注册同 IP 配额超限返回 429；
4. 登录防暴力保护（失败 3 次触发验证码、失败 10 次返回 429）、next 开放重定向拦截；
5. 退出登录仅允许 POST（GET 返回 405）；
6. 强制改密页解除 must_change_password；
7. 个人中心展示“我的评论列表”（含已公开/待审核/已驳回/已删除与所属页面）及作者发文权限申请提示；
8. 个人中心独立头像上传与自助改密流程。
"""

import io
import os
import tempfile
import unittest
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django

django.setup()

from PIL import Image as PILImage
from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from blog.models import BlogPage
from comments.models import Comment
from comments.services import CommentService
from home.models import HomePage
from identity.challenges import BaseChallengeVerifier, ChallengeVerificationError
from identity.credentials import (
    clear_login_failures,
    is_login_captcha_required,
    is_login_locked,
)
from identity.deletion_guard import authorize_user_hard_delete
from identity.models import AccountAuditLog, AvatarPurgeTask, MemberProfile, User


class StubChallengeVerifier(BaseChallengeVerifier):
    """测试用人机挑战校验桩：仅接受固定 stub 标识与验证码。"""

    VALID_ID = "stub-captcha-id"
    VALID_CODE = "8888"
    recorded_scopes: list[str] = []

    def verify_and_consume(self, *, request, expected_scope: str, captcha_id: str, code: str) -> None:
        self.__class__.recorded_scopes.append(expected_scope)
        if expected_scope not in ("register", "login"):
            raise ChallengeVerificationError("非法的安全验证场景。")
        if (captcha_id or "").strip() != self.VALID_ID or (code or "").strip() != self.VALID_CODE:
            raise ChallengeVerificationError("安全验证码错误或已失效。")


def _create_dummy_png_upload(filename: str = "avatar.png") -> SimpleUploadedFile:
    buf = io.BytesIO()
    img = PILImage.new("RGB", (32, 32), color=(37, 99, 235))
    img.save(buf, format="PNG")
    return SimpleUploadedFile(filename, buf.getvalue(), content_type="image/png")


def _reset_storage_cache() -> None:
    storages._storages.clear()
    storages.__dict__.pop("backends", None)


class IdentityFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = HomePage.objects.first()
        if cls.home is None:
            from wagtail.models import Page

            root = Page.get_first_root_node() or Page.add_root(title="Root")
            cls.home = HomePage(title="Home", slug=f"home-{uuid4().hex[:6]}")
            root.add_child(instance=cls.home)
            cls.home.save_revision().publish()
            cls.home.refresh_from_db()

    def setUp(self):
        self.client = Client()
        self.created_user_ids: list[int] = []
        self.created_pages: list[BlogPage] = []
        self.created_comments: list[Comment] = []
        self.redis_ips: list[str] = []
        self.redis_usernames: list[str] = []
        StubChallengeVerifier.recorded_scopes.clear()

    def tearDown(self):
        for comment in reversed(self.created_comments):
            try:
                comment.delete(allow_physical_purge=True)
            except Exception:
                pass
        for page in reversed(self.created_pages):
            try:
                page.delete(allow_physical_purge=True)
            except Exception:
                pass
        for uid in reversed(self.created_user_ids):
            try:
                AccountAuditLog.objects.filter(target_user_id_snapshot=uid).delete()
                AvatarPurgeTask.objects.filter(target_user_id_snapshot=uid).delete()
                user = User.objects.filter(pk=uid).first()
                if user is not None:
                    with authorize_user_hard_delete(user.pk):
                        user.delete()
            except Exception:
                pass
        self._cleanup_redis_keys()

    def _unique_ip(self, octet_c: int, octet_d: int) -> str:
        ip = f"10.241.{octet_c}.{octet_d}"
        self.redis_ips.append(ip)
        return ip

    def _track_user(self, user: User) -> User:
        if user and user.pk and user.pk not in self.created_user_ids:
            self.created_user_ids.append(user.pk)
        if user and user.username:
            self.redis_usernames.append(user.username)
        return user

    def _cleanup_redis_keys(self) -> None:
        try:
            backend = getattr(cache, "_cache", None)
            client = backend.get_client(write=True) if backend and hasattr(backend, "get_client") else None
            if client is None:
                return
            keys = []
            for ip in self.redis_ips:
                keys.extend(
                    [
                        f"myblog:rl:reg:hr:{ip}",
                        f"myblog:rl:reg:day:{ip}",
                        f"myblog:loginfail:ip:{ip}",
                    ]
                )
            for uname in self.redis_usernames:
                keys.append(f"myblog:loginfail:user:{uname.strip().lower()}")
            if keys:
                client.delete(*keys)
        except Exception:
            pass

    def test_01_register_without_verifier_returns_503_and_creates_no_user(self):
        """未配置验证码 Provider 时，注册 POST 返回 503 且绝不创建账号。"""
        username = f"unconf_{uuid4().hex[:8]}"
        ip = self._unique_ip(1, 10)
        self.redis_usernames.append(username)

        with override_settings(IDENTITY_CHALLENGE_VERIFIER=None):
            get_resp = self.client.get(reverse("identity:register"), REMOTE_ADDR=ip)
            self.assertEqual(get_resp.status_code, 200)
            get_html = get_resp.content.decode("utf-8")
            self.assertIn("csrfmiddlewaretoken", get_html)
            self.assertIn("人工核密线索，仅忘记密码联系管理员核实使用，绝不公开", get_html)

            post_resp = self.client.post(
                reverse("identity:register"),
                data={
                    "username": username,
                    "password": "StrongPass!2026",
                    "password_confirm": "StrongPass!2026",
                },
                REMOTE_ADDR=ip,
            )

        self.assertEqual(post_resp.status_code, 503)
        self.assertIn("安全校验服务暂未就绪", post_resp.content.decode("utf-8"))
        self.assertFalse(User.objects.filter(username=username).exists())

    def test_02_register_stub_verifier_honeypot_and_profile_privilege_escalation(self):
        """桩验证码注册成功、蜜罐拦截、且个人中心忽略越权字段并隐藏 recovery_hint。"""
        verifier_path = StubChallengeVerifier
        ip = self._unique_ip(2, 20)
        blocked_username = f"bot_{uuid4().hex[:8]}"
        bad_captcha_username = f"badcap_{uuid4().hex[:8]}"
        real_username = f"mem_{uuid4().hex[:8]}"
        self.redis_usernames.extend([blocked_username, bad_captcha_username, real_username])

        with override_settings(IDENTITY_CHALLENGE_VERIFIER=verifier_path):
            # 1) 蜜罐填写后立即阻断，绝不建号
            honeypot_resp = self.client.post(
                reverse("identity:register"),
                data={
                    "username": blocked_username,
                    "password": "StrongPass!2026",
                    "password_confirm": "StrongPass!2026",
                    "captcha_id": StubChallengeVerifier.VALID_ID,
                    "captcha_code": StubChallengeVerifier.VALID_CODE,
                    "website": "https://spam.example",
                },
                REMOTE_ADDR=ip,
            )
            self.assertEqual(honeypot_resp.status_code, 400)
            self.assertFalse(User.objects.filter(username=blocked_username).exists())

            # 2) 错误验证码返回 400，绝不建号
            bad_cap_resp = self.client.post(
                reverse("identity:register"),
                data={
                    "username": bad_captcha_username,
                    "password": "StrongPass!2026",
                    "password_confirm": "StrongPass!2026",
                    "captcha_id": StubChallengeVerifier.VALID_ID,
                    "captcha_code": "0000",
                },
                REMOTE_ADDR=ip,
            )
            self.assertEqual(bad_cap_resp.status_code, 400)
            self.assertFalse(User.objects.filter(username=bad_captcha_username).exists())

            # 3) 正常注册（携带越权字段 is_staff/is_superuser 应被彻底忽略）
            reg_resp = self.client.post(
                reverse("identity:register"),
                data={
                    "username": real_username,
                    "password": "StrongPass!2026",
                    "password_confirm": "StrongPass!2026",
                    "recovery_hint": "SecretRecoveryClue999",
                    "captcha_id": StubChallengeVerifier.VALID_ID,
                    "captcha_code": StubChallengeVerifier.VALID_CODE,
                    "is_staff": "true",
                    "is_superuser": "true",
                },
                REMOTE_ADDR=ip,
            )

        self.assertEqual(reg_resp.status_code, 302)
        self.assertEqual(reg_resp.url, reverse("identity:profile"))
        self.assertIn("register", StubChallengeVerifier.recorded_scopes)

        user = self._track_user(User.objects.get(username=real_username))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_active)
        self.assertEqual(user.groups.count(), 0)

        profile = MemberProfile.objects.get(user=user)
        self.assertEqual(profile.nickname, real_username)
        self.assertEqual(profile.recovery_hint, "SecretRecoveryClue999")

        # 4) 个人中心渲染不可泄露 recovery_hint
        profile_get = self.client.get(reverse("identity:profile"), REMOTE_ADDR=ip)
        self.assertEqual(profile_get.status_code, 200)
        self.assertNotIn("SecretRecoveryClue999", profile_get.content.decode("utf-8"))

        # 5) 个人中心提交越权字段无效（仅更新 nickname 与 bio）
        esc_resp = self.client.post(
            reverse("identity:profile"),
            data={
                "action": "update_profile",
                "nickname": "普通会员昵称",
                "bio": "专注学习 Django 与 Wagtail",
                "is_staff": "true",
                "is_superuser": "true",
                "is_active": "false",
                "recovery_hint": "TamperedClue",
            },
            REMOTE_ADDR=ip,
        )
        self.assertEqual(esc_resp.status_code, 302)
        user.refresh_from_db()
        profile.refresh_from_db()
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_active)
        self.assertEqual(profile.nickname, "普通会员昵称")
        self.assertEqual(profile.bio, "专注学习 Django 与 Wagtail")
        self.assertEqual(profile.recovery_hint, "SecretRecoveryClue999")

    def test_03_register_rate_limit_returns_429(self):
        """同 IP 注册配额超限后返回 HTTP 429 并阻断创建账号。"""
        verifier_path = StubChallengeVerifier
        ip = self._unique_ip(3, 30)
        u1 = f"rl1_{uuid4().hex[:8]}"
        u2 = f"rl2_{uuid4().hex[:8]}"
        self.redis_usernames.extend([u1, u2])

        with override_settings(
            IDENTITY_CHALLENGE_VERIFIER=verifier_path,
            IDENTITY_REGISTER_HOURLY_IP_LIMIT=1,
        ):
            r1 = self.client.post(
                reverse("identity:register"),
                data={
                    "username": u1,
                    "password": "StrongPass!2026",
                    "password_confirm": "StrongPass!2026",
                    "captcha_id": StubChallengeVerifier.VALID_ID,
                    "captcha_code": StubChallengeVerifier.VALID_CODE,
                },
                REMOTE_ADDR=ip,
            )
            self.assertEqual(r1.status_code, 302)
            self._track_user(User.objects.get(username=u1))

            r2 = self.client.post(
                reverse("identity:register"),
                data={
                    "username": u2,
                    "password": "StrongPass!2026",
                    "password_confirm": "StrongPass!2026",
                    "captcha_id": StubChallengeVerifier.VALID_ID,
                    "captcha_code": StubChallengeVerifier.VALID_CODE,
                },
                REMOTE_ADDR=ip,
            )
            self.assertEqual(r2.status_code, 429)
            self.assertIn("Retry-After", r2)
            self.assertFalse(User.objects.filter(username=u2).exists())

    def test_04_login_bruteforce_triggers_captcha_then_429_lockout(self):
        """登录连续失败 3 次触发验证码、失败 10 次触发 429 锁定。"""
        verifier_path = StubChallengeVerifier
        ip = self._unique_ip(4, 40)
        username = f"bf_{uuid4().hex[:8]}"
        password = "StrongPass!2026"
        user = self._track_user(User.objects.create_user(username=username, password=password))
        MemberProfile.get_or_create_for_user(user)

        with override_settings(IDENTITY_CHALLENGE_VERIFIER=verifier_path):
            # 1~3 次错误密码登录
            for i in range(3):
                resp = self.client.post(
                    reverse("identity:login"),
                    data={"username": username, "password": "WrongPassword!"},
                    REMOTE_ADDR=ip,
                )
                self.assertEqual(resp.status_code, 400, f"Attempt {i + 1} should return 400")
                if i == 2:
                    html = resp.content.decode("utf-8")
                    self.assertIn("captcha_code", html)

            self.assertTrue(is_login_captcha_required(username, ip))

            # 第 4 次即便密码正确，未填有效验证码也必须被拒绝
            no_captcha_resp = self.client.post(
                reverse("identity:login"),
                data={"username": username, "password": password},
                REMOTE_ADDR=ip,
            )
            self.assertEqual(no_captcha_resp.status_code, 400)

            # 继续携带合法验证码用错误密码失败 7 次（累计 10 次失败）
            for _ in range(7):
                fail_resp = self.client.post(
                    reverse("identity:login"),
                    data={
                        "username": username,
                        "password": "WrongPassword!",
                        "captcha_id": StubChallengeVerifier.VALID_ID,
                        "captcha_code": StubChallengeVerifier.VALID_CODE,
                    },
                    REMOTE_ADDR=ip,
                )
                self.assertEqual(fail_resp.status_code, 400)

            locked, _ = is_login_locked(username, ip)
            self.assertTrue(locked)

            # 第 11 次即使密码与验证码全对也必须返回 429 锁定
            locked_resp = self.client.post(
                reverse("identity:login"),
                data={
                    "username": username,
                    "password": password,
                    "captcha_id": StubChallengeVerifier.VALID_ID,
                    "captcha_code": StubChallengeVerifier.VALID_CODE,
                },
                REMOTE_ADDR=ip,
            )
            self.assertEqual(locked_resp.status_code, 429)
            self.assertIn("Retry-After", locked_resp)

            # 清理锁定后可凭正确密码与验证码正常登录（若未清理则仍拦截）
            clear_login_failures(username, ip)
            ok_resp = self.client.post(
                reverse("identity:login"),
                data={"username": username, "password": password},
                REMOTE_ADDR=ip,
            )
            self.assertEqual(ok_resp.status_code, 302)
            self.assertEqual(ok_resp.url, reverse("identity:profile"))

    def test_05_open_redirect_protection_and_post_only_logout(self):
        """验证登录/退出对开放重定向的拦截，以及退出接口仅支持 POST。"""
        ip = self._unique_ip(5, 50)
        username = f"redir_{uuid4().hex[:8]}"
        password = "StrongPass!2026"
        user = self._track_user(User.objects.create_user(username=username, password=password))
        MemberProfile.get_or_create_for_user(user)

        # 恶意 next 在 GET 页面中不应落入隐藏域
        get_login = self.client.get(
            f"{reverse('identity:login')}?next=https://evil.example/phish",
            REMOTE_ADDR=ip,
        )
        self.assertEqual(get_login.status_code, 200)
        self.assertNotIn("evil.example", get_login.content.decode("utf-8"))

        # 登录 POST 携带外部域名或协议相对路径 -> 回退个人中心
        for evil_next in ("https://evil.example/phish", "//evil.example/phish", "/\\evil.example"):
            self.client.logout()
            resp = self.client.post(
                reverse("identity:login"),
                data={"username": username, "password": password, "next": evil_next},
                REMOTE_ADDR=ip,
            )
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp.url, reverse("identity:profile"))

        # 站内合法相对路径 next 放行
        self.client.logout()
        safe_resp = self.client.post(
            reverse("identity:login"),
            data={"username": username, "password": password, "next": "/search/"},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(safe_resp.status_code, 302)
        self.assertEqual(safe_resp.url, "/search/")

        # GET 请求退出必须返回 405，且会话不被销毁
        get_logout = self.client.get(reverse("identity:logout"), REMOTE_ADDR=ip)
        self.assertEqual(get_logout.status_code, 405)
        still_auth = self.client.get(reverse("identity:profile"), REMOTE_ADDR=ip)
        self.assertEqual(still_auth.status_code, 200)

        # POST 退出过滤外部 next 并销毁会话
        post_logout = self.client.post(
            reverse("identity:logout"),
            data={"next": "https://evil.example/bounce"},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(post_logout.status_code, 302)
        self.assertEqual(post_logout.url, "/")
        after_logout = self.client.get(reverse("identity:profile"), REMOTE_ADDR=ip)
        self.assertEqual(after_logout.status_code, 302)
        self.assertTrue(after_logout.url.startswith(reverse("identity:login")))

    def test_06_force_password_change_and_profile_avatar_and_password_actions(self):
        """强制改密页面解除 must_change_password，并验证个人中心上传头像与自助改密。"""
        ip = self._unique_ip(6, 60)
        username = f"pwc_{uuid4().hex[:8]}"
        old_pw = "InitPass!2026"
        mid_pw = "ForcedNewPass!2026"
        final_pw = "FinalNewPass!2026"

        user = self._track_user(User.objects.create_user(username=username, password=old_pw))
        profile = MemberProfile.get_or_create_for_user(user)
        profile.must_change_password = True
        profile.save(update_fields=["must_change_password"])
        initial_epoch = profile.session_epoch

        # 1) 登录后自动跳转至强制改密页
        login_resp = self.client.post(
            reverse("identity:login"),
            data={"username": username, "password": old_pw},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(login_resp.status_code, 302)
        self.assertEqual(login_resp.url, reverse("identity:force_password_change"))

        # 未完成改密前访问个人中心被拦截回强制改密页
        blocked_profile = self.client.get(reverse("identity:profile"), REMOTE_ADDR=ip)
        self.assertEqual(blocked_profile.status_code, 302)
        self.assertEqual(blocked_profile.url, reverse("identity:force_password_change"))

        # 2) 提交强制改密表单后解除标记并保持当前会话
        force_resp = self.client.post(
            reverse("identity:force_password_change"),
            data={
                "old_password": old_pw,
                "new_password": mid_pw,
                "new_password_confirm": mid_pw,
            },
            REMOTE_ADDR=ip,
        )
        self.assertEqual(force_resp.status_code, 302)
        self.assertEqual(force_resp.url, reverse("identity:profile"))

        user.refresh_from_db()
        profile.refresh_from_db()
        self.assertFalse(profile.must_change_password)
        self.assertEqual(profile.session_epoch, initial_epoch + 1)
        self.assertTrue(user.check_password(mid_pw))

        # 3) 在个人中心上传独立头像（隔离到临时本地存储）
        with tempfile.TemporaryDirectory() as tmpdir:
            override_storages = {
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": tmpdir},
                },
                "staticfiles": settings.STORAGES["staticfiles"],
            }
            _reset_storage_cache()
            try:
                with override_settings(STORAGES=override_storages):
                    _reset_storage_cache()
                    avatar_resp = self.client.post(
                        reverse("identity:profile"),
                        data={
                            "action": "upload_avatar",
                            "avatar": _create_dummy_png_upload(),
                        },
                        REMOTE_ADDR=ip,
                    )
                    self.assertEqual(avatar_resp.status_code, 302)
                    profile.refresh_from_db()
                    self.assertTrue(profile.avatar_url.startswith("/avatar-gateway/"))
                    profile_html = self.client.get(reverse("identity:profile"), REMOTE_ADDR=ip).content.decode("utf-8")
                    self.assertIn(profile.avatar_url, profile_html)
            finally:
                _reset_storage_cache()

        # 4) 在个人中心使用 change_password 动作再次修改密码
        chg_resp = self.client.post(
            reverse("identity:profile"),
            data={
                "action": "change_password",
                "old_password": mid_pw,
                "new_password": final_pw,
                "new_password_confirm": final_pw,
            },
            REMOTE_ADDR=ip,
        )
        self.assertEqual(chg_resp.status_code, 302)
        user.refresh_from_db()
        profile.refresh_from_db()
        self.assertTrue(user.check_password(final_pw))
        self.assertEqual(profile.session_epoch, initial_epoch + 2)
        self.assertEqual(self.client.get(reverse("identity:profile"), REMOTE_ADDR=ip).status_code, 200)

    def test_07_profile_displays_my_comments_list_and_author_notice(self):
        """个人中心展示我的评论列表（四种状态）及专栏作者申请文字提示。"""
        ip = self._unique_ip(7, 70)
        username = f"comm_{uuid4().hex[:8]}"
        user = self._track_user(User.objects.create_user(username=username, password="StrongPass!2026"))
        MemberProfile.get_or_create_for_user(user)

        page = BlogPage(
            title=f"会员中心评论测试文章-{uuid4().hex[:6]}",
            slug=f"id-comm-page-{uuid4().hex[:6]}",
        )
        self.home.add_child(instance=page)
        page.save_revision().publish()
        page.refresh_from_db()
        self.created_pages.append(page)

        c_pub = CommentService.post_comment(
            page=page,
            author=user,
            raw_content="已公开评论正文内容",
            auto_approve=True,
        )
        c_pending = CommentService.post_comment(
            page=page,
            author=user,
            raw_content="待审核评论正文内容",
            auto_approve=False,
        )
        c_rejected = CommentService.post_comment(
            page=page,
            author=user,
            raw_content="被驳回评论正文内容",
            auto_approve=False,
        )
        CommentService.moderate_comment(
            c_rejected.id,
            action="reject",
            reviewer=user,
            expected_version_id=c_rejected.pending_version_id,
        )
        c_deleted = CommentService.post_comment(
            page=page,
            author=user,
            raw_content="已删除评论正文内容",
            auto_approve=True,
        )
        CommentService.soft_delete_comment(c_deleted.id, actor=user)
        self.created_comments.extend([c_pub, c_pending, c_rejected, c_deleted])

        self.client.force_login(user)
        resp = self.client.get(reverse("identity:profile"), REMOTE_ADDR=ip)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        self.assertIn(page.title, html)
        self.assertIn("已公开", html)
        self.assertIn("待审核", html)
        self.assertIn("已驳回", html)
        self.assertIn("已删除", html)
        self.assertIn("已公开评论正文内容", html)
        self.assertIn("如需申请专栏作者发文权限，请联系管理员审核开通", html)

    def test_08_login_fails_closed_on_redis_outage_and_hides_unpublished_title(self):
        """Redis 故障时登录保护返回 503 Fail-Closed；已下架/改标题页面不在我的评论泄露未公开新标题。"""
        from unittest.mock import patch

        ip = self._unique_ip(8, 80)
        username = f"p1_{uuid4().hex[:8]}"
        password = "StrongPass!2026"
        user = self._track_user(User.objects.create_user(username=username, password=password))
        MemberProfile.get_or_create_for_user(user)

        page = BlogPage(title="公开期原标题", slug=f"p1leak-{uuid4().hex[:6]}", owner=user)
        self.home.add_child(instance=page)
        page.save_revision(user=user).publish()
        self.created_pages.append(page)

        c = CommentService.post_comment(page=page, author=user, raw_content="历史评论", auto_approve=True)
        self.created_comments.append(c)

        # 页面下架并改为涉密草稿标题
        page.unpublish()
        page.title = "绝密未公开新标题-XYZ"
        page.save()

        self.client.force_login(user)
        prof_resp = self.client.get(reverse("identity:profile"), REMOTE_ADDR=ip)
        self.assertEqual(prof_resp.status_code, 200)
        html = prof_resp.content.decode("utf-8")
        self.assertNotIn("绝密未公开新标题-XYZ", html)
        self.assertIn("公开期原标题", html)

        # Redis 不可用时，登录 POST 必须 Fail-Closed 返回 503
        anon_client = Client()
        with patch("identity.credentials._get_redis_client", return_value=None):
            login_resp = anon_client.post(
                reverse("identity:login"),
                data={"username": username, "password": password},
                REMOTE_ADDR=ip,
            )
        self.assertEqual(login_resp.status_code, 503)
        self.assertIn("安全校验服务暂未就绪", login_resp.content.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
