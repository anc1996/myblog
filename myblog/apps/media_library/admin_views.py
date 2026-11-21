"""
Wagtail 管理后台专用媒体预览元数据只读接口。
"""

from __future__ import annotations

from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET
from wagtail.admin.auth import require_admin_access
from wagtail.permissions import policy_registry

from media_library.access import check_media_access
from media_library.models import Audio, Video


def _no_store_json(payload: dict, status: int = 200) -> JsonResponse:
    resp = JsonResponse(payload, status=status)
    resp["Cache-Control"] = "no-store, private"
    return resp


@require_admin_access
@require_GET
def media_preview_metadata(request):
    """
    为 Wagtail 后台左右分栏编辑器返回受控预览元数据。
    仅接受本地 Audio / Video 主键，复用 Wagtail 选用权限与 check_media_access。
    """
    kind = (request.GET.get("kind") or "").strip().lower()
    raw_id = (request.GET.get("id") or "").strip()

    if kind not in ("audio", "video"):
        return _no_store_json({"ok": False, "error": "不支持的媒体类型"}, status=400)
    if not raw_id.isdigit() or int(raw_id) <= 0:
        return _no_store_json({"ok": False, "error": "媒体主键格式非法"}, status=400)

    pk_val = int(raw_id)
    model_cls = Audio if kind == "audio" else Video
    qs = model_cls.objects.select_related("poster") if kind == "video" else model_cls.objects
    try:
        asset = qs.get(pk=pk_val)
    except model_cls.DoesNotExist:
        return _no_store_json({"ok": False, "error": "媒体记录不存在"}, status=404)

    policy = policy_registry.get_by_type(model_cls)
    can_choose = policy.user_has_permission_for_instance(request.user, "choose", asset)
    if not (can_choose and check_media_access(asset, request)):
        return _no_store_json({"ok": False, "error": "无权预览该受限媒体"}, status=403)

    src = reverse("media_gateway_asset", kwargs={"asset_type": kind, "asset_id": asset.public_id})
    poster_url = None
    if kind == "video" and getattr(asset, "poster", None) is not None:
        if check_media_access(asset.poster, request):
            poster_url = reverse(
                "media_gateway_asset",
                kwargs={"asset_type": "image", "asset_id": asset.poster.public_id},
            )

    return _no_store_json(
        {
            "ok": True,
            "kind": kind,
            "asset_id": str(asset.public_id),
            "title": asset.title,
            "src": src,
            "poster": poster_url,
        },
        status=200,
    )
