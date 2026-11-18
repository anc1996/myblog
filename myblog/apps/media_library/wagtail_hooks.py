"""
media_library.wagtail_hooks
拦截 Wagtail 原生文档下载路由 (/documents/<id>/<filename>)，统一转交受控媒体网关实时鉴权交付。
"""

from wagtail import hooks

from media_library.delivery import serve_media_asset


@hooks.register("before_serve_document")
def enforce_controlled_document_gateway(document, request):
    """禁止直接重定向到内网 RustFS 或绕过 CustomDocument.visibility 策略。"""
    return serve_media_asset(request, "document", document.public_id)
