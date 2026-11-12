"""
Phase 1 账号生命周期、受控删除、资产过户、赞踩重算、SQL 回滚与真实并发测试。
"""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import os
import unittest
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, transaction
from django.test import RequestFactory
from PIL import Image
from wagtail.models import Page
from wagtail.users.models import UserProfile

from blog.models import BlogPage
from comments.models import Comment, CommentReaction
from comments.services import CommentService
from content_lifecycle.recycle import RecycleService
from identity.account_lifecycle import AccountLifecycleService
from identity.avatar_purge import AvatarPurgeProcessor
from identity.avatar_storage import get_storage_and_namespace
from identity.avatars import upload_member_avatar
from identity.models import AccountAuditLog, AvatarPurgeTask, MemberProfile
from media_library.models import CustomDocument, CustomImage

User = get_user_model()


def _make_png_bytes(color=(16, 120, 210), size=(64, 64)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class IdentityLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = User.objects.filter(is_superuser=True, is_active=True).first()
        if not cls.admin:
            cls.admin = User.objects.create_superuser(
                username=f"id_admin_{uuid4().hex[:6]}",
                email="id_admin@test.com",
                password="StrongAdminPassword!123",
            )
        cls.home = Page.objects.filter(depth=2).first() or Page.get_first_root_node()

    def test_raw_user_delete_and_django_admin_delete_blocked(self):
        user = User.objects.create_user(username=f"rawdel_{uuid4().hex[:6]}", password="Password123!")
        try:
            with self.assertRaises(PermissionDenied):
                user.delete()
            with self.assertRaises(PermissionDenied):
                User.objects.filter(pk=user.pk).delete()

            from django.contrib.admin.sites import AdminSite
            from identity.admin import ControlledUserAdmin

            admin_obj = ControlledUserAdmin(User, AdminSite())
            rf = RequestFactory()
            req = rf.get("/django-admin/")
            req.user = self.admin
            self.assertNotIn("delete_selected", admin_obj.get_actions(req))
            self.assertFalse(admin_obj.has_delete_permission(req, user))
            with self.assertRaises(PermissionDenied):
                admin_obj.delete_model(req, user)
            with self.assertRaises(PermissionDenied):
                admin_obj.delete_queryset(req, User.objects.filter(pk=user.pk))
        finally:
            AccountLifecycleService.hard_delete(
                actor=self.admin,
                target_user_id=user.id,
                confirmed=True,
            )

    def test_deactivate_preserves_avatar_and_reactivate_restores(self):
        user = User.objects.create_user(username=f"deact_{uuid4().hex[:6]}", password="Password123!")
        uploaded = SimpleUploadedFile("avatar.png", _make_png_bytes(), content_type="image/png")
        profile = upload_member_avatar(user=user, uploaded_file=uploaded)
        storage, _ = get_storage_and_namespace("default")
        key = profile.avatar_key
        token = profile.avatar_token

        try:
            AccountLifecycleService.deactivate_user(actor=self.admin, target_user_id=user.id, reason="测试停用")
            user.refresh_from_db()
            self.assertFalse(user.is_active)
            self.assertTrue(storage.exists(key))
            self.assertEqual(AvatarPurgeTask.objects.filter(storage_key=key).count(), 0)

            from django.test import Client
            c = Client()
            resp = c.get(f"/avatar-gateway/{token}/")
            self.assertEqual(resp.status_code, 404)

            AccountLifecycleService.reactivate_user(actor=self.admin, target_user_id=user.id)
            resp2 = c.get(f"/avatar-gateway/{token}/")
            self.assertEqual(resp2.status_code, 200)
            resp2.close()
        finally:
            AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=user.id, confirmed=True)
            AvatarPurgeProcessor.process_pending_tasks(batch_size=20)

    def test_hard_delete_requires_transfer_and_recounts_reactions(self):
        author_u = User.objects.create_user(username=f"author_del_{uuid4().hex[:6]}", password="Password123!")
        other_u = User.objects.create_user(username=f"other_react_{uuid4().hex[:6]}", password="Password123!")
        receiver_u = User.objects.create_superuser(
            username=f"recv_{uuid4().hex[:6]}",
            email="recv@test.com",
            password="StrongAdminPassword!123",
        )

        # 上传会员头像 + 遗留 Wagtail UserProfile 头像
        av_file = SimpleUploadedFile("m.png", _make_png_bytes((200, 30, 30)), content_type="image/png")
        profile = upload_member_avatar(user=author_u, uploaded_file=av_file)
        member_avatar_key = profile.avatar_key

        storage, _ = get_storage_and_namespace("default")
        legacy_key = storage.save(f"avatar_images/legacy_{uuid4().hex[:8]}.png", SimpleUploadedFile("l.png", _make_png_bytes()))
        wp = UserProfile.get_for_user(author_u)
        wp.avatar.name = legacy_key
        wp.save()

        # 创建已发布页、草稿页、回收页、图片、文档
        live_p = BlogPage(title="已发布归属文章", slug=f"live-{uuid4().hex[:6]}", owner=author_u)
        draft_p = BlogPage(title="纯草稿归属文章", slug=f"draft-{uuid4().hex[:6]}", owner=author_u, live=False)
        recycled_p = BlogPage(title="回收站归属文章", slug=f"rec-{uuid4().hex[:6]}", owner=author_u)
        self.home.add_child(instance=live_p)
        self.home.add_child(instance=draft_p)
        self.home.add_child(instance=recycled_p)
        live_p.save_revision(user=author_u).publish(user=self.admin)
        draft_p.save_revision(user=author_u)
        recycled_p.save_revision(user=author_u)
        batch = RecycleService.recycle_page_or_subtree(recycled_p, actor=self.admin)

        img = CustomImage.objects.create(
            title="作者测试图",
            file=SimpleUploadedFile("ci.png", _make_png_bytes(), content_type="image/png"),
            uploaded_by_user=author_u,
        )
        doc = CustomDocument.objects.create(
            title="作者测试文档",
            file=SimpleUploadedFile("cd.txt", b"hello document", content_type="text/plain"),
            uploaded_by_user=author_u,
        )

        # 评论与赞踩：author_u 写一条评论 + 对 receiver_u 的两条评论分别点赞/点踩
        c_by_author = CommentService.post_comment(page=live_p, author=author_u, raw_content="注销测试评论", auto_approve=True)
        c_shared = CommentService.post_comment(page=live_p, author=receiver_u, raw_content="共享赞踩评论", auto_approve=True)
        c_only_author = CommentService.post_comment(page=live_p, author=receiver_u, raw_content="仅被删用户赞过的评论", auto_approve=True)
        CommentService.react_comment(c_shared.id, author_u, 1)
        CommentService.react_comment(c_shared.id, other_u, 1)
        CommentService.react_comment(c_only_author.id, author_u, -1)

        author_uid = author_u.id
        try:
            # 1. 不指定接管人时必须被拒绝
            with self.assertRaises(ValidationError):
                AccountLifecycleService.hard_delete(
                    actor=self.admin,
                    target_user_id=author_uid,
                    transfer_to_user_id=None,
                    confirmed=True,
                )
            self.assertTrue(User.objects.filter(pk=author_uid).exists())

            # 2. 指定停用/非管理员作为接管人必须被拒绝
            with self.assertRaises(ValidationError):
                AccountLifecycleService.hard_delete(
                    actor=self.admin,
                    target_user_id=author_uid,
                    transfer_to_user_id=other_u.id,
                    confirmed=True,
                )

            # 3. 指定合法接管人执行受控硬删
            res = AccountLifecycleService.hard_delete(
                actor=self.admin,
                target_user_id=author_uid,
                transfer_to_user_id=receiver_u.id,
                confirmed=True,
            )
            self.assertEqual(res["status"], "deleted")
            self.assertEqual(res["transferred_pages"], 3)
            self.assertEqual(res["transferred_images"], 1)
            self.assertEqual(res["transferred_documents"], 1)
            self.assertFalse(User.objects.filter(pk=author_uid).exists())

            # 页面/媒体已过户，评论保留且显示为“已注销用户”
            live_p.refresh_from_db()
            draft_p.refresh_from_db()
            recycled_p.refresh_from_db()
            img.refresh_from_db()
            doc.refresh_from_db()
            self.assertEqual(live_p.owner_id, receiver_u.id)
            self.assertEqual(draft_p.owner_id, receiver_u.id)
            self.assertEqual(recycled_p.owner_id, receiver_u.id)
            self.assertEqual(img.uploaded_by_user_id, receiver_u.id)
            self.assertEqual(doc.uploaded_by_user_id, receiver_u.id)

            c_by_author.refresh_from_db()
            self.assertIsNone(c_by_author.author_id)
            self.assertEqual(c_by_author.author_display, "已注销用户")
            self.assertEqual(c_by_author.get_author_presentation()["kind"], "deleted_member")

            # 赞踩计数准确重算
            c_shared.refresh_from_db()
            c_only_author.refresh_from_db()
            self.assertEqual(c_shared.like_count, 1)
            self.assertEqual(c_only_author.dislike_count, 0)

            # 两个头像清理任务独立存活且消费后真实删除对象
            tasks = AvatarPurgeTask.objects.filter(target_user_id_snapshot=author_uid)
            self.assertEqual(tasks.count(), 2)
            AvatarPurgeProcessor.process_pending_tasks(batch_size=20)
            self.assertFalse(storage.exists(member_avatar_key))
            self.assertFalse(storage.exists(legacy_key))
            self.assertTrue(AccountAuditLog.objects.filter(action="hard_delete", target_user_id_snapshot=author_uid).exists())
        finally:
            for p in (live_p, draft_p):
                if Page.objects.filter(pk=p.id).exists():
                    RecycleService.recycle_page_or_subtree(p, actor=self.admin)
                    RecycleService.purge_page_or_subtree(p.id, actor=self.admin)
            if Page.objects.filter(pk=recycled_p.id).exists():
                RecycleService.purge_page_or_subtree(recycled_p.id, actor=self.admin)
            from media_library.models import MediaReferenceRecord
            MediaReferenceRecord.objects.filter(asset_id__in=[img.public_id, doc.public_id]).delete()
            img.delete()
            doc.delete()
            for uid in (other_u.id, receiver_u.id):
                if User.objects.filter(pk=uid).exists():
                    AccountLifecycleService.hard_delete(
                        actor=self.admin,
                        target_user_id=uid,
                        transfer_to_user_id=self.admin.id,
                        confirmed=True,
                    )

    def test_hard_delete_sql_rollback_preserves_user_and_avatar(self):
        user = User.objects.create_user(username=f"rb_{uuid4().hex[:6]}", password="Password123!")
        profile = upload_member_avatar(
            user=user,
            uploaded_file=SimpleUploadedFile("rb.png", _make_png_bytes(), content_type="image/png"),
        )
        key = profile.avatar_key
        storage, _ = get_storage_and_namespace("default")

        try:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    AccountLifecycleService.hard_delete(
                        actor=self.admin,
                        target_user_id=user.id,
                        confirmed=True,
                    )
                    raise RuntimeError("模拟外层事务回滚")
            self.assertTrue(User.objects.filter(pk=user.id).exists())
            self.assertEqual(AvatarPurgeTask.objects.filter(storage_key=key).count(), 0)
            self.assertTrue(storage.exists(key))
        finally:
            AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=user.id, confirmed=True)
            AvatarPurgeProcessor.process_pending_tasks(batch_size=20)

    def test_concurrent_hard_delete_and_reactions_no_deadlock(self):
        """真实 PostgreSQL 并发演练：同时进行赞踩写入与受控账号删除，不得死锁且最终计数精确。"""
        target_u = User.objects.create_user(username=f"conc_del_{uuid4().hex[:6]}", password="Password123!")
        voter_u = User.objects.create_user(username=f"conc_vot_{uuid4().hex[:6]}", password="Password123!")
        author_u = User.objects.create_user(username=f"conc_own_{uuid4().hex[:6]}", password="Password123!")
        page = BlogPage(title="并发赞踩页", slug=f"conc-{uuid4().hex[:6]}", owner=author_u)
        self.home.add_child(instance=page)
        page.save_revision(user=author_u).publish(user=self.admin)
        c = CommentService.post_comment(page=page, author=author_u, raw_content="并发目标评论", auto_approve=True)
        CommentService.react_comment(c.id, target_u, 1)

        def _vote():
            close_old_connections()
            try:
                CommentService.react_comment(c.id, voter_u, 1)
                return "ok"
            finally:
                close_old_connections()

        def _delete():
            close_old_connections()
            try:
                AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=target_u.id, confirmed=True)
                return "ok"
            finally:
                close_old_connections()

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(_vote)
                f2 = pool.submit(_delete)
                self.assertEqual(f1.result(timeout=15), "ok")
                self.assertEqual(f2.result(timeout=15), "ok")
            c.refresh_from_db()
            self.assertEqual(c.like_count, 1)
            self.assertEqual(CommentReaction.objects.filter(comment=c, reaction_type=1).count(), 1)
        finally:
            RecycleService.recycle_page_or_subtree(page, actor=self.admin)
            RecycleService.purge_page_or_subtree(page.id, actor=self.admin)
            for uid in (voter_u.id, author_u.id):
                if User.objects.filter(pk=uid).exists():
                    AccountLifecycleService.hard_delete(
                        actor=self.admin,
                        target_user_id=uid,
                        transfer_to_user_id=self.admin.id,
                        confirmed=True,
                    )

    def test_concurrent_reply_to_user_and_hard_delete_no_deadlock(self):
        """真实 PostgreSQL 并发演练：B 回复 A 的评论同时管理员硬删 A，必须无死锁且数据一致。"""
        user_a = User.objects.create_user(username=f"conc_a_{uuid4().hex[:6]}", password="Password123!")
        user_b = User.objects.create_user(username=f"conc_b_{uuid4().hex[:6]}", password="Password123!")
        page = BlogPage(title="并发回复目标页", slug=f"creply-{uuid4().hex[:6]}", owner=self.admin)
        self.home.add_child(instance=page)
        page.save_revision(user=self.admin).publish(user=self.admin)

        root_c = CommentService.post_comment(page=page, author=self.admin, raw_content="根评论", auto_approve=True)
        sub_c = CommentService.post_comment(
            page=page,
            author=user_a,
            raw_content="A 的一级回复",
            parent_id=root_c.id,
            reply_to_comment_id=root_c.id,
            auto_approve=True,
        )

        def _reply_to_a():
            close_old_connections()
            try:
                CommentService.post_comment(
                    page=page,
                    author=user_b,
                    raw_content="B 并发回复 A",
                    parent_id=root_c.id,
                    reply_to_comment_id=sub_c.id,
                    auto_approve=True,
                )
                return "ok"
            finally:
                close_old_connections()

        def _delete_a():
            close_old_connections()
            try:
                AccountLifecycleService.hard_delete(actor=self.admin, target_user_id=user_a.id, confirmed=True)
                return "ok"
            finally:
                close_old_connections()

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(_reply_to_a)
                f2 = pool.submit(_delete_a)
                self.assertEqual(f1.result(timeout=15), "ok")
                self.assertEqual(f2.result(timeout=15), "ok")
            self.assertFalse(User.objects.filter(pk=user_a.id).exists())
            self.assertEqual(Comment.objects.filter(page=page, author=user_b).count(), 1)
        finally:
            RecycleService.recycle_page_or_subtree(page, actor=self.admin)
            RecycleService.purge_page_or_subtree(page.id, actor=self.admin)
            if User.objects.filter(pk=user_b.id).exists():
                AccountLifecycleService.hard_delete(
                    actor=self.admin,
                    target_user_id=user_b.id,
                    transfer_to_user_id=self.admin.id,
                    confirmed=True,
                )
