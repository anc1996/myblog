"""
账号生命周期治理服务 (AccountLifecycleService)。

统一收口停用、恢复、管理员重置密码、用户自助改密、会话代次失效与受控彻底删除：
1. 所有操作均在 PostgreSQL 本地事务内完成状态变更与审计记录；
2. 彻底删除遵循单向全局锁顺序：
   User (FOR UPDATE, id ASC) -> MemberProfile/UserProfile -> ContentMutationGuard
   -> Page (id ASC) -> 媒体与回收批次 (id ASC) -> Comment (id ASC)；
3. 删除前强制过户未销毁资产、重算受影响评论赞踩计数、并入队独立头像清理任务。
"""

from collections.abc import Iterable
import logging
from typing import Any
import uuid

from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone
from wagtail.models import Page

from identity.avatar_storage import get_storage_and_namespace
from identity.deletion_guard import authorize_user_hard_delete
from identity.models import AccountAuditLog, AvatarPurgeTask, MemberProfile, User
from identity.signals import suppress_user_save_epoch_bump

logger = logging.getLogger(__name__)


def _enqueue_purge_task_for_key(
    *,
    storage_alias: str,
    storage_namespace: str,
    storage_key: str,
    source: str,
    operation_id: uuid.UUID,
    target_user_id: int | None,
) -> AvatarPurgeTask | None:
    """在同一数据库事务中幂等入队头像物理清理任务。"""
    clean_key = (storage_key or "").strip()
    if not clean_key:
        return None
    active_statuses = ["pending", "processing", "blocked", "failed"]
    existing = AvatarPurgeTask.objects.filter(
        storage_namespace=storage_namespace,
        storage_key=clean_key,
        status__in=active_statuses,
    ).first()
    if existing:
        return existing
    return AvatarPurgeTask.objects.create(
        storage_alias=storage_alias or "default",
        storage_namespace=storage_namespace,
        storage_key=clean_key,
        source=source,
        operation_id=operation_id,
        target_user_id_snapshot=target_user_id,
        status="pending",
    )


class AccountLifecycleService:
    """账号全生命周期受控服务（停用、改密、撤权失效与硬删过户）。"""

    @classmethod
    def bump_session_epoch(cls, user_ids: Iterable[int]) -> int:
        """
        原子递增目标用户的 session_epoch，使所有旧会话在下次请求时立即失效。
        返回实际更新的资料条数。
        """
        unique_ids = sorted({int(uid) for uid in user_ids if uid is not None})
        if not unique_ids:
            return 0
        existing_user_ids = set(User.objects.filter(id__in=unique_ids).values_list("id", flat=True))
        if not existing_user_ids:
            return 0
        for uid in sorted(existing_user_ids):
            MemberProfile.objects.get_or_create(user_id=uid)
        now = timezone.now()
        return MemberProfile.objects.filter(user_id__in=existing_user_ids).update(
            session_epoch=F("session_epoch") + 1,
            role_updated_at=now,
            updated_at=now,
        )

    @classmethod
    def deactivate_user(
        cls,
        *,
        actor: User,
        target_user_id: int,
        reason: str = "",
        operation_id: uuid.UUID | None = None,
    ) -> User:
        """停用账号：保留头像对象、会员资料及所有历史关联，不触发头像物理清理。"""
        if not actor or not getattr(actor, "is_superuser", False):
            raise PermissionDenied("仅超级管理员可执行账号停用操作。")
        if int(actor.id) == int(target_user_id):
            raise ValidationError("禁止停用当前登录的管理员自身账号。")

        op_id = operation_id or uuid.uuid4()
        with transaction.atomic():
            target = User.objects.select_for_update().get(id=target_user_id)
            with suppress_user_save_epoch_bump():
                if target.is_active:
                    target.is_active = False
                    target.save(update_fields=["is_active"])
            cls.bump_session_epoch([target.id])
            AccountAuditLog.objects.create(
                action="deactivate",
                actor_id_snapshot=actor.id,
                actor_username_snapshot=actor.username,
                target_user_id_snapshot=target.id,
                target_username_snapshot=target.username,
                operation_id=op_id,
                details={"reason": (reason or "")[:200]},
            )
            return target

    @classmethod
    def reactivate_user(
        cls,
        *,
        actor: User,
        target_user_id: int,
        operation_id: uuid.UUID | None = None,
    ) -> User:
        """恢复已停用账号，递增代次以清理可能遗留的异常会话。"""
        if not actor or not getattr(actor, "is_superuser", False):
            raise PermissionDenied("仅超级管理员可执行账号恢复操作。")
        op_id = operation_id or uuid.uuid4()
        with transaction.atomic():
            target = User.objects.select_for_update().get(id=target_user_id)
            with suppress_user_save_epoch_bump():
                if not target.is_active:
                    target.is_active = True
                    target.save(update_fields=["is_active"])
            cls.bump_session_epoch([target.id])
            AccountAuditLog.objects.create(
                action="reactivate",
                actor_id_snapshot=actor.id,
                actor_username_snapshot=actor.username,
                target_user_id_snapshot=target.id,
                target_username_snapshot=target.username,
                operation_id=op_id,
                details={},
            )
            return target

    @classmethod
    def admin_reset_password(
        cls,
        *,
        actor: User,
        target_user_id: int,
        new_password: str,
        operation_id: uuid.UUID | None = None,
    ) -> User:
        """
        管理员人工核密后重置临时密码：
        校验密码强度并加密存储，置 must_change_password=True 并递增 session_epoch 踢下线全部旧会话。
        """
        if not actor or not getattr(actor, "is_superuser", False):
            raise PermissionDenied("仅超级管理员可执行人工重置密码。")
        op_id = operation_id or uuid.uuid4()
        with transaction.atomic():
            target = User.objects.select_for_update().get(id=target_user_id)
            validate_password(new_password, user=target)
            with suppress_user_save_epoch_bump():
                target.set_password(new_password)
                target.save(update_fields=["password"])

            profile, _ = MemberProfile.objects.select_for_update().get_or_create(user=target)
            profile.must_change_password = True
            profile.session_epoch += 1
            profile.role_updated_at = timezone.now()
            profile.save(update_fields=["must_change_password", "session_epoch", "role_updated_at", "updated_at"])

            AccountAuditLog.objects.create(
                action="admin_reset_password",
                actor_id_snapshot=actor.id,
                actor_username_snapshot=actor.username,
                target_user_id_snapshot=target.id,
                target_username_snapshot=target.username,
                operation_id=op_id,
                details={"must_change_password": True},
            )
            return target

    @classmethod
    def self_change_password(
        cls,
        *,
        user: User,
        old_password: str,
        new_password: str,
        request: Any = None,
        operation_id: uuid.UUID | None = None,
    ) -> MemberProfile:
        """
        会员自助改密（含强制改密页面解除标志）：
        验证旧密码与新密码强度，解除 must_change_password，递增 session_epoch 踢掉其他设备旧会话，
        若传入当前请求则同步更新当前会话认证哈希与最新 epoch。
        """
        if not user or not user.is_authenticated:
            raise PermissionDenied("请先登录后再修改密码。")

        op_id = operation_id or uuid.uuid4()
        with transaction.atomic():
            locked_user = User.objects.select_for_update().get(id=user.id)
            if not locked_user.is_active:
                raise PermissionDenied("账号已停用，无法修改密码。")
            profile, _ = MemberProfile.objects.select_for_update().get_or_create(user=locked_user)

            # 若传入请求会话且管理员同时重置了密码或踢出了会话，在持有行锁后复核代次与当前密码
            if request is not None and hasattr(request, "session") and request.session is not None:
                sess_epoch = request.session.get("session_epoch")
                if sess_epoch is not None and int(sess_epoch) != int(profile.session_epoch):
                    raise PermissionDenied("当前会话已失效，请使用最新密码重新登录后再试。")

            if not locked_user.check_password(old_password):
                raise ValidationError("当前原密码输入不正确。")

            validate_password(new_password, user=locked_user)
            with suppress_user_save_epoch_bump():
                locked_user.set_password(new_password)
                locked_user.save(update_fields=["password"])

            profile.must_change_password = False
            profile.session_epoch += 1
            profile.role_updated_at = timezone.now()
            profile.save(update_fields=["must_change_password", "session_epoch", "role_updated_at", "updated_at"])

            AccountAuditLog.objects.create(
                action="self_change_password",
                actor_id_snapshot=locked_user.id,
                actor_username_snapshot=locked_user.username,
                target_user_id_snapshot=locked_user.id,
                target_username_snapshot=locked_user.username,
                operation_id=op_id,
                details={"cleared_forced_flag": True},
            )

        # 更新内存中请求用户的密码哈希与当前会话代次，使当前浏览器免重新登录
        if request is not None:
            locked_user.backend = getattr(user, "backend", None) or request.session.get("_auth_user_backend")
            update_session_auth_hash(request, locked_user)
            request.session["session_epoch"] = profile.session_epoch
            request.user = locked_user

        return profile

    @classmethod
    def hard_delete(
        cls,
        *,
        actor: User,
        target_user_id: int,
        transfer_to_user_id: int | None = None,
        operation_id: uuid.UUID | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """
        受控账号彻底删除：
        严格按单向锁顺序完成身份冻结、页面/媒体/回收批次过户、赞踩精确重算、头像清理入队与物理删除。
        """
        if not confirmed:
            raise ValidationError("彻底删除账号必须传入 confirmed=True 明确二次确认。")
        if not actor or not getattr(actor, "is_superuser", False):
            raise PermissionDenied("仅超级管理员可执行彻底删除账号操作。")
        if int(actor.id) == int(target_user_id):
            raise ValidationError("禁止删除当前操作中的管理员自身账号。")
        if transfer_to_user_id is not None and int(transfer_to_user_id) == int(target_user_id):
            raise ValidationError("接管管理员不能是被删除的目标账号本人。")

        op_id = operation_id or uuid.uuid4()

        from comments.models import Comment, CommentReaction
        from content_lifecycle.guard import content_mutation_guard
        from content_lifecycle.models import RecycleBatch
        from media_library.models import CustomDocument, CustomImage
        from wagtail.users.models import UserProfile

        lock_ids = sorted({int(actor.id), int(target_user_id)} | ({int(transfer_to_user_id)} if transfer_to_user_id is not None else set()))

        with transaction.atomic():
            # 1. 升序锁定涉及的用户行，复核操作者、目标账号与接管人资格
            locked_users = {
                u.id: u
                for u in User.objects.select_for_update().filter(id__in=lock_ids).order_by("id")
            }
            locked_actor = locked_users.get(int(actor.id))
            target = locked_users.get(int(target_user_id))
            if not locked_actor or not locked_actor.is_active or not locked_actor.is_superuser:
                raise PermissionDenied("当前操作管理员权限无效。")
            if not target:
                raise ValidationError(f"目标账号 (id={target_user_id}) 不存在。")

            transfer_user = None
            if transfer_to_user_id is not None:
                transfer_user = locked_users.get(int(transfer_to_user_id))
                if not transfer_user or not transfer_user.is_active or not (transfer_user.is_superuser or transfer_user.is_staff):
                    raise ValidationError("指定的接管人必须是有效的在职管理员或后台人员。")

            # 锁定资料行，并先停用目标账号与递增 epoch
            member_profile = MemberProfile.objects.select_for_update().filter(user_id=target.id).first()
            wagtail_profile = UserProfile.objects.select_for_update().filter(user_id=target.id).first()

            with suppress_user_save_epoch_bump():
                if target.is_active:
                    target.is_active = False
                    target.save(update_fields=["is_active"])
            if member_profile:
                member_profile.session_epoch += 1
                member_profile.role_updated_at = timezone.now()
                member_profile.save(update_fields=["session_epoch", "role_updated_at", "updated_at"])

            # 暂存旧头像存储信息（避免随 User 级联删失）
            collected_avatars: list[tuple[str, str, str, str]] = []
            if member_profile and member_profile.avatar_key:
                alias = member_profile.avatar_storage_alias or "default"
                ns = member_profile.avatar_storage_namespace
                if not ns:
                    _, ns = get_storage_and_namespace(alias)
                collected_avatars.append((alias, ns, member_profile.avatar_key, "account_delete"))
            if wagtail_profile and wagtail_profile.avatar and wagtail_profile.avatar.name:
                _, default_ns = get_storage_and_namespace("default")
                collected_avatars.append(("default", default_ns, wagtail_profile.avatar.name, "account_delete"))

            # 2. 获取全局内容写锁，检查并过户全部归属资产（含草稿与回收站）
            with content_mutation_guard(timeout_seconds=3):
                owned_page_ids = list(
                    Page.objects.filter(owner_id=target.id).order_by("id").values_list("id", flat=True)
                )
                owned_image_ids = list(
                    CustomImage.objects.filter(uploaded_by_user_id=target.id).order_by("id").values_list("id", flat=True)
                )
                owned_doc_ids = list(
                    CustomDocument.objects.filter(uploaded_by_user_id=target.id).order_by("id").values_list("id", flat=True)
                )
                owned_batch_ids = list(
                    RecycleBatch.objects.filter(actor_id=target.id).order_by("id").values_list("id", flat=True)
                )

                has_assets = bool(owned_page_ids or owned_image_ids or owned_doc_ids or owned_batch_ids)
                if has_assets and transfer_user is None:
                    raise ValidationError(
                        "该账号名下仍有未永久销毁的页面（含草稿/回收站）、媒体资产或回收批次，必须指定接管管理员 transfer_to_user_id！"
                    )

                if has_assets and transfer_user is not None:
                    if owned_page_ids:
                        list(Page.objects.select_for_update().filter(id__in=owned_page_ids).order_by("id"))
                        Page.objects.filter(id__in=owned_page_ids).update(owner_id=transfer_user.id)
                    if owned_image_ids:
                        list(CustomImage.objects.select_for_update().filter(id__in=owned_image_ids).order_by("id"))
                        CustomImage.objects.filter(id__in=owned_image_ids).update(uploaded_by_user_id=transfer_user.id)
                    if owned_doc_ids:
                        list(CustomDocument.objects.select_for_update().filter(id__in=owned_doc_ids).order_by("id"))
                        CustomDocument.objects.filter(id__in=owned_doc_ids).update(uploaded_by_user_id=transfer_user.id)
                    if owned_batch_ids:
                        list(RecycleBatch.objects.select_for_update().filter(id__in=owned_batch_ids).order_by("id"))
                        RecycleBatch.objects.filter(id__in=owned_batch_ids).update(
                            actor_id=transfer_user.id,
                            actor_username=f"{transfer_user.username} (接管自:{target.username})"[:150],
                        )

            # 3. 按 Comment.id 升序锁定全部受影响评论，清除赞踩并精确重算计数
            reaction_comment_ids = set(
                CommentReaction.objects.filter(user_id=target.id).values_list("comment_id", flat=True)
            )
            author_comment_ids = set(
                Comment.objects.filter(Q(author_id=target.id) | Q(deleted_by_id=target.id) | Q(replied_to_user_id=target.id)).values_list("id", flat=True)
            )
            all_comment_ids = sorted(reaction_comment_ids | author_comment_ids)

            if all_comment_ids:
                list(Comment.objects.select_for_update().filter(id__in=all_comment_ids).order_by("id"))

            deleted_reactions_count = 0
            if reaction_comment_ids:
                deleted_reactions_count, _ = CommentReaction.objects.filter(user_id=target.id).delete()
                counts_map = {
                    row["comment_id"]: (row["likes"], row["dislikes"])
                    for row in CommentReaction.objects.filter(comment_id__in=reaction_comment_ids)
                    .values("comment_id")
                    .annotate(
                        likes=Count("id", filter=Q(reaction_type=1)),
                        dislikes=Count("id", filter=Q(reaction_type=-1)),
                    )
                }
                for cid in sorted(reaction_comment_ids):
                    likes, dislikes = counts_map.get(cid, (0, 0))
                    Comment.objects.filter(id=cid).update(like_count=likes, dislike_count=dislikes)

            # 4. 头像清理任务入队（不随 User 级联删除）
            queued_keys: list[str] = []
            for alias, ns, key, src in collected_avatars:
                task = _enqueue_purge_task_for_key(
                    storage_alias=alias,
                    storage_namespace=ns,
                    storage_key=key,
                    source=src,
                    operation_id=op_id,
                    target_user_id=target.id,
                )
                if task:
                    queued_keys.append(key)

            # 5. 写入审计日志
            target_username_snap = target.username
            target_id_snap = target.id
            AccountAuditLog.objects.create(
                action="hard_delete",
                actor_id_snapshot=locked_actor.id,
                actor_username_snapshot=locked_actor.username,
                target_user_id_snapshot=target_id_snap,
                target_username_snapshot=target_username_snap,
                operation_id=op_id,
                details={
                    "transfer_to_user_id": transfer_user.id if transfer_user else None,
                    "transferred_pages": len(owned_page_ids),
                    "transferred_images": len(owned_image_ids),
                    "transferred_documents": len(owned_doc_ids),
                    "transferred_batches": len(owned_batch_ids),
                    "recounted_comments": len(reaction_comment_ids),
                    "deleted_reactions": deleted_reactions_count,
                    "queued_avatar_keys": queued_keys,
                },
            )

            # 6. 仅在受控上下文内执行真实物理删除
            db_alias = target._state.db or "default"
            with authorize_user_hard_delete(target.id, using=db_alias):
                target.delete()

        return {
            "status": "deleted",
            "operation_id": str(op_id),
            "target_user_id": target_id_snap,
            "target_username": target_username_snap,
            "transfer_to_user_id": transfer_user.id if transfer_user else None,
            "transferred_pages": len(owned_page_ids),
            "transferred_images": len(owned_image_ids),
            "transferred_documents": len(owned_doc_ids),
            "transferred_batches": len(owned_batch_ids),
            "recounted_comments": len(reaction_comment_ids),
            "queued_avatar_keys": queued_keys,
        }
