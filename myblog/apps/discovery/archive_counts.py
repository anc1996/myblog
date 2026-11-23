"""
全站按日归档预聚合引擎、Transactional Outbox 处理与月历查询服务 (Archive Counts & Calendar)。

设计要点：
1. 口径按文章 `authored_on`（成文日期）统计入桶，与全站列表/RSS 一致；
2. 单页发布、修改成文日期、下架、移入回收站或恢复时，以固定锁顺序原子执行 +/-1 增量维护；
3. 大子树批量变动写入 ArchiveSyncOutbox，支持幂等重放与 generation 双代次平滑对账切换；
4. 打开月历最多读取当月 31 个日桶，彻底杜绝千万级规模下冷缓存全表 GROUP BY。
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from discovery.eligibility import apply_public_eligibility_filter
from discovery.models import (
    ArchiveContribution,
    ArchiveCountState,
    ArchiveDailyCount,
    ArchiveSyncOutbox,
    ContentDiscoveryEpoch,
    PublishedSearchDocument,
)
from discovery.registry import get_archive_enabled_type_keys, get_public_type_slug_map

logger = logging.getLogger(__name__)


def format_compact_count(count: int) -> str:
    """将海量篇数格式化为紧凑可读徽章（如 999、1.2万、100万），悬停仍展示精确数字。"""
    val = int(count or 0)
    if val < 10000:
        return f"{val:,}"
    if val < 100000000:
        wan = val / 10000.0
        return f"{int(wan)}万" if wan.is_integer() or val >= 1000000 else f"{wan:.1f}万"
    yi = val / 100000000.0
    return f"{yi:.1f}亿"


def _compute_heat_level(count: int, max_count: int) -> int:
    """按当月单日峰值计算 0~4 档热力等级（类 GitHub Contributions 热力日历）。"""
    if count <= 0:
        return 0
    denom = max(int(max_count or 1), 4)
    ratio = count / denom
    if ratio >= 0.75:
        return 4
    if ratio >= 0.50:
        return 3
    if ratio >= 0.25:
        return 2
    return 1


def _apply_bucket_delta(
    *,
    generation: int,
    site_id: int,
    type_key: str,
    bucket_date: date,
    delta: int,
) -> None:
    """在持有事务锁前提下安全增减指定日桶计数（受非负约束保护）。"""
    if delta == 0:
        return
    row = (
        ArchiveDailyCount.objects.select_for_update()
        .filter(
            generation=generation,
            site_id=site_id,
            type_key=type_key,
            bucket_date=bucket_date,
        )
        .first()
    )
    if delta > 0:
        if row is None:
            ArchiveDailyCount.objects.create(
                generation=generation,
                site_id=site_id,
                type_key=type_key,
                bucket_date=bucket_date,
                article_count=delta,
            )
        else:
            row.article_count += delta
            row.save(update_fields=["article_count"])
    elif row is not None:
        new_val = max(0, row.article_count + delta)
        if new_val == 0:
            row.delete()
        else:
            row.article_count = new_val
            row.save(update_fields=["article_count"])


def sync_page_archive_contribution(page_id: int) -> None:
    """
    单页归档计数幂等增量同步：
    核验页面最新公开资格与 archive_enabled 资格，对比 ArchiveContribution 旧记录，
    按固定桶键序更新旧桶 -1、新桶 +1。
    """
    if not page_id:
        return

    archive_types = set(get_archive_enabled_type_keys())

    with transaction.atomic():
        state, _ = ArchiveCountState.objects.select_for_update().get_or_create(
            name="global",
            defaults={"active_generation": 1, "is_stale": False},
        )
        gen = state.active_generation

        eligible_qs = apply_public_eligibility_filter(
            PublishedSearchDocument.objects.filter(
                page_id=page_id,
                is_live=True,
                is_recycled=False,
                index_generation=1,
            )
        )
        doc = eligible_qs.values("site_id", "type_key", "authored_on", "effective_revision_id").first()

        new_target: tuple[int, str, date, int] | None = None
        if doc and doc["type_key"] in archive_types and doc["authored_on"] is not None:
            new_target = (
                int(doc["site_id"] or 0),
                str(doc["type_key"]),
                doc["authored_on"],
                int(doc["effective_revision_id"] or 0),
            )

        old_contribs = list(
            ArchiveContribution.objects.select_for_update()
            .filter(generation=gen, page_id=page_id)
            .order_by("site_id")
        )

        if (
            new_target is not None
            and len(old_contribs) == 1
            and old_contribs[0].site_id == new_target[0]
            and old_contribs[0].type_key == new_target[1]
            and old_contribs[0].bucket_date == new_target[2]
        ):
            if old_contribs[0].effective_revision_id != new_target[3]:
                old_contribs[0].effective_revision_id = new_target[3]
                old_contribs[0].save(update_fields=["effective_revision_id", "updated_at"])
            return

        deltas: dict[tuple[int, str, date], int] = {}
        for old in old_contribs:
            key = (old.site_id, old.type_key, old.bucket_date)
            deltas[key] = deltas.get(key, 0) - 1
            old.delete()

        if new_target is not None:
            n_site, n_type, n_date, n_rev = new_target
            key = (n_site, n_type, n_date)
            deltas[key] = deltas.get(key, 0) + 1
            ArchiveContribution.objects.create(
                generation=gen,
                site_id=n_site,
                page_id=page_id,
                type_key=n_type,
                bucket_date=n_date,
                effective_revision_id=n_rev,
            )

        for (b_site, b_type, b_date) in sorted(deltas.keys()):
            delta = deltas[(b_site, b_type, b_date)]
            if delta != 0:
                _apply_bucket_delta(
                    generation=gen,
                    site_id=b_site,
                    type_key=b_type,
                    bucket_date=b_date,
                    delta=delta,
                )


def enqueue_subtree_archive_reconcile(root_path: str = "", page_id: int | None = None) -> ArchiveSyncOutbox:
    """大子树或权限批量变更时在同一事务中记脏并入队 Outbox，避免长事务阻塞。"""
    with transaction.atomic():
        ArchiveCountState.objects.update_or_create(
            name="global",
            defaults={"is_stale": True},
        )
        return ArchiveSyncOutbox.objects.create(
            event_type="subtree_reconcile",
            page_id=page_id,
            root_path=root_path or "",
            status="pending",
        )


def process_archive_outbox(limit: int = 100) -> dict[str, int]:
    """消费待处理的归档同步 Outbox 任务（支持重复安全重放）。"""
    processed = 0
    failed = 0
    has_subtree_reconcile = False

    pending_ids = list(
        ArchiveSyncOutbox.objects.filter(status="pending")
        .order_by("created_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    for oid in pending_ids:
        with transaction.atomic():
            ev = ArchiveSyncOutbox.objects.select_for_update().filter(id=oid, status="pending").first()
            if not ev:
                continue
            ev.status = "processing"
            ev.attempts += 1
            ev.save(update_fields=["status", "attempts"])

        try:
            if ev.event_type == "subtree_reconcile":
                has_subtree_reconcile = True
            elif ev.event_type == "page_sync" and ev.page_id:
                sync_page_archive_contribution(ev.page_id)
            ArchiveSyncOutbox.objects.filter(id=oid).update(
                status="done",
                processed_at=timezone.now(),
            )
            processed += 1
        except Exception as exc:
            logger.exception("Archive outbox event %s failed: %s", oid, exc)
            ArchiveSyncOutbox.objects.filter(id=oid).update(
                status="failed",
                last_error=str(exc)[:1000],
            )
            failed += 1

    if has_subtree_reconcile:
        reconcile_archive_counts(apply=True)

    return {"processed": processed, "failed": failed}


def reconcile_archive_counts(*, apply: bool = False, batch_size: int = 1000) -> dict[str, Any]:
    """
    按双代次（next_generation）对账与重建 ArchiveDailyCount：
    - apply=False（默认）：只读比对当前汇总与真实公开候选集差异，不落库修改；
    - apply=True：构建新代次数据后在短事务内原子切换 active_generation 并清除 is_stale。
    """
    archive_types = list(get_archive_enabled_type_keys())
    active_gen, is_stale = ArchiveCountState.get_active_generation_readonly()

    base_qs = apply_public_eligibility_filter(
        PublishedSearchDocument.objects.filter(
            is_live=True,
            is_recycled=False,
            index_generation=1,
            type_key__in=archive_types,
        )
    )

    expected_buckets: dict[tuple[int, str, date], int] = {}
    contribs: list[tuple[int, int, str, date, int]] = []

    for row in base_qs.values("page_id", "site_id", "type_key", "authored_on", "effective_revision_id").iterator(chunk_size=batch_size):
        if row["authored_on"] is None:
            continue
        s_id = int(row["site_id"] or 0)
        t_key = str(row["type_key"])
        b_date = row["authored_on"]
        expected_buckets[(s_id, t_key, b_date)] = expected_buckets.get((s_id, t_key, b_date), 0) + 1
        contribs.append((s_id, int(row["page_id"]), t_key, b_date, int(row["effective_revision_id"] or 0)))

    current_rows = {
        (r["site_id"], r["type_key"], r["bucket_date"]): int(r["article_count"])
        for r in ArchiveDailyCount.objects.filter(generation=active_gen).values(
            "site_id", "type_key", "bucket_date", "article_count"
        )
    }

    all_keys = set(expected_buckets.keys()) | set(current_rows.keys())
    diffs = [
        {
            "site_id": k[0],
            "type_key": k[1],
            "bucket_date": k[2].isoformat(),
            "expected": expected_buckets.get(k, 0),
            "actual": current_rows.get(k, 0),
        }
        for k in sorted(all_keys)
        if expected_buckets.get(k, 0) != current_rows.get(k, 0)
    ]

    if not apply:
        return {
            "mode": "dry_run",
            "active_generation": active_gen,
            "is_stale": is_stale,
            "diff_count": len(diffs),
            "diffs": diffs[:50],
            "total_expected_articles": len(contribs),
        }

    with transaction.atomic():
        state, _ = ArchiveCountState.objects.select_for_update().get_or_create(
            name="global",
            defaults={"active_generation": 1, "is_stale": False},
        )
        next_gen = state.active_generation + 1

        ArchiveDailyCount.objects.filter(generation=next_gen).delete()
        ArchiveContribution.objects.filter(generation=next_gen).delete()

        daily_objs = [
            ArchiveDailyCount(
                generation=next_gen,
                site_id=k[0],
                type_key=k[1],
                bucket_date=k[2],
                article_count=cnt,
            )
            for k, cnt in expected_buckets.items()
            if cnt > 0
        ]
        if daily_objs:
            ArchiveDailyCount.objects.bulk_create(daily_objs, batch_size=batch_size)

        contrib_objs = [
            ArchiveContribution(
                generation=next_gen,
                site_id=c[0],
                page_id=c[1],
                type_key=c[2],
                bucket_date=c[3],
                effective_revision_id=c[4],
            )
            for c in contribs
        ]
        if contrib_objs:
            ArchiveContribution.objects.bulk_create(contrib_objs, batch_size=batch_size)

        old_gen = state.active_generation
        state.active_generation = next_gen
        state.is_stale = False
        state.save(update_fields=["active_generation", "is_stale", "updated_at"])

        if old_gen != next_gen:
            ArchiveDailyCount.objects.filter(generation__lt=next_gen).delete()
            ArchiveContribution.objects.filter(generation__lt=next_gen).delete()

        ContentDiscoveryEpoch.increment_epoch()

    return {
        "mode": "applied",
        "active_generation": next_gen,
        "diff_count": len(diffs),
        "total_articles": len(contribs),
    }


def get_archive_calendar_data(
    *,
    site_id: int | None,
    year: int | None = None,
    month: int | None = None,
    type_slug: str | None = None,
    base_url: str = "/timeline/",
) -> dict[str, Any]:
    """
    读取指定月份的日历表数据（只读 ArchiveDailyCount 当月最多 31 个日桶，绝不扫描千万级文章表）。
    """
    site_today = timezone.localdate()
    target_year = year or site_today.year
    if target_year < 1970:
        target_year = 1970
    elif target_year > 2100:
        target_year = 2100
    target_month = month or site_today.month
    if target_month < 1 or target_month > 12:
        target_month = site_today.month

    month_start = date(target_year, target_month, 1)
    if target_month == 12:
        next_month_start = date(target_year + 1, 1, 1)
    else:
        next_month_start = date(target_year, target_month + 1, 1)

    archive_types = list(get_archive_enabled_type_keys())
    type_map = get_public_type_slug_map()
    filter_type_keys = archive_types
    invalid_type = False
    if type_slug:
        if type_slug in type_map and type_map[type_slug] in archive_types:
            filter_type_keys = [type_map[type_slug]]
        else:
            invalid_type = True
            filter_type_keys = []

    active_gen, is_stale = ArchiveCountState.get_active_generation_readonly()
    norm_site_id = int(site_id or 0)

    day_counts: dict[date, int] = {}
    if not invalid_type and filter_type_keys:
        rows = (
            ArchiveDailyCount.objects.filter(
                generation=active_gen,
                site_id=norm_site_id,
                type_key__in=filter_type_keys,
                bucket_date__gte=month_start,
                bucket_date__lt=next_month_start,
                bucket_date__lte=site_today,
            )
            .values("bucket_date")
            .annotate(day_total=Sum("article_count"))
        )
        for r in rows:
            day_counts[r["bucket_date"]] = int(r["day_total"] or 0)

    year_start = date(target_year, 1, 1)
    next_year_start = date(target_year + 1, 1, 1)
    year_month_totals: dict[int, int] = {m: 0 for m in range(1, 13)}
    if not invalid_type and filter_type_keys:
        y_rows = (
            ArchiveDailyCount.objects.filter(
                generation=active_gen,
                site_id=norm_site_id,
                type_key__in=filter_type_keys,
                bucket_date__gte=year_start,
                bucket_date__lt=next_year_start,
                bucket_date__lte=site_today,
            )
            .values("bucket_date__month")
            .annotate(m_total=Sum("article_count"))
        )
        for yr in y_rows:
            m_idx = int(yr["bucket_date__month"])
            year_month_totals[m_idx] = int(yr["m_total"] or 0)

    # 按年份聚合总篇数（带 Redis 短缓存，绑定 epoch + generation + site_today，跨几十年也无需每次扫表）
    from django.core.cache import cache
    epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()
    years_cache_key = (
        f"myblog:archyears:v1:s{norm_site_id}:e{epoch}:g{active_gen}:"
        f"d{site_today.isoformat()}:t{type_slug or 'all'}"
    )
    year_totals_map: dict[int, int] | None = cache.get(years_cache_key) if not is_stale else None
    if year_totals_map is None:
        year_totals_map = {}
        if not invalid_type and filter_type_keys:
            yr_agg = (
                ArchiveDailyCount.objects.filter(
                    generation=active_gen,
                    site_id=norm_site_id,
                    type_key__in=filter_type_keys,
                    bucket_date__lte=site_today,
                )
                .values("bucket_date__year")
                .annotate(y_total=Sum("article_count"))
                .order_by("-bucket_date__year")
            )
            for r in yr_agg:
                if r["bucket_date__year"] is not None:
                    year_totals_map[int(r["bucket_date__year"])] = int(r["y_total"] or 0)
        if not is_stale:
            cache.set(years_cache_key, year_totals_map, timeout=180)

    available_years = sorted(set(year_totals_map.keys()) | {site_today.year, target_year}, reverse=True)
    type_qs = f"&type={type_slug}" if type_slug else ""

    # 构建年代（Decades: 2020s, 2010s, 2000s, 1990s...）双层折叠导航，支持一键找 2002 等任意历史年份
    min_known_year = min(available_years) if available_years else 1990
    min_decade = min((min_known_year // 10) * 10, (target_year // 10) * 10, 1990)
    max_decade = max((site_today.year // 10) * 10, (target_year // 10) * 10)
    active_decade = (target_year // 10) * 10

    decades: list[dict[str, Any]] = []
    for dec in range(max_decade, min_decade - 10, -10):
        dec_years_raw = [y for y in range(min(dec + 9, max(site_today.year, target_year)), dec - 1, -1)]
        if not dec_years_raw:
            continue
        dec_total = sum(year_totals_map.get(y, 0) for y in dec_years_raw)
        active_years_in_dec = [y for y in dec_years_raw if year_totals_map.get(y, 0) > 0]
        jump_year = active_years_in_dec[0] if active_years_in_dec else dec_years_raw[0]
        decades.append({
            "decade": dec,
            "label": f"{dec}年代",
            "range_label": f"{dec}–{dec + 9}",
            "count": dec_total,
            "count_display": format_compact_count(dec_total),
            "is_active": (dec == active_decade),
            "jump_year": jump_year,
            "url": f"{base_url}?year={jump_year}&month={target_month}{type_qs}",
            "years": [
                {
                    "year": y,
                    "count": year_totals_map.get(y, 0),
                    "count_display": format_compact_count(year_totals_map.get(y, 0)),
                    "is_active": (y == target_year),
                    "has_items": year_totals_map.get(y, 0) > 0,
                    "url": f"{base_url}?year={y}&month={target_month}{type_qs}",
                }
                for y in dec_years_raw
            ],
        })

    active_decade_years = next(
        (d["years"] for d in decades if d["decade"] == active_decade),
        [],
    )

    selectable_years_set = set(range(max(site_today.year, target_year), min_decade - 1, -1)) | set(available_years)
    years_summary = [
        {
            "year": y,
            "count": year_totals_map.get(y, 0),
            "count_display": format_compact_count(year_totals_map.get(y, 0)),
            "is_active": (y == target_year),
            "has_items": year_totals_map.get(y, 0) > 0,
            "url": f"{base_url}?year={y}&month={target_month}{type_qs}",
        }
        for y in sorted(selectable_years_set, reverse=True)
    ]

    cal = calendar.Calendar(firstweekday=0)
    weeks: list[list[dict[str, Any]]] = []
    days_flat: list[dict[str, Any]] = []
    month_count = 0
    max_day_count = max(day_counts.values()) if day_counts else 0

    for week_dates in cal.monthdatescalendar(target_year, target_month):
        week_cells: list[dict[str, Any]] = []
        for d in week_dates:
            in_month = (d.month == target_month and d.year == target_year)
            cnt = day_counts.get(d, 0) if in_month else 0
            if in_month:
                month_count += cnt
            day_url = f"{base_url}?year={target_year}&month={target_month}&day={d.isoformat()}{type_qs}" if (in_month and cnt > 0) else None
            heat_level = _compute_heat_level(cnt, max_day_count) if in_month else 0
            cell = {
                "date": d.isoformat(),
                "day": d.day,
                "in_month": in_month,
                "count": cnt,
                "count_display": format_compact_count(cnt),
                "heat_level": heat_level,
                "is_today": (d == site_today),
                "url": day_url,
            }
            week_cells.append(cell)
            if in_month:
                days_flat.append({
                    "date": d.isoformat(),
                    "count": cnt,
                    "count_display": format_compact_count(cnt),
                    "heat_level": heat_level,
                    "url": day_url,
                })
        weeks.append(week_cells)

    prev_month_end = month_start - timedelta(days=1)
    prev_month_str = f"{prev_month_end.year:04d}-{prev_month_end.month:02d}"
    next_month_str = f"{next_month_start.year:04d}-{next_month_start.month:02d}"
    prev_year = max(1970, target_year - 1)
    next_year = min(2100, target_year + 1)
    year_total_count = sum(year_month_totals.values())

    year_months = [
        {
            "month": m,
            "label": f"{m}月",
            "count": year_month_totals.get(m, 0),
            "count_display": format_compact_count(year_month_totals.get(m, 0)),
            "is_active": (m == target_month),
            "url": f"{base_url}?year={target_year}&month={m}{type_qs}",
        }
        for m in range(1, 13)
    ]

    return {
        "year": target_year,
        "year_count": year_total_count,
        "year_count_display": format_compact_count(year_total_count),
        "month": f"{target_year:04d}-{target_month:02d}",
        "month_num": target_month,
        "date_basis": "authored_on",
        "timezone": timezone.get_current_timezone_name(),
        "month_count": month_count,
        "month_count_display": format_compact_count(month_count),
        "max_day_count": max_day_count,
        "days": days_flat,
        "weeks": weeks,
        "year_months": year_months,
        "available_years": available_years,
        "years_summary": years_summary,
        "decades": decades,
        "active_decade": active_decade,
        "active_decade_years": active_decade_years,
        "previous_year": {
            "year": prev_year,
            "url": f"{base_url}?year={prev_year}&month={target_month}{type_qs}",
        },
        "next_year": {
            "year": next_year,
            "url": f"{base_url}?year={next_year}&month={target_month}{type_qs}",
        },
        "previous_month": {
            "month": prev_month_str,
            "year": prev_month_end.year,
            "month_num": prev_month_end.month,
            "url": f"{base_url}?year={prev_month_end.year}&month={prev_month_end.month}{type_qs}",
        },
        "next_month": {
            "month": next_month_str,
            "year": next_month_start.year,
            "month_num": next_month_start.month,
            "url": f"{base_url}?year={next_month_start.year}&month={next_month_start.month}{type_qs}",
        },
        "today_url": f"{base_url}?year={site_today.year}&month={site_today.month}{type_qs}",
        "generation": active_gen,
        "status": "updating" if is_stale else "ready",
        "as_of": site_today.isoformat(),
    }
