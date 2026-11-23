"""
千万级规模混合分页协议与有界计数工具 (Pagination & Bounded Counting)。

核心原则：
1. 浅页窗口可直跳页码（默认上限 min(100, 1 + floor(2000 / page_size))），绝不放行百万级 OFFSET；
2. 超过浅页窗口返回 PageWindowExceededError（HTTP 422 + 日期/游标定位指引）；
3. 深页翻页走双向 Keyset 游标（v2:...）或按日期定位（seek_date / seek_direction="oldest"）；
4. 小集合（<= 5000）返回精确总数，大集合优先读取预聚合汇总或明标下限（"超过 5,000 篇"）。
"""

from __future__ import annotations

from datetime import date
from typing import Any
from django.utils import timezone

MAX_SHALLOW_ITEMS = 2000
MAX_SHALLOW_PAGE_CAP = 100
COUNT_PROBE_LIMIT = 5000


class PageWindowExceededError(Exception):
    """请求的页码超出浅页直跳窗口上限时抛出，防止深 OFFSET 拖垮数据库。"""

    def __init__(self, requested_page: int, max_jump_page: int) -> None:
        self.requested_page = requested_page
        self.max_jump_page = max_jump_page
        super().__init__(
            f"Requested page {requested_page} exceeds shallow window limit {max_jump_page}."
        )


def compute_max_jump_page(page_size: int) -> int:
    safe_size = max(1, int(page_size or 20))
    return min(MAX_SHALLOW_PAGE_CAP, 1 + (MAX_SHALLOW_ITEMS // safe_size))


def build_count_meta(
    *,
    value: int,
    kind: str,
    as_of: str | None = None,
) -> dict[str, Any]:
    """构造带明确语义的总数元数据（exact | summary | lower_bound）。"""
    stamp = as_of or timezone.now().isoformat(timespec="seconds")
    if kind == "summary":
        label = f"共 {value:,} 篇（汇总）"
    elif kind == "lower_bound":
        label = f"超过 {value:,} 篇"
    else:
        kind = "exact"
        label = f"共 {value:,} 篇"
    return {
        "value": int(value),
        "kind": kind,
        "label": label,
        "as_of": stamp,
    }


def build_page_links(
    *,
    current_page: int | None,
    reachable_last_page: int,
    max_jump_page: int,
) -> list[dict[str, Any]]:
    """
    生成带省略号的局部页码窗口（如 `1 ... 4 5 [6] 7 8 ... 100`）。
    当 current_page 为 None（例如游标深翻或日期定位结果）时，不渲染伪造的绝对页码。
    """
    if current_page is None or reachable_last_page <= 0:
        return []

    last = min(reachable_last_page, max_jump_page)
    if last == 1:
        return [{"type": "page", "page": 1, "label": "1", "is_current": current_page == 1}]

    pages_to_show: set[int] = {1, last}
    for p in range(max(1, current_page - 2), min(last, current_page + 2) + 1):
        pages_to_show.add(p)

    sorted_pages = sorted(pages_to_show)
    links: list[dict[str, Any]] = []
    prev = 0
    for p in sorted_pages:
        if prev and p - prev > 1:
            links.append({"type": "ellipsis", "page": None, "label": "…", "is_current": False})
        links.append({
            "type": "page",
            "page": p,
            "label": str(p),
            "is_current": (p == current_page),
        })
        prev = p
    return links


def build_pagination_payload(
    *,
    mode: str,
    current_page: int | None,
    page_size: int,
    total_count: int,
    count_kind: str,
    has_next: bool,
    has_previous: bool,
    next_cursor: str | None,
    previous_cursor: str | None,
) -> dict[str, Any]:
    """构造统一的 pagination 协议对象，供模板、AJAX 与 JSON API 共用。"""
    max_jump_page = compute_max_jump_page(page_size)
    total_pages: int | None = None
    if count_kind in ("exact", "summary"):
        total_pages = max(1, (total_count + page_size - 1) // page_size) if total_count > 0 else 1
        reachable_last = min(total_pages, max_jump_page)
        is_window_capped = total_pages > max_jump_page
    else:
        reachable_last = max_jump_page
        is_window_capped = True

    page_links = build_page_links(
        current_page=current_page,
        reachable_last_page=reachable_last,
        max_jump_page=max_jump_page,
    )

    return {
        "mode": mode,
        "page": current_page,
        "page_size": page_size,
        "max_jump_page": max_jump_page,
        "total_pages": total_pages,
        "reachable_last_page": reachable_last,
        "is_window_capped": is_window_capped,
        "page_links": page_links,
        "has_next": bool(has_next),
        "has_previous": bool(has_previous),
        "next_cursor": next_cursor,
        "previous_cursor": previous_cursor,
    }


def try_summary_count_from_archive_daily(
    *,
    site_id: int | None,
    type_keys: list[str],
    date_from: date | None,
    date_to: date | None,
) -> int | None:
    """
    当查询超过 COUNT_PROBE_LIMIT 时，若筛选条件仅涉及站点、支持归档的类型与日期区间，
    直接从 ArchiveDailyCount 读取预聚合总和（仅扫描少数日桶行，不扫描千万级主表）。
    """
    try:
        from django.db.models import Sum
        from discovery.models import ArchiveCountState, ArchiveDailyCount
        from discovery.registry import get_archive_enabled_type_keys

        active_gen, is_stale = ArchiveCountState.get_active_generation_readonly()
        if is_stale:
            return None

        archive_types = set(get_archive_enabled_type_keys())
        if not archive_types:
            return None

        if type_keys:
            if not set(type_keys).issubset(archive_types):
                return None
            target_types = list(type_keys)
        else:
            target_types = list(archive_types)

        qs = ArchiveDailyCount.objects.filter(
            generation=active_gen,
            site_id=(site_id or 0),
            type_key__in=target_types,
        )
        if date_from:
            qs = qs.filter(bucket_date__gte=date_from)
        if date_to:
            qs = qs.filter(bucket_date__lte=date_to)

        if not ArchiveDailyCount.objects.filter(generation=active_gen, site_id=(site_id or 0)).exists():
            return None

        total = qs.aggregate(s=Sum("article_count"))["s"]
        return int(total or 0)
    except Exception:
        return None
