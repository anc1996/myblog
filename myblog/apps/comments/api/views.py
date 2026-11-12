"""
comments.api.views
全站通用评论 RESTful JSON API 视图集。

遵循规范：
1. GET /api/v1/pages/<page_id>/comments/：根评论分页列表，支持 hot/latest 排序；
2. GET /api/v1/comments/<root_id>/replies/：二级回复懒加载；
3. POST /api/v1/pages/<page_id>/comments/：发表评论/回复（honeypot 蜜罐、C-08 熔断限流）；
4. POST /api/v1/comments/<comment_id>/edit/：5 分钟编辑窗口提交新版本；
5. POST /api/v1/comments/<comment_id>/delete/：作者软删除；
6. POST /api/v1/comments/<comment_id>/react/：点赞/点踩互动切换。
"""

import hashlib
import json
from django.db.models import Q
from django.http import JsonResponse
from django.core.exceptions import PermissionDenied, ValidationError
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache

from captcha.service import read_existing_binding
from comments.models import (
    Comment,
    CommentVersion,
    CommentReaction,
    CommentMutationReceipt,
    CommunitySettings,
    PageCommentSettings,
)
from comments.access import get_accessible_comment_page
from comments.rate_limit import (
    check_comment_rate_limit,
    get_client_ip,
    is_member_comment_captcha_required,
)
from comments.services import (
    CommentConflictError,
    CommentQueueBudgetExceeded,
    CommentQuotaExceeded,
    CommentService,
)
from identity.challenges import (
    ChallengeServiceUnavailable,
    ChallengeVerificationError,
    get_challenge_verifier,
)


def _cors_json_response(data: dict, status: int = 200, *, no_store: bool = False) -> JsonResponse:
    resp = JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
    resp["Access-Control-Allow-Origin"] = "*"
    resp["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp["Access-Control-Allow-Headers"] = "Content-Type, X-CSRFToken"
    if no_store:
        resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp["Pragma"] = "no-cache"
    return resp


def _format_exc_message(exc: Exception) -> str:
    if hasattr(exc, "messages") and exc.messages:
        return "；".join(str(m) for m in exc.messages)
    return str(exc)


def _rate_limit_error_response(retry_after: int, err_code: str) -> JsonResponse:
    if err_code == "risk_control_unavailable":
        return _cors_json_response(
            {
                "status": "error",
                "code": "risk_control_unavailable",
                "message": "风控校验服务暂时不可用，请稍后重试。",
            },
            status=503,
            no_store=True,
        )
    resp = _cors_json_response(
        {
            "status": "error",
            "code": "rate_limited",
            "message": "操作过于频繁，请稍后重试。",
        },
        status=429,
        no_store=True,
    )
    if retry_after:
        resp["Retry-After"] = str(max(1, int(retry_after)))
    return resp


@require_http_methods(["GET", "OPTIONS"])
def list_comments_api(request, page_id: int):
    """获取指定页面的根评论列表（分页）"""
    if request.method == "OPTIONS":
        return _cors_json_response({})

    try:
        page, is_restricted = get_accessible_comment_page(request, page_id, for_write=False)
    except PermissionDenied as exc:
        return _cors_json_response({"status": "error", "code": 403, "message": str(exc)}, status=403)

    sort = request.GET.get("sort", "latest")
    page_str = request.GET.get("page", "1")
    page_num = int(page_str) if page_str.isdigit() and int(page_str) > 0 else 1
    page_size_str = request.GET.get("page_size", "10")
    page_size = max(1, min(int(page_size_str) if page_size_str.isdigit() else 10, 50))

    community = CommunitySettings.get_solo()
    settings_obj = PageCommentSettings.objects.filter(page=page).first()
    page_allow_comments = settings_obj.allow_comments if settings_obj else True
    allow_guest_comments = bool(settings_obj.allow_guest_comments) if settings_obj else False
    allow_comments = (community.comment_mode != "closed") and page_allow_comments
    effective_allow_guests = allow_comments and (community.comment_mode == "controlled_guests") and allow_guest_comments
    close_reason = (
        "全站评论功能当前已暂停维护"
        if community.comment_mode == "closed" and not (settings_obj and settings_obj.close_reason)
        else (settings_obj.close_reason if settings_obj else "")
    )

    is_auth = bool(request.user.is_authenticated)
    requires_login = allow_comments and (not is_auth) and (not effective_allow_guests)
    captcha_scope = "member_comment" if is_auth else ("guest_comment" if effective_allow_guests else "")
    if not allow_comments:
        requires_captcha = False
    elif not is_auth:
        requires_captcha = effective_allow_guests
    else:
        try:
            requires_captcha = is_member_comment_captcha_required(request, request.user)
        except ChallengeServiceUnavailable:
            requires_captcha = False

    # D6-A: 正常公开根评论 + 作者自删占位根评论（仅当有回复时）
    qs = Comment.objects.filter(
        Q(page=page, parent__isnull=True) & (
            (Q(status="active", is_publicly_visible=True))
            | (Q(status="deleted", deletion_reason="author", reply_count__gt=0))
        )
    )
    if sort == "hot":
        qs = qs.order_by("-is_pinned", "-like_count", "-created_at", "-id")
    else:
        qs = qs.order_by("-is_pinned", "-created_at", "-id")

    total_count = qs.count()
    offset = (page_num - 1) * page_size
    comments = qs.select_related("author", "author__member_profile")[offset : offset + page_size]

    # 预加载当前批次生效的版本 HTML
    ver_ids = [c.approved_version_id for c in comments if c.approved_version_id]
    ver_map = {
        v.id: v.safe_html
        for v in CommentVersion.objects.filter(id__in=ver_ids)
    }

    # 若当前用户已登录，批量查询其点赞状态
    user_reactions = {}
    if request.user.is_authenticated:
        c_ids = [c.id for c in comments]
        user_reactions = dict(
            CommentReaction.objects.filter(comment_id__in=c_ids, user=request.user).values_list("comment_id", "reaction_type")
        )

    items = []
    for c in comments:
        if c.status == "deleted" and c.deletion_reason == "author":
            # D6-A: 作者自删根占位条
            items.append({
                "id": c.id,
                "kind": "author_deleted",
                "message": "该评论已由作者自行删除",
                "author": {"id": None, "username": "（已删除）"},
                "safe_html": "",
                "like_count": 0,
                "dislike_count": 0,
                "reply_count": c.reply_count,
                "user_reaction": 0,
                "created_at": c.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "is_editable": False,
                "is_pinned": False,
                "can_delete": False,
                "can_react": False,
                "state_generation": c.state_generation,
            })
        else:
            items.append({
                "id": c.id,
                "kind": "comment",
                "author": c.get_author_presentation(),
                "safe_html": ver_map.get(c.approved_version_id, "（内容已归档）"),
                "like_count": c.like_count,
                "dislike_count": c.dislike_count,
                "reply_count": c.reply_count,
                "user_reaction": user_reactions.get(c.id, 0),
                "created_at": c.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "is_editable": c.is_editable if (request.user.is_authenticated and c.author_id == request.user.id) else False,
                "is_pinned": c.is_pinned,
                "can_delete": bool(request.user.is_authenticated and c.author_id == request.user.id and c.status != "deleted"),
                "can_react": bool(request.user.is_authenticated and c.author_id != request.user.id and allow_comments),
                "state_generation": c.state_generation,
            })

    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
    resp = _cors_json_response({
        "status": "success",
        "total_count": total_count,
        "current_page": page_num,
        "page_size": page_size,
        "total_pages": total_pages,
        "comment_settings": {
            "allow_comments": allow_comments,
            "allow_guest_comments": allow_guest_comments,
            "effective_allow_guests": effective_allow_guests,
            "comment_mode": community.comment_mode,
            "requires_login": requires_login,
            "requires_captcha": requires_captcha,
            "captcha_scope": captcha_scope,
            "close_reason": close_reason,
        },
        "items": items,
    })
    if is_restricted:
        resp["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
    return resp


@require_http_methods(["GET", "OPTIONS"])
def list_replies_api(request, root_id: int):
    """获取根评论下的二级回复列表"""
    if request.method == "OPTIONS":
        return _cors_json_response({})

    root = Comment.objects.filter(
        Q(id=root_id, parent__isnull=True) & (
            Q(is_publicly_visible=True)
            | Q(status="deleted", deletion_reason="author", reply_count__gt=0)
        )
    ).first()
    if not root or not root.page_id:
        return _cors_json_response({"status": "error", "code": 404, "message": "根评论不存在或已被隐藏"}, status=404)

    try:
        page, is_restricted = get_accessible_comment_page(request, root.page_id, for_write=False)
    except PermissionDenied as exc:
        return _cors_json_response({"status": "error", "code": 403, "message": str(exc)}, status=403)

    community = CommunitySettings.get_solo()
    settings_obj = PageCommentSettings.objects.filter(page=page).first()
    allow_comments = (community.comment_mode != "closed") and (settings_obj.allow_comments if settings_obj else True)

    replies = Comment.objects.filter(
        parent=root,
        is_publicly_visible=True,
    ).select_related(
        "author",
        "author__member_profile",
        "replied_to_user",
        "replied_to_user__member_profile",
    ).order_by("created_at")

    ver_ids = [r.approved_version_id for r in replies if r.approved_version_id]
    ver_map = {
        v.id: v.safe_html
        for v in CommentVersion.objects.filter(id__in=ver_ids)
    }

    user_reactions = {}
    if request.user.is_authenticated:
        r_ids = [r.id for r in replies]
        user_reactions = dict(
            CommentReaction.objects.filter(comment_id__in=r_ids, user=request.user).values_list("comment_id", "reaction_type")
        )

    items = []
    for r in replies:
        replied_to_data = None
        if r.replied_to_user_id and r.replied_to_user is not None:
            replied_profile = getattr(r.replied_to_user, "member_profile", None)
            replied_to_data = {
                "id": r.replied_to_user_id,
                "username": r.replied_to_user.username,
                "display_name": (
                    replied_profile.display_name
                    if replied_profile
                    else r.replied_to_user.username
                ),
            }

        items.append({
            "id": r.id,
            "root_id": root.id,
            "author": r.get_author_presentation(),
            "replied_to_user": replied_to_data,
            "safe_html": ver_map.get(r.approved_version_id, "（内容已归档）"),
            "like_count": r.like_count,
            "dislike_count": r.dislike_count,
            "user_reaction": user_reactions.get(r.id, 0),
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "is_editable": r.is_editable if (request.user.is_authenticated and r.author_id == request.user.id) else False,
            "can_delete": bool(request.user.is_authenticated and r.author_id == request.user.id and r.status != "deleted"),
            "can_react": bool(request.user.is_authenticated and r.author_id != request.user.id and allow_comments),
            "state_generation": r.state_generation,
        })

    resp = _cors_json_response({
        "status": "success",
        "root_id": root_id,
        "reply_count": len(items),
        "items": items,
    })
    if is_restricted:
        resp["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
    return resp


@never_cache
@require_http_methods(["POST"])
def post_comment_api(request, page_id: int):
    """发表新评论或回复（双轨准入、先核销验证码后进 SQL 事务、Redis Lua 限流与日配额）"""
    # 1. 蜜罐静默防御 (Honeypot)
    if request.POST.get("contact_website"):
        return _cors_json_response({"status": "success", "message": "评论已提交审核"}, no_store=True)

    # 2. 校验页面访问资格与双轨模式前置检查
    try:
        page, _ = get_accessible_comment_page(request, page_id, for_write=False)
    except PermissionDenied as exc:
        return _cors_json_response({"status": "error", "code": 403, "message": str(exc)}, status=403, no_store=True)

    community = CommunitySettings.get_solo()
    if community.comment_mode == "closed":
        return _cors_json_response(
            {"status": "error", "code": "comments_closed", "message": "全站评论功能当前已暂停。"},
            status=403,
            no_store=True,
        )
    settings_obj = PageCommentSettings.objects.filter(page=page).first()
    if settings_obj and not settings_obj.allow_comments:
        close_msg = f"（{settings_obj.close_reason}）" if settings_obj.close_reason else ""
        return _cors_json_response(
            {"status": "error", "code": "page_comments_closed", "message": f"该页面已关闭评论互动{close_msg}"},
            status=403,
            no_store=True,
        )

    author = request.user if request.user.is_authenticated else None
    guest_name = (request.POST.get("guest_name") or request.POST.get("author_name") or "").strip()
    raw_content = request.POST.get("content", "").strip()
    captcha_id = (request.POST.get("captcha_id") or "").strip()
    captcha_code = (request.POST.get("captcha_code") or "").strip()
    parent_id = request.POST.get("parent_id")
    reply_to_id = request.POST.get("reply_to_comment_id")
    request_key = (request.POST.get("request_key") or "").strip()

    pid = int(parent_id) if parent_id and parent_id.isdigit() else None
    rid = int(reply_to_id) if reply_to_id and reply_to_id.isdigit() else None

    # 2.5 若已登录会员携带了幂等键，在消耗限流或验证码之前先核验是否属于已提交请求的端到端重放
    if author is not None and request_key:
        if len(request_key) > 64:
            return _cors_json_response(
                {"status": "error", "code": 400, "message": "请求幂等键长度超出限制。"},
                status=400,
                no_store=True,
            )
        canonical_payload = json.dumps(
            [int(page.id), pid, rid, guest_name, raw_content],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        content_digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        receipt = CommentMutationReceipt.objects.filter(
            subject_key=f"user:{author.id}",
            action="post_comment",
            request_key=request_key,
        ).first()
        if receipt is not None:
            if receipt.request_hash != content_digest:
                return _cors_json_response(
                    {
                        "status": "error",
                        "code": "idempotency_conflict",
                        "message": "幂等键冲突：相同 request_key 不允许提交不同的评论内容。",
                    },
                    status=409,
                    no_store=True,
                )
            if receipt.result_comment_id:
                existing = Comment.objects.filter(id=receipt.result_comment_id).first()
                if existing is not None:
                    return _cors_json_response(
                        {
                            "status": "success",
                            "idempotent_replay": True,
                            "data": {
                                "id": existing.id,
                                "status": existing.status,
                                "is_publicly_visible": existing.is_publicly_visible,
                                "can_edit_until": (
                                    existing.can_edit_until.strftime("%Y-%m-%d %H:%M:%S")
                                    if existing.can_edit_until
                                    else None
                                ),
                            },
                        },
                        status=201,
                        no_store=True,
                    )

    guest_browser_key = None
    if author is None:
        if community.comment_mode != "controlled_guests":
            return _cors_json_response(
                {
                    "status": "error",
                    "code": "login_required",
                    "message": "全站当前仅限登录会员发表评论，请先登录后参与讨论。",
                },
                status=403,
                no_store=True,
            )
        if not (settings_obj and settings_obj.allow_guest_comments):
            return _cors_json_response(
                {
                    "status": "error",
                    "code": "guest_comments_disabled",
                    "message": "当前文章仅限登录会员发表评论，暂未开放访客留言。",
                },
                status=403,
                no_store=True,
            )
        binding_token = read_existing_binding(request)
        if not binding_token:
            return _cors_json_response(
                {
                    "status": "error",
                    "code": "captcha_binding_missing",
                    "captcha_scope": "guest_comment",
                    "message": "缺少安全验证会话 Cookie，请刷新验证码后重试。",
                },
                status=400,
                no_store=True,
            )
        binding_hash = hashlib.sha256(binding_token.encode("utf-8")).hexdigest()[:32]
        guest_browser_key = f"guest:{binding_hash}"
        if not guest_name:
            return _cors_json_response(
                {
                    "status": "error",
                    "code": "guest_name_required",
                    "message": "请填写您的昵称后再发表评论。",
                },
                status=400,
                no_store=True,
            )

    # 3. 预检防泛洪频次（避免无效验证码恶意爆刷）
    allowed_attempt, retry_att, att_err = check_comment_rate_limit(
        request, author, action="post_attempt", guest_browser_key=guest_browser_key
    )
    if not allowed_attempt:
        return _rate_limit_error_response(retry_att, att_err)

    # 4. 在进入 SQL 事务之前原子核销验证码（防止 SQL 回滚导致验证码可重放）
    if author is None:
        needs_captcha = True
        expected_scope = "guest_comment"
    else:
        expected_scope = "member_comment"
        # 若未提供验证码，交由下方单次 Redis Lua 原子判断是否触发外链/高频，消除并发窗口穿透；
        # 若已提供验证码，则在加限流槽前先原子核销该验证码。
        needs_captcha = bool(captcha_id or captcha_code)

    if needs_captcha:
        if not captcha_id or not captcha_code:
            return _cors_json_response(
                {
                    "status": "error",
                    "code": "captcha_required",
                    "captcha_scope": expected_scope,
                    "message": "请输入图片安全验证码后再提交评论。",
                },
                status=400,
                no_store=True,
            )
        try:
            verifier = get_challenge_verifier()
            if verifier is None:
                raise ChallengeServiceUnavailable("安全校验服务未就绪，请稍后再试。")
            verifier.verify_and_consume(
                request=request,
                expected_scope=expected_scope,
                captcha_id=captcha_id,
                code=captcha_code,
            )
        except ChallengeServiceUnavailable:
            return _cors_json_response(
                {
                    "status": "error",
                    "code": "captcha_unavailable",
                    "message": "安全校验服务暂时不可用，请稍后再试。",
                },
                status=503,
                no_store=True,
            )
        except ChallengeVerificationError as exc:
            return _cors_json_response(
                {
                    "status": "error",
                    "code": "captcha_invalid",
                    "captcha_scope": expected_scope,
                    "message": str(exc) or "安全验证码输入错误或已失效，请刷新后重试。",
                },
                status=400,
                no_store=True,
            )

    # 5. C-08 Redis Lua 原子滑动窗口流控与会员高频信号原子判定（单次 Lua 消除穿透）
    allowed, retry_after, err_code = check_comment_rate_limit(
        request,
        author,
        action="post",
        guest_browser_key=guest_browser_key,
        has_captcha=bool(needs_captcha),
        raw_content=raw_content,
    )
    if not allowed:
        if err_code == "captcha_required":
            return _cors_json_response(
                {
                    "status": "error",
                    "code": "captcha_required",
                    "captcha_scope": "member_comment",
                    "message": "检测到连续发评或包含链接，请完成人机验证码后继续提交。",
                },
                status=400,
                no_store=True,
            )
        return _rate_limit_error_response(retry_after, err_code)

    ip = get_client_ip(request)
    ua = request.META.get("HTTP_USER_AGENT", "")

    try:
        comment = CommentService.post_comment(
            page=page,
            author=author,
            guest_name=guest_name,
            raw_content=raw_content,
            parent_id=pid,
            reply_to_comment_id=rid,
            ip_address=ip,
            user_agent=ua,
            request_key=request_key,
            guest_browser_key=guest_browser_key,
            require_guest_browser=(author is None),
        )
        return _cors_json_response({
            "status": "success",
            "data": {
                "id": comment.id,
                "status": comment.status,
                "is_publicly_visible": comment.is_publicly_visible,
                "can_edit_until": comment.can_edit_until.strftime("%Y-%m-%d %H:%M:%S") if comment.can_edit_until else None,
            },
        }, status=201, no_store=True)
    except CommentConflictError as exc:
        return _cors_json_response(
            {"status": "error", "code": exc.error_code, "message": _format_exc_message(exc)},
            status=409,
            no_store=True,
        )
    except (CommentQuotaExceeded, CommentQueueBudgetExceeded) as exc:
        return _cors_json_response(
            {"status": "error", "code": exc.error_code, "message": _format_exc_message(exc)},
            status=429,
            no_store=True,
        )
    except PermissionDenied as exc:
        return _cors_json_response(
            {"status": "error", "code": 403, "message": _format_exc_message(exc)},
            status=403,
            no_store=True,
        )
    except ValidationError as exc:
        return _cors_json_response(
            {"status": "error", "code": 400, "message": _format_exc_message(exc)},
            status=400,
            no_store=True,
        )


@never_cache
@require_http_methods(["POST"])
def edit_comment_api(request, comment_id: int):
    """5 分钟安全编辑窗口内提交新版本"""
    if not request.user.is_authenticated:
        return _cors_json_response({"status": "error", "code": 401, "message": "请登录后再编辑评论"}, status=401, no_store=True)

    allowed, retry_after, err_code = check_comment_rate_limit(request, request.user, action="edit")
    if not allowed:
        return _rate_limit_error_response(retry_after, err_code)

    new_content = request.POST.get("content", "").strip()
    expected_version_id = request.POST.get("expected_version_id")
    expected_generation = request.POST.get("expected_generation")
    exp_ver = int(expected_version_id) if expected_version_id and expected_version_id.isdigit() else None
    exp_gen = int(expected_generation) if expected_generation and expected_generation.isdigit() else None
    ip = get_client_ip(request)

    try:
        ver = CommentService.edit_comment(
            comment_id,
            request.user,
            new_content,
            expected_version_id=exp_ver,
            expected_generation=exp_gen,
            ip_address=ip,
        )
        return _cors_json_response({
            "status": "success",
            "data": {
                "comment_id": comment_id,
                "version_number": ver.version_number,
                "status": ver.status,
            },
        }, no_store=True)
    except PermissionDenied as exc:
        return _cors_json_response({"status": "error", "code": 403, "message": _format_exc_message(exc)}, status=403, no_store=True)
    except ValidationError as exc:
        return _cors_json_response({"status": "error", "code": 400, "message": _format_exc_message(exc)}, status=400, no_store=True)


@never_cache
@require_http_methods(["POST"])
def delete_comment_api(request, comment_id: int):
    """软删除评论"""
    if not request.user.is_authenticated:
        return _cors_json_response({"status": "error", "code": 401, "message": "请登录后再操作"}, status=401, no_store=True)

    allowed, retry_after, err_code = check_comment_rate_limit(request, request.user, action="delete")
    if not allowed:
        return _rate_limit_error_response(retry_after, err_code)

    try:
        comment = CommentService.soft_delete_comment(
            comment_id,
            request.user,
            reason="author",
            ip_address=get_client_ip(request),
        )
        return _cors_json_response({
            "status": "success",
            "message": "评论已成功下架删除",
            "comment_id": comment.id,
        }, no_store=True)
    except PermissionDenied as exc:
        return _cors_json_response({"status": "error", "code": 403, "message": _format_exc_message(exc)}, status=403, no_store=True)
    except ValidationError as exc:
        return _cors_json_response({"status": "error", "code": 400, "message": _format_exc_message(exc)}, status=400, no_store=True)


@never_cache
@require_http_methods(["POST"])
def react_comment_api(request, comment_id: int):
    """点赞/点踩互动切换"""
    if not request.user.is_authenticated:
        return _cors_json_response({"status": "error", "code": 401, "message": "请登录后再参与互动"}, status=401, no_store=True)

    allowed, retry_after, err_code = check_comment_rate_limit(request, request.user, action="react")
    if not allowed:
        return _rate_limit_error_response(retry_after, err_code)

    reaction_val = request.POST.get("reaction") or request.POST.get("reaction_type")
    try:
        r_type = int(reaction_val)
    except (TypeError, ValueError):
        return _cors_json_response({"status": "error", "code": 400, "message": "非法的互动参数"}, status=400, no_store=True)

    try:
        res = CommentService.react_comment(comment_id, request.user, r_type)
        return _cors_json_response({
            "status": "success",
            "data": res,
        }, no_store=True)
    except PermissionDenied as exc:
        return _cors_json_response({"status": "error", "code": 403, "message": _format_exc_message(exc)}, status=403, no_store=True)
    except ValidationError as exc:
        return _cors_json_response({"status": "error", "code": 400, "message": _format_exc_message(exc)}, status=400, no_store=True)
