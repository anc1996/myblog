"""
Phase 1 独立头像 Pillow 重编码、安全网关、Wagtail 后台统一接管、
AvatarPurgeProcessor 与 CrossStorageReconciler 孤儿对账测试。
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from io import BytesIO
import os
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client, override_settings
from PIL import Image

from content_lifecycle.reconciliation import CrossStorageReconciler
from identity.account_lifecycle import AccountLifecycleService
from identity.avatar_purge import AvatarPurgeProcessor
from identity.avatar_storage import get_storage_and_namespace
from identity.avatars import sanitize_and_encode_avatar, upload_member_avatar
from identity.models import AvatarPurgeTask
from media_library.models import CustomImage

User = get_user_model()


def _make_jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (80, 60), (40, 180, 90))
    exif = img.getexif()
    exif[0x010E] = "Secret GPS / Camera Note"
    exif[0x0112] = 6  # Orientation rotate 90 CW
    buf = BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _make_animated_gif() -> bytes:
    f1 = Image.new("RGB", (20, 20), (255, 0, 0))
    f2 = Image.new("RGB", (20, 20), (0, 255, 0))
    buf = BytesIO()
    f1.save(buf, format="GIF", save_all=True, append_images=[f2], duration=100, loop=0)
    return buf.getvalue()


class IdentityAvatarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = User.objects.filter(is_superuser=True, is_active=True).first()
        if not cls.admin:
            cls.admin = User.objects.create_superuser(
                username=f"av_admin_{uuid4().hex[:6]}",
                email="av_admin@test.com",
                password="StrongAdminPassword!123",
            )

    def test_pillow_sanitization_strips_exif_and_rejects_bad_uploads(self):
        # 1. 带 EXIF 与旋转信息的 JPEG：重编码为静态 WebP、尺寸旋转归正、EXIF 清空
        raw_jpg = _make_jpeg_with_exif()
        out_webp = sanitize_and_encode_avatar(SimpleUploadedFile("exif.jpg", raw_jpg, content_type="image/jpeg"))
        decoded = Image.open(BytesIO(out_webp))
        self.assertEqual(decoded.format, "WEBP")
        self.assertEqual(decoded.size, (60, 80))  # 80x60 旋转 90 度后归正为 60x80
        self.assertEqual(len(decoded.getexif()), 0)
        self.assertNotIn("exif", decoded.info)

        # 2. SVG / 伪装扩展名的 SVG 均被拒绝
        svg_payload = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        with self.assertRaises(ValidationError):
            sanitize_and_encode_avatar(SimpleUploadedFile("evil.png", svg_payload, content_type="image/png"))

        # 3. 动态 GIF 被拒绝
        with self.assertRaises(ValidationError):
            sanitize_and_encode_avatar(SimpleUploadedFile("anim.gif", _make_animated_gif(), content_type="image/gif"))

        # 4. 超大文件（>2MB）被拒绝
        with self.assertRaises(ValidationError):
            sanitize_and_encode_avatar(SimpleUploadedFile("big.jpg", b"x" * (2 * 1024 * 1024 + 10)))

        # 5. 超大像素尺寸（>2048px）被拒绝
        huge_buf = BytesIO()
        Image.new("RGB", (2200, 100), (10, 10, 10)).save(huge_buf, format="PNG")
        with self.assertRaises(ValidationError):
            sanitize_and_encode_avatar(SimpleUploadedFile("wide.png", huge_buf.getvalue()))

    def test_avatar_upload_does_not_create_custom_image_and_purges_old_key_in_real_storage(self):
        user = User.objects.create_user(username=f"av_u_{uuid4().hex[:6]}", password="Password123!", is_staff=True)
        storage, _ = get_storage_and_namespace("default")
        before_custom_img_count = CustomImage.objects.count()

        try:
            p1 = upload_member_avatar(
                user=user,
                uploaded_file=SimpleUploadedFile("v1.jpg", _make_jpeg_with_exif(), content_type="image/jpeg"),
            )
            key1 = p1.avatar_key
            tok1 = p1.avatar_token
            self.assertEqual(CustomImage.objects.count(), before_custom_img_count)
            self.assertTrue(key1.startswith("member-avatars/"))
            self.assertTrue(storage.exists(key1))

            # 网关返回正确响应头
            client = Client()
            resp = client.get(f"/avatar-gateway/{tok1}/")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp["Content-Type"], "image/webp")
            self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
            resp.close()

            head_resp = client.head(f"/avatar-gateway/{tok1}/")
            self.assertEqual(head_resp.status_code, 200)
            self.assertEqual(head_resp["X-Content-Type-Options"], "nosniff")

            # 换图：新对象生效，旧令牌失效，旧键入队清理且消费后物理消失
            p2 = upload_member_avatar(
                user=user,
                uploaded_file=SimpleUploadedFile("v2.jpg", _make_jpeg_with_exif(), content_type="image/jpeg"),
            )
            key2 = p2.avatar_key
            self.assertNotEqual(key1, key2)
            self.assertEqual(client.get(f"/avatar-gateway/{tok1}/").status_code, 404)
            g2 = client.get(f"/avatar-gateway/{p2.avatar_token}/")
            self.assertEqual(g2.status_code, 200)
            g2.close()

            stats = AvatarPurgeProcessor.process_pending_tasks(batch_size=20)
            self.assertGreaterEqual(stats["done"], 1)
            self.assertFalse(storage.exists(key1))
            self.assertTrue(storage.exists(key2))

            # 清理失败重试验证：模拟存储删除报错 -> 状态转为 failed 且 attempts 递增
            fake_task = AvatarPurgeTask.objects.create(
                storage_alias="default",
                storage_namespace=p2.avatar_storage_namespace,
                storage_key="member-avatars/nonexistent/retry_probe.webp",
                source="avatar_replace",
                operation_id=uuid4(),
            )
            with patch("identity.avatar_purge.controlled_delete_storage_object", side_effect=OSError("模拟网络故障")):
                with patch.object(type(storage), "exists", return_value=True):
                    AvatarPurgeProcessor.process_pending_tasks(batch_size=20)
            fake_task.refresh_from_db()
            self.assertEqual(fake_task.status, "failed")
            self.assertEqual(fake_task.attempts, 1)
            # 恢复后再次到期消费 -> 对象不存在按 done 结束
            AvatarPurgeTask.objects.filter(pk=fake_task.pk).update(next_attempt_at=datetime.now(dt_timezone.utc) - timedelta(seconds=5))
            AvatarPurgeProcessor.process_pending_tasks(batch_size=20)
            fake_task.refresh_from_db()
            self.assertEqual(fake_task.status, "done")
        finally:
            AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=user.id, confirmed=True)
            AvatarPurgeProcessor.process_pending_tasks(batch_size=20)

    def test_wagtail_admin_account_hides_legacy_avatar_panel(self):
        from wagtail.users.models import UserProfile
        admin_staff = User.objects.create_superuser(
            username=f"acc_admin_{uuid4().hex[:6]}",
            email="acc_admin@test.com",
            password="StrongAdminPassword!123",
        )
        try:
            c = Client()
            c.force_login(admin_staff)
            resp = c.get("/admin/account/")
            self.assertEqual(resp.status_code, 200)
            html = resp.content.decode("utf-8")
            self.assertIn("unified_avatar-avatar", html)
            self.assertNotIn('name="avatar-avatar"', html)

            # 通过 /admin/account/ 提交统一头像上传，写入 MemberProfile 且 UserProfile.avatar 保持为空
            post_resp = c.post(
                "/admin/account/",
                {
                    "name_email-first_name": "Admin",
                    "name_email-last_name": "Tester",
                    "name_email-email": "acc_admin@test.com",
                    "theme-theme": "system",
                    "theme-contrast": "system",
                    "theme-density": "default",
                    "unified_avatar-avatar": SimpleUploadedFile("adm.jpg", _make_jpeg_with_exif(), content_type="image/jpeg"),
                    "avatar-avatar": SimpleUploadedFile("forged.jpg", _make_jpeg_with_exif(), content_type="image/jpeg"),
                },
            )
            self.assertEqual(post_resp.status_code, 302)
            admin_staff.refresh_from_db()
            mp = admin_staff.member_profile
            self.assertTrue(mp.avatar_key.startswith("member-avatars/"))
            wp = UserProfile.get_for_user(admin_staff)
            self.assertFalse(bool(wp.avatar and wp.avatar.name))
        finally:
            AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=admin_staff.id, confirmed=True)
            AvatarPurgeProcessor.process_pending_tasks(batch_size=20)

    def test_reconcile_orphan_avatars_dry_run_and_grace_period(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storages_cfg = {
                "default": {
                    "BACKEND": "media_library.storage.ControlledFileSystemStorage",
                    "OPTIONS": {"location": tmpdir},
                },
                "staticfiles": {
                    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
                },
            }
            with override_settings(STORAGES=storages_cfg):
                user = User.objects.create_user(username=f"rec_u_{uuid4().hex[:6]}", password="Password123!")
                storage, _ = get_storage_and_namespace("default")
                try:
                    profile = upload_member_avatar(
                        user=user,
                        uploaded_file=SimpleUploadedFile("active.jpg", _make_jpeg_with_exif()),
                    )
                    in_use_key = profile.avatar_key
                    fresh_orphan = storage.save("member-avatars/orphan/fresh.webp", ContentFile(b"fresh"))
                    old_orphan_1 = storage.save("member-avatars/orphan/old.webp", ContentFile(b"old1"))
                    old_orphan_2 = storage.save("avatar_images/old_legacy.jpg", ContentFile(b"old2"))

                    # 仅把两个旧孤儿的 mtime 调早至 48 小时前
                    old_ts = (datetime.now(dt_timezone.utc) - timedelta(hours=48)).timestamp()
                    import os
                    for k in (old_orphan_1, old_orphan_2):
                        os.utime(os.path.join(tmpdir, k), (old_ts, old_ts))

                    # 1. dry_run=True 只列候选，不删也不入队
                    dry_res = CrossStorageReconciler.reconcile_orphan_avatars(dry_run=True, grace_period_seconds=86400)
                    cand_keys = {c["storage_key"] for c in dry_res["candidates"]}
                    self.assertEqual(cand_keys, {old_orphan_1, old_orphan_2})
                    self.assertTrue(storage.exists(old_orphan_1))
                    self.assertTrue(storage.exists(old_orphan_2))

                    # 2. dry_run=False 只擦除超期孤儿，保留在用与未满 24h 对象
                    real_res = CrossStorageReconciler.reconcile_orphan_avatars(dry_run=False, grace_period_seconds=86400)
                    self.assertEqual(real_res["purged_count"], 2)
                    self.assertFalse(storage.exists(old_orphan_1))
                    self.assertFalse(storage.exists(old_orphan_2))
                    self.assertTrue(storage.exists(fresh_orphan))
                    self.assertTrue(storage.exists(in_use_key))
                finally:
                    AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=user.id, confirmed=True)
                    AvatarPurgeProcessor.process_pending_tasks(batch_size=20)

    def test_deactivated_user_upload_rejected_and_db_error_aborts_avatar_purge(self):
        from django.core.exceptions import PermissionDenied
        from wagtail.users.models import UserProfile

        user = User.objects.create_user(username=f"deact_av_{uuid4().hex[:6]}", password="Password123!")
        storage, _ = get_storage_and_namespace("default")
        try:
            p = upload_member_avatar(
                user=user,
                uploaded_file=SimpleUploadedFile("ok.jpg", _make_jpeg_with_exif()),
            )
            active_key = p.avatar_key
            AccountLifecycleService.deactivate_user(actor=self.admin, target_user_id=user.id)
            user.refresh_from_db()

            # 1. 已停用用户即使绕过中间件直调 upload_member_avatar 也被行锁后 is_active 校验阻断
            with self.assertRaises(PermissionDenied):
                upload_member_avatar(
                    user=user,
                    uploaded_file=SimpleUploadedFile("late.jpg", _make_jpeg_with_exif()),
                )
            p.refresh_from_db()
            self.assertEqual(p.avatar_key, active_key)

            # 2. UserProfile 引用检查抛出异常时，清理处理器与对账器绝不静默误删
            task = AvatarPurgeTask.objects.create(
                storage_alias="default",
                storage_namespace=p.avatar_storage_namespace,
                storage_key="member-avatars/nonexistent/db_err_probe.webp",
                source="avatar_replace",
                operation_id=uuid4(),
            )
            with patch.object(UserProfile.objects, "filter", side_effect=RuntimeError("DB 模拟异常")):
                stats = AvatarPurgeProcessor.process_pending_tasks(batch_size=20)
            self.assertEqual(stats["failed"], 1)
            task.refresh_from_db()
            self.assertEqual(task.status, "failed")

            with patch.object(UserProfile.objects, "exclude", side_effect=RuntimeError("DB 模拟异常")):
                rec_res = CrossStorageReconciler.reconcile_orphan_avatars(dry_run=False)
            self.assertTrue(rec_res.get("aborted"))
            self.assertFalse(rec_res.get("healthy"))
            self.assertTrue(storage.exists(active_key))
        finally:
            AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=user.id, confirmed=True)
            AvatarPurgeProcessor.process_pending_tasks(batch_size=20)
