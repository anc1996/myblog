"""
analytics.templatetags.analytics_tags
公开页分析描述符输出模板标签：
- analytics_page_meta(context, page, entry_key="")
- analytics_entry_meta(context, entry_kind="", entry_key="", search_query="", search_total=None, search_scoped=False)

严格准则：
1. 绝不执行任何写 SQL；
2. 当 AnalyticsSettings 表尚未迁移或发生数据库只读异常时安全降级，不阻塞页面 HTML 渲染；
3. 严格排除预览态、未发布、已回收、别名页、被 get_public_blocked_page_ids 拦截的页面，以及未在 discovery.registry 注册分析能力的类型；
4. 仅输出带 HTML 转义的 <meta name="analytics-descriptor" content="...">。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from django import template
from django.db import DatabaseError
from django.db.models import Sum
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils import timezone

from analytics.collector import _get_redis_client, _resolve_prefix, resolve_page_analytics_spec
from analytics.models import AnalyticsSettings, ArticleRating, PageAnalyticsDaily, PageAnalyticsTotal
from analytics.tokens import sign_descriptor_token

register = template.Library()
logger = logging.getLogger(__name__)

_RATING_GRADE_OPTIONS = (
    ("excellent", "🌟", "优秀"),
    ("good", "👍", "良好"),
    ("pass", "👌", "及格"),
    ("poor", "👎", "差"),
)


def _is_preview_request(request: Any, page: Any = None) -> bool:
    if request is not None:
        if any(
            bool(getattr(request, flag, False))
            for flag in ("is_preview", "in_preview_panel", "_is_listing_preview", "_is_tag_preview")
        ):
            return True
    if page is not None and getattr(page, "is_preview", False):
        return True
    return False


def _resolve_site_readonly(request: Any, page: Any = None):
    from wagtail.models import Site

    req_site = None
    if request is not None:
        try:
            req_site = Site.find_for_request(request)
        except Exception:
            req_site = None

    if page is not None and hasattr(page, "get_site"):
        try:
            page_site = page.get_site()
        except Exception:
            page_site = None
        if req_site is not None and page_site is not None and req_site.id != page_site.id:
            return None
        if req_site is None:
            req_site = page_site

    if req_site is None:
        try:
            req_site = Site.objects.filter(is_default_site=True).first()
        except Exception:
            req_site = None
    return req_site


@register.simple_tag(takes_context=True)
def analytics_page_meta(context, page: Any, entry_key: str = "") -> str:
    """为已公开且已声明 AnalyticsCapabilities 的页面输出签名分析描述符 meta 标签。"""
    request = context.get("request") if isinstance(context, dict) else getattr(context, "get", lambda k: None)("request")
    if _is_preview_request(request, page):
        return ""

    if page is None:
        return ""
    page_id = getattr(page, "id", None)
    page_path = getattr(page, "path", None)
    if not page_id or not page_path:
        return ""
    if not getattr(page, "live", False):
        return ""
    if getattr(page, "is_recycled", False):
        return ""
    if getattr(page, "alias_of_id", None) is not None:
        return ""

    target_page = page
    if getattr(getattr(page, "_meta", None), "label", "") == "wagtailcore.Page" and hasattr(page, "specific"):
        try:
            target_page = page.specific
        except Exception:
            return ""
        if getattr(target_page, "is_recycled", False):
            return ""

    caps, type_key = resolve_page_analytics_spec(target_page)
    if caps is None:
        return ""

    try:
        from content_lifecycle.public_access import get_public_blocked_page_ids

        if page_id in get_public_blocked_page_ids([(page_id, page_path)]):
            return ""
    except DatabaseError:
        return ""
    except Exception as exc:
        logger.debug("analytics_page_meta public access check failed: %s", exc)
        return ""

    site = _resolve_site_readonly(request, target_page)
    if site is None:
        return ""

    ana_settings = AnalyticsSettings.get_for_site_readonly(site)
    if not ana_settings.collection_enabled:
        return ""

    payload = {
        "v": 1,
        "kind": caps.kind,
        "site_id": int(site.id),
        "page_id": int(page_id),
        "live_revision_id": int(getattr(target_page, "live_revision_id", 0) or 0),
        "type_key": str(type_key or ""),
        "entry_kind": str(caps.entry_kind or ""),
        "entry_key": str(entry_key or "")[:128],
        "body_markers": list(caps.body_markers or ()),
    }
    token = sign_descriptor_token(payload)
    return format_html('<meta name="analytics-descriptor" content="{}">', token)


@register.simple_tag(takes_context=True)
def analytics_rating_widget(context, page: Any) -> str:
    """
    在公开内容详情页正文末尾渲染四档文章评价挂件。
    仅在公开、非预览、非回收、非别名、未受阻断且 AnalyticsCapabilities.rating_enabled=True
    与 AnalyticsSettings.rating_enabled=True 时渲染；绝不执行写 SQL，也不展示全站总票数。
    """
    request = context.get("request") if isinstance(context, dict) else getattr(context, "get", lambda k: None)("request")
    if _is_preview_request(request, page):
        return ""
    if page is None:
        return ""

    page_id = getattr(page, "id", None)
    page_path = getattr(page, "path", None)
    if not page_id or not page_path or not getattr(page, "live", False):
        return ""
    if getattr(page, "is_recycled", False) or getattr(page, "alias_of_id", None) is not None:
        return ""

    target_page = page
    if getattr(getattr(page, "_meta", None), "label", "") == "wagtailcore.Page" and hasattr(page, "specific"):
        try:
            target_page = page.specific
        except Exception:
            return ""
        if getattr(target_page, "is_recycled", False):
            return ""

    caps, _ = resolve_page_analytics_spec(target_page)
    if caps is None or caps.kind != "detail" or not getattr(caps, "rating_enabled", False):
        return ""

    try:
        from content_lifecycle.public_access import get_public_blocked_page_ids

        if page_id in get_public_blocked_page_ids([(page_id, page_path)]):
            return ""
    except DatabaseError:
        return ""
    except Exception as exc:
        logger.debug("analytics_rating_widget public access check failed: %s", exc)
        return ""

    site = _resolve_site_readonly(request, target_page)
    if site is None:
        return ""

    ana_settings = AnalyticsSettings.get_for_site_readonly(site)
    if not getattr(ana_settings, "rating_enabled", False):
        return ""

    live_revision_id = int(getattr(target_page, "live_revision_id", 0) or 0)
    if live_revision_id <= 0:
        return ""

    user = getattr(request, "user", None) if request is not None else None
    is_authenticated = bool(
        user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", True)
    )

    is_author = False
    initial_grade = None
    initial_version = 0
    if is_authenticated:
        try:
            from analytics.rating_service import is_user_page_author

            is_author = bool(is_user_page_author(user, target_page))
            if not is_author:
                existing = (
                    ArticleRating.objects.filter(page_id=page_id, user_id=user.id)
                    .values("grade", "version")
                    .first()
                )
                if existing:
                    initial_grade = existing["grade"]
                    initial_version = int(existing["version"] or 0)
        except DatabaseError:
            is_author = False
        except Exception as exc:
            logger.debug("analytics_rating_widget user state lookup failed: %s", exc)

    try:
        rating_api_url = reverse("api_v1:analytics:page_rating", args=[int(page_id)])
    except NoReverseMatch:
        rating_api_url = f"/api/v1/analytics/pages/{int(page_id)}/rating/"

    try:
        reading_proof_api_url = reverse("api_v1:analytics:reading_proof", args=[int(page_id)])
    except NoReverseMatch:
        reading_proof_api_url = f"/api/v1/analytics/pages/{int(page_id)}/reading-proof/"

    try:
        login_base = reverse("identity:login")
    except NoReverseMatch:
        login_base = "/account/login/"

    next_path = ""
    if request is not None and hasattr(request, "get_full_path"):
        next_path = request.get_full_path() or ""
    if not next_path:
        next_path = getattr(target_page, "url", "") or "/"
    login_url = f"{login_base}?next={quote(next_path, safe='/')}"

    return render_to_string(
        "analytics/partials/_article_rating.html",
        {
            "page_id": int(page_id),
            "live_revision_id": live_revision_id,
            "is_authenticated": is_authenticated,
            "is_author": is_author,
            "can_rate_initial": bool(is_authenticated and not is_author),
            "initial_grade": initial_grade,
            "initial_version": initial_version,
            "rating_api_url": rating_api_url,
            "reading_proof_api_url": reading_proof_api_url,
            "login_url": login_url,
            "rating_options": _RATING_GRADE_OPTIONS,
        },
        request=request,
    )


def _format_compact_pv(num: int) -> str:
    val = max(0, int(num))
    if val >= 10000:
        return f"{val / 10000:.1f}万".replace(".0万", "万")
    return str(val)


@register.simple_tag(takes_context=True)
def analytics_public_stats(context, page: Any) -> str:
    """
    在前台公开内容页输出受控的“累计阅读 · 今日阅读”紧凑徽章。
    遵循 D4 隐私边界：仅当站点开启 `public_pv_display_enabled` 且页面公开时渲染，
    绝不写 SQL，绝不暴露 UV、来源、评分或非公开页数据。
    """
    request = context.get("request") if isinstance(context, dict) else getattr(context, "get", lambda k: None)("request")
    if _is_preview_request(request, page):
        return ""
    if page is None:
        return ""
    page_id = getattr(page, "id", None)
    page_path = getattr(page, "path", None)
    if not page_id or not page_path or not getattr(page, "live", False):
        return ""
    if getattr(page, "is_recycled", False) or getattr(page, "alias_of_id", None) is not None:
        return ""

    target_page = page
    if getattr(getattr(page, "_meta", None), "label", "") == "wagtailcore.Page" and hasattr(page, "specific"):
        try:
            target_page = page.specific
        except Exception:
            return ""
        if getattr(target_page, "is_recycled", False):
            return ""

    caps, _ = resolve_page_analytics_spec(target_page)
    if caps is None or caps.kind != "detail":
        return ""

    try:
        from content_lifecycle.public_access import get_public_blocked_page_ids

        if page_id in get_public_blocked_page_ids([(page_id, page_path)]):
            return ""
    except DatabaseError:
        return ""
    except Exception:
        return ""

    site = _resolve_site_readonly(request, target_page)
    if site is None:
        return ""

    ana_settings = AnalyticsSettings.get_for_site_readonly(site)
    if not ana_settings.public_pv_display_enabled:
        return ""

    today = timezone.localdate()
    settled_total_pv = 0
    settled_today_pv = 0
    try:
        settled_total_pv = int(
            PageAnalyticsTotal.objects.filter(site_id=site.id, page_id=page_id)
            .values_list("total_pv", flat=True)
            .first()
            or 0
        )
        settled_today_pv = int(
            PageAnalyticsDaily.objects.filter(
                site_id=site.id,
                page_id=page_id,
                stat_date=today,
            )
            .exclude(health_status="missing")
            .aggregate(total=Sum("pv"))
            .get("total")
            or 0
        )
    except DatabaseError:
        return ""

    # 读取 Redis 尚未结算的当日 PV 增量（Fail-Open，绝不影响页面正常渲染）
    unsettled_today_pv = 0
    try:
        client = _get_redis_client(write=False)
        if client is not None:
            prefix = _resolve_prefix()
            active_key = f"{prefix}:analytics:active:pv:{site.id}:{today.isoformat()}"
            for raw_field, raw_val in client.hscan_iter(active_key, match=f"{int(page_id)}|*|pv"):
                try:
                    unsettled_today_pv += max(0, int(raw_val))
                except (TypeError, ValueError):
                    continue
    except Exception:
        unsettled_today_pv = 0

    today_pv = settled_today_pv + unsettled_today_pv
    total_pv = max(today_pv, settled_total_pv + unsettled_today_pv)
    return format_html(
        '<span class="detail-stat-pill" title="累计阅读次数与今日阅读次数">👁️ 累计阅读 {} · 今日 {}</span>',
        _format_compact_pv(total_pv),
        _format_compact_pv(today_pv),
    )


@register.simple_tag(takes_context=True)
def analytics_entry_meta(
    context,
    entry_kind: str = "",
    entry_key: str = "",
    search_query: str = "",
    search_total: Any = None,
    search_scoped: bool = False,
) -> str:
    """为非 Page 聚合入口（如作者专栏、站内搜索）输出签名分析描述符 meta 标签。"""
    request = context.get("request") if isinstance(context, dict) else getattr(context, "get", lambda k: None)("request")
    if _is_preview_request(request):
        return ""

    if not entry_kind:
        return ""

    from discovery.registry import get_entry_analytics

    spec = get_entry_analytics(entry_kind)
    if spec is None or spec.analytics_capabilities.kind != "entry":
        return ""

    site = _resolve_site_readonly(request)
    if site is None:
        return ""

    ana_settings = AnalyticsSettings.get_for_site_readonly(site)
    if not ana_settings.collection_enabled:
        return ""

    payload: dict[str, Any] = {
        "v": 1,
        "kind": "entry",
        "site_id": int(site.id),
        "page_id": None,
        "live_revision_id": 0,
        "type_key": "",
        "entry_kind": spec.entry_kind,
        "entry_key": str(entry_key or "")[:128],
        "body_markers": [],
    }
    if spec.entry_kind == "search" and search_query:
        payload["search_query"] = str(search_query)[:200]
        if search_total is not None:
            try:
                payload["search_total"] = int(search_total)
            except (TypeError, ValueError):
                payload["search_total"] = None
        else:
            payload["search_total"] = None
        payload["search_scoped"] = bool(search_scoped)

    token = sign_descriptor_token(payload)
    return format_html('<meta name="analytics-descriptor" content="{}">', token)
