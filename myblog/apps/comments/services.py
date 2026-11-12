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
import json
import logging
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.db.models.functions import Greatest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from wagtail.models import Page, Site

from .models import (
    Comment,
    CommentVersion,
    CommentReaction,
    CommentReviewAction,
    PageCommentSettings,
    CommunitySettings,
    CommentDailyQuota,
    CommentGovernanceSubject,
    CommentGovernanceAuditLog,
    CommentGovernanceBatch,
    CommentGovernanceItem,
    CommentMutationReceipt,
)
from .access import (
    can_user_access_page_comment_settings,
    can_user_manage_guest_comments,
    get_comment_user_tier,
)
from .sanitizer import sanitize_comment_for_tier

logger = logging.getLogger(__name__)


class CommentConflictError(ValidationError):
    """请求幂等键冲突或状态并发冲突（映射 HTTP 409）。"""
    http_status = 409
    error_code = "idempotency_conflict"


class CommentQuotaExceeded(ValidationError):
    """评论日配额超出上限（映射 HTTP 429）。"""
    http_status = 429
    error_code = "daily_quota_exceeded"


class CommentQueueBudgetExceeded(ValidationError):
    """全站待审队列预算熔断（映射 HTTP 429）。"""
    http_status = 429
    error_code = "pending_queue_budget_exceeded"


def _resolve_quota_site(page) -> Site:
    site = None
    if page is not None:
        try:
            site = page.get_site()
        except Exception:
            site = None
    if site is None:
        site = Site.objects.filter(is_default_site=True).order_by("id").first()
    if site is None:
        site = Site.objects.order_by("id").first()
    if site is None:
        raise ValidationError("系统尚未配置默认站点，无法核验评论日配额。")
    return site


def _consume_daily_quota_tx(
    *,
    site_id: int,
    subject_kind: str,
    subject_key: str,
    quota_date,
    action: str,
    cap: int,
    error_message: str,
) -> int:
    """
    在当前 SQL 事务内原子检查并累加评论日配额（单句 PostgreSQL UPSERT 条件更新，并发零穿透）。
    """
    if int(cap) <= 0:
        raise CommentQuotaExceeded(error_message)

    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO comments_commentdailyquota
                    (site_id, subject_kind, subject_key, quota_date, action, used_count, updated_at)
                VALUES (%s, %s, %s, %s, %s, 1, NOW())
                ON CONFLICT (site_id, subject_kind, subject_key, quota_date, action)
                DO UPDATE SET
                    used_count = comments_commentdailyquota.used_count + 1,
                    updated_at = NOW()
                WHERE comments_commentdailyquota.used_count < %s
                RETURNING used_count;
                """,
                [site_id, subject_kind, subject_key, quota_date, action, int(cap)],
            )
            row = cursor.fetchone()
        if row is None:
            raise CommentQuotaExceeded(error_message)
        return int(row[0])

    # 非 PostgreSQL 回退实现
    try:
        with transaction.atomic():
            obj, created = CommentDailyQuota.objects.select_for_update().get_or_create(
                site_id=site_id,
                subject_kind=subject_kind,
                subject_key=subject_key,
                quota_date=quota_date,
                action=action,
                defaults={"used_count": 1},
            )
    except IntegrityError:
        obj = CommentDailyQuota.objects.select_for_update().get(
            site_id=site_id,
            subject_kind=subject_kind,
            subject_key=subject_key,
            quota_date=quota_date,
            action=action,
        )
        created = False
    if not created:
        if obj.used_count >= int(cap):
            raise CommentQuotaExceeded(error_message)
        obj.used_count += 1
        obj.save(update_fields=["used_count", "updated_at"])
    return int(obj.used_count)


class CommentService:

    @classmethod
    def _lock_and_verify_active_user(cls, user) -> tuple[bool, object]:
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
                    "SELECT is_superuser, date_joined FROM identity_user WHERE id = %s AND is_active = TRUE FOR KEY SHARE",
                    [user_id],
                )
                row = cursor.fetchone()
            if row is None:
                raise PermissionDenied("账号已停用或不存在，无法执行操作。")
            return bool(row[0]), row[1]
        else:
            user_model = get_user_model()
            u_row = user_model.objects.filter(pk=user_id, is_active=True).values("is_superuser", "date_joined").first()
            if u_row is None:
                raise PermissionDenied("账号已停用或不存在，无法执行操作。")
            return bool(u_row["is_superuser"]), u_row["date_joined"]

    @classmethod
    def _check_governance_permission(cls, actor, *, require_global: bool = False) -> None:
        if not actor or not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_active", False):
            raise PermissionDenied("操作人未登录或账号已停用。")
        if getattr(actor, "is_superuser", False) or actor.has_perm("comments.govern_comment_globally"):
            return
        if not require_global and actor.has_perm("comments.moderate_comment"):
            return
        raise PermissionDenied("当前账号缺少评论治理权限。")

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
        *,
        guest_browser_key: str | None = None,
        require_guest_browser: bool = False,
        site=None,
    ) -> Comment:
        """发表新评论或二级回复（事务内锁定页面复核准入模式、双轨日配额与原子幂等）。"""
        if not raw_content or not raw_content.strip():
            raise ValidationError("评论正文不能为空。")

        if page is None or not getattr(page, "id", None):
            raise ValidationError("目标页面无效。")

        clean_guest_name = (guest_name or "").strip()
        if len(clean_guest_name) > 50:
            raise ValidationError("访客昵称不能超过 50 个字符。")

        clean_request_key = (request_key or "").strip()
        if len(clean_request_key) > 64:
            raise ValidationError("请求幂等键长度超出限制。")

        db_author = author if (author and getattr(author, "is_authenticated", False)) else None
        is_guest = db_author is None

        if is_guest and require_guest_browser and not guest_browser_key:
            raise PermissionDenied("缺少有效的访客安全验证会话，请刷新验证码后重试。")

        if db_author is not None:
            subject_key = f"user:{db_author.id}"
        elif guest_browser_key:
            subject_key = guest_browser_key if guest_browser_key.startswith("guest:") else f"guest:{guest_browser_key}"
        else:
            subject_key = f"ip:{ip_address or 'unknown'}"

        canonical_payload = json.dumps(
            [
                int(page.id),
                int(parent_id) if parent_id is not None else None,
                int(reply_to_comment_id) if reply_to_comment_id is not None else None,
                clean_guest_name,
                raw_content,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        content_digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        now = timezone.now()
        edit_window = now + timedelta(minutes=5)

        with transaction.atomic():
            # 1. 原子幂等凭证抢占（先于日配额扣减，确保并发重放不重复扣额、同 key 不同载荷报 409）
            receipt = None
            if clean_request_key:
                try:
                    with transaction.atomic():
                        receipt = CommentMutationReceipt.objects.create(
                            subject_key=subject_key,
                            action="post_comment",
                            request_key=clean_request_key,
                            request_hash=content_digest,
                            result_comment_id=None,
                        )
                except IntegrityError:
                    receipt = CommentMutationReceipt.objects.select_for_update().get(
                        subject_key=subject_key,
                        action="post_comment",
                        request_key=clean_request_key,
                    )
                    if receipt.request_hash != content_digest:
                        raise CommentConflictError("幂等键冲突：相同 request_key 不允许提交不同的评论内容。")
                    if not receipt.result_comment_id:
                        raise CommentConflictError("重复请求状态异常，请更换幂等键后重试。")
                    existing = Comment.objects.filter(id=receipt.result_comment_id).first()
                    if existing is None:
                        raise CommentConflictError("该幂等键关联的评论记录已不存在。")
                    return existing

            # 先收集并按 User.id 升序获取共享键锁，严格保证 User -> Comment 单向锁序，防止与 hard_delete 形成 AB-BA 等待
            target_reply_user_id = None
            if parent_id is not None and reply_to_comment_id:
                reply_row = Comment.objects.filter(id=reply_to_comment_id).values("page_id", "author_id").first()
                if reply_row and reply_row["page_id"] == page.id and reply_row["author_id"]:
                    target_reply_user_id = reply_row["author_id"]

            lock_user_pairs: list[tuple[int, bool]] = []
            if db_author is not None:
                lock_user_pairs.append((int(db_author.pk), True))
            if target_reply_user_id is not None and (not db_author or int(target_reply_user_id) != int(db_author.pk)):
                lock_user_pairs.append((int(target_reply_user_id), False))

            reply_user_alive = target_reply_user_id is not None
            author_is_superuser = False
            author_date_joined = None
            for uid, must_be_active in sorted(lock_user_pairs, key=lambda x: x[0]):
                if must_be_active:
                    author_is_superuser, author_date_joined = cls._lock_and_verify_active_user(db_author)
                else:
                    if connection.vendor == "postgresql":
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT 1 FROM identity_user WHERE id = %s FOR KEY SHARE", [uid])
                            if cursor.fetchone() is None:
                                reply_user_alive = False
                    else:
                        if not get_user_model().objects.filter(pk=uid).exists():
                            reply_user_alive = False

            # 2. 事务内锁定目标 wagtailcore_page 行 (FOR SHARE) 并重读页面与评论设置，杜绝发布/回收与配置竞态
            if connection.vendor == "postgresql":
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '3s';")
                        cursor.execute(
                            "SELECT id FROM wagtailcore_page WHERE id = %s FOR SHARE;",
                            [page.id],
                        )
                        if cursor.fetchone() is None:
                            raise PermissionDenied("目标页面不存在或已被销毁。")
                except PermissionDenied:
                    raise
                except Exception as lock_exc:
                    logger.warning("Failed to lock page FOR SHARE inside post_comment: %s", lock_exc)
                    raise PermissionDenied("页面正在执行发布或回收操作，请稍后重试。")

            fresh_page_base = Page.objects.filter(id=page.id).first()
            if fresh_page_base is None:
                raise PermissionDenied("目标页面不存在或已被销毁。")
            fresh_page = fresh_page_base.specific

            supports = getattr(fresh_page, "supports_comments", None)
            if supports is False or (supports is None and not getattr(fresh_page, "allow_comments", False)):
                raise PermissionDenied("该类型页面未开放评论互动功能。")
            if not fresh_page.live or not fresh_page.live_revision_id or getattr(fresh_page, "is_recycled", False):
                raise PermissionDenied("页面处于未发布或回收状态，无法发表评论。")

            from content_lifecycle.models import RecycleEntry
            ancestor_ids = list(fresh_page.get_ancestors().values_list("id", flat=True))
            check_ids = [fresh_page.id] + ancestor_ids
            if RecycleEntry.objects.filter(page_id__in=check_ids, status__in=["recycled", "restoring"]).exists():
                raise PermissionDenied("目标页面或其父级栏目处于回收站中，无法发表评论。")
            for anc in fresh_page.get_ancestors():
                if getattr(anc, "is_recycled", False):
                    raise PermissionDenied("目标页面的父级栏目处于回收站中，无法发表评论。")

            # 3. 事务内检查主体限制与双轨全局/单篇设置
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock_shared(hashtext('myblog:comments:community_settings'));"
                    )
                    gov_lock_keys: list[str] = []
                    if db_author is not None:
                        gov_lock_keys.append(f"myblog:comments:gov:user:user:{db_author.id}")
                    if ip_address:
                        gov_lock_keys.append(f"myblog:comments:gov:ip:ip:{ip_address}")
                    for gk in sorted(gov_lock_keys):
                        cursor.execute("SELECT pg_advisory_xact_lock_shared(hashtext(%s));", [gk])

            if cls.is_subject_restricted(author=db_author, ip_address=ip_address):
                raise ValidationError("您当前已被系统限制发表评论，请稍后再试。")

            community_settings = CommunitySettings.get_solo()
            if community_settings.comment_mode == "closed":
                raise PermissionDenied("全站评论功能当前已暂停，暂不接收新评论。")

            settings_obj = PageCommentSettings.objects.filter(page_id=fresh_page.id).first()
            if settings_obj and not settings_obj.allow_comments:
                close_msg = f"（说明：{settings_obj.close_reason}）" if settings_obj.close_reason else ""
                raise ValidationError(f"该页面已关闭评论互动{close_msg}。")

            if is_guest:
                if community_settings.comment_mode != "controlled_guests":
                    raise PermissionDenied("全站当前仅限登录会员发表评论，请先登录后参与讨论。")
                if not (settings_obj and settings_obj.allow_guest_comments):
                    raise PermissionDenied("当前文章仅限登录会员发表评论，暂未开放访客留言。")

                if connection.vendor == "postgresql":
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_xact_lock(hashtext('myblog:comments:guest_pending_budget'));"
                        )

                pending_total = Comment.objects.filter(pending_version__isnull=False).count()
                if pending_total >= int(community_settings.max_pending_queue_budget):
                    raise CommentQueueBudgetExceeded("全站待审留言队列繁忙，请稍后再试或登录会员发表评论。")

            # 4. 确定发评人档位并按档位清洗正文与检查链接规则
            if db_author is not None:
                db_author.is_superuser = author_is_superuser
                db_author.date_joined = author_date_joined
            tier = get_comment_user_tier(db_author, community_settings=community_settings)
            safe_html, external_links, risk_reasons = sanitize_comment_for_tier(raw_content, tier=tier)

            if is_guest:
                # 访客强制进入人工待审，严禁任何 auto_approve 绕过
                auto_approve = False
                if "guest_submission" not in risk_reasons:
                    risk_reasons.append("guest_submission")
            elif settings_obj and settings_obj.auto_moderate_new:
                auto_approve = False
            elif "external_links_threshold" in risk_reasons:
                auto_approve = False
            elif tier == "new_member" and community_settings.new_member_default_pending:
                if "new_member_default_pending" not in risk_reasons:
                    risk_reasons.append("new_member_default_pending")

            root_comment = None
            replied_to_user = None
            actual_reply_to = None

            if parent_id is not None:
                # 必须先以排他锁锁定根评论，校验不变量 (C-05)
                try:
                    root_comment = Comment.objects.select_for_update().get(id=parent_id)
                except Comment.DoesNotExist:
                    raise ValidationError("所回复的根评论不存在。")

                if root_comment.page_id != fresh_page.id:
                    raise ValidationError("不能跨页面回复评论！")
                if root_comment.parent_id is not None:
                    raise ValidationError("系统仅支持二级扁平回复，禁止递归嵌套！")
                if not root_comment.is_publicly_visible or root_comment.status != "active":
                    raise ValidationError("目标根评论已被删除或未公开，无法追加回复。")

                # 如果指定了具体回复的子回复对象，提取被回复人
                if reply_to_comment_id:
                    try:
                        actual_reply_to = Comment.objects.select_related("author").get(id=reply_to_comment_id)
                        if actual_reply_to.page_id != fresh_page.id:
                            raise ValidationError("回复的目标评论与当前页面不一致。")
                        if actual_reply_to.id != root_comment.id and actual_reply_to.parent_id != root_comment.id:
                            raise ValidationError("回复的目标评论不属于当前根评论树。")
                        replied_to_user = actual_reply_to.author if reply_user_alive else None
                    except Comment.DoesNotExist:
                        pass

            # 5. 在同事务内扣减 PostgreSQL 持久日配额（超级管理员免限；按字典序稳定加锁防交叉死锁）
            if not (db_author is not None and author_is_superuser):
                quota_site = site or _resolve_quota_site(fresh_page)
                quota_date = timezone.localdate()
                quota_targets: list[tuple[str, str, int, str]] = []
                if is_guest:
                    if guest_browser_key:
                        browser_quota_key = (
                            guest_browser_key
                            if guest_browser_key.startswith("guest:")
                            else f"guest:{guest_browser_key}"
                        )
                        quota_targets.append((
                            "guest_browser",
                            browser_quota_key,
                            int(community_settings.guest_daily_browser_cap),
                            f"今日访客浏览器留言已达上限（每日 {community_settings.guest_daily_browser_cap} 条），请明天再试或登录会员发表。",
                        ))
                    if ip_address:
                        quota_targets.append((
                            "ip",
                            f"ip:{ip_address}",
                            int(community_settings.guest_daily_ip_cap),
                            f"今日当前网络访客留言已达上限（每日 {community_settings.guest_daily_ip_cap} 条），请明天再试或登录会员发表。",
                        ))
                else:
                    member_cap = (
                        int(community_settings.new_member_daily_cap)
                        if tier == "new_member"
                        else int(community_settings.member_daily_cap)
                    )
                    quota_targets.append((
                        "user",
                        f"user:{db_author.id}",
                        member_cap,
                        f"您今日的评论次数已达上限（每日 {member_cap} 条），请明天再试。",
                    ))

                for q_kind, q_key, q_cap, q_msg in sorted(quota_targets, key=lambda x: (x[0], x[1])):
                    _consume_daily_quota_tx(
                        site_id=quota_site.id,
                        subject_kind=q_kind,
                        subject_key=q_key,
                        quota_date=quota_date,
                        action="post_comment",
                        cap=q_cap,
                        error_message=q_msg,
                    )

            # 创建 Comment 实例
            comment = Comment(
                page=fresh_page,
                page_document_uuid=getattr(fresh_page, "document_uuid", None),
                page_title_snapshot=getattr(fresh_page, "title", ""),
                author=db_author,
                author_subject_key=subject_key,
                guest_name=clean_guest_name,
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
                risk_reasons=risk_reasons,
            )
            comment.latest_version = version
            comment.pending_version = version
            comment.save(update_fields=["latest_version", "pending_version", "pending_submitted_at"])

            # 若满足自动审批策略（如超级管理员，且绝不适用于访客），即时审批生效
            if not is_guest and (auto_approve or (db_author is not None and author_is_superuser)):
                cls.moderate_comment(
                    comment_id=comment.id,
                    action="approve",
                    reviewer=db_author,
                    expected_version_id=version.id,
                )
                comment.refresh_from_db()

            if receipt is not None:
                receipt.result_comment_id = comment.id
                receipt.save(update_fields=["result_comment_id"])

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

        with transaction.atomic():
            author_is_superuser = False
            author_date_joined = None
            if author and getattr(author, "is_authenticated", False):
                author_is_superuser, author_date_joined = cls._lock_and_verify_active_user(author)
                author.is_superuser = author_is_superuser
                author.date_joined = author_date_joined

            c_simple = Comment.objects.get(id=comment_id)
            lock_ids = sorted(set([c_simple.id] + ([c_simple.parent_id] if c_simple.parent_id else [])))
            locked_map = {
                c.id: c
                for c in Comment.objects.filter(id__in=lock_ids).order_by("id").select_for_update()
            }
            comment = locked_map[comment_id]
            if comment.author_id != getattr(author, "id", None) and not author_is_superuser:
                raise PermissionDenied("仅原作者有权编辑本人评论。")
            if not comment.is_editable and not author_is_superuser:
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

            community_settings = CommunitySettings.get_solo()
            tier = get_comment_user_tier(author, community_settings=community_settings)
            safe_html, _, risk_reasons = sanitize_comment_for_tier(new_raw_content, tier=tier)

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
                risk_reasons=risk_reasons,
            )

            # D7-A: approved_version 保持原 V1 不变，pending_version 指向新 V2
            comment.latest_version = new_version
            comment.pending_version = new_version
            comment.pending_submitted_at = now
            comment.state_generation += 1
            comment.save(update_fields=["latest_version", "pending_version", "pending_submitted_at", "state_generation", "updated_at"])

            # 管理员本人编辑自动通过
            if author and author_is_superuser:
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
        *,
        require_expected_version: bool = False,
    ) -> Comment:
        """
        审核批准/驳回/隐藏评论。
        严格落实版本 CAS 校验 (C-03) 与 is_publicly_visible 状态跃迁计数 (C-04)。
        """
        now = timezone.now()

        with transaction.atomic():
            if reviewer and getattr(reviewer, "is_authenticated", False):
                cls._lock_and_verify_active_user(reviewer)

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

            if comment.status == "deleted":
                raise ValidationError("已软删除的评论禁止执行审核或状态变更。")

            # 2. 执行批准 (approve)
            if action == "approve":
                if not comment.pending_version_id:
                    raise ValidationError("当前没有待审核版本可批准，禁止重复批准或复活已处理评论。")
                if require_expected_version and not expected_version_id:
                    raise ValidationError("审核冲突：缺少待审版本预期版本号 (expected_version_id)。")
                target_ver_id = expected_version_id or comment.pending_version_id
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
                if require_expected_version and comment.pending_version_id and not expected_version_id:
                    raise ValidationError("驳回冲突：缺少待审版本预期版本号 (expected_version_id)。")
                target_ver_id = expected_version_id or comment.pending_version_id
                if expected_version_id and comment.pending_version_id != expected_version_id:
                    raise ValidationError("驳回冲突：该待审版本已被更新、批准或替换！")
                if target_ver_id:
                    target_ver = CommentVersion.objects.filter(id=target_ver_id, comment_id=comment.id).first()
                    if not target_ver:
                        raise ValidationError("待驳回的评论版本不存在。")
                    CommentVersion.objects.filter(id=target_ver.id, comment_id=comment.id).update(
                        status="rejected", reviewed_at=now, reviewed_by=reviewer
                    )
                    applied_version_number = target_ver.version_number
                comment.pending_version = None

                # 若该评论未曾有过已批准版本，整体置为 rejected
                if not comment.approved_version:
                    comment.status = "rejected"
                    comment.is_publicly_visible = False
                comment.state_generation += 1
                comment.save(update_fields=["pending_version", "status", "is_publicly_visible", "state_generation", "updated_at"])

            # 4. 执行隐藏/下架 (hide)
            elif action == "hide":
                CommentVersion.objects.filter(comment=comment, status="pending").update(status="superseded")
                comment.pending_version = None
                comment.pending_submitted_at = None
                comment.status = "rejected"
                comment.is_publicly_visible = False
                comment.is_pinned = False
                comment.state_generation += 1
                comment.save(update_fields=[
                    "pending_version", "pending_submitted_at", "status",
                    "is_publicly_visible", "is_pinned", "state_generation", "updated_at",
                ])

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
            if actor and getattr(actor, "is_authenticated", False):
                cls._lock_and_verify_active_user(actor)

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

            CommentVersion.objects.filter(comment=comment, status="pending").update(status="superseded")
            comment.pending_version = None
            comment.pending_submitted_at = None
            comment.status = "deleted"
            comment.deletion_reason = del_reason
            comment.deleted_at = now
            comment.deleted_by = actor if getattr(actor, "is_authenticated", False) else None
            comment.is_publicly_visible = False
            comment.is_pinned = False
            comment.state_generation += 1
            comment.save(update_fields=[
                "pending_version", "pending_submitted_at",
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
            if actor and getattr(actor, "is_authenticated", False):
                cls._lock_and_verify_active_user(actor)
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

    @classmethod
    def update_page_comment_settings(
        cls,
        *,
        page_id: int,
        actor,
        allow_comments: bool,
        close_reason: str = "",
        auto_moderate_new: bool = False,
        allow_guest_comments: bool | None = None,
    ) -> PageCommentSettings:
        """
        服务层更新单篇页面评论设置（P0-6 双重鉴权防伪造）。
        allow_guest_comments 为 None 时保持原值；非 None 时仅允许超管、全站治理员或该页主编修改。
        """
        page = Page.objects.filter(id=page_id).first()
        if page is None:
            raise ValidationError("目标页面不存在。")

        if not can_user_access_page_comment_settings(actor, page):
            raise PermissionDenied("无权修改该页面的评论设置。")

        if allow_guest_comments is not None and not can_user_manage_guest_comments(actor, page):
            raise PermissionDenied("仅超级管理员、全站评论治理员或栏目主编可修改受控访客留言开关。")

        clean_reason = (close_reason or "").strip()[:255]
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '3s';")
                    cursor.execute("SELECT id FROM wagtailcore_page WHERE id = %s FOR NO KEY UPDATE;", [page.id])

            try:
                with transaction.atomic():
                    settings_obj, created = PageCommentSettings.objects.select_for_update().get_or_create(
                        page_id=page.id,
                        defaults={
                            "allow_comments": bool(allow_comments),
                            "allow_guest_comments": bool(allow_guest_comments) if allow_guest_comments is not None else False,
                            "close_reason": clean_reason,
                            "auto_moderate_new": bool(auto_moderate_new),
                        },
                    )
            except IntegrityError:
                settings_obj = PageCommentSettings.objects.select_for_update().get(page_id=page.id)
                created = False

            if not created:
                settings_obj.allow_comments = bool(allow_comments)
                settings_obj.close_reason = clean_reason
                settings_obj.auto_moderate_new = bool(auto_moderate_new)
                if allow_guest_comments is not None:
                    settings_obj.allow_guest_comments = bool(allow_guest_comments)
                settings_obj.state_generation += 1
                settings_obj.save()

            return settings_obj

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
        cls._check_governance_permission(actor)
        if kind not in ("user", "ip"):
            raise ValidationError("非法的治理主体类型。")
        clean_key = (subject_key or "").strip()
        if not clean_key:
            raise ValidationError("主体标识不能为空。")
        now = timezone.now()
        target_until = now + timedelta(hours=hours)

        with transaction.atomic():
            if actor and getattr(actor, "is_authenticated", False):
                cls._lock_and_verify_active_user(actor)
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s));",
                        [f"myblog:comments:gov:{kind}:{clean_key}"],
                    )
            obj, created = CommentGovernanceSubject.objects.select_for_update().get_or_create(
                kind=kind,
                subject_key=clean_key,
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

            CommentGovernanceAuditLog.objects.create(
                kind=kind,
                subject_key=clean_key,
                action="restrict",
                actor=actor,
                actor_username_snapshot=getattr(actor, "username", "") or "",
                reason=(reason or "")[:255],
                restricted_until=obj.restricted_until,
            )
            return obj

    @classmethod
    def lift_restriction(cls, kind: str, subject_key: str, actor=None, reason: str = "管理后台提前解封") -> bool:
        """提前解除指定用户或IP的治理限制"""
        cls._check_governance_permission(actor)
        if kind not in ("user", "ip"):
            raise ValidationError("非法的治理主体类型。")
        clean_key = (subject_key or "").strip()
        now = timezone.now()
        with transaction.atomic():
            if actor and getattr(actor, "is_authenticated", False):
                cls._lock_and_verify_active_user(actor)
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s));",
                        [f"myblog:comments:gov:{kind}:{clean_key}"],
                    )
            sub = (
                CommentGovernanceSubject.objects.select_for_update()
                .filter(kind=kind, subject_key=clean_key, restricted_until__gt=now)
                .first()
            )
            if sub is None:
                return False
            sub.restricted_until = now
            sub.generation += 1
            sub.updated_by = actor
            sub.save(update_fields=["restricted_until", "generation", "updated_by", "updated_at"])
            CommentGovernanceAuditLog.objects.create(
                kind=kind,
                subject_key=clean_key,
                action="lift_restriction",
                actor=actor,
                actor_username_snapshot=getattr(actor, "username", "") or "",
                reason=(reason or "")[:255],
                restricted_until=now,
            )
            return True

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
