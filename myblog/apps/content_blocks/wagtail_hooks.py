"""
content_blocks Wagtail 8.1 管理后台钩子：
1. 注册 Vditor 粘贴传图与复杂表格实时预览管理端路由；
2. 显式加载 widget_adapters 确保 Telepath 适配器已注册。
"""

from django.urls import path
from wagtail import hooks

from .admin_views import vditor_image_upload, table_live_preview
from . import widget_adapters  # noqa: F401


@hooks.register("register_admin_urls")
def register_content_blocks_admin_urls():
    return [
        path(
            "content-blocks/vditor-image-upload/",
            vditor_image_upload,
            name="content_blocks_vditor_image_upload",
        ),
        path(
            "content-blocks/table-preview/",
            table_live_preview,
            name="content_blocks_table_preview",
        ),
    ]
