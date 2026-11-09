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


def _handle_range_request(file_obj, file_size: int, range_header: str, content_type: str) -> HttpResponse:
    """解析 Range 头并返回 HTTP 206 分段响应"""
    match = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not match:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        return response

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else file_size - 1

    if start >= file_size or end >= file_size or start > end:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        return response

    length = end - start + 1
    file_obj.seek(start)
    data = file_obj.read(length)

    response = HttpResponse(data, status=206, content_type=content_type)
    response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response["Content-Length"] = str(length)
    response["Accept-Ranges"] = "bytes"
    return response


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

    # 生产环境：X-Accel-Redirect 内部流式代理 (MEDIA-GATEWAY-02)
    if getattr(settings, "USE_X_ACCEL_REDIRECT", False):
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = f"/internal-rustfs/{file_name}"
        response["Accept-Ranges"] = "bytes"
        return response

    # 检查 HTTP Range 请求头（音视频进度拖动）
    range_header = request.META.get("HTTP_RANGE")
    file_size = getattr(asset.file, "size", 0)

    if range_header and file_size > 0:
        try:
            file_obj = asset.file.open("rb")
            return _handle_range_request(file_obj, file_size, range_header, content_type)
        except Exception:
            pass

    # 普通文件交付响应
    try:
        file_obj = asset.file.open("rb")
        response = FileResponse(file_obj, content_type=content_type)
        response["Accept-Ranges"] = "bytes"
        return response
    except Exception:
        return HttpResponseNotFound("404 Not Found: Unable to read file from storage")


def serve_rendition_asset(request, rendition_id: int):
    """派生图片（缩略图）交付入口：由原图权限控制"""
    try:
        rendition = CustomRendition.objects.get(id=rendition_id)
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

    if getattr(settings, "USE_X_ACCEL_REDIRECT", False):
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = f"/internal-rustfs/{file_name}"
        return response

    try:
        file_obj = rendition.file.open("rb")
        return FileResponse(file_obj, content_type=content_type)
    except Exception:
        return HttpResponseNotFound("404 Not Found: Unable to read rendition file")
