"""
comments.services
评论核心业务服务层。

实现高可靠事务与并发控制：
1. post_comment: 新增评论/回复，强制锁根评论校验同页与二级扁平约束 (C-05)；
2. edit_comment: 5 分钟安全编辑窗口，新版本排他覆盖旧 pending 版本 (C-03)；
3. moderate_comment: 版本 CAS 防盲批校验 (C-03)，基于 is_publicly_visible 状态跃迁原子更新 reply_count (C-04)；
4. soft_delete_comment: 软删除评论，级联更新父级 reply_count；
5. react_comment: 点赞/点踩互斥切换，F() 原子增减与非负约束。
"""

import hashlib
import logging
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import F
from django.db.models.functions import Greatest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from .models import (
    Comment,
    CommentVersion,
    CommentReaction,
    CommentReviewAction,
    PageCommentSettings,
    CommentGovernanceSubject,
    CommentGovernanceBatch,
    CommentGovernanceItem,
    CommentMutationReceipt,
)
from .sanitizer import sanitize_comment_html, inspect_links_and_sanitize

logger = logging.getLogger(__name__)


class CommentService:

    @classmethod
    def _lock_and_verify_active_user(cls, user) -> None:
        """
        在锁定 Comment 之前先核验并按共享键锁（FOR KEY SHARE）锁定目标 User，
        确保全站统一遵循 User -> Comment 单向锁顺序。

        教学说明：
        1. AccountLifecycleService.hard_delete 删除账号时按“先锁 User，再锁 Comment”加锁；
           评论写操作也必须先锁定目标 User 再锁 Comment，防止高并发下锁序相反触发死锁。
        2. PostgreSQL 下使用 FOR KEY SHARE 而非 FOR UPDATE：
           并发点赞、发评均只加 FOR KEY SHARE，彼此兼容互不阻塞；
           同时又能与账号停用/硬删的 FOR UPDATE 互斥，确保停用或注销后不会写出孤儿外键。
        """
        user_id = getattr(user, "pk", None)
        if not user_id:
            raise PermissionDenied("账号已停用或不存在，无法执行操作。")

        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM identity_user WHERE id = %s AND is_active = TRUE FOR KEY SHARE",
                    [user_id],
                )
                row = cursor.fetchone()
            if row is None:
                raise PermissionDenied("账号已停用或不存在，无法执行操作。")
        else:
            user_model = get_user_model()
            if not user_model.objects.filter(pk=user_id, is_active=True).exists():
                raise PermissionDenied("账号已停用或不存在，无法执行操作。")

    @classmethod
    def post_comment(
        cls,
        page,
        author,
        raw_content: str,
        guest_name: str | None = None,
        parent_id: int | None = None,
        reply_to_comment_id: int | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        auto_approve: bool = False,
        request_key: str | None = None,
    ) -> Comment:
        """发表新评论或二级回复"""
        if not raw_content or not raw_content.strip():
            raise ValidationError("评论正文不能为空。")

        # 1. 检查发评主体是否受限 (24h 治理限制)
        if cls.is_subject_restricted(author=author, ip_address=ip_address):
            raise ValidationError("您当前已被系统限制发表评论，请稍后再试。")

        # 2. 检查页面级评论开关 (PageCommentSettings)
        settings_obj = PageCommentSettings.objects.filter(page_id=page.id).first()
        if settings_obj and not settings_obj.allow_comments:
            close_msg = f"（说明：{settings_obj.close_reason}）" if settings_obj.close_reason else ""
            raise ValidationError(f"该页面已关闭评论互动{close_msg}。")
        if settings_obj and settings_obj.auto_moderate_new:
            auto_approve = False

        # 3. 幂等校验 (MutationReceipt)
        subject_key = f"user:{author.id}" if author and getattr(author, "is_authenticated", False) else f"ip:{ip_address or 'unknown'}"
        content_digest = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
        if request_key:
            receipt = CommentMutationReceipt.objects.filter(
                subject_key=subject_key,
                action="post_comment",
                request_key=request_key,
            ).first()
            if receipt and receipt.result_comment_id:
                existing = Comment.objects.filter(id=receipt.result_comment_id).first()
                if existing:
                    return existing

        safe_html = sanitize_comment_html(raw_content)
        safe_html, external_links = inspect_links_and_sanitize(raw_content)
        if external_links >= 2:
            auto_approve = False
        now = timezone.now()
        edit_window = now + timedelta(minutes=5)

        with transaction.atomic():
            # 先收集并按 User.id 升序获取共享键锁，严格保证 User -> Comment 单向锁序，防止与 hard_delete 形成 AB-BA 等待
            target_reply_user_id = None
            if parent_id is not None and reply_to_comment_id:
                reply_row = Comment.objects.filter(id=reply_to_comment_id).values("page_id", "author_id").first()
                if reply_row and reply_row["page_id"] == page.id and reply_row["author_id"]:
                    target_reply_user_id = reply_row["author_id"]

            lock_user_pairs: list[tuple[int, bool]] = []
            if author and getattr(author, "is_authenticated", False):
                lock_user_pairs.append((int(author.pk), True))
            if target_reply_user_id is not None and (not author or int(target_reply_user_id) != int(getattr(author, "pk", 0) or 0)):
                lock_user_pairs.append((int(target_reply_user_id), False))

            reply_user_alive = target_reply_user_id is not None
            for uid, must_be_active in sorted(lock_user_pairs, key=lambda x: x[0]):
                if must_be_active:
                    cls._lock_and_verify_active_user(author)
                else:
                    if connection.vendor == "postgresql":
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT 1 FROM identity_user WHERE id = %s FOR KEY SHARE", [uid])
                            if cursor.fetchone() is None:
                                reply_user_alive = False
                    else:
                        if not get_user_model().objects.filter(pk=uid).exists():
                            reply_user_alive = False

            root_comment = None
            replied_to_user = None
            actual_reply_to = None

            if parent_id is not None:
                # 必须先以排他锁锁定根评论，校验不变量 (C-05)
                try:
                    root_comment = Comment.objects.select_for_update().get(id=parent_id)
                except Comment.DoesNotExist:
                    raise ValidationError("所回复的根评论不存在。")

                if root_comment.page_id != page.id:
                    raise ValidationError("不能跨页面回复评论！")
                if root_comment.parent_id is not None:
                    raise ValidationError("系统仅支持二级扁平回复，禁止递归嵌套！")
                if not root_comment.is_publicly_visible or root_comment.status != "active":
                    raise ValidationError("目标根评论已被删除或未公开，无法追加回复。")

                # 如果指定了具体回复的子回复对象，提取被回复人
                if reply_to_comment_id:
                    try:
                        actual_reply_to = Comment.objects.select_related("author").get(id=reply_to_comment_id)
                        if actual_reply_to.page_id != page.id:
                            raise ValidationError("回复的目标评论与当前页面不一致。")
                        replied_to_user = actual_reply_to.author if reply_user_alive else None
                    except Comment.DoesNotExist:
                        pass

            # 创建 Comment 实例
            comment = Comment(
                page=page,
                page_document_uuid=getattr(page, "document_uuid", None),
                page_title_snapshot=getattr(page, "title", ""),
                author=author,
                author_subject_key=subject_key,
                guest_name=guest_name or "",
                parent=root_comment,
                reply_to_comment=actual_reply_to,
                replied_to_user=replied_to_user,
                status="pending",
                is_publicly_visible=False,
                can_edit_until=edit_window,
                ip_address=ip_address,
                user_agent=user_agent or "",
                pending_submitted_at=now,
            )
            # 使用 super 绕过软删除检查直接创建
            super(Comment, comment).save()

            # 创建首个版本 V1
            version = CommentVersion.objects.create(
                comment=comment,
                version_number=1,
                raw_content=raw_content,
                safe_html=safe_html,
                status="pending",
                risk_reasons=["external_links_threshold"] if external_links >= 2 else [],
            )
            comment.latest_version = version
            comment.pending_version = version
            comment.save(update_fields=["latest_version", "pending_version", "pending_submitted_at"])

            # 若满足自动审批策略（如超级管理员），即时审批生效
            if auto_approve or (author and author.is_superuser):
                cls.moderate_comment(
                    comment_id=comment.id,
                    action="approve",
                    reviewer=author,
                    expected_version_id=version.id,
                )
                comment.refresh_from_db()

            if request_key:
                CommentMutationReceipt.objects.get_or_create(
                    subject_key=subject_key,
                    action="post_comment",
                    request_key=request_key,
                    defaults={
                        "request_hash": content_digest,
                        "result_comment_id": comment.id,
                    },
                )

            return comment

    @classmethod
    def edit_comment(
        cls,
        comment_id: int,
        author,
        new_raw_content: str,
        expected_version_id: int | None = None,
        expected_generation: int | None = None,
        ip_address: str | None = None,
    ) -> CommentVersion:
        """5 分钟安全编辑窗口内提交新版本 (C-03)"""
        # 检查主体受限
        if cls.is_subject_restricted(author=author, ip_address=ip_address):
            raise ValidationError("您当前已被系统限制编辑评论，请稍后再试。")

        if not new_raw_content or not new_raw_content.strip():
            raise ValidationError("修改后的评论内容不能为空。")

        safe_html, _ = inspect_links_and_sanitize(new_raw_content)

        with transaction.atomic():
            if author and getattr(author, "is_authenticated", False):
                cls._lock_and_verify_active_user(author)

            comment = Comment.objects.select_for_update().get(id=comment_id)
            if comment.author_id != getattr(author, "id", None) and not getattr(author, "is_superuser", False):
                raise PermissionDenied("仅原作者有权编辑本人评论。")
            if not comment.is_editable and not getattr(author, "is_superuser", False):
                raise ValidationError("评论已超出 5 分钟安全编辑时限，不可修改。")
            if comment.status == "deleted":
                raise ValidationError("已删除的评论不可编辑。")
            if expected_generation is not None and comment.state_generation != expected_generation:
                raise ValidationError("编辑版本冲突：评论状态已被改变，请刷新后再编辑。")
            if expected_version_id and comment.latest_version_id != expected_version_id:
                raise ValidationError("编辑版本冲突：已有更新的草稿被提交，请刷新后再编辑。")

            # 校验页面设置
            if comment.page_id:
                settings_obj = PageCommentSettings.objects.filter(page_id=comment.page_id).first()
                if settings_obj and not settings_obj.allow_comments:
                    raise ValidationError("该页面已关闭评论互动，无法修改评论。")

            # 将当前残留的 pending 版本置为 superseded (已替代)
            CommentVersion.objects.filter(comment=comment, status="pending").update(status="superseded")

            # 创建新版本号
            max_ver = (
                CommentVersion.objects.filter(comment=comment)
                .order_by("-version_number")
                .values_list("version_number", flat=True)
                .first()
                or 1
            )
            now = timezone.now()
            new_version = CommentVersion.objects.create(
                comment=comment,
                version_number=max_ver + 1,
                raw_content=new_raw_content,
                safe_html=safe_html,
                status="pending",
            )

            # D7-A: approved_version 保持原 V1 不变，pending_version 指向新 V2
            comment.latest_version = new_version
            comment.pending_version = new_version
            comment.pending_submitted_at = now
            comment.state_generation += 1
            comment.save(update_fields=["latest_version", "pending_version", "pending_submitted_at", "state_generation", "updated_at"])

            # 管理员本人编辑自动通过
            if author and author.is_superuser:
                cls.moderate_comment(
                    comment_id=comment.id,
                    action="approve",
                    reviewer=author,
                    expected_version_id=new_version.id,
                )

            return new_version

    @classmethod
    def moderate_comment(
        cls,
        comment_id: int,
        action: str,  # 'approve' | 'reject' | 'hide'
        reviewer=None,
        expected_version_id: int | None = None,
        expected_generation: int | None = None,
        reason: str = "",
        ip_address: str | None = None,
    ) -> Comment:
        """
        审核批准/驳回/隐藏评论。
        严格落实版本 CAS 校验 (C-03) 与 is_publicly_visible 状态跃迁计数 (C-04)。
        """
        now = timezone.now()

        with transaction.atomic():
            # 1. 查找关联根评论并全局按 id ASC 升序加锁防死锁 (C-04)
            c_simple = Comment.objects.get(id=comment_id)
            lock_ids = [c_simple.id]
            if c_simple.parent_id:
                lock_ids.append(c_simple.parent_id)
            lock_ids = sorted(set(lock_ids))

            locked_map = {
                c.id: c
                for c in Comment.objects.filter(id__in=lock_ids).order_by("id").select_for_update()
            }
            comment = locked_map[comment_id]
            root = locked_map.get(comment.parent_id) if comment.parent_id else None

            if expected_generation is not None and comment.state_generation != expected_generation:
                raise ValidationError("审核冲突：该评论已被并发操作修改，请刷新后再审核！")

            was_visible = bool(comment.is_publicly_visible)
            prev_status = comment.status
            applied_version_number = None

            # 2. 执行批准 (approve)
            if action == "approve":
                target_ver_id = expected_version_id or comment.pending_version_id or comment.latest_version_id
                if not target_ver_id:
                    raise ValidationError("无有效版本可批准。")

                # C-03 CAS 强校验：防盲批
                if expected_version_id and comment.pending_version_id != expected_version_id:
                    raise ValidationError("审核冲突：该评论已被提交了更新的版本，请核对最新内容后再审核！")

                target_ver = CommentVersion.objects.get(id=target_ver_id, comment_id=comment.id)
                target_ver.status = "approved"
                target_ver.reviewed_at = now
                target_ver.reviewed_by = reviewer
                target_ver.save(update_fields=["status", "reviewed_at", "reviewed_by"])
                applied_version_number = target_ver.version_number

                # 其他待审版本标记已被替代
                CommentVersion.objects.filter(comment=comment, status="pending").exclude(id=target_ver.id).update(status="superseded")

                comment.approved_version = target_ver
                comment.pending_version = None
                comment.status = "active"
                comment.is_publicly_visible = True
                comment.state_generation += 1
                comment.save(update_fields=["approved_version", "pending_version", "status", "is_publicly_visible", "state_generation", "updated_at"])

            # 3. 执行驳回 (reject)
            elif action == "reject":
                if expected_version_id:
                    if comment.pending_version_id and comment.pending_version_id != expected_version_id:
                        raise ValidationError("驳回冲突：该待审版本已被更新或替换！")
                    CommentVersion.objects.filter(id=expected_version_id, comment_id=comment.id).update(
                        status="rejected", reviewed_at=now, reviewed_by=reviewer
                    )
                if expected_version_id and comment.pending_version_id == expected_version_id:
                    comment.pending_version = None
                elif not expected_version_id:
                    comment.pending_version = None

                # 若该评论未曾有过已批准版本，整体置为 rejected
                if not comment.approved_version:
                    comment.status = "rejected"
                    comment.is_publicly_visible = False
                comment.state_generation += 1
                comment.save(update_fields=["pending_version", "status", "is_publicly_visible", "state_generation", "updated_at"])

            # 4. 执行隐藏/下架 (hide)
            elif action == "hide":
                comment.status = "rejected"
                comment.is_publicly_visible = False
                comment.is_pinned = False
                comment.state_generation += 1
                comment.save(update_fields=["status", "is_publicly_visible", "is_pinned", "state_generation", "updated_at"])

            # 审计日志记录
            CommentReviewAction.objects.create(
                comment=comment,
                actor=reviewer if reviewer and getattr(reviewer, "is_authenticated", False) else None,
                action=action,
                version_number=applied_version_number,
                previous_status=prev_status,
                new_status=comment.status,
                reason=reason,
                ip_address=ip_address,
            )

            # 5. C-04 核心计数公式：基于 is_publicly_visible 状态跃迁累加差值
            now_visible = bool(comment.is_publicly_visible)
            if root and was_visible != now_visible:
                if not was_visible and now_visible:
                    # 首次公开可见：+1
                    Comment.objects.filter(id=root.id).update(reply_count=F("reply_count") + 1)
                elif was_visible and not now_visible:
                    # 从可见变为隐藏/下架：-1
                    Comment.objects.filter(id=root.id).update(reply_count=Greatest(F("reply_count") - 1, 0))

            return comment

    @classmethod
    def soft_delete_comment(
        cls,
        comment_id: int,
        actor,
        reason: str = "author",
        ip_address: str | None = None,
    ) -> Comment:
        """软删除评论（作者自删或管理员下架）"""
        with transaction.atomic():
            c_simple = Comment.objects.get(id=comment_id)
            lock_ids = sorted(set([c_simple.id] + ([c_simple.parent_id] if c_simple.parent_id else [])))
            locked_map = {
                c.id: c
                for c in Comment.objects.filter(id__in=lock_ids).order_by("id").select_for_update()
            }
            comment = locked_map[comment_id]
            root = locked_map.get(comment.parent_id) if comment.parent_id else None

            is_author = comment.author_id == getattr(actor, "id", None)
            is_moderator = getattr(actor, "is_superuser", False) or (actor and actor.has_perm("comments.moderate_comment"))
            if not (is_author or is_moderator):
                raise PermissionDenied("无权删除此评论。")

            del_reason = reason if reason in ("author", "moderator", "risk_control") else ("author" if is_author else "moderator")
            was_visible = bool(comment.is_publicly_visible)
            prev_status = comment.status
            now = timezone.now()

            comment.status = "deleted"
            comment.deletion_reason = del_reason
            comment.deleted_at = now
            comment.deleted_by = actor if getattr(actor, "is_authenticated", False) else None
            comment.is_publicly_visible = False
            comment.is_pinned = False
            comment.state_generation += 1
            comment.save(update_fields=[
                "status", "deletion_reason", "deleted_at", "deleted_by",
                "is_publicly_visible", "is_pinned", "state_generation", "updated_at"
            ])

            # 记录审计日志
            CommentReviewAction.objects.create(
                comment=comment,
                actor=actor if getattr(actor, "is_authenticated", False) else None,
                action=f"delete_{del_reason}",
                version_number=comment.approved_version.version_number if comment.approved_version else None,
                previous_status=prev_status,
                new_status="deleted",
                reason=f"软删除: {del_reason}",
                ip_address=ip_address,
            )

            # 若为子回复软删且此前可见，父评论 reply_count 减 1
            if root and was_visible:
                Comment.objects.filter(id=root.id).update(reply_count=Greatest(F("reply_count") - 1, 0))

            return comment

    @classmethod
    def react_comment(cls, comment_id: int, user, target_reaction: int) -> dict[str, int]:
        """
        点赞/点踩互动切换 (C-05)。
        target_reaction: 1 为点赞，-1 为点踩，0 为取消反应。
        """
        if target_reaction not in (1, -1, 0):
            raise ValidationError("非法的互动类型。")
        if not user or not user.is_authenticated:
            raise PermissionDenied("请先登录后再参与互动。")

        with transaction.atomic():
            cls._lock_and_verify_active_user(user)
            comment = Comment.objects.select_for_update().get(id=comment_id)
            if not comment.is_publicly_visible:
                raise ValidationError("无法对未公开或已删除的评论进行互动。")
            if comment.author_id == user.id:
                raise ValidationError("不能对自己的评论点赞或点踩。")
            if target_reaction != 0 and comment.page_id:
                settings_obj = PageCommentSettings.objects.filter(page_id=comment.page_id).first()
                if settings_obj and not settings_obj.allow_comments:
                    raise ValidationError("该页面已关闭评论互动，暂不支持新增或切换赞踩。")

            existing_reaction = CommentReaction.objects.filter(comment=comment, user=user).first()
            old_type = existing_reaction.reaction_type if existing_reaction else 0

            if old_type == target_reaction:
                return {"like_count": comment.like_count, "dislike_count": comment.dislike_count}

            like_delta = 0
            dislike_delta = 0

            # 撤销旧反应
            if old_type == 1:
                like_delta -= 1
            elif old_type == -1:
                dislike_delta -= 1

            # 加上新反应
            if target_reaction == 1:
                like_delta += 1
                if existing_reaction:
                    existing_reaction.reaction_type = 1
                    existing_reaction.save(update_fields=["reaction_type"])
                else:
                    CommentReaction.objects.create(comment=comment, user=user, reaction_type=1)
            elif target_reaction == -1:
                dislike_delta += 1
                if existing_reaction:
                    existing_reaction.reaction_type = -1
                    existing_reaction.save(update_fields=["reaction_type"])
                else:
                    CommentReaction.objects.create(comment=comment, user=user, reaction_type=-1)
            elif target_reaction == 0 and existing_reaction:
                existing_reaction.delete()

            # F() 原子更新与非负保护
            updates = {}
            if like_delta != 0:
                updates["like_count"] = Greatest(F("like_count") + like_delta, 0)
            if dislike_delta != 0:
                updates["dislike_count"] = Greatest(F("dislike_count") + dislike_delta, 0)

            if updates:
                Comment.objects.filter(id=comment.id).update(**updates)
                comment.refresh_from_db()

            return {"like_count": comment.like_count, "dislike_count": comment.dislike_count}

    @classmethod
    def toggle_pin_comment(cls, comment_id: int, actor, ip_address: str | None = None) -> Comment:
        """置顶/取消置顶评论 (仅管理员或被授权人)"""
        is_moderator = getattr(actor, "is_superuser", False) or (actor and actor.has_perm("comments.moderate_comment"))
        if not is_moderator:
            raise PermissionDenied("无权置顶评论。")

        with transaction.atomic():
            comment = Comment.objects.select_for_update().get(id=comment_id)
            if comment.status != "active" or not comment.is_publicly_visible:
                raise ValidationError("仅正常且已公开的评论可以置顶。")

            comment.is_pinned = not comment.is_pinned
            comment.state_generation += 1
            comment.save(update_fields=["is_pinned", "state_generation", "updated_at"])

            CommentReviewAction.objects.create(
                comment=comment,
                actor=actor,
                action="pin" if comment.is_pinned else "unpin",
                version_number=comment.approved_version.version_number if comment.approved_version else None,
                previous_status=comment.status,
                new_status=comment.status,
                reason="管理员置顶切换",
                ip_address=ip_address,
            )
            return comment

    # ================= 社区治理与主体限制 =================

    @classmethod
    def restrict_subject(
        cls,
        kind: str,
        subject_key: str,
        hours: int = 24,
        reason: str = "",
        actor=None,
    ) -> CommentGovernanceSubject:
        """对指定用户或IP实施临时限制 (默认24小时)"""
        if kind not in ("user", "ip"):
            raise ValidationError("非法的治理主体类型。")
        now = timezone.now()
        target_until = now + timedelta(hours=hours)

        with transaction.atomic():
            obj, created = CommentGovernanceSubject.objects.select_for_update().get_or_create(
                kind=kind,
                subject_key=subject_key,
                defaults={
                    "restricted_until": target_until,
                    "reason": reason,
                    "updated_by": actor,
                },
            )
            if not created:
                # 取最远时间
                obj.restricted_until = max(obj.restricted_until, target_until)
                obj.generation += 1
                obj.reason = reason
                obj.updated_by = actor
                obj.save(update_fields=["restricted_until", "generation", "reason", "updated_by", "updated_at"])
            return obj

    @classmethod
    def lift_restriction(cls, kind: str, subject_key: str, actor=None) -> bool:
        """提前解除指定用户或IP的治理限制"""
        now = timezone.now()
        updated = CommentGovernanceSubject.objects.filter(
            kind=kind,
            subject_key=subject_key,
            restricted_until__gt=now,
        ).update(restricted_until=now, updated_by=actor)
        return updated > 0

    @classmethod
    def is_subject_restricted(cls, author=None, ip_address: str | None = None) -> bool:
        """查询主体是否当前处于治理限制期"""
        now = timezone.now()
        if author and getattr(author, "is_authenticated", False):
            if CommentGovernanceSubject.objects.filter(
                kind="user",
                subject_key=f"user:{author.id}",
                restricted_until__gt=now,
            ).exists():
                return True
        if ip_address:
            if CommentGovernanceSubject.objects.filter(
                kind="ip",
                subject_key=f"ip:{ip_address}",
                restricted_until__gt=now,
            ).exists():
                return True
        return False

    @classmethod
    def batch_hide_comments(cls, actor, comment_ids: list[int], reason: str = "批量风控隐藏") -> dict[str, int]:
        """全站跨页面批量隐藏违规评论 (按 id 升序加锁防死锁，每批最多 100 条)"""
        success_count = 0
        conflict_count = 0
        fail_count = 0

        for cid in sorted(set(comment_ids))[:100]:
            try:
                cls.moderate_comment(
                    comment_id=cid,
                    action="hide",
                    reviewer=actor,
                    reason=reason,
                )
                success_count += 1
            except ValidationError:
                conflict_count += 1
            except Exception as e:
                logger.error("批量隐藏评论 #%d 失败: %s", cid, e)
                fail_count += 1

        return {"success": success_count, "conflict": conflict_count, "failed": fail_count}
