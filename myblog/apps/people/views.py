"""
people.views
全站作者专栏大厅 (/authors/)、远程联想 (/authors/lookup/) 与作者个人专栏 (/authors/<slug>/)。
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import escape
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from people.services import (
    get_author_detail_data,
    get_author_directory_data,
    get_author_lookup_data,
    resolve_author_for_detail,
)


def _is_fragment_request(request) -> bool:
    return bool(
        request.headers.get("X-Listing-Response") == "fragment-v1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.GET.get("format") == "partial"
    )


def _check_lookup_rate_limit(request) -> bool:
    """轻量公共联想限流（每 IP 每分钟最多 90 次请求），缓存不可用时不阻断正常浏览。"""
    try:
        xff = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
        ip = xff or request.META.get("REMOTE_ADDR") or "unknown"
        key = f"myblog:author_lookup_rl:{ip}"
        cur = cache.get(key)
        if cur is None:
            cache.set(key, 1, timeout=60)
            return True
        if int(cur) >= 90:
            return False
        cache.incr(key)
        return True
    except Exception:
        return True


def _ensure_author_discovery_enabled():
    if not getattr(settings, "AUTHOR_DISCOVERY_ENABLED", True):
        raise Http404("Author discovery is disabled.")


@never_cache
@require_GET
def author_directory_view(request):
    """作者专栏大厅 (/authors/)。"""
    _ensure_author_discovery_enabled()
    ctx = get_author_directory_data(request)
    is_ajax = _is_fragment_request(request)

    if is_ajax:
        html = render_to_string(
            "people/partials/_author_directory.html",
            ctx,
            request=request,
        )
        resp = JsonResponse({
            "html": html,
            "canonical_url": ctx["canonical_url"],
            "full_url": ctx["full_url"],
            "total_count": ctx["filtered_count"],
            "cursor_reset": False,
        })
    else:
        resp = TemplateResponse(request, "people/author_directory.html", ctx)

    resp["Vary"] = "X-Listing-Response"
    return resp


@never_cache
@require_GET
def author_lookup_view(request):
    """公开作者远程联想接口 (/authors/lookup/)。"""
    _ensure_author_discovery_enabled()
    if not _check_lookup_rate_limit(request):
        return JsonResponse(
            {"items": [], "more": False, "error": "请求过于频繁，请稍后再试。"},
            status=429,
        )
    data = get_author_lookup_data(request, limit=20)
    resp = JsonResponse(data, status=200)
    resp["Cache-Control"] = "no-store"
    return resp


@never_cache
@require_GET
def author_detail_view(request, slug: str):
    """作者个人专栏主页 (/authors/<slug>/)。"""
    _ensure_author_discovery_enabled()
    status_kind, payload = resolve_author_for_detail(request, slug)
    is_ajax = _is_fragment_request(request)

    if status_kind == "redirect":
        target = reverse("people:author_detail", kwargs={"slug": payload})
        allowed_keys = ("type", "role", "q", "title_only", "date_from", "date_to", "sort", "page_size", "secondary", "page", "seek_date", "seek_direction")
        kept = [(k, v) for k in allowed_keys for v in request.GET.getlist(k) if v != ""]
        if kept:
            from urllib.parse import urlencode
            target = f"{target}?{urlencode(kept, doseq=True)}"
        return redirect(target, permanent=True)

    if status_kind == "missing":
        missing_ctx = {
            "recommended_authors": payload,
            "directory_url": reverse("people:author_directory"),
        }
        if is_ajax:
            html = render_to_string("people/partials/_author_missing_inner.html", missing_ctx, request=request)
            resp = JsonResponse({"html": html, "mode": "missing", "canonical_url": reverse("people:author_directory")}, status=404)
        else:
            resp = TemplateResponse(request, "people/author_missing.html", missing_ctx, status=404)
        resp["Vary"] = "X-Listing-Response"
        resp["X-Robots-Tag"] = "noindex"
        return resp

    person = payload
    ctx, http_status = get_author_detail_data(request, person)

    if is_ajax:
        if http_status == 400 and "items" not in ctx:
            safe_err = escape(ctx.get("error_message", "参数错误"))
            resp = JsonResponse(
                {
                    "html": f'<div class="ui-alert ui-alert-warning" role="alert"><div class="ui-alert-body">{safe_err}</div></div>',
                    "error_message": ctx.get("error_message", "参数错误"),
                    "canonical_url": ctx["canonical_url"],
                },
                status=400,
            )
        else:
            html = render_to_string("people/partials/_author_results.html", ctx, request=request)
            resp = JsonResponse(
                {
                    "html": html,
                    "canonical_url": ctx["canonical_url"],
                    "full_url": ctx["full_url"],
                    "total_count": ctx["total_count"],
                    "next_cursor": ctx["next_cursor"],
                    "previous_cursor": ctx["previous_cursor"],
                    "cursor_reset": ctx["cursor_reset"],
                    "error_message": ctx["error_message"],
                    "active_filters": ctx["active_filters"],
                },
                status=http_status,
            )
    else:
        if http_status == 400 and "items" not in ctx:
            resp = TemplateResponse(request, "people/author_detail.html", ctx, status=400)
        else:
            resp = TemplateResponse(request, "people/author_detail.html", ctx, status=http_status)

    resp["Vary"] = "X-Listing-Response"
    return resp
