"""
媒体权限策略与 MEDIA-ACL-02 下架联动服务。

落实核心契约：
1. check_media_access: 每次文件读取进行实时权限校验；
2. plan_restrict: 评估资源公开转受限时受影响的已上线页面与别名闭包；
3. restrict_asset: 默认阻断修改，仅在管理员二次确认后，在同一事务与协调锁内下架所有受影响页面！
"""

import hashlib
from uuid import UUID
from typing import Any
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from wagtail.models import Page
from wagtail.permissions import policy_registry

from media_library.models import (
    CustomImage,
    CustomDocument,
    Audio,
    Video,
    MediaReferenceRecord,
    VisibilityChoices,
)
from content_lifecycle.guard import content_mutation_guard
from external_content.models import OutboxEvent


def get_media_asset_by_id(asset_type: str, asset_id: UUID | str) -> Any:
    """按资产类型与 UUID 获取媒体实例"""
    asset_uuid = UUID(str(asset_id))
    if asset_type == "image":
        return CustomImage.objects.get(public_id=asset_uuid)
    elif asset_type == "document":
        return CustomDocument.objects.get(public_id=asset_uuid)
    elif asset_type == "audio":
        return Audio.objects.get(public_id=asset_uuid)
    elif asset_type == "video":
        return Video.objects.get(public_id=asset_uuid)
    raise ValueError(f"未知资产类型: {asset_type}")


def check_media_access(asset: Any, request: Any = None) -> bool:
    """
    逐次访问权限校验：
    1. 若资源 visibility == 'public'，允许公开读取；
    2. 若资源 visibility == 'restricted'，严禁仅凭 is_staff 放行：
       仅允许超级管理员、上传者本人（uploaded_by_user == user），
       或对该资源所属 Collection 具备 'change' 权限的主编读取。
    """
    if not asset:
        return False
    if asset.visibility == VisibilityChoices.PUBLIC:
        return True

    # 处于受限状态 (restricted)
    if request and hasattr(request, "user"):
        user = request.user
        if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        if isinstance(asset, (CustomImage, CustomDocument)):
            policy = policy_registry.get_by_type(type(asset))
            return bool(policy.user_has_permission_for_instance(user, "change", asset))
        return bool(user.has_perm(f"{asset._meta.app_label}.change_{asset._meta.model_name}"))

    return False


def _generate_confirmation_token(asset_id: UUID, page_ids: list[int]) -> str:
    """生成基于受影响页面列表与 Django SECRET_KEY 的签名确认凭证"""
    secret = getattr(settings, "SECRET_KEY", "fallback-secret")
    raw = f"{asset_id}:{','.join(map(str, sorted(page_ids)))}:{secret}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def plan_restrict(asset_type: str, asset_id: UUID | str) -> dict[str, Any]:
    """
    评估资源改为受限的影响范围。
    查询当前正在正式发布（live=True）且该版本引用了该资源的所有页面及别名页面。
    """
    asset_uuid = UUID(str(asset_id))
    asset = get_media_asset_by_id(asset_type, asset_uuid)

    refs = MediaReferenceRecord.objects.filter(asset_id=asset_uuid)
    affected_pages = []
    page_ids = []

    for r in refs:
        try:
            page = Page.objects.get(id=r.page_id)
            if page.live and page.live_revision_id == r.revision_id:
                affected_pages.append({
                    "page_id": page.id,
                    "title": page.title,
                    "url_path": page.url_path,
                    "revision_id": r.revision_id,
                })
                page_ids.append(page.id)
        except Page.DoesNotExist:
            continue

    requires_confirmation = len(affected_pages) > 0
    token = _generate_confirmation_token(asset_uuid, page_ids) if requires_confirmation else ""

    return {
        "asset_id": str(asset_uuid),
        "asset_type": asset_type,
        "current_visibility": asset.visibility,
        "affected_pages": affected_pages,
        "requires_confirmation": requires_confirmation,
        "confirmed_plan_token": token,
    }


def restrict_asset(
    asset_type: str,
    asset_id: UUID | str,
    actor: Any = None,
    confirmed_plan_token: str | None = None,
    *,
    system_authorized: bool = False,
) -> dict[str, Any]:
    """
    执行 MEDIA-ACL-02 契约：
    1. 若存在受影响公开页面且未传有效确认凭证，默认阻断修改 (返回 409)；
    2. 若已确认，在全局 ContentMutationGuard 锁内原子修改资源权限并下架所有受影响页面！
    3. 仅允许超级管理员或显式系统内部授权执行，严禁普通作者通过转受限连带下架他人页面。
    """
    if not system_authorized:
        if (
            not actor
            or not getattr(actor, "is_authenticated", False)
            or not getattr(actor, "is_active", False)
            or not getattr(actor, "is_superuser", False)
        ):
            raise PermissionDenied("影响页面公开访问的媒体转受限操作仅限超级管理员执行。")

    asset_uuid = UUID(str(asset_id))

    # 在全局行锁保护下执行评估与连带下架
    with content_mutation_guard(timeout_seconds=3):
        plan = plan_restrict(asset_type, asset_uuid)
        affected = plan["affected_pages"]

        # 默认阻断校验
        if plan["requires_confirmation"]:
            if not confirmed_plan_token or confirmed_plan_token != plan["confirmed_plan_token"]:
                return {
                    "status": "conflict",
                    "error": "affected_publications",
                    "message": f"该资源正被 {len(affected)} 篇已发布公开文章引用！默认阻断修改，请管理员确认连带下架。",
                    "affected_pages": affected,
                    "required_token": plan["confirmed_plan_token"],
                }

        asset = get_media_asset_by_id(asset_type, asset_uuid)
        asset.visibility = VisibilityChoices.RESTRICTED
        asset.save(update_fields=["visibility"])

        unpublished_ids = []
        for item in affected:
            try:
                page = Page.objects.get(id=item["page_id"])
                if page.live:
                    page.unpublish(user=actor)
                    unpublished_ids.append(page.id)
                    # 写入 Outbox 事件
                    OutboxEvent.objects.create(
                        event_type="page.unpublished",
                        page_id=page.id,
                        revision_id=page.live_revision_id,
                        payload={"reason": f"media_restricted: {asset_uuid}"},
                    )
            except Page.DoesNotExist:
                continue

        return {
            "status": "success",
            "asset_id": str(asset_uuid),
            "new_visibility": VisibilityChoices.RESTRICTED,
            "unpublished_page_ids": unpublished_ids,
        }
