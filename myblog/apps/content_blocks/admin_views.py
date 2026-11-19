"""
Wagtail 后台编辑器专用接口：
1. vditor_image_upload: Vditor 粘贴/拖拽截图直传 CustomImage（按作者权限隔离，默认 RESTRICTED，返回 asset://image/<uuid>）；
2. table_live_preview: 复杂表格后台左右分栏实时预览（仅校验与渲染，绝不写库或生成 Revision）。
"""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST
from wagtail.admin.auth import require_admin_access
from wagtail.images import get_image_model
from wagtail.images.forms import get_image_form
from wagtail.models import Collection
from wagtail.permissions import policy_registry

from media_library.models import VisibilityChoices


def _error_json(code: str, message: str, status: int, details=None) -> JsonResponse:
    payload = {"ok": False, "error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    return JsonResponse(payload, status=status)


def _resolve_upload_collection(user):
    ImageModel = get_image_model()
    policy = policy_registry.get_by_type(ImageModel)
    if not policy.user_has_permission(user, "add"):
        return None, _error_json("permission_denied", "当前账号没有上传图片的权限", 403)

    collections = list(policy.collections_user_has_permission_for(user, "add").order_by("id"))
    if not collections:
        return None, _error_json("no_writable_collection", "当前账号没有可写入的图片素材集合", 403)

    if user.is_superuser:
        root_col = Collection.get_first_root_node()
        if root_col and any(c.pk == root_col.pk for c in collections):
            return root_col, None
        return collections[0], None

    # 受限作者优先放入非 Root 的作者个人工作区集合
    non_root = [c for c in collections if c.depth > 1]
    return (non_root[0] if non_root else collections[0]), None


@require_admin_access
@require_POST
def vditor_image_upload(request):
    """接收 Vditor 编辑器粘贴/拖拽上传的图片并落库到受控 CustomImage"""
    collection, err_resp = _resolve_upload_collection(request.user)
    if err_resp is not None:
        return err_resp

    uploaded = request.FILES.get("file[]") or request.FILES.get("file") or request.FILES.get("image")
    if not uploaded:
        return _error_json("missing_file", "未检测到上传图片文件", 400)

    max_size = int(getattr(settings, "WAGTAILIMAGES_MAX_UPLOAD_SIZE", 10 * 1024 * 1024))
    if uploaded.size > max_size:
        return _error_json("file_too_large", f"图片大小超过限制（最大 {max_size // (1024 * 1024)} MB）", 400)

    raw_name = Path(getattr(uploaded, "name", "") or "pasted-image.png").stem.strip()
    title = (raw_name[:120] or "粘贴截图").replace("\r", " ").replace("\n", " ")

    ImageModel = get_image_model()
    ImageForm = get_image_form(ImageModel)
    form = ImageForm(
        data={"title": title, "collection": str(collection.pk)},
        files={"file": uploaded},
        user=request.user,
    )
    if not form.is_valid():
        return _error_json("invalid_image", "图片格式或内容校验失败", 400, details=form.errors)

    with transaction.atomic():
        image = form.save(commit=False)
        image.uploaded_by_user = request.user
        image.collection = collection
        image.visibility = VisibilityChoices.RESTRICTED
        image.save()

    preview_url = reverse(
        "media_gateway_asset",
        kwargs={"asset_type": "image", "asset_id": image.public_id},
    )
    safe_alt = title.replace("[", "(").replace("]", ")")
    return JsonResponse(
        {
            "ok": True,
            "asset_id": str(image.public_id),
            "title": image.title,
            "markdown": f"![{safe_alt}](asset://image/{image.public_id})",
            "preview_url": preview_url,
        },
        status=201,
    )


@require_admin_access
@require_POST
def table_live_preview(request):
    """后台复杂表格左右分栏实时预览（只读渲染，不写库）"""
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "errors": ["请求体不是合法 JSON"]}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "errors": ["请求参数必须是 JSON 对象"]}, status=400)

    from content_blocks.blocks.table import CustomTableBlock

    block = CustomTableBlock()
    block_value = {
        "caption": str(payload.get("caption") or ""),
        "has_header_row": bool(payload.get("has_header_row", True)),
        "has_header_col": bool(payload.get("has_header_col", False)),
        "alignment": str(payload.get("alignment") or "center"),
        "table_data": payload.get("table_data", ""),
    }

    val_res = block.validate_value(block_value)
    if not val_res.is_valid:
        return JsonResponse({"ok": False, "errors": val_res.errors}, status=400)

    rendered = block.render_clean(block_value)
    # 只提取出 .responsive-table-wrapper 部分用于后台右侧实时预览
    html_str = rendered.html
    marker = '<div class="responsive-table-wrapper">'
    idx = html_str.find(marker)
    if idx >= 0:
        end_marker = '</div>\n        </div>\n    </div>\n</div>'
        end_idx = html_str.rfind("</table></div>")
        if end_idx >= 0:
            html_str = html_str[idx : end_idx + len("</table></div>")]

    return JsonResponse({"ok": True, "html": html_str}, status=200)
