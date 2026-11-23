from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from django.db.models import Q
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from taggit.models import Tag

from content_lifecycle.access import AccessContext, AccessMode
from discovery.contracts import QuerySpec
from discovery.facets import (
    _tag_name_priority,
    get_type_display_label,
)
from discovery.models import PublishedSearchTag
from discovery.pagination import (
    PageWindowExceededError,
    build_count_meta,
    build_pagination_payload,
)
from discovery.query import ContentQuery


TRUE_VALUES = {"1", "true", "on", "yes"}
ALLOWED_SORTS = {"relevance", "authored_desc", "authored_asc"}
ALLOWED_DAYS = {"7", "30", "365"}
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

UNIFIED_TYPE_OPTIONS = (
    ("", "全部"),
    ("blog_page", "博客文章"),
    ("guide", "专题指南"),
    ("project", "开源项目"),
    ("navigation", "栏目导航页"),
)

DAYS_OPTIONS = (
    ("", "全部时间"),
    ("7", "近 7 天"),
    ("30", "近 30 天"),
    ("365", "近 1 年"),
)

SORT_LABELS = {
    "relevance": "按相关度",
    "authored_desc": "成文日期：新 → 旧",
    "authored_asc": "成文日期：旧 → 新",
}


def _parse_tag_tokens(request) -> list[str]:
    """解析多选 tag 参数（支持重复 ?tag=a&tag=b 与逗号分隔 ?tag=a,b）"""
    tokens: list[str] = []
    for raw in request.GET.getlist("tag"):
        for part in str(raw).split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in tokens:
                tokens.append(cleaned)
    return tokens


def _resolve_tag_tokens(raw_tokens: list[str]) -> tuple[list[int], list[dict[str, Any]], bool]:
    """
    将 tag_id 或小写 tag_slug/名称解析为规范化标签分组与展开后的所有 tag_ids。
    返回: (all_tag_ids, selected_groups, has_unmatched)
    """
    if not raw_tokens:
        return [], [], False

    all_tag_ids: list[int] = []
    selected_groups: list[dict[str, Any]] = []
    seen_norm_keys: set[str] = set()
    has_unmatched = False

    for tok in raw_tokens:
        matched_rows: list[dict[str, Any]] = []
        if tok.isdigit():
            tid = int(tok)
            base_row = (
                PublishedSearchTag.objects.filter(tag_id=tid)
                .values("tag_id", "tag_name", "tag_slug")
                .first()
            )
            if not base_row:
                tag_obj = Tag.objects.filter(id=tid).values("id", "name", "slug").first()
                if tag_obj:
                    base_row = {
                        "tag_id": tag_obj["id"],
                        "tag_name": tag_obj["name"],
                        "tag_slug": tag_obj["slug"],
                    }
            if base_row:
                t_name = (base_row["tag_name"] or "").strip()
                matched_rows = list(
                    PublishedSearchTag.objects.filter(tag_name__iexact=t_name)
                    .values("tag_id", "tag_name", "tag_slug")
                    .distinct()
                )
                if not any(r["tag_id"] == tid for r in matched_rows):
                    matched_rows.insert(0, base_row)
            else:
                matched_rows = [{"tag_id": tid, "tag_name": f"#{tid}", "tag_slug": str(tid)}]
        else:
            pub_matches = list(
                PublishedSearchTag.objects.filter(
                    Q(tag_slug__iexact=tok) | Q(tag_name__iexact=tok)
                )
                .values("tag_id", "tag_name", "tag_slug")
                .distinct()
            )
            tag_matches = list(
                Tag.objects.filter(Q(slug__iexact=tok) | Q(name__iexact=tok))
                .values("id", "name", "slug")
            )
            matched_names = {
                (r["tag_name"] or "").strip() for r in pub_matches if r.get("tag_name")
            } | {
                (r["name"] or "").strip() for r in tag_matches if r.get("name")
            }
            if matched_names:
                name_q = Q()
                for mname in matched_names:
                    name_q |= Q(tag_name__iexact=mname)
                matched_rows = list(
                    PublishedSearchTag.objects.filter(name_q)
                    .values("tag_id", "tag_name", "tag_slug")
                    .distinct()
                )
                for r in tag_matches:
                    if not any(m["tag_id"] == r["id"] for m in matched_rows):
                        matched_rows.append({
                            "tag_id": r["id"],
                            "tag_name": r["name"],
                            "tag_slug": r["slug"],
                        })

        if not matched_rows:
            has_unmatched = True
            continue

        # 选出最优显示名称与规范 URL token
        best_row = max(
            matched_rows,
            key=lambda r: _tag_name_priority((r.get("tag_name") or "").strip(), 1),
        )
        display_name = (best_row.get("tag_name") or "").strip() or tok
        norm_key = display_name.lower()
        group_ids: list[int] = []
        for r in matched_rows:
            rid = int(r["tag_id"])
            if rid not in group_ids:
                group_ids.append(rid)
            if rid not in all_tag_ids:
                all_tag_ids.append(rid)

        if norm_key in seen_norm_keys:
            continue
        seen_norm_keys.add(norm_key)

        canonical_token = (best_row.get("tag_slug") or "").strip() or str(best_row["tag_id"])
        selected_groups.append({
            "token": canonical_token,
            "raw_token": tok,
            "norm_key": norm_key,
            "label": display_name,
            "tag_ids": group_ids,
        })

    return all_tag_ids, selected_groups, has_unmatched


def _build_search_url(
    base_url: str,
    base_state: dict[str, Any],
    *,
    set_params: dict[str, Any] | None = None,
    remove_keys: set[str] | None = None,
    remove_tag_norm_key: str | None = None,
    toggle_tag: dict[str, Any] | None = None,
    reset_pagination: bool = True,
) -> str:
    """基于当前状态构造多维组合筛选 URL，绝不误清已有其他筛选条件"""
    state: dict[str, Any] = {
        "query": base_state.get("query", ""),
        "title_only": base_state.get("title_only", False),
        "type": base_state.get("type", ""),
        "year": base_state.get("year"),
        "month": base_state.get("month"),
        "date_from": base_state.get("date_from", ""),
        "date_to": base_state.get("date_to", ""),
        "days": base_state.get("days", ""),
        "sort": base_state.get("sort", ""),
        "page_size": base_state.get("page_size"),
        "secondary": base_state.get("secondary", "none"),
        "expand": base_state.get("expand", "auto"),
        "cursor": base_state.get("cursor"),
        "page": base_state.get("page"),
        "seek_date": base_state.get("seek_date", ""),
        "seek_direction": base_state.get("seek_direction", ""),
    }
    tag_groups = [dict(g) for g in base_state.get("tag_groups", [])]

    if reset_pagination:
        state["cursor"] = None
        state["page"] = None
        state["seek_date"] = ""
        state["seek_direction"] = ""

    if remove_keys:
        for rk in remove_keys:
            if rk == "tag":
                tag_groups = []
            elif rk in ("title_only",):
                state[rk] = False
            elif rk in ("year", "month", "page_size", "cursor", "page"):
                state[rk] = None
            elif rk == "secondary":
                state[rk] = "none"
            elif rk == "expand":
                state[rk] = "auto"
            else:
                state[rk] = ""

    if remove_tag_norm_key is not None:
        tag_groups = [g for g in tag_groups if g["norm_key"] != remove_tag_norm_key]

    if toggle_tag is not None:
        t_norm = (toggle_tag.get("name") or "").strip().lower()
        t_ids = set(toggle_tag.get("tag_ids") or ([toggle_tag["id"]] if toggle_tag.get("id") else []))
        exists = any(
            g["norm_key"] == t_norm or bool(set(g.get("tag_ids", [])) & t_ids)
            for g in tag_groups
        )
        if exists:
            tag_groups = [
                g for g in tag_groups
                if g["norm_key"] != t_norm and not (set(g.get("tag_ids", [])) & t_ids)
            ]
        else:
            t_token = (toggle_tag.get("slug") or "").strip() or str(toggle_tag.get("id") or "")
            if t_token:
                tag_groups.append({
                    "token": t_token,
                    "norm_key": t_norm,
                    "label": toggle_tag.get("name") or t_token,
                    "tag_ids": list(t_ids),
                })

    if set_params:
        for sk, sv in set_params.items():
            state[sk] = sv

    pairs: list[tuple[str, str]] = []
    if state["query"]:
        pairs.append(("query", state["query"]))
    if state["title_only"]:
        pairs.append(("title_only", "1"))
    if state["type"]:
        pairs.append(("type", state["type"]))
    for g in tag_groups:
        pairs.append(("tag", g["token"]))
    if state["year"]:
        pairs.append(("year", str(state["year"])))
    if state["month"]:
        pairs.append(("month", str(state["month"])))
    if state["days"]:
        pairs.append(("days", str(state["days"])))
    else:
        if state["date_from"]:
            pairs.append(("date_from", state["date_from"]))
        if state["date_to"]:
            pairs.append(("date_to", state["date_to"]))
    if state["sort"]:
        pairs.append(("sort", state["sort"]))
    if state["page_size"] and state["page_size"] != DEFAULT_PAGE_SIZE:
        pairs.append(("page_size", str(state["page_size"])))
    if state["secondary"] == "words":
        pairs.append(("secondary", "words"))
    if state["expand"] in ("off", "on"):
        pairs.append(("expand", state["expand"]))
    if state["cursor"]:
        pairs.append(("cursor", state["cursor"]))
    elif state["seek_direction"] == "oldest":
        pairs.append(("seek_direction", "oldest"))
    elif state["seek_date"]:
        pairs.append(("seek_date", state["seek_date"]))
    elif state["page"] and int(state["page"]) > 1:
        pairs.append(("page", str(state["page"])))

    qs = urlencode(pairs, doseq=True)
    return f"{base_url}?{qs}" if qs else base_url


def search(request):
    """全站统一中文搜索与组合筛选入口"""
    canonical_url = reverse("search")
    raw_query = request.GET.get("query")
    if raw_query is None:
        raw_query = request.GET.get("q", "")
    search_query = str(raw_query).strip()[:200]

    title_only_raw = (request.GET.get("title_only") or "").strip().lower()
    title_only = title_only_raw in TRUE_VALUES

    raw_tag_tokens = _parse_tag_tokens(request)
    tag_ids, selected_tag_groups, has_unmatched_tag = _resolve_tag_tokens(raw_tag_tokens)
    effective_tag_ids = tag_ids if tag_ids else ([-1] if has_unmatched_tag else [])
    active_tag_ids_set = set(tag_ids)
    active_tag_norm_keys = {g["norm_key"] for g in selected_tag_groups}

    type_key = request.GET.get("type", "").strip()
    type_keys = [type_key] if type_key else []

    year_str = request.GET.get("year", "").strip()
    year = int(year_str) if year_str.isdigit() else None

    month_str = request.GET.get("month", "").strip()
    month = int(month_str) if month_str.isdigit() and 1 <= int(month_str) <= 12 else None

    # 日期范围与快捷天数 (7 / 30 / 365)
    raw_date_from = (request.GET.get("date_from") or "").strip()
    raw_date_to = (request.GET.get("date_to") or "").strip()
    parsed_date_from: date | None = None
    parsed_date_to: date | None = None
    if raw_date_from:
        try:
            parsed_date_from = date.fromisoformat(raw_date_from)
        except (ValueError, TypeError):
            parsed_date_from = None
    if raw_date_to:
        try:
            parsed_date_to = date.fromisoformat(raw_date_to)
        except (ValueError, TypeError):
            parsed_date_to = None

    raw_days = (request.GET.get("days") or "").strip()
    days = raw_days if raw_days in ALLOWED_DAYS else ""

    effective_date_from = parsed_date_from
    effective_date_to = parsed_date_to
    if days and effective_date_from is None:
        effective_date_from = timezone.localdate() - timedelta(days=int(days))

    raw_sort = (request.GET.get("sort") or "").strip().lower()
    explicit_sort = raw_sort if raw_sort in ALLOWED_SORTS else ""
    effective_sort = explicit_sort or ("relevance" if search_query else "authored_desc")

    secondary = (request.GET.get("secondary") or "none").strip()
    if secondary not in ("none", "words"):
        secondary = "none"
    expand = (request.GET.get("expand") or "auto").strip()
    if expand not in ("auto", "off", "on"):
        expand = "auto"

    cursor = (request.GET.get("cursor") or "").strip() or None
    page_num: int | None = None
    seek_date: date | None = None
    seek_direction = "date"
    if cursor is None:
        raw_dir = (request.GET.get("seek_direction") or "").strip().lower()
        if raw_dir == "oldest":
            seek_direction = "oldest"
        else:
            raw_sd = (request.GET.get("seek_date") or "").strip()
            if raw_sd:
                try:
                    seek_date = date.fromisoformat(raw_sd)
                except (ValueError, TypeError):
                    seek_date = None
        if seek_direction != "oldest" and seek_date is None:
            raw_page = (request.GET.get("page") or "").strip()
            if raw_page:
                try:
                    parsed_p = int(raw_page)
                    page_num = parsed_p if parsed_p >= 1 else 1
                except (ValueError, TypeError):
                    page_num = None

    raw_page_size = (request.GET.get("page_size") or "").strip()
    try:
        page_size = max(1, min(int(raw_page_size) if raw_page_size else DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE))
    except (ValueError, TypeError):
        page_size = DEFAULT_PAGE_SIZE

    base_state = {
        "query": search_query,
        "title_only": title_only,
        "type": type_key,
        "tag_groups": selected_tag_groups,
        "year": year,
        "month": month,
        "date_from": parsed_date_from.isoformat() if parsed_date_from else "",
        "date_to": parsed_date_to.isoformat() if parsed_date_to else "",
        "days": days,
        "sort": explicit_sort,
        "page_size": page_size,
        "secondary": secondary,
        "expand": expand,
        "cursor": cursor,
        "page": page_num,
        "seek_date": seek_date.isoformat() if seek_date else "",
        "seek_direction": seek_direction if seek_direction == "oldest" else "",
    }

    spec = QuerySpec(
        q=search_query or None,
        search_fields="title" if title_only else "all",
        type_keys=type_keys,
        tag_ids=effective_tag_ids,
        year=year,
        month=month,
        date_from=effective_date_from,
        date_to=effective_date_to,
        sort=effective_sort,
        secondary=secondary,
        expand=expand,
        cursor=cursor,
        page=page_num,
        page_size=page_size,
        seek_date=seek_date,
        seek_direction=seek_direction,
        count_mode="bounded",
        facets=["types", "tags", "years"],
    )

    access_context = AccessContext(
        user=request.user,
        mode=AccessMode.PUBLIC,
    )

    http_status = 200
    error_message: str | None = None
    result = None

    try:
        result = ContentQuery.execute(spec, access_context)
    except PageWindowExceededError as exc:
        http_status = 422
        error_message = (
            f"页码 {exc.requested_page} 超出直跳窗口上限（最多支持直跳第 {exc.max_jump_page} 页），"
            "请使用「下一页」游标或按成文日期定位。"
        )
    except ValueError:
        http_status = 400
        error_message = "无效的分页游标参数，请重置或返回第 1 页重试。"

    if result is not None:
        items = result.items
        total_count = result.total_count
        facets = result.facets
        archive_groups = result.archive_groups
        next_cursor = result.next_cursor
        previous_cursor = result.previous_cursor
        cursor_reset = result.cursor_reset
        expansion_used = result.expansion_used
        secondary_used = result.secondary_used
        pagination_payload = result.pagination
        count_meta = result.count_meta
    else:
        items = []
        total_count = 0
        facets = {}
        archive_groups = []
        next_cursor = None
        previous_cursor = None
        cursor_reset = False
        expansion_used = False
        secondary_used = False
        pagination_payload = build_pagination_payload(
            mode="shallow_page",
            current_page=None,
            page_size=page_size,
            total_count=0,
            count_kind="lower_bound",
            has_next=False,
            has_previous=False,
            next_cursor=None,
            previous_cursor=None,
        )
        count_meta = build_count_meta(value=0, kind="lower_bound")

    # SEARCH-MODE-02: 模式 A 命中少于 5 条时的手动展开判断
    a_count = len([it for it in items if it.match_mode == "A"])
    show_expand_button = (
        (1 <= a_count < 5)
        and not secondary_used
        and bool(search_query)
    )
    show_expand_url = (
        _build_search_url(
            canonical_url,
            base_state,
            set_params={"secondary": "words"},
            reset_pagination=True,
        )
        if show_expand_button
        else ""
    )

    # 构建用于 _listing_pagination.html 的非分页查询串
    pagination_base_url = _build_search_url(canonical_url, base_state, reset_pagination=True)
    pagination_query_string = pagination_base_url.split("?", 1)[1] if "?" in pagination_base_url else ""

    pagination_vm = {
        "items": items,
        "total_count": total_count,
        "count_meta": count_meta,
        "pagination": pagination_payload,
        "next_cursor": next_cursor,
        "previous_cursor": previous_cursor,
        "canonical_url": canonical_url,
        "query_string": pagination_query_string,
        "active_filters": {
            "seek_date": seek_date.isoformat() if seek_date else "",
        },
        "error_message": error_message,
    }

    # 1. 内容类型选项（全部 / 博客文章 / 专题指南 / 开源项目 / 栏目导航页）
    raw_type_counts = {item["key"]: int(item["count"]) for item in facets.get("types", [])}
    all_types_count = sum(v for k, v in raw_type_counts.items() if k != "navigation")
    type_options: list[dict[str, Any]] = []
    for t_key, t_label in UNIFIED_TYPE_OPTIONS:
        if t_key == "":
            t_count = all_types_count
            is_active = not type_key
            t_url = _build_search_url(canonical_url, base_state, set_params={"type": ""}, reset_pagination=True)
        else:
            t_count = raw_type_counts.get(t_key, 0)
            is_active = type_key == t_key
            next_type = "" if is_active else t_key
            t_url = _build_search_url(canonical_url, base_state, set_params={"type": next_type}, reset_pagination=True)
        type_options.append({
            "key": t_key,
            "label": t_label,
            "count": t_count,
            "is_active": is_active,
            "url": t_url,
        })

    # 2. 热门标签分面（基于大小写归一化结果，支持多选 toggle）
    tag_facet_items: list[dict[str, Any]] = []
    for tg in facets.get("tags", []):
        tg_ids = set(tg.get("tag_ids") or [tg["id"]])
        tg_norm = (tg.get("name") or "").strip().lower()
        is_active = bool(tg_ids & active_tag_ids_set) or (tg_norm in active_tag_norm_keys)
        tag_facet_items.append({
            "id": tg["id"],
            "tag_ids": list(tg_ids),
            "name": tg["name"],
            "slug": tg.get("slug") or str(tg["id"]),
            "count": tg["count"],
            "is_active": is_active,
            "url": _build_search_url(canonical_url, base_state, toggle_tag=tg, reset_pagination=True),
        })

    # 3. 年份归档分面（支持 toggle）
    year_facet_items: list[dict[str, Any]] = []
    for yr in facets.get("years", []):
        yr_val = yr["year"]
        is_active = year == yr_val
        next_yr = None if is_active else yr_val
        year_facet_items.append({
            "year": yr_val,
            "count": yr["count"],
            "is_active": is_active,
            "url": _build_search_url(canonical_url, base_state, set_params={"year": next_yr}, reset_pagination=True),
        })

    # 4. 快捷时间范围胶囊（全部时间 / 近 7 天 / 近 30 天 / 近 1 年）
    days_options: list[dict[str, Any]] = []
    for d_key, d_label in DAYS_OPTIONS:
        if d_key == "":
            is_active = not days and not parsed_date_from and not parsed_date_to
            d_url = _build_search_url(
                canonical_url,
                base_state,
                remove_keys={"days", "date_from", "date_to"},
                reset_pagination=True,
            )
        else:
            is_active = days == d_key
            if is_active:
                d_url = _build_search_url(
                    canonical_url,
                    base_state,
                    remove_keys={"days", "date_from", "date_to"},
                    reset_pagination=True,
                )
            else:
                d_url = _build_search_url(
                    canonical_url,
                    base_state,
                    set_params={"days": d_key, "date_from": "", "date_to": ""},
                    reset_pagination=True,
                )
        days_options.append({
            "key": d_key,
            "label": d_label,
            "is_active": is_active,
            "url": d_url,
        })

    # 5. 已选筛选条件胶囊列表 (active_filter_pills)
    active_filter_pills: list[dict[str, Any]] = []
    if search_query:
        active_filter_pills.append({
            "key": "query",
            "label": "关键词",
            "value": search_query,
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"query", "secondary"}, reset_pagination=True
            ),
        })
    if title_only:
        active_filter_pills.append({
            "key": "title_only",
            "label": "范围",
            "value": "仅搜标题",
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"title_only"}, reset_pagination=True
            ),
        })
    if type_key:
        active_filter_pills.append({
            "key": "type",
            "label": "类型",
            "value": get_type_display_label(type_key),
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"type"}, reset_pagination=True
            ),
        })
    for tg_grp in selected_tag_groups:
        active_filter_pills.append({
            "key": "tag",
            "label": "标签",
            "value": f"#{tg_grp['label']}",
            "remove_url": _build_search_url(
                canonical_url,
                base_state,
                remove_tag_norm_key=tg_grp["norm_key"],
                reset_pagination=True,
            ),
        })
    if year:
        active_filter_pills.append({
            "key": "year",
            "label": "年份",
            "value": f"{year} 年",
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"year", "month"}, reset_pagination=True
            ),
        })
    if days:
        days_label_map = dict(DAYS_OPTIONS)
        active_filter_pills.append({
            "key": "days",
            "label": "时间",
            "value": days_label_map.get(days, f"近 {days} 天"),
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"days"}, reset_pagination=True
            ),
        })
    elif parsed_date_from or parsed_date_to:
        df_txt = parsed_date_from.isoformat() if parsed_date_from else "不限"
        dt_txt = parsed_date_to.isoformat() if parsed_date_to else "至今"
        active_filter_pills.append({
            "key": "date_range",
            "label": "成文日期",
            "value": f"{df_txt} ~ {dt_txt}",
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"date_from", "date_to"}, reset_pagination=True
            ),
        })
    if explicit_sort:
        active_filter_pills.append({
            "key": "sort",
            "label": "排序",
            "value": SORT_LABELS.get(explicit_sort, explicit_sort),
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"sort"}, reset_pagination=True
            ),
        })

    # 6. 为结果卡片构造友好的类型中文名与标签跳转链接
    display_items: list[dict[str, Any]] = []
    for it in items:
        tag_links = []
        for t in (it.tags or []):
            t_obj = {
                "id": t["id"],
                "tag_ids": [t["id"]],
                "name": t["name"],
                "slug": t.get("slug") or str(t["id"]),
            }
            tag_links.append({
                "id": t["id"],
                "name": t["name"],
                "slug": t.get("slug") or str(t["id"]),
                "is_active": (t["id"] in active_tag_ids_set) or ((t["name"] or "").strip().lower() in active_tag_norm_keys),
                "url": _build_search_url(canonical_url, base_state, toggle_tag=t_obj, reset_pagination=True),
            })
        display_items.append({
            "item": it,
            "type_label": get_type_display_label(it.type_key),
            "tag_links": tag_links,
        })

    selected_tag_tokens = [g["token"] for g in selected_tag_groups]
    active_filters_state = {
        "query": search_query,
        "title_only": title_only,
        "type": type_key,
        "tags": selected_tag_tokens,
        "year": year_str if year else "",
        "days": days,
        "date_from": parsed_date_from.isoformat() if parsed_date_from else "",
        "date_to": parsed_date_to.isoformat() if parsed_date_to else "",
        "sort": effective_sort,
        "explicit_sort": explicit_sort,
        "seek_date": seek_date.isoformat() if seek_date else "",
    }

    context = {
        "search_query": search_query,
        "title_only": title_only,
        "query_result": result,
        "items": items,
        "display_items": display_items,
        "total_count": total_count,
        "facets": facets,
        "archive_groups": archive_groups,
        "next_cursor": next_cursor,
        "previous_cursor": previous_cursor,
        "cursor_reset": cursor_reset,
        "expansion_used": expansion_used,
        "secondary_used": secondary_used,
        "show_expand_button": show_expand_button,
        "show_expand_url": show_expand_url,
        "selected_type": type_key,
        "selected_tag": selected_tag_tokens[0] if selected_tag_tokens else "",
        "selected_tag_tokens": selected_tag_tokens,
        "selected_year": str(year) if year else "",
        "selected_days": days,
        "date_from": parsed_date_from.isoformat() if parsed_date_from else "",
        "date_to": parsed_date_to.isoformat() if parsed_date_to else "",
        "effective_sort": effective_sort,
        "type_options": type_options,
        "tag_facet_items": tag_facet_items,
        "year_facet_items": year_facet_items,
        "days_options": days_options,
        "active_filter_pills": active_filter_pills,
        "reset_url": canonical_url,
        "canonical_url": canonical_url,
        "pagination_vm": pagination_vm,
        "error_message": error_message,
        "http_status": http_status,
    }

    is_ajax = bool(
        request.headers.get("X-Listing-Response") == "fragment-v1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.GET.get("format") == "partial"
    )

    if is_ajax:
        html_fragment = render_to_string(
            "search/partials/_search_content.html",
            context,
            request=request,
        )
        full_url = _build_search_url(canonical_url, base_state, reset_pagination=False)
        return JsonResponse(
            {
                "html": html_fragment,
                "canonical_url": canonical_url,
                "full_url": full_url,
                "total_count": total_count,
                "cursor_reset": cursor_reset,
                "facets": facets,
                "next_cursor": next_cursor,
                "previous_cursor": previous_cursor,
                "error_message": error_message,
                "active_filters": active_filters_state,
            },
            status=http_status,
        )

    return TemplateResponse(
        request,
        "search/search.html",
        context,
        status=http_status,
    )
