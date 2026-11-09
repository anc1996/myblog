"""
列表请求参数解析与安全清洗服务 (Listing Parameters Parser)。

落实 RFC 规范：
1. 严格白名单参数提取，防范 SQL 参数注入与超长恶意参数；
2. 规范化布尔开关 (title_only, include_descendants)；
3. 智能默认排序（有关键词优先按相关度 relevance，无关键词按成文日期 authored_desc）；
4. 动态读取已声明专属筛选器的 query_param。
"""

import logging
from datetime import date
from typing import Any
from discovery.listing.contracts import ListingPolicy, ListingFilters
from discovery.listing.registry import get_listing_filter

logger = logging.getLogger(__name__)

TRUE_VALUES = {"1", "true", "on", "yes"}
FALSE_VALUES = {"0", "false", "off", "no"}
ALLOWED_SORTS = {"authored_desc", "authored_asc", "relevance"}


def parse_listing_filters(request: Any, policy: ListingPolicy) -> ListingFilters:
    """从 HTTP GET 请求安全提取并清洗列表查询参数"""
    get_params = getattr(request, "GET", {})

    # 1. 搜索词与仅搜标题开关
    raw_q = (get_params.get("q") or "").strip()[:200]
    title_only_val = get_params.get("title_only")
    title_only = title_only_val.lower() in TRUE_VALUES if title_only_val else False

    # 2. 包含子栏目开关
    if not policy.descendants_toggle:
        include_descendants = policy.descendants_default
    else:
        inc_val = get_params.get("include_descendants")
        if inc_val is not None:
            include_descendants = inc_val.lower() in TRUE_VALUES
        else:
            include_descendants = policy.descendants_default

    # 3. 成文日期闭区间
    date_from = None
    date_to = None
    df_str = get_params.get("date_from")
    if df_str:
        try:
            date_from = date.fromisoformat(df_str.strip())
        except (ValueError, TypeError):
            date_from = None

    dt_str = get_params.get("date_to")
    if dt_str:
        try:
            date_to = date.fromisoformat(dt_str.strip())
        except (ValueError, TypeError):
            date_to = None

    # 4. 排序规则判定
    raw_sort = (get_params.get("sort") or "").strip().lower()
    if raw_sort in ALLOWED_SORTS:
        effective_sort = raw_sort
    else:
        # 有关键词且未指定排序时默认相关度，无关键词默认成文日期倒序
        effective_sort = "relevance" if raw_q else "authored_desc"

    # 5. 分页游标与每页数量
    cursor = (get_params.get("cursor") or "").strip() or None
    navigation_cursor = (get_params.get("navigation_cursor") or "").strip() or None

    try:
        page_size_val = int(get_params.get("page_size", policy.page_size))
    except (ValueError, TypeError):
        page_size_val = policy.page_size
    page_size = max(1, min(page_size_val, policy.max_page_size))

    # 6. 专属筛选器参数解析
    extra_filters: dict[str, Any] = {}
    for f_key in policy.filter_keys:
        filter_def = get_listing_filter(f_key)
        if filter_def:
            val = get_params.get(filter_def.query_param)
            if val is not None and str(val).strip():
                extra_filters[filter_def.query_param] = str(val).strip()

    return ListingFilters(
        q=raw_q,
        title_only=title_only,
        include_descendants=include_descendants,
        date_from=date_from,
        date_to=date_to,
        sort=effective_sort,
        cursor=cursor,
        navigation_cursor=navigation_cursor,
        page_size=page_size,
        extra_filters=extra_filters,
    )
