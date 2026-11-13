"""
S7 私有媒体统一受控交付、Range 续传与 MEDIA-REF-01 引用保护单元测试。

实测覆盖：
1. MediaReferenceTracker: Revision 保存时批量登记媒体引用与零引用安全删除检查；
2. 引用保护拦截：存在任何页面版本引用时坚决阻止物理删除 (fail closed)；
3. MEDIA-ACL-02 契约：公开改受限时默认阻断 (409 conflict)，管理员确认后同一事务连带下架文章；
4. 媒体网关鉴权：受限资源匿名访问直接拦截 403 Forbidden，管理员访问返回 200 OK；
5. HTTP Range 206 续传：模拟音视频播放器 Range 请求，断言返回 206 Partial Content 与 Content-Range；
6. X-Accel-Redirect 生产流式代理头输出校验。
"""

import io
import os
from pathlib import Path
import unittest
from uuid import uuid4

from django.core.files.base import ContentFile
from django.test import RequestFactory
from PIL import Image as PILImage

PROJECT_DIR = Path(__file__).resolve().parents[1]


def create_dummy_png(name="test.png", width=10, height=10):
    """生成符合 Wagtail / Willow 校验的合法最小 PNG 字节流文件"""
    buf = io.BytesIO()
    img = PILImage.new("RGB", (width, height), color="blue")
    img.save(buf, format="PNG")
    return ContentFile(buf.getvalue(), name=name)


class S7MediaDeliveryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
        import django
        django.setup()
        cls.factory = RequestFactory()

    def test_media_reference_tracker_and_deletion_protection(self):
        from media_library.models import CustomImage, MediaReferenceRecord
        from media_library.references import MediaReferenceTracker
        from content_blocks.contracts import MediaReference

        # 1. 创建一张测试图片
        img = CustomImage.objects.create(
            title="被引用的重要图片",
            public_id=uuid4(),
            file=create_dummy_png("ref_test.png"),
        )

        # 2. 初始状态：无引用，允许安全删除
        can_del, msg, refs = MediaReferenceTracker.check_asset_references(img.public_id)
        self.assertTrue(can_del)
        self.assertEqual(len(refs), 0)

        # 3. 模拟页面版本 1 引用了该图片
        page_id = 999
        rev_id = 1
        MediaReferenceTracker.register_revision_references(
            page_id=page_id,
            revision_id=rev_id,
            references=[MediaReference(asset_id=img.public_id, asset_type="image", block_path="body.0")],
        )

        # 4. 再次检查：存在引用，一票否决阻断物理删除！
        can_del, msg, refs = MediaReferenceTracker.check_asset_references(img.public_id)
        self.assertFalse(can_del)
        self.assertIn("禁止物理删除", msg)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["page_id"], page_id)

        # 5. 清理引用记录与测试图片
        MediaReferenceRecord.objects.filter(asset_id=img.public_id).delete()
        img.delete()

    def test_media_acl_02_default_blocking_and_confirmed_unpublish(self):
        """
        测试 MEDIA-ACL-02 契约：
        1. 公开图片被已发布页面引用；
        2. 未传确认凭证修改受限：触发 409 conflict 默认阻断；
        3. 传入有效确认凭证：修改受限并在同一事务中连带下架引用页面！
        """
        from home.models import HomePage
        from blog.models import BlogPage
        from media_library.models import CustomImage, VisibilityChoices
        from media_library.references import MediaReferenceTracker
        from media_library.access import plan_restrict, restrict_asset
        from content_blocks.contracts import MediaReference

        home = HomePage.objects.first()
        img = CustomImage.objects.create(
            title="公开共享封面",
            public_id=uuid4(),
            visibility=VisibilityChoices.PUBLIC,
            file=create_dummy_png("public_share.png"),
        )

        # 创建已发布页面并关联此图片
        page = BlogPage(title="测试公开文章", slug=f"acl-test-{uuid4().hex[:6]}", featured_image=img)
        home.add_child(instance=page)
        rev = page.save_revision()
        rev.publish()
        page.refresh_from_db()
        self.assertTrue(page.live)

        # 登记正式发布引用
        MediaReferenceTracker.register_revision_references(
            page_id=page.id,
            revision_id=page.live_revision_id,
            references=[MediaReference(asset_id=img.public_id, asset_type="image", block_path="featured_image")],
        )

        # 1. 评估影响：检测到正在被 1 篇已发布文章引用
        plan = plan_restrict("image", img.public_id)
        self.assertTrue(plan["requires_confirmation"])
        self.assertEqual(len(plan["affected_pages"]), 1)
        token = plan["confirmed_plan_token"]

        # 2. 未传确认凭证：默认阻断 (409 conflict)，文章保持 live
        res_blocked = restrict_asset("image", img.public_id, confirmed_plan_token=None, system_authorized=True)
        self.assertEqual(res_blocked["status"], "conflict")
        self.assertEqual(res_blocked["error"], "affected_publications")
        page.refresh_from_db()
        self.assertTrue(page.live, "未确认时页面绝不能被下架")

        # 3. 传有效凭证：执行连带下架，图片变受限，文章被正式下架
        res_confirmed = restrict_asset("image", img.public_id, confirmed_plan_token=token, system_authorized=True)
        self.assertEqual(res_confirmed["status"], "success")
        img.refresh_from_db()
        self.assertEqual(img.visibility, VisibilityChoices.RESTRICTED)
        page.refresh_from_db()
        self.assertFalse(page.live, "管理员确认后受影响文章必须被下架")

        # 4. 清理测试数据
        page.delete(allow_physical_purge=True)
        img.delete()

    def test_media_gateway_access_control(self):
        from django.contrib.auth.models import AnonymousUser
        from media_library.models import CustomImage, VisibilityChoices
        from media_library.delivery import serve_media_asset
        from identity.models import User

        # 1. 公开图片：匿名可读 200 OK
        public_img = CustomImage.objects.create(
            title="公共壁纸",
            public_id=uuid4(),
            visibility=VisibilityChoices.PUBLIC,
            file=create_dummy_png("pub.png"),
        )
        req_anon = self.factory.get(f"/media-gateway/image/{public_img.public_id}/")
        req_anon.user = AnonymousUser()
        res_pub = serve_media_asset(req_anon, "image", public_img.public_id)
        self.assertEqual(res_pub.status_code, 200)

        # 2. 受限图片：匿名读取 403 Forbidden
        restricted_img = CustomImage.objects.create(
            title="内部绝密图",
            public_id=uuid4(),
            visibility=VisibilityChoices.RESTRICTED,
            file=create_dummy_png("sec.png"),
        )
        res_forbidden = serve_media_asset(req_anon, "image", restricted_img.public_id)
        self.assertEqual(res_forbidden.status_code, 403)

        # 3. 受限图片：管理员登录后读取 200 OK
        admin_user = User(username=f"admin_{uuid4().hex[:6]}", is_staff=True, is_superuser=True)
        req_admin = self.factory.get(f"/media-gateway/image/{restricted_img.public_id}/")
        req_admin.user = admin_user
        res_admin = serve_media_asset(req_admin, "image", restricted_img.public_id)
        self.assertEqual(res_admin.status_code, 200)

        # 清理
        public_img.delete()
        restricted_img.delete()

    def test_media_gateway_range_206_playback(self):
        """测试 HTTP Range 206 续传（音视频分段播放）"""
        from media_library.models import Audio, VisibilityChoices
        from media_library.delivery import serve_media_asset
        from django.contrib.auth.models import AnonymousUser

        # 创建一段 100 字节的模拟音频
        raw_audio_data = b"0123456789" * 10  # 100 bytes
        audio = Audio.objects.create(
            title="测试音频流",
            public_id=uuid4(),
            visibility=VisibilityChoices.PUBLIC,
            file=ContentFile(raw_audio_data, name="test.mp3"),
        )

        # 模拟前端播放器请求 Range: bytes=10-29 (20 bytes)
        req_range = self.factory.get(
            f"/media-gateway/audio/{audio.public_id}/",
            HTTP_RANGE="bytes=10-29",
        )
        req_range.user = AnonymousUser()
        res = serve_media_asset(req_range, "audio", audio.public_id)

        # 验证返回 HTTP 206 分段内容
        self.assertEqual(res.status_code, 206)
        self.assertEqual(res["Content-Range"], "bytes 10-29/100")
        self.assertEqual(res["Content-Length"], "20")
        self.assertEqual(res["Accept-Ranges"], "bytes")
        self.assertEqual(res.content, raw_audio_data[10:30])

        audio.delete()

    def test_x_accel_redirect_in_production_mode(self):
        from django.test import override_settings
        from django.contrib.auth.models import AnonymousUser
        from media_library.models import CustomImage, VisibilityChoices
        from media_library.delivery import serve_media_asset

        img = CustomImage.objects.create(
            title="生产环境代理测试图",
            public_id=uuid4(),
            visibility=VisibilityChoices.PUBLIC,
            file=create_dummy_png("nginx_accel.png"),
        )

        req = self.factory.get(f"/media-gateway/image/{img.public_id}/")
        req.user = AnonymousUser()

        # 开启 USE_X_ACCEL_REDIRECT 模式
        with override_settings(USE_X_ACCEL_REDIRECT=True):
            res = serve_media_asset(req, "image", img.public_id)
            self.assertEqual(res.status_code, 200)
            self.assertIn("X-Accel-Redirect", res)
            self.assertIn(img.file.name, res["X-Accel-Redirect"])

        img.delete()


if __name__ == "__main__":
    unittest.main()
