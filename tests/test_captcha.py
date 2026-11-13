"""
Phase 2 公共图片验证码模块 (captcha) 单元、并发与端到端集成测试。

覆盖验证：
1. Pillow 13.0.0 无外部字体渲染、避歧义字符集与有效 PNG 编码；
2. /api/v1/captcha/challenges/ 与 /api/v1/captcha/image/<id>/ 的 CSRF、no-store、nosniff、无明文泄露与刷新作废；
3. 一次输入错误立即作废、跨 scope 重放拒绝、绑定 Cookie 缺失/篡改拒绝；
4. 多线程并发核销同一个有效挑战时严格仅 1 次成功；
5. 跨 IP（4G/IPv6）漫游不误杀正常 Cookie 会话；同 IP 限流与单会话活跃上限返回 429；Redis 不可用 Fail-Closed 返回 503；
6. 真实 Pillow+Redis 验证码端到端打通 /account/register/ 与触发验证码后的 /account/login/。
"""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import os
import re
import unittest
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse
from PIL import Image

from captcha.providers import PillowRedisCaptchaVerifier
from captcha.renderer import CAPTCHA_ALPHABET, generate_captcha_code, render_captcha_png
from captcha.service import CAPTCHA_BINDING_COOKIE_NAME, _get_redis_client
from identity.account_lifecycle import AccountLifecycleService
from identity.challenges import ChallengeVerificationError
from identity.models import MemberProfile

User = get_user_model()


class CaptchaModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = User.objects.filter(is_superuser=True, is_active=True).first()
        if not cls.admin:
            cls.admin = User.objects.create_superuser(
                username=f"cap_admin_{uuid4().hex[:6]}",
                email="cap_admin@test.com",
                password="StrongAdminPassword!123",
            )

    def setUp(self):
        self.created_user_ids: list[int] = []
        self.tracked_ips: list[str] = []
        self.tracked_usernames: list[str] = []
        self.tracked_captcha_ids: list[str] = []

    def tearDown(self):
        for uid in reversed(self.created_user_ids):
            if User.objects.filter(pk=uid).exists():
                AccountLifecycleService.hard_delete(
                    actor=self.admin,
                    target_user_id=uid,
                    confirmed=True,
                )
        try:
            r = _get_redis_client(write=True)
            if r is not None:
                from captcha.service import _resolve_prefix
                p = _resolve_prefix()
                keys = []
                for ip in self.tracked_ips:
                    keys.extend([
                        f"{p}:captcha:rl:ip:{ip}",
                        f"{p}:rl:reg:hr:{ip}",
                        f"{p}:rl:reg:day:{ip}",
                        f"{p}:loginfail:ip:{ip}",
                        f"{p}:logintry:ip:{ip}",
                    ])
                for uname in self.tracked_usernames:
                    keys.append(f"{p}:loginfail:user:{uname.lower()}")
                    keys.append(f"{p}:logintry:user:{uname.lower()}")
                for cid in self.tracked_captcha_ids:
                    keys.extend([f"{p}:captcha:ch:{cid}", f"{p}:captcha:img:{cid}"])
                if keys:
                    r.delete(*keys)
        except Exception:
            pass

    def _ip(self, d: int) -> str:
        ip = f"10.242.9.{d}"
        self.tracked_ips.append(ip)
        return ip

    def test_renderer_uses_unambiguous_alphabet_and_valid_png(self):
        ambiguous = {"0", "O", "1", "I", "L"}
        self.assertFalse(set(CAPTCHA_ALPHABET) & ambiguous)
        for _ in range(30):
            code = generate_captcha_code(4)
            self.assertEqual(len(code), 4)
            self.assertTrue(set(code).issubset(set(CAPTCHA_ALPHABET)))
            self.assertFalse(set(code) & ambiguous)

        png_bytes = render_captcha_png("K7M9")
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        img = Image.open(BytesIO(png_bytes))
        img.verify()
        self.assertEqual(img.format, "PNG")
        self.assertEqual(img.size, (160, 52))

    def test_issue_challenge_and_image_endpoint_security(self):
        ip = self._ip(11)
        csrf_client = Client(enforce_csrf_checks=True)
        # 从注册页获取 csrftoken Cookie
        page_resp = csrf_client.get(reverse("identity:register"), REMOTE_ADDR=ip)
        self.assertEqual(page_resp.status_code, 200)
        csrf_token = csrf_client.cookies["csrftoken"].value
        m = re.search(r'name="captcha_id"[^>]*value="([0-9a-f]{32})"', page_resp.content.decode("utf-8"))
        self.assertIsNotNone(m)
        initial_id = m.group(1)
        self.tracked_captcha_ids.append(initial_id)

        # 1. 无 CSRF 令牌禁止调用签发接口
        no_csrf = csrf_client.post(
            reverse("api_v1:captcha_challenge_issue"),
            data={"scope": "register"},
            REMOTE_ADDR=ip,
        )
        self.assertEqual(no_csrf.status_code, 403)

        # 2. 合法签发，刷新并作废 initial_id，绝不返回明文答案
        with patch("captcha.service.generate_captcha_code", return_value="W8Q3"):
            issue_resp = csrf_client.post(
                reverse("api_v1:captcha_challenge_issue"),
                data='{"scope":"register","previous_captcha_id":"' + initial_id + '"}',
                content_type="application/json",
                HTTP_X_CSRFTOKEN=csrf_token,
                REMOTE_ADDR=ip,
            )
        self.assertEqual(issue_resp.status_code, 200)
        self.assertIn("no-store", issue_resp["Cache-Control"])
        self.assertNotIn("W8Q3", issue_resp.content.decode("utf-8"))
        payload = issue_resp.json()
        new_id = payload["captcha_id"]
        self.tracked_captcha_ids.append(new_id)
        self.assertTrue(payload["image_data_url"].startswith("data:image/png;base64,"))
        self.assertTrue(csrf_client.cookies[CAPTCHA_BINDING_COOKIE_NAME]["httponly"])

        # 旧挑战已被刷新作废
        self.assertEqual(
            csrf_client.get(reverse("api_v1:captcha_challenge_image", args=[initial_id]), REMOTE_ADDR=ip).status_code,
            404,
        )

        # 新图像端点携带同浏览器 Cookie 返回 200 PNG + no-store + nosniff
        img_resp = csrf_client.get(reverse("api_v1:captcha_challenge_image", args=[new_id]), REMOTE_ADDR=ip)
        self.assertEqual(img_resp.status_code, 200)
        self.assertEqual(img_resp["Content-Type"], "image/png")
        self.assertIn("no-store", img_resp["Cache-Control"])
        self.assertEqual(img_resp["X-Content-Type-Options"], "nosniff")
        self.assertTrue(img_resp.content.startswith(b"\x89PNG"))

        # 另一无 Cookie 客户端访问返回 404
        other_client = Client()
        self.assertEqual(
            other_client.get(reverse("api_v1:captcha_challenge_image", args=[new_id]), REMOTE_ADDR=ip).status_code,
            404,
        )

        # 非法 scope 返回 400
        bad_scope = csrf_client.post(
            reverse("api_v1:captcha_challenge_issue"),
            data={"scope": "admin_bypass"},
            HTTP_X_CSRFTOKEN=csrf_token,
            REMOTE_ADDR=ip,
        )
        self.assertEqual(bad_scope.status_code, 400)

    def test_wrong_attempt_destroys_challenge_and_scope_binding_enforced(self):
        ip = self._ip(22)
        client = Client()
        with patch("captcha.service.generate_captcha_code", return_value="A4B5"):
            r = client.post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "register"}, REMOTE_ADDR=ip)
        cid = r.json()["captcha_id"]
        self.tracked_captcha_ids.append(cid)
        binding_cookie = client.cookies[CAPTCHA_BINDING_COOKIE_NAME].value

        from django.test import RequestFactory
        rf = RequestFactory()

        # 1. 输错一次即作废，随后拿正确答案也失败
        req1 = rf.post("/account/register/", REMOTE_ADDR=ip)
        req1.COOKIES[CAPTCHA_BINDING_COOKIE_NAME] = binding_cookie
        verifier = PillowRedisCaptchaVerifier()
        with self.assertRaises(ChallengeVerificationError):
            verifier.verify_and_consume(request=req1, expected_scope="register", captcha_id=cid, code="ZZZZ")
        with self.assertRaises(ChallengeVerificationError):
            verifier.verify_and_consume(request=req1, expected_scope="register", captcha_id=cid, code="A4B5")

        # 1b. 同绑定提交空答案同样立即作废挑战
        with patch("captcha.service.generate_captcha_code", return_value="B2C3"):
            r_empty = client.post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "register"}, REMOTE_ADDR=ip)
        cid_empty = r_empty.json()["captcha_id"]
        self.tracked_captcha_ids.append(cid_empty)
        with self.assertRaises(ChallengeVerificationError):
            verifier.verify_and_consume(request=req1, expected_scope="register", captcha_id=cid_empty, code="")
        with self.assertRaises(ChallengeVerificationError):
            verifier.verify_and_consume(request=req1, expected_scope="register", captcha_id=cid_empty, code="B2C3")

        # 2. 跨 scope 提交失败
        with patch("captcha.service.generate_captcha_code", return_value="C6D7"):
            r2 = client.post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "register"}, REMOTE_ADDR=ip)
        cid2 = r2.json()["captcha_id"]
        self.tracked_captcha_ids.append(cid2)
        req2 = rf.post("/account/login/", REMOTE_ADDR=ip)
        req2.COOKIES[CAPTCHA_BINDING_COOKIE_NAME] = binding_cookie
        with self.assertRaises(ChallengeVerificationError):
            verifier.verify_and_consume(request=req2, expected_scope="login", captcha_id=cid2, code="C6D7")

        # 3. 缺失或伪造 Cookie 拒绝，且不销毁原持有者的有效挑战
        with patch("captcha.service.generate_captcha_code", return_value="E8F9"):
            r3 = client.post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "login"}, REMOTE_ADDR=ip)
        cid3 = r3.json()["captcha_id"]
        self.tracked_captcha_ids.append(cid3)
        req3 = rf.post("/account/login/", REMOTE_ADDR=ip)
        req3.COOKIES[CAPTCHA_BINDING_COOKIE_NAME] = "tampered-value"
        with self.assertRaises(ChallengeVerificationError):
            verifier.verify_and_consume(request=req3, expected_scope="login", captcha_id=cid3, code="E8F9")

        # 持另一合法绑定 Cookie B 也无法通过或删掉持有者 A 的 cid3，原持有者随后仍可正常核销
        other_client = Client()
        r_other = other_client.post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "login"}, REMOTE_ADDR=ip)
        self.tracked_captcha_ids.append(r_other.json()["captcha_id"])
        req_other = rf.post("/account/login/", REMOTE_ADDR=ip)
        req_other.COOKIES[CAPTCHA_BINDING_COOKIE_NAME] = other_client.cookies[CAPTCHA_BINDING_COOKIE_NAME].value
        with self.assertRaises(ChallengeVerificationError):
            verifier.verify_and_consume(request=req_other, expected_scope="login", captcha_id=cid3, code="WRONG")
        req_owner = rf.post("/account/login/", REMOTE_ADDR=ip)
        req_owner.COOKIES[CAPTCHA_BINDING_COOKIE_NAME] = binding_cookie
        verifier.verify_and_consume(request=req_owner, expected_scope="login", captcha_id=cid3, code="E8F9")

    def test_concurrent_verify_and_consume_only_one_wins(self):
        ip = self._ip(33)
        client = Client()
        with patch("captcha.service.generate_captcha_code", return_value="M9N2"):
            r = client.post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "register"}, REMOTE_ADDR=ip)
        cid = r.json()["captcha_id"]
        self.tracked_captcha_ids.append(cid)
        binding_cookie = client.cookies[CAPTCHA_BINDING_COOKIE_NAME].value

        from django.test import RequestFactory
        verifier = PillowRedisCaptchaVerifier()

        def _attempt():
            rf = RequestFactory()
            req = rf.post("/account/register/", REMOTE_ADDR=ip)
            req.COOKIES[CAPTCHA_BINDING_COOKIE_NAME] = binding_cookie
            try:
                verifier.verify_and_consume(request=req, expected_scope="register", captcha_id=cid, code="m9n2")
                return "ok"
            except ChallengeVerificationError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=5) as pool:
            outcomes = [pool.submit(_attempt) for _ in range(5)]
            results = [f.result(timeout=10) for f in outcomes]
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("rejected"), 4)

    def test_ip_roaming_succeeds_while_rate_limits_and_redis_outage_block(self):
        ip_a = self._ip(44)
        ip_b = "2001:db8::99"
        client = Client()

        # 1. 跨 IP 切换不误杀同浏览器 Cookie 会话
        with patch("captcha.service.generate_captcha_code", return_value="P3Q4"):
            r = client.post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "guest_comment"}, REMOTE_ADDR=ip_a)
        cid = r.json()["captcha_id"]
        self.tracked_captcha_ids.append(cid)
        binding_cookie = client.cookies[CAPTCHA_BINDING_COOKIE_NAME].value

        from django.test import RequestFactory
        req = RequestFactory().post("/api/v1/pages/1/comments/post/", REMOTE_ADDR=ip_b)
        req.COOKIES[CAPTCHA_BINDING_COOKIE_NAME] = binding_cookie
        PillowRedisCaptchaVerifier().verify_and_consume(
            request=req,
            expected_scope="guest_comment",
            captcha_id=cid,
            code="P3Q4",
        )

        # 2. 单会话活跃挑战数限制 -> 429
        ip_cap = self._ip(45)
        c2 = Client()
        with override_settings(CAPTCHA_ACTIVE_BINDING_LIMIT=2, CAPTCHA_ISSUE_IP_MINUTE_LIMIT=50):
            a1 = c2.post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "register"}, REMOTE_ADDR=ip_cap)
            a2 = c2.post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "register"}, REMOTE_ADDR=ip_cap)
            self.tracked_captcha_ids.extend([a1.json()["captcha_id"], a2.json()["captcha_id"]])
            a3 = c2.post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "register"}, REMOTE_ADDR=ip_cap)
            self.assertEqual(a3.status_code, 429)

        # 3. 单 IP 签发频次限制 -> 429，且超限后不触发 Pillow 绘图（限流在渲染之前）
        ip_rl = self._ip(46)
        with override_settings(CAPTCHA_ISSUE_IP_MINUTE_LIMIT=2, CAPTCHA_ACTIVE_BINDING_LIMIT=50):
            b1 = Client().post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "login"}, REMOTE_ADDR=ip_rl)
            b2 = Client().post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "login"}, REMOTE_ADDR=ip_rl)
            self.tracked_captcha_ids.extend([b1.json()["captcha_id"], b2.json()["captcha_id"]])
            with patch("captcha.service.render_captcha_png") as mock_render:
                b3 = Client().post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "login"}, REMOTE_ADDR=ip_rl)
                self.assertEqual(mock_render.call_count, 0)
            self.assertEqual(b3.status_code, 429)
            self.assertIn("Retry-After", b3)
            # HEAD /account/register/ 不调用签发与渲染
            with patch("captcha.service.render_captcha_png") as mock_head_render:
                head_resp = Client().head(reverse("identity:register"), REMOTE_ADDR=ip_rl)
                self.assertEqual(head_resp.status_code, 200)
                self.assertEqual(mock_head_render.call_count, 0)

        # 4. Redis 故障时 Fail-Closed 返回 503
        with patch("captcha.service._get_redis_client", return_value=None):
            err_issue = client.post(reverse("api_v1:captcha_challenge_issue"), data={"scope": "register"}, REMOTE_ADDR=ip_a)
            self.assertEqual(err_issue.status_code, 503)

    def test_concurrent_login_bruteforce_capped_at_threshold_and_widget_recovers(self):
        ip = self._ip(66)
        username = f"conc_bf_{uuid4().hex[:8]}"
        self.tracked_usernames.append(username)
        password = "StrongPassword!2026"
        u = User.objects.create_user(username=username, password=password)
        self.created_user_ids.append(u.id)
        MemberProfile.get_or_create_for_user(u)

        # 1. 并发 8 个线程无验证码试探同一账号密码：受原子预占保护，最多仅 3 次进入 authenticate()
        auth_calls = 0
        from django.contrib.auth import authenticate as real_authenticate

        def _counting_auth(*args, **kwargs):
            nonlocal auth_calls
            auth_calls += 1
            return real_authenticate(*args, **kwargs)

        def _post_login():
            from django.db import close_old_connections
            close_old_connections()
            try:
                r = Client().post(
                    reverse("identity:login"),
                    data={"username": username, "password": "WrongPassword!"},
                    REMOTE_ADDR=ip,
                )
                return r.status_code
            finally:
                close_old_connections()

        with patch("identity.views.authenticate", side_effect=_counting_auth):
            with ThreadPoolExecutor(max_workers=8) as pool:
                statuses = [f.result(timeout=10) for f in [pool.submit(_post_login) for _ in range(8)]]
        self.assertLessEqual(auth_calls, 3)
        self.assertTrue(all(s in (400, 429) for s in statuses))

        # 2. 当 pre-issue 返回空 payload（如限流或故障）时，模板仍渲染可点击刷新的“换一张”按钮结构
        with patch("identity.views._issue_widget_challenge", return_value=(None, None)):
            r_fallback = Client().get(reverse("identity:register"), REMOTE_ADDR=self._ip(67))
        self.assertEqual(r_fallback.status_code, 200)
        fallback_html = r_fallback.content.decode("utf-8")
        self.assertIn("js-captcha-refresh", fallback_html)
        self.assertIn("js-captcha-widget", fallback_html)

    def test_e2e_register_and_login_with_real_pillow_redis_captcha(self):
        ip = self._ip(55)
        username = f"cap_mem_{uuid4().hex[:8]}"
        self.tracked_usernames.append(username)
        password = "StrongPassword!2026"
        client = Client()

        # 1. GET /account/register/ 签发真实图片验证码
        with patch("captcha.service.generate_captcha_code", return_value="K7M9"):
            get_resp = client.get(reverse("identity:register"), REMOTE_ADDR=ip)
        self.assertEqual(get_resp.status_code, 200)
        html = get_resp.content.decode("utf-8")
        self.assertIn("data:image/png;base64,", html)
        m = re.search(r'name="captcha_id"[^>]*value="([0-9a-f]{32})"', html)
        self.assertIsNotNone(m)
        reg_cid = m.group(1)
        self.tracked_captcha_ids.append(reg_cid)

        # 2. 用正确验证码（小写输入）完成注册
        reg_resp = client.post(
            reverse("identity:register"),
            data={
                "username": username,
                "password": password,
                "password_confirm": password,
                "nickname": "验证码新会员",
                "captcha_id": reg_cid,
                "captcha_code": "k7m9",
            },
            REMOTE_ADDR=ip,
        )
        self.assertEqual(reg_resp.status_code, 302)
        user = User.objects.get(username=username)
        self.created_user_ids.append(user.id)
        self.assertFalse(user.is_staff)
        self.assertEqual(MemberProfile.objects.get(user=user).nickname, "验证码新会员")

        # 3. 重放已核销的 reg_cid 注册第二个账号被拒
        dup_resp = client.post(
            reverse("identity:register"),
            data={
                "username": f"{username}_2",
                "password": password,
                "password_confirm": password,
                "captcha_id": reg_cid,
                "captcha_code": "K7M9",
            },
            REMOTE_ADDR=ip,
        )
        self.assertEqual(dup_resp.status_code, 400)
        self.assertFalse(User.objects.filter(username=f"{username}_2").exists())

        # 4. 触发 3 次登录失败 -> 弹出真实登录验证码并验证通过
        login_client = Client()
        for _ in range(3):
            bad_login = login_client.post(
                reverse("identity:login"),
                data={"username": username, "password": "WrongPassword!"},
                REMOTE_ADDR=ip,
            )
            self.assertEqual(bad_login.status_code, 400)

        with patch("captcha.service.generate_captcha_code", return_value="R5T8"):
            login_get = login_client.get(reverse("identity:login") + f"?username={username}", REMOTE_ADDR=ip)
        self.assertEqual(login_get.status_code, 200)
        login_html = login_get.content.decode("utf-8")
        self.assertIn("data:image/png;base64,", login_html)
        m_login = re.search(r'name="captcha_id"[^>]*value="([0-9a-f]{32})"', login_html)
        self.assertIsNotNone(m_login)
        login_cid = m_login.group(1)
        self.tracked_captcha_ids.append(login_cid)

        ok_login = login_client.post(
            reverse("identity:login"),
            data={
                "username": username,
                "password": password,
                "captcha_id": login_cid,
                "captcha_code": "r5t8",
            },
            REMOTE_ADDR=ip,
        )
        self.assertEqual(ok_login.status_code, 302)
        self.assertEqual(ok_login.url, reverse("identity:profile"))
