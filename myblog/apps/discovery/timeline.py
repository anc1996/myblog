"""
discovery.timeline
全站月历归档与时间线分页服务 (Calendar & Timeline Archive)。

特点：
1. 月历只读预聚合表 ArchiveDailyCount（当月最多 31 行），不扫千万级文章表；
2. 钻取单日/单月文章列表走 ContentQuery SARGable 日期区间 + 混合分页（浅页/游标/日期定位）；
3. 按文章 authored_on（成文日期）统计入桶，兼顾浅深色主题与移动端响应式。
"""

from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode
from django.shortcuts import render
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.utils import timezone
from wagtail.models import Site

from content_lifecycle.access import AccessContext, AccessMode
from discovery.archive_counts import get_archive_calendar_data
from discovery.contracts import QuerySpec
from discovery.models import ArchiveCountState, ArchiveDailyCount
from discovery.pagination import PageWindowExceededError, build_count_meta, build_pagination_payload
from discovery.query import ContentQuery
from discovery.registry import get_archive_enabled_type_keys, get_public_type_slug_map


def _parse_int(raw: str | None) -> int | None:
    if raw and str(raw).strip().isdigit():
        return int(str(raw).strip())
    return None


def timeline_view(request):
    """全站月历归档与按日/按月下钻分页视图"""
    site = Site.find_for_request(request)
    site_id = site.id if site else None
    site_today = timezone.localdate()

    req_year = _parse_int(request.GET.get("year"))
    req_month = _parse_int(request.GET.get("month"))
    if req_month is not None and not (1 <= req_month <= 12):
        req_month = None

    selected_day: date | None = None
    raw_day = (request.GET.get("day") or request.GET.get("jump_date") or "").strip()
    if raw_day:
        try:
            selected_day = date.fromisoformat(raw_day)
            req_year = selected_day.year
            req_month = selected_day.month
        except (ValueError, TypeError):
            selected_day = None

    selected_year = req_year or site_today.year
    if selected_year < 1970:
        selected_year = 1970
    elif selected_year > 2100:
        selected_year = 2100
    selected_month = req_month or site_today.month
    scope = (request.GET.get("scope") or "").strip().lower()

    type_slug = (request.GET.get("type") or "").strip().lower() or None
    type_map = get_public_type_slug_map()
    archive_types = list(get_archive_enabled_type_keys())
    if type_slug and type_slug in type_map and type_map[type_slug] in archive_types:
        query_type_keys = [type_map[type_slug]]
    else:
        type_slug = None
        query_type_keys = archive_types

    # 1. 月历网格（只读 ArchiveDailyCount 最多 31 个日桶）
    calendar_data = get_archive_calendar_data(
        site_id=site_id,
        year=selected_year,
        month=selected_month,
        type_slug=type_slug,
        base_url="/timeline/",
    )

    # 2. 计算当前下钻区间 [date_from, date_to]（SARGable 索引区间）
    if selected_day is not None:
        drill_from = selected_day
        drill_to = min(selected_day, site_today)
        drill_label = f"{selected_day.isoformat()} 当日发表"
        active_scope = "day"
    elif req_year is not None and (scope == "year" or req_month is None):
        drill_from = date(selected_year, 1, 1)
        drill_to = min(date(selected_year, 12, 31), site_today)
        drill_label = f"{selected_year} 年全年归档"
        active_scope = "year"
    elif req_year is not None or req_month is not None:
        month_start = date(selected_year, selected_month, 1)
        if selected_month == 12:
            month_end = date(selected_year, 12, 31)
        else:
            month_end = date(selected_year, selected_month + 1, 1) - timedelta(days=1)
        drill_from = month_start
        drill_to = min(month_end, site_today)
        drill_label = f"{selected_year} 年 {selected_month} 月归档"
        active_scope = "month"
    else:
        drill_from = None
        drill_to = site_today
        drill_label = "全站最新归档"
        active_scope = "all"

    # 2.5 支持在当前归档时间段内二次关键词检索与排序（应对单年 100 万篇/单日上千篇快速筛选）
    q_term = (request.GET.get("q") or "").strip()[:200]
    raw_sort = (request.GET.get("sort") or "").strip().lower()
    if raw_sort in ("authored_desc", "authored_asc", "relevance"):
        effective_sort = raw_sort
    else:
        effective_sort = "authored_desc"

    # 3. 解析分页参数（复用千万级混合分页协议）
    cursor = (request.GET.get("cursor") or "").strip() or None
    page_num = None
    seek_date = None
    seek_direction = "date"
    if cursor is None:
        if (request.GET.get("seek_direction") or "").strip().lower() == "oldest":
            seek_direction = "oldest"
        elif request.GET.get("seek_date"):
            try:
                seek_date = date.fromisoformat(request.GET.get("seek_date").strip())
            except (ValueError, TypeError):
                seek_date = None
        if seek_direction != "oldest" and seek_date is None and request.GET.get("page"):
            try:
                p_val = int(str(request.GET.get("page")).strip())
                if p_val >= 1:
                    page_num = p_val
            except (ValueError, TypeError):
                page_num = None

    if drill_from and drill_to < drill_from:
        # 未来月份区间，直接空结果避免无效扫描
        query_result = None
        items = []
        pagination = build_pagination_payload(
            mode="shallow_page",
            current_page=1,
            page_size=20,
            total_count=0,
            count_kind="exact",
            has_next=False,
            has_previous=False,
            next_cursor=None,
            previous_cursor=None,
        )
        count_meta = build_count_meta(value=0, kind="exact")
        http_status = 200
        error_message = None
    else:
        spec = QuerySpec(
            q=q_term or None,
            type_keys=query_type_keys,
            site_id=site_id,
            date_from=drill_from,
            date_to=drill_to,
            sort=effective_sort,
            facets=[],
            page_size=20,
            cursor=cursor,
            page=page_num,
            seek_date=seek_date,
            seek_direction=seek_direction,
            count_mode="bounded",
            compute_facets_flag=False,
        )
        http_status = 200
        error_message = None
        try:
            query_result = ContentQuery.execute(spec, AccessContext(mode=AccessMode.PUBLIC))
            items = query_result.items
            pagination = query_result.pagination
            count_meta = query_result.count_meta
        except PageWindowExceededError as exc:
            query_result = None
            items = []
            http_status = 422
            error_message = f"页码 {exc.requested_page} 超出直跳上限（最多 {exc.max_jump_page} 页），请使用游标或日期定位。"
            pagination = build_pagination_payload(
                mode="shallow_page",
                current_page=None,
                page_size=20,
                total_count=0,
                count_kind="lower_bound",
                has_next=False,
                has_previous=False,
                next_cursor=None,
                previous_cursor=None,
            )
            count_meta = build_count_meta(value=0, kind="lower_bound")

    # 全站归档总篇数（直接读 ArchiveDailyCount 聚合）
    active_gen, _is_stale = ArchiveCountState.get_active_generation_readonly()
    from django.db.models import Sum
    site_total_summary = ArchiveDailyCount.objects.filter(
        generation=active_gen,
        site_id=int(site_id or 0),
        type_key__in=query_type_keys,
        bucket_date__lte=site_today,
    ).aggregate(s=Sum("article_count"))["s"]
    site_total_count = int(site_total_summary or (query_result.total_count if query_result else 0))

    type_tabs = [
        {"slug": "", "label": "全部类型", "is_active": type_slug is None, "url": f"/timeline/?year={selected_year}&month={selected_month}"},
        {"slug": "blog", "label": "博客文章", "is_active": type_slug == "blog", "url": f"/timeline/?year={selected_year}&month={selected_month}&type=blog"},
        {"slug": "guide", "label": "技术指南", "is_active": type_slug == "guide", "url": f"/timeline/?year={selected_year}&month={selected_month}&type=guide"},
        {"slug": "project", "label": "开源项目", "is_active": type_slug == "project", "url": f"/timeline/?year={selected_year}&month={selected_month}&type=project"},
    ]

    qs_params: dict[str, Any] = {}
    if req_year is not None or selected_day is not None:
        qs_params["year"] = selected_year
    if (req_month is not None and active_scope != "year") or selected_day is not None:
        qs_params["month"] = selected_month
    if active_scope == "year":
        qs_params["scope"] = "year"
    if selected_day is not None:
        qs_params["day"] = selected_day.isoformat()
    if type_slug:
        qs_params["type"] = type_slug
    if q_term:
        qs_params["q"] = q_term
    if effective_sort != "authored_desc":
        qs_params["sort"] = effective_sort

    view_model = {
        "items": items,
        "total_count": count_meta["value"],
        "count_meta": count_meta,
        "pagination": pagination,
        "next_cursor": pagination.get("next_cursor"),
        "previous_cursor": pagination.get("previous_cursor"),
        "is_empty": len(items) == 0,
        "error_message": error_message,
        "cursor_reset": bool(query_result and query_result.cursor_reset),
        "canonical_url": "/timeline/",
        "query_string": urlencode(qs_params),
        "active_filters": {
            "q": q_term,
            "sort": effective_sort,
            "seek_date": seek_date.isoformat() if seek_date else "",
        },
    }

    context = {
        "calendar": calendar_data,
        "view_model": view_model,
        "type_tabs": type_tabs,
        "active_type": type_slug or "",
        "active_scope": active_scope,
        "drill_label": drill_label,
        "selected_day": selected_day.isoformat() if selected_day else "",
        "selected_year": selected_year,
        "selected_month": selected_month,
        "q_term": q_term,
        "effective_sort": effective_sort,
        "total_count": site_total_count,
        "canonical_url": "/timeline/",
    }

    is_ajax = bool(
        request.headers.get("X-Listing-Response") == "fragment-v1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.GET.get("format") == "partial"
    )

    if is_ajax:
        html = render_to_string(
            "discovery/timeline/_timeline_content.html",
            context,
            request=request,
        )
        return JsonResponse(
            {
                "html": html,
                "total_count": site_total_count,
                "month": calendar_data["month"],
                "month_count": calendar_data["month_count"],
            },
            status=http_status,
        )

    resp = render(request, "discovery/timeline/timeline_page.html", context)
    resp.status_code = http_status
    return resp
