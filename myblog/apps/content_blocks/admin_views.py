"""
Wagtail 后台编辑器专用接口：
1. vditor_image_upload: Vditor 粘贴/拖拽截图直传 CustomImage（按作者权限隔离，默认 RESTRICTED，返回 asset://image/<uuid>）；
2. richtext_image_upload: Draftail 富文本粘贴/拖拽截图直传 CustomImage（默认 RESTRICTED，返回原生 IMAGE 实体所需 id 与预览信息）；
3. resolve_chosen_media: 校验并解析 Chooser 选中的 CustomImage / CustomDocument 为稳定的 asset:// 引用；
4. table_live_preview: 复杂表格后台左右分栏实时预览（仅校验与渲染，绝不写库或生成 Revision）。
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from wagtail.admin.auth import require_admin_access
from wagtail.documents import get_document_model
from wagtail.images import get_image_model
from wagtail.images.formats import get_image_format
from wagtail.images.forms import get_image_form
from wagtail.models import Collection, GroupCollectionPermission
from wagtail.permissions import policy_registry

from media_library.access import check_media_access
from media_library.models import VisibilityChoices

logger = logging.getLogger(__name__)


def _error_json(code: str, message: str, status: int, details=None) -> JsonResponse:
    payload = {"ok": False, "error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    resp = JsonResponse(payload, status=status)
    resp["Cache-Control"] = "no-store, private"
    return resp


def _no_store_json(payload: dict, status: int = 200) -> JsonResponse:
    resp = JsonResponse(payload, status=status)
    resp["Cache-Control"] = "no-store, private"
    return resp


def _resolve_upload_collection(user, model_cls=None):
    """
    解析当前用户允许写入的素材集合：
    - 超级管理员优先写入 Root 集合；
    - 非超级管理员优先写入非 Root 的个人/工作区集合（depth > 1）；
    - 若无非 Root 集合，仅当用户对唯一可用 Root 集合同时具备 change 权限（主编角色）时才放行，
      绝不把缺少工作区集合的受限作者静默降级到共享根集合。
    """
    target_model = model_cls or get_image_model()
    policy = policy_registry.get_by_type(target_model)
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
    auth_meta = policy.auth_model._meta
    change_codename = f"change_{auth_meta.model_name}"
    explicit_change_perms = GroupCollectionPermission.objects.filter(
        group__user=user,
        permission__content_type__app_label=auth_meta.app_label,
        permission__codename=change_codename,
    ).select_related("collection")
    explicit_change_col_ids: set[int] = set()
    for gcp in explicit_change_perms:
        explicit_change_col_ids.update(
            Collection.objects.descendant_of(gcp.collection, inclusive=True).values_list("id", flat=True)
        )

    non_root = [c for c in collections if c.depth > 1]
    if non_root:
        # 仅当用户对非 Root 子集合拥有显式授权（或其祖先具备 change 主编权）时才允许落库，
        # 防止仅有 Root add 却无 change 权的受限账号通过集合树继承落入共享目录。
        add_codename = f"add_{auth_meta.model_name}"
        explicit_non_root_add_ids = set(
            GroupCollectionPermission.objects.filter(
                group__user=user,
                permission__content_type__app_label=auth_meta.app_label,
                permission__codename=add_codename,
                collection__depth__gt=1,
            ).values_list("collection_id", flat=True)
        )
        allowed_non_root = [
            c for c in non_root
            if c.pk in explicit_change_col_ids or c.pk in explicit_non_root_add_ids
        ]
        if allowed_non_root:
            # 排除全站公共只读集合名（若恰好含权限），按 tree 节点顺序优先作者专属工作区
            non_public = [c for c in allowed_non_root if c.name != "全站公共素材库"]
            candidates = non_public or allowed_non_root
            return candidates[0], None

    if collections[0].pk in explicit_change_col_ids:
        return collections[0], None
    return None, _error_json("no_writable_collection", "当前账号缺少独立可写素材集合，拒绝落入共享根集合", 403)


def _safe_upload_title(raw_name: str, model_cls, collection, *, default_stem: str = "pasted-image") -> str:
    """为自动粘贴/拖拽上传生成不会在同集合内直接撞名的标题。"""
    stem = Path(raw_name or "").stem.strip().replace("\r", " ").replace("\n", " ")
    base = stem[:120] if stem else default_stem
    if base and not model_cls.objects.filter(collection=collection, title=base).exists():
        return base

    candidate = base or default_stem
    for _ in range(5):
        suffix = f"{secrets.randbelow(1_000_000_000):09d}"
        prefix = (base or default_stem)[:108]
        candidate = f"{prefix}-{suffix}"
        if not model_cls.objects.filter(collection=collection, title=candidate).exists():
            return candidate
    return candidate


def _save_uploaded_image_asset(request, *, default_title: str = "粘贴截图"):
    collection, err_resp = _resolve_upload_collection(request.user, get_image_model())
    if err_resp is not None:
        return None, err_resp

    uploaded = request.FILES.get("file[]") or request.FILES.get("file") or request.FILES.get("image")
    if not uploaded:
        return None, _error_json("missing_file", "未检测到上传图片文件", 400)

    max_size = int(getattr(settings, "WAGTAILIMAGES_MAX_UPLOAD_SIZE", 10 * 1024 * 1024))
    if uploaded.size > max_size:
        return None, _error_json("file_too_large", f"图片大小超过限制（最大 {max_size // (1024 * 1024)} MB）", 400)

    ImageModel = get_image_model()
    raw_name = getattr(uploaded, "name", "") or ""
    title = _safe_upload_title(raw_name, ImageModel, collection, default_stem=default_title)

    ImageForm = get_image_form(ImageModel)
    form = ImageForm(
        data={"title": title, "collection": str(collection.pk)},
        files={"file": uploaded},
        user=request.user,
    )
    if not form.is_valid():
        return None, _error_json("invalid_image", "图片格式或内容校验失败", 400, details=form.errors)

    with transaction.atomic():
        image = form.save(commit=False)
        image.uploaded_by_user = request.user
        image.collection = collection
        image.visibility = VisibilityChoices.RESTRICTED
        image.save()

    return image, None


def _sanitize_markdown_label(raw: str, fallback: str) -> str:
    cleaned = (raw or "").replace("\r", " ").replace("\n", " ").replace("[", "(").replace("]", ")").replace("|", "｜").strip()
    return cleaned or fallback


@require_admin_access
@require_POST
def vditor_image_upload(request):
    """接收 Vditor 编辑器粘贴/拖拽上传的图片并落库到受控 CustomImage"""
    image, err_resp = _save_uploaded_image_asset(request, default_title="粘贴截图")
    if err_resp is not None:
        return err_resp

    preview_url = reverse(
        "media_gateway_asset",
        kwargs={"asset_type": "image", "asset_id": image.public_id},
    )
    safe_alt = _sanitize_markdown_label(image.title, "图片")
    return _no_store_json(
        {
            "ok": True,
            "id": image.pk,
            "asset_id": str(image.public_id),
            "title": image.title,
            "markdown": f"![{safe_alt}](asset://image/{image.public_id})",
            "preview_url": preview_url,
        },
        status=201,
    )


@require_admin_access
@require_POST
def richtext_image_upload(request):
    """接收 Draftail 富文本粘贴/拖拽上传的图片并落库到受控 CustomImage"""
    image_format_name = (request.POST.get("format") or "left").strip()
    try:
        image_format = get_image_format(image_format_name)
    except KeyError:
        return _error_json("invalid_format", "未注册的图片排版格式", 400)

    alt_text = request.POST.get("alt") or ""
    if len(alt_text) > 2048:
        return _error_json("invalid_alt", "图片替代文本过长", 400)

    image, err_resp = _save_uploaded_image_asset(request, default_title="富文本截图")
    if err_resp is not None:
        return err_resp

    gateway_url = reverse(
        "media_gateway_asset",
        kwargs={"asset_type": "image", "asset_id": image.public_id},
    )
    preview = {
        "url": gateway_url,
        "width": getattr(image, "width", None),
        "height": getattr(image, "height", None),
    }
    try:
        rendition = image.get_rendition(image_format.filter_spec)
        preview = {
            "url": rendition.url,
            "width": rendition.width,
            "height": rendition.height,
        }
    except Exception:
        # 原图已落库；派生图生成异常只记录日志并降级用原图网关，避免前端重试造成重复上传
        logger.warning(
            "richtext_image_upload_rendition_fallback image_id=%s format=%s",
            image.pk,
            image_format.name,
            exc_info=True,
        )

    return _no_store_json(
        {
            "ok": True,
            "image": {
                "id": image.pk,
                "asset_id": str(image.public_id),
                "title": image.title,
                "alt": alt_text,
                "format": image_format.name,
            },
            "preview": preview,
        },
        status=201,
    )


@require_admin_access
@require_GET
def resolve_chosen_media(request):
    """
    为 Vditor 等外部控件解析 Wagtail 原生 Chooser 选中的图片或文档：
    校验 choose + check_media_access 后返回稳定 public_id 与 Markdown 插入串。
    """
    kind = (request.GET.get("kind") or "").strip().lower()
    raw_id = (request.GET.get("id") or "").strip()
    if kind not in ("image", "document"):
        return _no_store_json({"ok": False, "error": "不支持的媒体类型"}, status=400)
    if not raw_id.isdigit() or int(raw_id) <= 0:
        return _no_store_json({"ok": False, "error": "媒体主键格式非法"}, status=400)

    model_cls = get_image_model() if kind == "image" else get_document_model()
    try:
        asset = model_cls.objects.get(pk=int(raw_id))
    except model_cls.DoesNotExist:
        return _no_store_json({"ok": False, "error": "媒体记录不存在或无权访问"}, status=404)

    policy = policy_registry.get_by_type(model_cls)
    can_choose = policy.user_has_permission_for_instance(request.user, "choose", asset)
    if not (can_choose and check_media_access(asset, request)):
        # 与不存在统一返回 404，防止受限作者通过主键遍历探测站内受限资产存在性
        return _no_store_json({"ok": False, "error": "媒体记录不存在或无权访问"}, status=404)

    asset_uuid = str(asset.public_id)
    preview_url = reverse(
        "media_gateway_asset",
        kwargs={"asset_type": kind, "asset_id": asset.public_id},
    )
    safe_label = _sanitize_markdown_label(asset.title, "图片" if kind == "image" else "附件")
    if kind == "image":
        markdown_str = f"![{safe_label}](asset://image/{asset_uuid})"
    else:
        markdown_str = f"[{safe_label}](asset://document/{asset_uuid})"

    payload = {
        "ok": True,
        "kind": kind,
        "id": asset.pk,
        "asset_id": asset_uuid,
        "title": asset.title,
        "markdown": markdown_str,
        "preview_url": preview_url,
    }
    if kind == "document":
        payload["filename"] = getattr(asset, "filename", "") or ""
    return _no_store_json(payload, status=200)


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
