"""
analytics.views
第 1 期公开分析上下文签发与异步事件上报接口。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods
from urllib.parse import urlsplit
from wagtail.models import Page, Site

from analytics.collector import (
    is_bot_or_prefetch_request,
    issue_analytics_context,
    issue_reading_proof_for_visit,
    record_analytics_events,
)
from analytics.models import ArticleRating
from analytics.rating_service import (
    RatingConflictError,
    RatingError,
    RatingPermissionError,
    _is_page_public_for_analytics,
    check_rating_rate_limit,
    get_rating_state,
    submit_rating,
)
from analytics.tokens import (
    ANALYTICS_COOKIE_MAX_AGE,
    ANALYTICS_COOKIE_NAME,
    verify_visit_token,
)

logger = logging.getLogger(__name__)

_MAX_PAYLOAD_BYTES = 64 * 1024


def _parse_json_payload(request) -> dict[str, Any] | None:
    raw_body = request.body or b""
    if len(raw_body) > _MAX_PAYLOAD_BYTES:
        return None
    if not raw_body:
        return {}
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _no_store_json(data: dict[str, Any], status: int = 200) -> JsonResponse:
    resp = JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
    resp["Cache-Control"] = "private, no-store"
    return resp


def _is_same_origin(request) -> bool:
    origin = (request.META.get("HTTP_ORIGIN") or "").strip()
    if not origin or origin == "null":
        return True
    try:
        origin_host = urlsplit(origin).netloc.lower()
        req_host = (request.get_host() or "").lower()
        return bool(origin_host and req_host and origin_host == req_host)
    except Exception:
        return False


@csrf_exempt
@never_cache
@require_POST
def analytics_context_view(request):
    """POST /api/v1/analytics/context/：核验公开上下文并签发 30 分钟阅读令牌（只读不写 SQL）。"""
    if not _is_same_origin(request):
        return _no_store_json({"enabled": False, "reason": "cross_origin_forbidden"}, status=403)

    payload = _parse_json_payload(request)
    if payload is None:
        return _no_store_json({"enabled": False, "reason": "invalid_payload"}, status=400)

    body, new_cookie_val = issue_analytics_context(request, payload)
    resp = _no_store_json(body, status=200)
    if new_cookie_val:
        resp.set_cookie(
            ANALYTICS_COOKIE_NAME,
            new_cookie_val,
            max_age=ANALYTICS_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
            secure=request.is_secure(),
        )
    return resp


@csrf_exempt
@never_cache
@require_POST
def analytics_events_view(request):
    """
    POST /api/v1/analytics/events/：接收 JSON 与 sendBeacon (text/plain) 上报。
    Redis 故障时优雅返回 202 {"accepted": False, "degraded": True}，绝不抛 500 或回退写 SQL。
    """
    if not _is_same_origin(request):
        return _no_store_json({"accepted": False, "reason": "cross_origin_forbidden"}, status=403)

    if is_bot_or_prefetch_request(request):
        return _no_store_json({"accepted": False, "reason": "bot_or_prefetch"}, status=202)

    payload = _parse_json_payload(request)
    if payload is None:
        return _no_store_json({"accepted": False, "reason": "invalid_payload"}, status=400)

    visit_token = payload.get("visit_token") or ""
    visit_claims = verify_visit_token(visit_token)
    if not visit_claims:
        return _no_store_json({"accepted": False, "reason": "invalid_visit_token"}, status=400)

    raw_events = payload.get("events")
    if isinstance(raw_events, list):
        events = [ev for ev in raw_events[:20] if isinstance(ev, dict)]
    else:
        events = [payload]

    if not events:
        return _no_store_json({"accepted": False, "reason": "empty_events"}, status=400)

    result = record_analytics_events(visit_claims, events)
    if result.get("degraded"):
        return _no_store_json(result, status=202)
    return _no_store_json(result, status=200)


@never_cache
@require_http_methods(["GET", "POST"])
def page_rating_view(request, page_id: int):
    """
    GET /api/v1/analytics/pages/<page_id>/rating/: 只读回显登录用户本人对该页的评价状态；
    POST /api/v1/analytics/pages/<page_id>/rating/: 提交、切换或撤销四档文章评价（含独立限流与幂等）。
    """
    if not _is_same_origin(request):
        return _no_store_json(
            {"ok": False, "error": "cross_origin_forbidden", "message": "禁止跨源请求。"},
            status=403,
        )

    if not request.user or not request.user.is_authenticated:
        return _no_store_json(
            {"ok": False, "error": "authentication_required", "message": "请先登录后再参与文章评价。"},
            status=401,
        )
    if not getattr(request.user, "is_active", True):
        return _no_store_json(
            {"ok": False, "error": "account_inactive", "message": "当前账号状态不可用。"},
            status=403,
        )

    req_site = Site.find_for_request(request)

    if request.method == "GET":
        # 确保浏览器后续 POST 评价时已持有 csrftoken Cookie
        get_token(request)
        page = Page.objects.filter(id=page_id).select_related("live_revision", "content_type").first()
        if page is None:
            return _no_store_json({"ok": False, "error": "page_not_found", "message": "页面不存在。"}, status=404)

        is_public = _is_page_public_for_analytics(page)
        if req_site is not None:
            page_site = page.get_site() if hasattr(page, "get_site") else None
            if page_site is not None and int(page_site.id) != int(req_site.id):
                is_public = False

        if not is_public:
            has_own_rating = ArticleRating.objects.filter(page_id=page.id, user_id=request.user.id).exists()
            if not has_own_rating:
                return _no_store_json({"ok": False, "error": "page_not_found", "message": "页面不存在。"}, status=404)

        state = get_rating_state(request.user, page, site=req_site)
        return _no_store_json(state, status=200)

    # POST 写评价：解析载荷 -> 独立 Redis Lua 限流（Fail-Closed 503） -> 强一致提交
    payload = _parse_json_payload(request)
    if payload is None:
        return _no_store_json({"ok": False, "error": "invalid_payload", "message": "请求 JSON 格式无效。"}, status=400)

    allowed, retry_after, err_code = check_rating_rate_limit(request, request.user, page_id)
    if not allowed:
        if err_code == "rate_limit_unavailable":
            return _no_store_json(
                {"ok": False, "error": "rate_limit_unavailable", "message": "评价风控服务暂不可用，请稍后再试。"},
                status=503,
            )
        resp = _no_store_json(
            {"ok": False, "error": "rate_limited", "message": "评价操作过于频繁，请稍后再试。"},
            status=429,
        )
        if retry_after:
            resp["Retry-After"] = str(max(1, int(retry_after)))
        return resp

    try:
        result = submit_rating(
            request.user,
            page_id,
            target_grade=payload.get("target_grade"),
            expected_revision_id=payload.get("expected_revision_id", 0),
            expected_version=payload.get("expected_version", 0),
            request_id=payload.get("request_id", ""),
            reading_proof=payload.get("reading_proof", ""),
            site=req_site,
        )
        return _no_store_json(result, status=200)
    except RatingConflictError as exc:
        return _no_store_json(
            {"ok": False, "error": exc.error_code, "message": str(exc.message if hasattr(exc, "message") else exc)},
            status=409,
        )
    except RatingPermissionError as exc:
        return _no_store_json(
            {"ok": False, "error": exc.error_code, "message": str(exc)},
            status=getattr(exc, "http_status", 403),
        )
    except RatingError as exc:
        return _no_store_json(
            {"ok": False, "error": exc.error_code, "message": str(exc.message if hasattr(exc, "message") else exc)},
            status=getattr(exc, "http_status", 400),
        )


@never_cache
@require_POST
def reading_proof_view(request, page_id: int):
    """
    POST /api/v1/analytics/pages/<page_id>/reading-proof/：
    核验 visit_token 与 Redis 阅读进度门槛后，签发 10 分钟有效的 reading_proof。
    """
    if not _is_same_origin(request):
        return _no_store_json(
            {"eligible": False, "reason": "cross_origin_forbidden"},
            status=403,
        )

    if not request.user or not request.user.is_authenticated:
        return _no_store_json(
            {"eligible": False, "reason": "authentication_required"},
            status=401,
        )
    if not getattr(request.user, "is_active", True):
        return _no_store_json(
            {"eligible": False, "reason": "account_inactive"},
            status=403,
        )

    payload = _parse_json_payload(request)
    if payload is None:
        return _no_store_json({"eligible": False, "reason": "invalid_payload"}, status=400)

    visit_token = str(payload.get("visit_token") or "")
    is_short_body = bool(payload.get("is_short_body", False))
    req_site = Site.find_for_request(request)

    result = issue_reading_proof_for_visit(
        request.user,
        page_id,
        visit_token,
        site_id=req_site.id if req_site else None,
        is_short_body=is_short_body,
    )
    if result.get("eligible"):
        return _no_store_json(result, status=200)

    reason = result.get("reason")
    if result.get("degraded") or reason == "analytics_unavailable":
        return _no_store_json(result, status=503)
    if reason in {"invalid_visit_token", "invalid_visit_kind", "invalid_visit_claims", "page_mismatch", "site_mismatch"}:
        return _no_store_json(result, status=400)
    if reason in {"page_not_public", "recycled", "blocked", "site_not_found"}:
        return _no_store_json(result, status=404)
    if reason == "threshold_not_met":
        return _no_store_json(result, status=200)
    return _no_store_json(result, status=403)
