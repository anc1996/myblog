"""
Phase 1 会话代次守卫、组权限撤权实时失效、强制改密白名单与超管治理后台测试。
"""

import os
import unittest
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse

from identity.account_lifecycle import AccountLifecycleService
from identity.models import MemberProfile

User = get_user_model()


class IdentitySessionGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = User.objects.filter(is_superuser=True, is_active=True).first()
        if not cls.admin:
            cls.admin = User.objects.create_superuser(
                username=f"sess_admin_{uuid4().hex[:6]}",
                email="sess_admin@test.com",
                password="StrongAdminPassword!123",
            )

    def test_group_removal_and_missing_epoch_invalidate_session_immediately(self):
        user = User.objects.create_user(username=f"sess_u_{uuid4().hex[:6]}", password="Password123!")
        grp = Group.objects.create(name=f"Authors_{uuid4().hex[:6]}")
        user.groups.add(grp)

        client = Client()
        try:
            client.force_login(user)
            # force_login 经 user_logged_in 信号自动同步 session_epoch
            self.assertEqual(client.get("/account/profile/").status_code, 200)

            # 1. 移出权限组：下一次请求立即失效
            user.groups.remove(grp)
            resp = client.get("/account/profile/")
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/account/login/", resp.url)

            # 2. 对 API 返回 403 session_invalid
            client.force_login(user)
            AccountLifecycleService.bump_session_epoch([user.id])
            api_resp = client.post("/api/v1/posts/")
            self.assertEqual(api_resp.status_code, 403)
            self.assertEqual(api_resp.json()["code"], "session_invalid")

            # 3. 伪造或缺失 session_epoch 不会在中间件中悄悄补齐，直接注销
            client.force_login(user)
            sess = client.session
            sess.pop("session_epoch", None)
            sess.save()
            resp_missing = client.get("/account/profile/")
            self.assertEqual(resp_missing.status_code, 302)
            self.assertIn("/account/login/", resp_missing.url)
        finally:
            grp.delete()
            AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=user.id, confirmed=True)

    def test_admin_reset_password_enforces_must_change_and_kicks_old_sessions(self):
        user = User.objects.create_user(username=f"rst_u_{uuid4().hex[:6]}", password="OldPassword123!")
        old_client = Client()
        new_client = Client()

        try:
            old_client.force_login(user)
            self.assertEqual(old_client.get("/account/profile/").status_code, 200)

            # 超管重置临时密码
            AccountLifecycleService.admin_reset_password(
                actor=self.admin,
                target_user_id=user.id,
                new_password="TempPassword!999",
            )

            # 旧会话立即失效
            self.assertEqual(old_client.get("/account/profile/").status_code, 302)
            self.assertIn("/account/login/", old_client.get("/account/profile/").url)

            # 新设备用临时密码登录：先进入强改密流
            login_resp = new_client.post(
                "/account/login/",
                {"username": user.username, "password": "TempPassword!999"},
            )
            self.assertEqual(login_resp.status_code, 302)
            self.assertIn("/account/password-change/", login_resp.url)

            # 未改密前访问个人中心被强制跳转到改密页，API 返回 403 must_change_password
            prof_resp = new_client.get("/account/profile/")
            self.assertEqual(prof_resp.status_code, 302)
            self.assertIn("/account/password-change/", prof_resp.url)
            api_resp = new_client.get("/api/v1/posts/")
            self.assertEqual(api_resp.status_code, 403)
            self.assertEqual(api_resp.json()["code"], "must_change_password")

            # 改密后当前会话保持有效并可访问个人中心
            chg_resp = new_client.post(
                "/account/password-change/",
                {
                    "old_password": "TempPassword!999",
                    "new_password": "NewStrongPass!888",
                    "new_password_confirm": "NewStrongPass!888",
                },
            )
            self.assertEqual(chg_resp.status_code, 302)
            self.assertEqual(new_client.get("/account/profile/").status_code, 200)
        finally:
            AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=user.id, confirmed=True)

    def test_admin_user_manage_recovery_hint_superuser_only(self):
        staff = User.objects.create_user(username=f"stf_{uuid4().hex[:6]}", password="Password123!", is_staff=True)
        access_perm = Permission.objects.get(content_type__app_label="wagtailadmin", codename="access_admin")
        staff.user_permissions.add(access_perm)
        target = User.objects.create_user(username=f"tgt_{uuid4().hex[:6]}", password="Password123!")
        profile = MemberProfile.get_or_create_for_user(target)
        profile.recovery_hint = "秘密联系线索-8899"
        profile.save()

        try:
            c_staff = Client()
            c_staff.force_login(staff)
            self.assertEqual(c_staff.get(f"/admin/identity/users/{target.id}/manage/").status_code, 302)
            self.assertEqual(
                c_staff.get(
                    f"/admin/identity/users/{target.id}/manage/",
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                ).status_code,
                403,
            )

            c_admin = Client()
            c_admin.force_login(self.admin)
            resp = c_admin.get(f"/admin/identity/users/{target.id}/manage/")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("秘密联系线索-8899", resp.content.decode("utf-8"))
        finally:
            for uid in (staff.id, target.id):
                AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=uid, confirmed=True)

    def test_native_wagtail_and_django_admin_password_reset_bypasses_blocked(self):
        from django.contrib.contenttypes.models import ContentType
        from django.core.exceptions import PermissionDenied
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from identity.admin import ControlledUserAdmin

        staff = User.objects.create_user(username=f"stf_pw_{uuid4().hex[:6]}", password="Password123!", is_staff=True)
        user_ct = ContentType.objects.get_for_model(User)
        perms = Permission.objects.filter(
            content_type__app_label__in=["wagtailadmin", "identity"],
            codename__in=["access_admin", "change_user"],
        )
        staff.user_permissions.add(*perms)
        target = User.objects.create_user(username=f"tgt_pw_{uuid4().hex[:6]}", password="InitialPassword!123")

        try:
            # 1. 非超管员工不得借 Wagtail 原生 edit 视图修改他人密码
            c_staff = Client()
            c_staff.force_login(staff)
            edit_url = reverse("wagtailusers_users:edit", args=[target.id])
            r1 = c_staff.post(
                edit_url,
                {"username": target.username, "password1": "HackedPass!999", "password2": "HackedPass!999"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            self.assertEqual(r1.status_code, 403)
            target.refresh_from_db()
            self.assertTrue(target.check_password("InitialPassword!123"))

            # 2. 非超管访问 Django Admin 改密端点被拒绝
            admin_obj = ControlledUserAdmin(User, AdminSite())
            rf = RequestFactory()
            req = rf.post(f"/django-admin/identity/user/{target.id}/password/")
            req.user = staff
            self.assertFalse(admin_obj.has_change_permission(req, target))
            with self.assertRaises(PermissionDenied):
                admin_obj.user_change_password(req, str(target.id))

            # 3. 即使超管调用原生 Wagtail 用户编辑页提交 password1/2 也会被导流至受控治理页，原密码不变
            c_admin = Client()
            c_admin.force_login(self.admin)
            r2 = c_admin.post(
                edit_url,
                {"username": target.username, "password1": "Bypassed!999", "password2": "Bypassed!999"},
            )
            self.assertEqual(r2.status_code, 302)
            self.assertIn(f"/admin/identity/users/{target.id}/manage/", r2.url)
            target.refresh_from_db()
            self.assertTrue(target.check_password("InitialPassword!123"))

            # 4. 原生 Wagtail 删除入口 /admin/users/delete/<id>/ 也被拦截导流至受控治理页
            del_url = reverse("wagtailusers_users:delete", args=[target.id])
            r3 = c_admin.post(del_url)
            self.assertEqual(r3.status_code, 302)
            self.assertIn(f"/admin/identity/users/{target.id}/manage/", r3.url)
            self.assertTrue(User.objects.filter(pk=target.id).exists())
        finally:
            for uid in (staff.id, target.id):
                AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=uid, confirmed=True)

    def test_self_change_password_rejects_stale_session_race_and_reverse_clear_signals(self):
        from django.core.exceptions import PermissionDenied, ValidationError
        from django.test import RequestFactory

        user = User.objects.create_user(username=f"rev_u_{uuid4().hex[:6]}", password="OldPassword!123")
        grp = Group.objects.create(name=f"RevGrp_{uuid4().hex[:6]}")
        perm = Permission.objects.get(content_type__app_label="wagtailadmin", codename="access_admin")
        grp.user_set.add(user)

        client = Client()
        try:
            # 1. 反向 group.user_set.clear() 立即使旧会话失效
            client.force_login(user)
            self.assertEqual(client.get("/account/profile/").status_code, 200)
            grp.user_set.clear()
            self.assertEqual(client.get("/account/profile/").status_code, 302)
            self.assertIn("/account/login/", client.get("/account/profile/").url)

            # 2. 反向 permission.group_set.clear() 同样失效组内成员会话
            grp.user_set.add(user)
            grp.permissions.add(perm)
            client.force_login(user)
            self.assertEqual(client.get("/account/profile/").status_code, 200)
            perm.group_set.clear()
            self.assertEqual(client.get("/account/profile/").status_code, 302)

            # 3. 持旧会话发起自助改密时遇到管理员并发重置密码：锁内校验拦截且不得覆盖临时密码
            client.force_login(user)
            stale_session = client.session
            AccountLifecycleService.admin_reset_password(
                actor=self.admin,
                target_user_id=user.id,
                new_password="TempPassword!777",
            )
            rf = RequestFactory()
            req = rf.post("/account/profile/")
            req.session = stale_session
            req.user = user  # 模拟持旧内存密码哈希的用户对象
            with self.assertRaises((PermissionDenied, ValidationError)):
                AccountLifecycleService.self_change_password(
                    user=user,
                    old_password="OldPassword!123",
                    new_password="AttackerPass!999",
                    request=req,
                )
            user.refresh_from_db()
            self.assertTrue(user.check_password("TempPassword!777"))
            self.assertTrue(user.member_profile.must_change_password)
        finally:
            grp.delete()
            AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=user.id, confirmed=True)
