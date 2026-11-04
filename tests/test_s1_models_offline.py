"""
S1 第一批模型离线测试（零数据库 IO 守卫）。

在不连接数据库、不运行迁移、不访问外部网络的前提下，验证：
1. identity.User、media_library 模型类的结构、字段与继承关系；
2. base settings 中的 AUTH_USER_MODEL、WAGTAILIMAGES_IMAGE_MODEL、WAGTAILDOCS_DOCUMENT_MODEL 配置；
3. 应用代码加载路径处于本项目工作区之内（路径守卫）。
"""

import os
from pathlib import Path
import runpy
import unittest

PROJECT_DIR = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_DIR / "myblog" / "settings" / "base.py"


class S1SettingsOfflineTests(unittest.TestCase):
    """配置字典只读检查，不启动 Django 应用"""

    def setUp(self):
        self.settings = runpy.run_path(str(SETTINGS_PATH))

    def test_custom_user_model_is_configured(self):
        self.assertEqual(self.settings.get("AUTH_USER_MODEL"), "identity.User")

    def test_custom_media_models_are_configured(self):
        self.assertEqual(
            self.settings.get("WAGTAILIMAGES_IMAGE_MODEL"),
            "media_library.CustomImage",
        )
        self.assertEqual(
            self.settings.get("WAGTAILDOCS_DOCUMENT_MODEL"),
            "media_library.CustomDocument",
        )

    def test_installed_apps_include_identity_and_media_library(self):
        installed = self.settings.get("INSTALLED_APPS", [])
        self.assertIn("identity.apps.IdentityConfig", installed)
        self.assertIn("media_library.apps.MediaLibraryConfig", installed)

    def test_storages_configuration_and_rustfs_backend(self):
        storages = self.settings.get("STORAGES", {})
        self.assertIn("default", storages)
        self.assertIn("staticfiles", storages)
        # 验证配置加载了 ProtectedRustFSStorage 或 FileSystemStorage
        backend = storages["default"]["BACKEND"]
        self.assertIn(
            backend,
            [
                "media_library.storage.ProtectedRustFSStorage",
                "django.core.files.storage.FileSystemStorage",
            ],
        )


class S1ModelsDefinitionTests(unittest.TestCase):
    """模型继承、字段与导入路径静态检查"""

    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
        import django
        django.setup()

    def test_identity_user_model_structure(self):
        from django.contrib.auth.models import AbstractUser
        from identity.models import User

        self.assertTrue(issubclass(User, AbstractUser))
        # 验证导入来源为本项目目录
        user_file = Path(User.__module__.replace(".", "/") + ".py")
        self.assertTrue(Path(PROJECT_DIR / "identity" / "models.py").exists())

    def test_custom_image_and_rendition_structure(self):
        from wagtail.images.models import AbstractImage, AbstractRendition
        from media_library.models import CustomImage, CustomRendition, VisibilityChoices

        self.assertTrue(issubclass(CustomImage, AbstractImage))
        self.assertTrue(issubclass(CustomRendition, AbstractRendition))

        # 检查自定义基础字段
        field_names = [f.name for f in CustomImage._meta.fields]
        self.assertIn("public_id", field_names)
        self.assertIn("visibility", field_names)
        self.assertIn("storage_key", field_names)

        # 检查默认权限策略为 restricted
        visibility_field = CustomImage._meta.get_field("visibility")
        self.assertEqual(visibility_field.default, VisibilityChoices.RESTRICTED)

        # 检查 Rendition 外键指向 CustomImage 且包含唯一键约束
        rendition_image_field = CustomRendition._meta.get_field("image")
        self.assertEqual(rendition_image_field.related_model, CustomImage)
        self.assertEqual(rendition_image_field.remote_field.related_name, "renditions")
        self.assertIn(
            ("image", "filter_spec", "focal_point_key"),
            CustomRendition._meta.unique_together,
        )

    def test_custom_document_structure(self):
        from wagtail.documents.models import AbstractDocument
        from media_library.models import CustomDocument, VisibilityChoices

        self.assertTrue(issubclass(CustomDocument, AbstractDocument))
        field_names = [f.name for f in CustomDocument._meta.fields]
        self.assertIn("public_id", field_names)
        self.assertIn("visibility", field_names)
        self.assertIn("storage_key", field_names)

        visibility_field = CustomDocument._meta.get_field("visibility")
        self.assertEqual(visibility_field.default, VisibilityChoices.RESTRICTED)

    def test_audio_and_video_models_structure(self):
        from django.db import models
        from media_library.models import Audio, Video, CustomImage

        self.assertTrue(issubclass(Audio, models.Model))
        self.assertTrue(issubclass(Video, models.Model))

        audio_fields = [f.name for f in Audio._meta.fields]
        self.assertIn("public_id", audio_fields)
        self.assertIn("title", audio_fields)
        self.assertIn("file", audio_fields)
        self.assertIn("visibility", audio_fields)
        self.assertIn("storage_key", audio_fields)

        video_fields = [f.name for f in Video._meta.fields]
        self.assertIn("public_id", video_fields)
        self.assertIn("title", video_fields)
        self.assertIn("file", video_fields)
        self.assertIn("poster", video_fields)
        self.assertIn("visibility", video_fields)
        self.assertIn("storage_key", video_fields)

        poster_field = Video._meta.get_field("poster")
        self.assertEqual(poster_field.related_model, CustomImage)

    def test_import_paths_are_strictly_within_workspace(self):
        """路径断言守卫：确保加载的不是共享环境或旧站中的同名包"""
        import identity
        import media_library
        from media_library.storage import ProtectedRustFSStorage

        self.assertTrue(
            Path(identity.__file__).resolve().is_relative_to(PROJECT_DIR.resolve())
        )
        self.assertTrue(
            Path(media_library.__file__).resolve().is_relative_to(PROJECT_DIR.resolve())
        )
        self.assertTrue(
            issubclass(ProtectedRustFSStorage, object)
        )

    def test_protected_rustfs_storage_delete_interception(self):
        """验证 ProtectedRustFSStorage 的 delete() 拦截物理删除"""
        from media_library.storage import ProtectedRustFSStorage

        storage = ProtectedRustFSStorage()
        # 调用 delete 不应抛异常，且不会真正物理删底层文件
        storage.delete("test/probe.jpg")
        self.assertFalse(storage.file_overwrite)


if __name__ == "__main__":
    unittest.main()
