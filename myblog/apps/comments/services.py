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

import logging
from datetime import timedelta
from django.db import transaction
from django.db.models import F
from django.db.models.functions import Greatest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from .models import Comment, CommentVersion, CommentReaction
from .sanitizer import sanitize_comment_html

logger = logging.getLogger(__name__)


class CommentService:

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
    ) -> Comment:
        """发表新评论或二级回复"""
        if not raw_content or not raw_content.strip():
            raise ValidationError("评论正文不能为空。")

        safe_html = sanitize_comment_html(raw_content)
        now = timezone.now()
        edit_window = now + timedelta(minutes=5)

        with transaction.atomic():
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
                        replied_to_user = actual_reply_to.author
                    except Comment.DoesNotExist:
                        pass

            # 创建 Comment 实例
            comment = Comment(
                page=page,
                page_document_uuid=getattr(page, "document_uuid", None),
                page_title_snapshot=getattr(page, "title", ""),
                author=author,
                parent=root_comment,
                reply_to_comment=actual_reply_to,
                replied_to_user=replied_to_user,
                status="pending",
                is_publicly_visible=False,
                can_edit_until=edit_window,
                ip_address=ip_address,
                user_agent=user_agent or "",
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
            )
            comment.latest_version_id = version.id
            comment.pending_version_id = version.id
            comment.save(update_fields=["latest_version_id", "pending_version_id"])

            # 若满足自动审批策略（如超级管理员），即时审批生效
            if auto_approve or (author and author.is_superuser):
                cls.moderate_comment(
                    comment_id=comment.id,
                    action="approve",
                    reviewer=author,
                    expected_version_id=version.id,
                )
                comment.refresh_from_db()

            return comment

    @classmethod
    def edit_comment(cls, comment_id: int, author, new_raw_content: str) -> CommentVersion:
        """5 分钟安全编辑窗口内提交新版本 (C-03)"""
        if not new_raw_content or not new_raw_content.strip():
            raise ValidationError("修改后的评论内容不能为空。")

        safe_html = sanitize_comment_html(new_raw_content)

        with transaction.atomic():
            comment = Comment.objects.select_for_update().get(id=comment_id)
            if comment.author_id != getattr(author, "id", None) and not getattr(author, "is_superuser", False):
                raise PermissionDenied("仅原作者有权编辑本人评论。")
            if not comment.is_editable and not getattr(author, "is_superuser", False):
                raise ValidationError("评论已超出 5 分钟安全编辑时限，不可修改。")
            if comment.status == "deleted":
                raise ValidationError("已删除的评论不可编辑。")

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
            new_version = CommentVersion.objects.create(
                comment=comment,
                version_number=max_ver + 1,
                raw_content=new_raw_content,
                safe_html=safe_html,
                status="pending",
            )

            comment.latest_version_id = new_version.id
            comment.pending_version_id = new_version.id
            comment.save(update_fields=["latest_version_id", "pending_version_id", "updated_at"])

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

            was_visible = bool(comment.is_publicly_visible)

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

                comment.approved_version_id = target_ver.id
                comment.pending_version_id = None
                comment.status = "active"
                comment.is_publicly_visible = True
                comment.save(update_fields=["approved_version_id", "pending_version_id", "status", "is_publicly_visible", "updated_at"])

            # 3. 执行驳回 (reject)
            elif action == "reject":
                if expected_version_id:
                    CommentVersion.objects.filter(id=expected_version_id, comment_id=comment.id).update(
                        status="rejected", reviewed_at=now, reviewed_by=reviewer
                    )
                if comment.pending_version_id == expected_version_id:
                    comment.pending_version_id = None

                # 若该评论未曾有过已批准版本，整体置为 rejected
                if not comment.approved_version_id:
                    comment.status = "rejected"
                    comment.is_publicly_visible = False
                comment.save(update_fields=["pending_version_id", "status", "is_publicly_visible", "updated_at"])

            # 4. 执行隐藏/下架 (hide)
            elif action == "hide":
                comment.status = "rejected"
                comment.is_publicly_visible = False
                comment.save(update_fields=["status", "is_publicly_visible", "updated_at"])

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
    def soft_delete_comment(cls, comment_id: int, actor) -> Comment:
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

            was_visible = bool(comment.is_publicly_visible)
            comment.status = "deleted"
            comment.is_publicly_visible = False
            comment.save(update_fields=["status", "is_publicly_visible", "updated_at"])

            # 若为子回复软删，父评论 reply_count 减 1
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
            comment = Comment.objects.select_for_update().get(id=comment_id)
            if not comment.is_publicly_visible:
                raise ValidationError("无法对未公开或已删除的评论进行互动。")
            if comment.author_id == user.id:
                raise ValidationError("不能对自己的评论点赞或点踩。")

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
