"""
私有媒体统一受控交付网关 (Media Gateway)。

路由入口：
- /media-gateway/<str:asset_type>/<uuid:asset_id>/
- /media-gateway/rendition/<int:rendition_id>/

落实 MEDIA-GATEWAY-02 契约：
1. 每次请求逐次执行权限鉴权，无权访问直接返回 403 Forbidden；
2. 支持 HTTP HEAD 与 Range 206 续传（音视频拖拽播放）；
3. 生产环境支持 X-Accel-Redirect 内部重定向给 Web 服务器向内网 RustFS 拉流，不占用 Python 同步进程；
4. 禁用公网长效直链。
"""

import mimetypes
import os
import re
from urllib.parse import quote
from uuid import UUID
from django.conf import settings
from django.http import (
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseNotFound,
    FileResponse,
)
from media_library.access import get_media_asset_by_id, check_media_access
from media_library.models import CustomRendition

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _maybe_record_document_download_conversion(request, asset_type: str, asset_id: UUID, response: HttpResponse) -> None:
    """
    仅当文档通过 /media-gateway/document/<uuid>/ 权限核验并实际返回 200/206（非 HEAD / 403 / 404 / 416）时，
    结合同页短时下载归因令牌与 MediaReferenceRecord 引用关系执行服务端去重记账（自动合并 Range 分段下载与重试）。
    """
    if asset_type != "document" or getattr(request, "method", "GET") == "HEAD":
        return
    if getattr(response, "status_code", 0) not in (200, 206):
        return
    dl_vt = (request.COOKIES.get("myblog_ana_dl_vt") or "").strip()
    if not dl_vt:
        return
    try:
        from analytics.collector import record_server_conversion
        from analytics.tokens import verify_visit_token
        from media_library.models import MediaReferenceRecord

        visit_claims = verify_visit_token(dl_vt)
        if not visit_claims or visit_claims.get("kind") != "detail":
            return
        page_id = visit_claims.get("page_id")
        rev_id = int(visit_claims.get("live_revision_id") or 0)
        if page_id is None or rev_id <= 0:
            return
        if not MediaReferenceRecord.objects.filter(
            asset_id=asset_id,
            asset_type="document",
            page_id=int(page_id),
            revision_id=rev_id,
        ).exists():
            return
        record_server_conversion(
            visit_claims,
            "doc_download",
            target_id=f"doc:{asset_id}",
        )
    except Exception:
        pass


def _sanitize_download_filename(raw_name: str, fallback: str = "document") -> str:
    """清洗下载文件名：仅保留 basename，剥除控制字符、回车换行与引号。"""
    candidate = str(raw_name or "").replace("\\", "/").split("/")[-1]
    candidate = _CONTROL_CHARS_RE.sub("", candidate).replace('"', "").strip()
    return candidate or fallback


def _build_document_content_disposition(asset, file_name: str) -> str:
    """
    构造符合 RFC 5987 的附件强制下载响应头：
    - 同时提供 ASCII 回退 filename 与 UTF-8 百分号编码 filename*；
    - 防止 .md / .txt 等文本在同源标签页直接内联渲染或保存为随机 UUID。
    """
    raw_name = getattr(asset, "filename", "") or file_name or getattr(asset, "title", "") or "document"
    clean_name = _sanitize_download_filename(raw_name, fallback="document")
    stem, ext = os.path.splitext(clean_name)
    ascii_stem = "".join(ch if 32 <= ord(ch) < 127 and ch not in '\\/:*?"<>|;' else "_" for ch in stem).strip("_") or "document"
    ascii_ext = "".join(ch if 32 <= ord(ch) < 127 and ch not in '\\/:*?"<>|;' else "" for ch in ext)
    ascii_name = f"{ascii_stem}{ascii_ext}"
    utf8_name = quote(clean_name, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'


def _apply_document_security_headers(response: HttpResponse, asset_type: str, asset, file_name: str) -> None:
    """统一补充防嗅探头与文档强制附件下载头。"""
    response["X-Content-Type-Options"] = "nosniff"
    if asset_type == "document":
        response["Content-Security-Policy"] = "default-src 'none'"
        response["Content-Disposition"] = _build_document_content_disposition(asset, file_name)
        response["Cache-Control"] = "private, no-store"


def _handle_range_request(
    file_obj,
    file_size: int,
    range_header: str,
    content_type: str,
    *,
    asset_type: str = "",
    asset=None,
    file_name: str = "",
    is_head: bool = False,
) -> HttpResponse:
    """解析 Range 头并返回 HTTP 206 分段响应（保证退出前关闭文件流）"""
    try:
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if not match:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{file_size}"
            _apply_document_security_headers(response, asset_type, asset, file_name)
            return response

        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else file_size - 1

        if start >= file_size or end >= file_size or start > end:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{file_size}"
            _apply_document_security_headers(response, asset_type, asset, file_name)
            return response

        length = end - start + 1
        if is_head:
            response = HttpResponse(status=206, content_type=content_type)
        else:
            file_obj.seek(start)
            data = file_obj.read(length)
            response = HttpResponse(data, status=206, content_type=content_type)

        response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        response["Content-Length"] = str(length)
        response["Accept-Ranges"] = "bytes"
        _apply_document_security_headers(response, asset_type, asset, file_name)
        return response
    finally:
        try:
            file_obj.close()
        except Exception:
            pass


def serve_media_asset(request, asset_type: str, asset_id: UUID):
    """统一媒体资产交付入口"""
    try:
        asset = get_media_asset_by_id(asset_type, asset_id)
    except Exception:
        return HttpResponseNotFound("404 Not Found: Media asset does not exist")

    # 权限核验
    if not check_media_access(asset, request):
        return HttpResponseForbidden("403 Forbidden: Restricted media asset access denied")

    if not asset.file:
        return HttpResponseNotFound("404 Not Found: File attachment missing")

    file_name = getattr(asset.file, "name", "")
    content_type, _ = mimetypes.guess_type(file_name)
    content_type = content_type or "application/octet-stream"
    is_head = getattr(request, "method", "GET") == "HEAD"

    # 生产环境：X-Accel-Redirect 内部流式代理 (MEDIA-GATEWAY-02)
    if getattr(settings, "USE_X_ACCEL_REDIRECT", False):
        response = HttpResponse(content_type=content_type)
        encoded_key = quote(file_name.lstrip("/"), safe="/")
        response["X-Accel-Redirect"] = f"/internal-rustfs/{encoded_key}"
        response["Accept-Ranges"] = "bytes"
        _apply_document_security_headers(response, asset_type, asset, file_name)
        _maybe_record_document_download_conversion(request, asset_type, asset_id, response)
        return response

    # 检查 HTTP Range 请求头（音视频进度拖动）
    range_header = request.META.get("HTTP_RANGE")
    file_size = getattr(asset.file, "size", 0)

    if range_header and file_size > 0:
        try:
            file_obj = asset.file.open("rb")
            response = _handle_range_request(
                file_obj,
                file_size,
                range_header,
                content_type,
                asset_type=asset_type,
                asset=asset,
                file_name=file_name,
                is_head=is_head,
            )
            _maybe_record_document_download_conversion(request, asset_type, asset_id, response)
            return response
        except Exception:
            pass

    # 普通文件交付响应
    try:
        if is_head:
            response = HttpResponse(status=200, content_type=content_type)
            if file_size > 0:
                response["Content-Length"] = str(file_size)
            response["Accept-Ranges"] = "bytes"
            _apply_document_security_headers(response, asset_type, asset, file_name)
            return response
        file_obj = asset.file.open("rb")
        response = FileResponse(file_obj, content_type=content_type)
        response["Accept-Ranges"] = "bytes"
        _apply_document_security_headers(response, asset_type, asset, file_name)
        _maybe_record_document_download_conversion(request, asset_type, asset_id, response)
        return response
    except Exception:
        return HttpResponseNotFound("404 Not Found: Unable to read file from storage")


def serve_rendition_asset(request, rendition_id: int):
    """派生图片（缩略图）交付入口：由原图权限控制"""
    from media_library.models import VisibilityChoices
    try:
        rendition = CustomRendition.objects.select_related("image", "image__collection").get(id=rendition_id)
    except CustomRendition.DoesNotExist:
        return HttpResponseNotFound("404 Not Found: Rendition does not exist")

    # 校验原图访问权限
    if not check_media_access(rendition.image, request):
        return HttpResponseForbidden("403 Forbidden: Restricted rendition access denied")

    if not rendition.file:
        return HttpResponseNotFound("404 Not Found: Rendition file missing")

    file_name = getattr(rendition.file, "name", "")
    content_type, _ = mimetypes.guess_type(file_name)
    content_type = content_type or "image/jpeg"
    cache_control = (
        "public, max-age=300, must-revalidate"
        if getattr(rendition.image, "visibility", None) == VisibilityChoices.PUBLIC
        else "private, max-age=300"
    )

    if getattr(settings, "USE_X_ACCEL_REDIRECT", False):
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = f"/internal-rustfs/{file_name}"
        response["Cache-Control"] = cache_control
        return response

    try:
        file_obj = rendition.file.open("rb")
        response = FileResponse(file_obj, content_type=content_type)
        response["Cache-Control"] = cache_control
        return response
    except Exception:
        return HttpResponseNotFound("404 Not Found: Unable to read rendition file")
