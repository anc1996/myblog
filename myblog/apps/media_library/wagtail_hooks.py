"""
media_library.wagtail_hooks
1. 注册受控 Audio / Video SnippetViewSet（支持页面内直接上传与作者权限隔离）；
2. 拦截 Wagtail 原生文档下载路由 (/documents/<id>/<filename>)，统一转交受控媒体网关实时鉴权交付；
3. 注册后台音视频分栏预览元数据只读接口。
"""

from django.urls import path
from wagtail import hooks
from wagtail.snippets.models import register_snippet

from media_library.admin_views import media_preview_metadata
from media_library.delivery import serve_media_asset
from media_library.snippets import AudioSnippetViewSet, VideoSnippetViewSet

register_snippet(AudioSnippetViewSet)
register_snippet(VideoSnippetViewSet)


@hooks.register("register_admin_urls")
def register_media_library_admin_urls():
    return [
        path(
            "media-library/preview-metadata/",
            media_preview_metadata,
            name="media_library_preview_metadata",
        ),
    ]


@hooks.register("before_serve_document")
def enforce_controlled_document_gateway(document, request):
    """禁止直接重定向到内网 RustFS 或绕过 CustomDocument.visibility 策略。"""
    return serve_media_asset(request, "document", document.public_id)
