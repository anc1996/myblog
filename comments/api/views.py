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

import json
from django.http import JsonResponse
from django.core.exceptions import PermissionDenied, ValidationError
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache

from comments.models import Comment, CommentVersion, CommentReaction
from comments.access import get_accessible_comment_page
from comments.rate_limit import check_comment_rate_limit, get_client_ip
from comments.services import CommentService


def _cors_json_response(data: dict, status: int = 200) -> JsonResponse:
    resp = JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
    resp["Access-Control-Allow-Origin"] = "*"
    resp["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp["Access-Control-Allow-Headers"] = "Content-Type, X-CSRFToken"
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

    qs = Comment.objects.filter(
        page=page,
        parent__isnull=True,
        is_publicly_visible=True,
    )
    if sort == "hot":
        qs = qs.order_by("-like_count", "-created_at")
    else:
        qs = qs.order_by("-created_at")

    total_count = qs.count()
    offset = (page_num - 1) * page_size
    comments = qs.select_related("author")[offset : offset + page_size]

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
        items.append({
            "id": c.id,
            "author": {
                "id": c.author_id,
                "username": c.author.username if c.author else "匿名/已注销",
            },
            "safe_html": ver_map.get(c.approved_version_id, "（内容已归档）"),
            "like_count": c.like_count,
            "dislike_count": c.dislike_count,
            "reply_count": c.reply_count,
            "user_reaction": user_reactions.get(c.id, 0),
            "created_at": c.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "is_editable": c.is_editable if (request.user.is_authenticated and c.author_id == request.user.id) else False,
        })

    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
    resp = _cors_json_response({
        "status": "success",
        "total_count": total_count,
        "current_page": page_num,
        "page_size": page_size,
        "total_pages": total_pages,
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

    root = Comment.objects.filter(id=root_id, parent__isnull=True, is_publicly_visible=True).first()
    if not root or not root.page_id:
        return _cors_json_response({"status": "error", "code": 404, "message": "根评论不存在或已被隐藏"}, status=404)

    try:
        page, is_restricted = get_accessible_comment_page(request, root.page_id, for_write=False)
    except PermissionDenied as exc:
        return _cors_json_response({"status": "error", "code": 403, "message": str(exc)}, status=403)

    replies = Comment.objects.filter(
        parent=root,
        is_publicly_visible=True,
    ).select_related("author", "replied_to_user").order_by("created_at")

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
        items.append({
            "id": r.id,
            "author": {
                "id": r.author_id,
                "username": r.author.username if r.author else "匿名/已注销",
            },
            "replied_to_user": {
                "id": r.replied_to_user_id,
                "username": r.replied_to_user.username if r.replied_to_user else None,
            } if r.replied_to_user_id else None,
            "safe_html": ver_map.get(r.approved_version_id, "（内容已归档）"),
            "like_count": r.like_count,
            "dislike_count": r.dislike_count,
            "user_reaction": user_reactions.get(r.id, 0),
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "is_editable": r.is_editable if (request.user.is_authenticated and r.author_id == request.user.id) else False,
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


@require_http_methods(["POST"])
def post_comment_api(request, page_id: int):
    """发表新评论或回复"""
    if not request.user.is_authenticated:
        return _cors_json_response({"status": "error", "code": 401, "message": "请登录后再发表评论"}, status=401)

    # 1. 蜜罐静默防御 (Honeypot)
    if request.POST.get("contact_website"):
        return _cors_json_response({"status": "success", "message": "评论已提交审核"})

    # 2. C-08 滑动窗口流控与 Fail-Closed 熔断
    allowed, retry_after, err_code = check_comment_rate_limit(request, request.user, action="post")
    if not allowed:
        code = 503 if err_code == "risk_control_unavailable" else 429
        resp = _cors_json_response({"status": "error", "code": code, "message": "操作过于频繁，请稍后重试。"}, status=code)
        if retry_after:
            resp["Retry-After"] = str(retry_after)
        return resp

    # 3. 校验页面访问资格与行级共享锁 (C-01 & C-07)
    try:
        page, _ = get_accessible_comment_page(request, page_id, for_write=True)
    except PermissionDenied as exc:
        return _cors_json_response({"status": "error", "code": 403, "message": str(exc)}, status=403)

    raw_content = request.POST.get("content", "").strip()
    parent_id = request.POST.get("parent_id")
    reply_to_id = request.POST.get("reply_to_comment_id")

    pid = int(parent_id) if parent_id and parent_id.isdigit() else None
    rid = int(reply_to_id) if reply_to_id and reply_to_id.isdigit() else None

    ip = get_client_ip(request)
    ua = request.META.get("HTTP_USER_AGENT", "")

    try:
        comment = CommentService.post_comment(
            page=page,
            author=request.user,
            raw_content=raw_content,
            parent_id=pid,
            reply_to_comment_id=rid,
            ip_address=ip,
            user_agent=ua,
        )
        return _cors_json_response({
            "status": "success",
            "data": {
                "id": comment.id,
                "status": comment.status,
                "is_publicly_visible": comment.is_publicly_visible,
                "can_edit_until": comment.can_edit_until.strftime("%Y-%m-%d %H:%M:%S") if comment.can_edit_until else None,
            },
        }, status=201)
    except (ValidationError, PermissionDenied) as exc:
        return _cors_json_response({"status": "error", "code": 400, "message": str(exc)}, status=400)


@require_http_methods(["POST"])
def edit_comment_api(request, comment_id: int):
    """5 分钟安全编辑窗口内提交新版本"""
    if not request.user.is_authenticated:
        return _cors_json_response({"status": "error", "code": 401, "message": "请登录后再编辑评论"}, status=401)

    new_content = request.POST.get("content", "").strip()
    try:
        ver = CommentService.edit_comment(comment_id, request.user, new_content)
        return _cors_json_response({
            "status": "success",
            "data": {
                "comment_id": comment_id,
                "version_number": ver.version_number,
                "status": ver.status,
            },
        })
    except (ValidationError, PermissionDenied) as exc:
        return _cors_json_response({"status": "error", "code": 400, "message": str(exc)}, status=400)


@require_http_methods(["POST"])
def delete_comment_api(request, comment_id: int):
    """软删除评论"""
    if not request.user.is_authenticated:
        return _cors_json_response({"status": "error", "code": 401, "message": "请登录后再操作"}, status=401)

    try:
        comment = CommentService.soft_delete_comment(comment_id, request.user)
        return _cors_json_response({
            "status": "success",
            "message": "评论已成功下架删除",
            "comment_id": comment.id,
        })
    except (ValidationError, PermissionDenied) as exc:
        return _cors_json_response({"status": "error", "code": 400, "message": str(exc)}, status=400)


@require_http_methods(["POST"])
def react_comment_api(request, comment_id: int):
    """点赞/点踩互动切换"""
    if not request.user.is_authenticated:
        return _cors_json_response({"status": "error", "code": 401, "message": "请登录后再参与互动"}, status=401)

    reaction_val = request.POST.get("reaction")
    try:
        r_type = int(reaction_val)
    except (TypeError, ValueError):
        return _cors_json_response({"status": "error", "code": 400, "message": "非法的互动参数"}, status=400)

    try:
        res = CommentService.react_comment(comment_id, request.user, r_type)
        return _cors_json_response({
            "status": "success",
            "data": res,
        })
    except (ValidationError, PermissionDenied) as exc:
        return _cors_json_response({"status": "error", "code": 400, "message": str(exc)}, status=400)
