"""
analytics.scoring
第 2 期口碑降噪算法、千万级安全 P90 高浏览阈值、滞回告警状态机与榜单快照计算引擎。

核心工程与统计约束：
1. 口碑平滑与 Wilson 下界：
   - 分值映射：excellent=1.00, good=0.75, pass=0.45, poor=0.00；
   - 权重：member=1.0, new_member=0.5（new_member 仅入后台参考分 R_all，不触发也不解除低口碑）；
   - R_trusted = (20 * 0.65 + sum_s_trusted) / (20 + n_trusted)；
   - 95% Wilson 下界 W (z=1.96)；
2. 千万级容量红线：
   - 严禁拉取全站页面 ID 到 Python 计算分位或排行；
   - P90 在 PostgreSQL 内以 values("page_id").annotate(org_pv=Sum("organic_pv")) + COUNT +
     ORDER BY -org_pv LIMIT 1 OFFSET floor(0.1*N) 计算；组内 < 20 页退回 500；
   - 榜单按 type_key 在 PostgreSQL 预筛选 Top-200 后做组内归一化并应用降权因子；
3. 滞回防抖：
   - 同一 (site, page, live_revision_id, alert_kind) 在同一 stat_date 至多评估一次；
   - 第 1 个健康日命中置 observing，连续 2 个健康日命中置 active；
   - 连续 3 个健康日满足恢复条件才置 resolved；缺测日不推进也不解除活动告警。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import logging
import math
from typing import Any

from django.db import DatabaseError, transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Sum
from django.utils import timezone
from wagtail.models import Page, PageViewRestriction, Site

from analytics.models import (
    AnalyticsAlert,
    AnalyticsSettings,
    ArticleRating,
    PageAnalyticsDaily,
    RankingSnapshot,
    ScopeAnalyticsDaily,
)
from content_lifecycle.models import RecycleEntry
from content_lifecycle.public_access import get_public_blocked_page_ids
from discovery.registry import get_analytics_capabilities, list_analytics_adapters

logger = logging.getLogger(__name__)

GRADE_SCORES: dict[str, float] = {
    "excellent": 1.00,
    "good": 0.75,
    "pass": 0.45,
    "poor": 0.00,
}
TRUST_WEIGHTS: dict[str, float] = {
    "member": 1.0,
    "new_member": 0.5,
}

BAYESIAN_PRIOR_N = 20.0
BAYESIAN_PRIOR_SCORE = 0.65
WILSON_Z = 1.96

REPUTATION_RULE_VERSION = "v1.0"
RANKING_RULE_VERSION = "v1.0"

_ACTIVE_RECYCLE_STATUSES = ("recycled", "restoring")
_RANK_BOARD_KINDS = ("surging", "popular_7d", "popular_30d", "reputation")
_MAX_CANDIDATES_PER_TYPE = 200
_MAX_SNAPSHOT_ITEMS = 100


def _coerce_site(site: Site | int | None) -> Site | None:
    if site is None:
        return None
    if isinstance(site, Site):
        return site
    return Site.objects.filter(id=int(site)).first()


def _coerce_date(value: date | str | None, *, default_days_ago: int = 1) -> date:
    if value is None:
        return timezone.localdate() - timedelta(days=default_days_ago)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def _wilson_lower_bound(poor_count: int, total_count: int, z: float = WILSON_Z) -> float:
    """计算 95% 置信度下差评比例的 Wilson 下界。"""
    if total_count <= 0 or poor_count <= 0:
        return 0.0
    n = float(total_count)
    p = min(1.0, max(0.0, float(poor_count) / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2.0 * n)
    variance = (p * (1.0 - p)) / n + z2 / (4.0 * n * n)
    margin = z * math.sqrt(max(0.0, variance))
    return max(0.0, min(1.0, (center - margin) / denom))


def get_recent_healthy_dates(
    site: Site | int,
    as_of_date: date | None = None,
    *,
    target_days: int = 30,
    max_lookback_days: int = 120,
) -> list[date]:
    """
    获取截至 as_of_date 的最近 target_days 个非缺测自然日（倒序返回）。
    显式排除 ScopeAnalyticsDaily 中站点级 health_status='missing' 的日期。
    """
    site_id = site.id if isinstance(site, Site) else int(site)
    end_date = as_of_date or timezone.localdate()
    lookback_start = end_date - timedelta(days=max(target_days, max_lookback_days))

    missing_dates = set(
        ScopeAnalyticsDaily.objects.filter(
            site_id=site_id,
            scope_kind="site",
            scope_key="all",
            stat_date__gte=lookback_start,
            stat_date__lte=end_date,
            health_status="missing",
        ).values_list("stat_date", flat=True)
    )

    healthy_dates: list[date] = []
    offset = 0
    max_steps = max(target_days, max_lookback_days)
    while len(healthy_dates) < target_days and offset < max_steps:
        day = end_date - timedelta(days=offset)
        offset += 1
        if day in missing_dates:
            continue
        healthy_dates.append(day)
    return healthy_dates


def _is_site_day_missing(site_id: int, stat_date: date) -> bool:
    status = (
        ScopeAnalyticsDaily.objects.filter(
            site_id=site_id,
            scope_kind="site",
            scope_key="all",
            stat_date=stat_date,
        )
        .values_list("health_status", flat=True)
        .first()
    )
    return status == "missing"


def _get_rank_eligible_type_keys() -> list[str]:
    keys: list[str] = []
    for adapter in list_analytics_adapters():
        caps = getattr(adapter, "analytics_capabilities", None)
        if caps and caps.kind == "detail" and getattr(caps, "rank_eligible", False):
            keys.append(adapter.type_key)
    return keys


def compute_reputation_metrics(
    site: Site | int,
    page: Page | int,
    live_revision_id: int,
    as_of_date: date | str | None = None,
) -> dict[str, Any]:
    """
    计算指定页面正式版本在近 30 个健康日内的贝叶斯平滑口碑分与 Wilson 下界。
    仅读取聚合计数，绝不返回任何评价者身份信息。
    """
    site_obj = _coerce_site(site)
    if site_obj is None:
        raise ValueError("Invalid site for compute_reputation_metrics")
    page_id = page.id if isinstance(page, Page) else int(page)
    rev_id = int(live_revision_id or 0)
    end_date = _coerce_date(as_of_date, default_days_ago=0) if as_of_date is not None else timezone.localdate()

    settings_obj = AnalyticsSettings.get_for_site_readonly(site_obj)
    min_votes = max(1, int(getattr(settings_obj, "min_rating_votes", 20) or 20))
    observe_votes = max(1, int(getattr(settings_obj, "observe_rating_votes", 5) or 5))

    healthy_dates = get_recent_healthy_dates(site_obj.id, end_date, target_days=30)

    # 仅在 PostgreSQL 侧按 (trust_tier, grade) 分组聚合，最多返回 8 行，不暴露任何 user_id
    agg_rows = list(
        ArticleRating.objects.filter(
            site_id=site_obj.id,
            page_id=page_id,
            live_revision_id=rev_id,
            grade__in=tuple(GRADE_SCORES.keys()),
            first_eligible_day__in=healthy_dates,
        )
        .values("trust_tier", "grade")
        .annotate(cnt=Count("id"))
    )

    return _build_reputation_metrics_from_agg_rows(
        agg_rows=agg_rows,
        min_votes=min_votes,
        observe_votes=observe_votes,
        window_healthy_days=len(healthy_dates),
        as_of_date=end_date,
    )


def _build_reputation_metrics_from_agg_rows(
    *,
    agg_rows: list[dict[str, Any]],
    min_votes: int,
    observe_votes: int,
    window_healthy_days: int,
    as_of_date: date,
) -> dict[str, Any]:
    trusted_counts = {g: 0 for g in GRADE_SCORES}
    new_member_counts = {g: 0 for g in GRADE_SCORES}

    n_trusted = 0
    sum_s_trusted = 0.0
    n_new = 0
    sum_s_new = 0.0

    for row in agg_rows:
        grade = row.get("grade")
        tier = row.get("trust_tier") or "member"
        cnt = int(row.get("cnt") or 0)
        if grade not in GRADE_SCORES or cnt <= 0:
            continue
        score = GRADE_SCORES[grade]
        if tier == "new_member":
            new_member_counts[grade] += cnt
            n_new += cnt
            sum_s_new += score * cnt
        else:
            trusted_counts[grade] += cnt
            n_trusted += cnt
            sum_s_trusted += score * cnt

    d_trusted = trusted_counts["poor"]
    r_trusted = (BAYESIAN_PRIOR_N * BAYESIAN_PRIOR_SCORE + sum_s_trusted) / (BAYESIAN_PRIOR_N + float(n_trusted))

    weighted_n_all = float(n_trusted) * TRUST_WEIGHTS["member"] + float(n_new) * TRUST_WEIGHTS["new_member"]
    weighted_s_all = sum_s_trusted * TRUST_WEIGHTS["member"] + sum_s_new * TRUST_WEIGHTS["new_member"]
    r_all = (BAYESIAN_PRIOR_N * BAYESIAN_PRIOR_SCORE + weighted_s_all) / (BAYESIAN_PRIOR_N + weighted_n_all)

    wilson_lower = _wilson_lower_bound(d_trusted, n_trusted, z=WILSON_Z)
    poor_ratio = (float(d_trusted) / float(n_trusted)) if n_trusted > 0 else 0.0

    is_low_reputation = bool(
        n_trusted >= min_votes
        and d_trusted >= 8
        and wilson_lower >= 0.20
        and r_trusted < 0.55
    )
    is_early_observe = bool(
        n_trusted >= observe_votes
        and n_trusted < min_votes
        and poor_ratio >= 0.40
    )
    is_recovery_satisfied = bool(
        n_trusted >= min_votes
        and (wilson_lower < 0.15 or r_trusted >= 0.60)
    )

    return {
        "n_trusted": n_trusted,
        "d_trusted": d_trusted,
        "sum_s_trusted": round(sum_s_trusted, 4),
        "n_new_member": n_new,
        "d_new_member": new_member_counts["poor"],
        "r_trusted": round(r_trusted, 4),
        "r_all": round(r_all, 4),
        "wilson_lower": round(wilson_lower, 4),
        "poor_ratio_trusted": round(poor_ratio, 4),
        "counts_trusted": trusted_counts,
        "counts_new_member": new_member_counts,
        "is_low_reputation": is_low_reputation,
        "is_early_observe": is_early_observe,
        "is_recovery_satisfied": is_recovery_satisfied,
        "min_rating_votes": min_votes,
        "observe_rating_votes": observe_votes,
        "window_healthy_days": window_healthy_days,
        "as_of_date": as_of_date.isoformat(),
        "rule_version": REPUTATION_RULE_VERSION,
    }


def _eligible_daily_queryset_for_site(site: Site, type_key: str | None = None):
    """
    构造千万级安全的已发布未回收页面日表查询：
    所有站点子树、上线态、非别名与回收站过滤全部下推至 PostgreSQL SQL，绝不拉全表 ID 到 Python。
    """
    recycled_subq = RecycleEntry.objects.filter(
        page_id=OuterRef("page_id"),
        status__in=_ACTIVE_RECYCLE_STATUSES,
    )
    qs = PageAnalyticsDaily.objects.filter(
        site_id=site.id,
        page__live=True,
        page__alias_of__isnull=True,
    ).exclude(health_status="missing").filter(~Exists(recycled_subq))

    if site.root_page_id:
        root_path = site.root_page.path
        qs = qs.filter(page__path__startswith=root_path, page__depth__gt=site.root_page.depth)
    if type_key:
        qs = qs.filter(type_key=type_key)
    return qs


def compute_type_high_pv_threshold(
    site: Site | int,
    type_key: str,
    as_of_date: date | str | None = None,
    *,
    healthy_dates: list[date] | None = None,
) -> dict[str, Any]:
    """
    千万级安全高 PV 阈值计算：
    在 PostgreSQL 按 (site, type_key) 对近 30 个完整健康日聚合自然流量 organic_pv，
    仅通过 SQL COUNT 与 ORDER BY -org_pv LIMIT 1 OFFSET floor(0.1*N) 计算 P90。
    组内 < 20 页直接退回 500，否则 max(high_pv_min, P90)。
    """
    site_obj = _coerce_site(site)
    if site_obj is None:
        return {"threshold": 500, "p90": None, "group_size": 0, "fallback": True}

    settings_obj = AnalyticsSettings.get_for_site_readonly(site_obj)
    high_pv_min = int(getattr(settings_obj, "high_pv_min", 300) or 300)
    end_date = _coerce_date(as_of_date, default_days_ago=1)
    dates_window = healthy_dates if healthy_dates is not None else get_recent_healthy_dates(
        site_obj.id, end_date, target_days=30
    )

    restricted_subq = PageViewRestriction.objects.filter(page_id=OuterRef("page_id"))
    grouped_qs = (
        _eligible_daily_queryset_for_site(site_obj, type_key=type_key)
        .filter(stat_date__in=dates_window)
        .filter(~Exists(restricted_subq))
        .values("page_id")
        .annotate(org_pv=Sum("organic_pv"))
    )

    n_pages = int(grouped_qs.count())
    if n_pages < 20:
        return {
            "threshold": 500,
            "p90": None,
            "group_size": n_pages,
            "high_pv_min": high_pv_min,
            "fallback": True,
        }

    offset = int(math.floor(0.1 * n_pages))
    p90_val = (
        grouped_qs.order_by("-org_pv", "page_id")
        .values_list("org_pv", flat=True)[offset : offset + 1]
        .first()
    )
    p90_int = max(0, int(p90_val or 0))
    threshold = max(high_pv_min, p90_int)
    return {
        "threshold": threshold,
        "p90": p90_int,
        "group_size": n_pages,
        "high_pv_min": high_pv_min,
        "fallback": False,
    }


def _evaluate_alert_transition(
    *,
    site_id: int,
    page_id: int,
    live_revision_id: int,
    alert_kind: str,
    stat_date: date,
    is_hit: bool,
    is_clear: bool,
    evidence: dict[str, Any],
    now: datetime,
) -> str:
    """
    单条告警滞回状态机流转（在原子事务内行锁执行）。
    同一 (site, page, live_revision_id, alert_kind) 在同一 stat_date 至多评估一次。
    返回状态变更动作描述: "skipped_duplicate" / "created_observing" / "promoted_active" / "resolved" / "updated" / "noop"。
    """
    with transaction.atomic():
        alert = (
            AnalyticsAlert.objects.select_for_update()
            .filter(
                site_id=site_id,
                page_id=page_id,
                live_revision_id=live_revision_id,
                alert_kind=alert_kind,
            )
            .first()
        )
        if alert is not None and alert.last_evaluated_date is not None and alert.last_evaluated_date >= stat_date:
            return "skipped_duplicate"

        if alert is None:
            if not is_hit:
                return "noop"
            AnalyticsAlert.objects.create(
                site_id=site_id,
                page_id=page_id,
                live_revision_id=live_revision_id,
                alert_kind=alert_kind,
                status="observing",
                consecutive_hit_days=1,
                consecutive_clear_days=0,
                last_evaluated_date=stat_date,
                evidence_json=evidence,
            )
            return "created_observing"

        prev_status = alert.status
        merged_evidence = dict(alert.evidence_json or {})
        merged_evidence.update(evidence)

        if prev_status == "confirmed_demoted":
            alert.last_evaluated_date = stat_date
            alert.evidence_json = merged_evidence
            alert.save(update_fields=["last_evaluated_date", "evidence_json"])
            return "updated"

        if prev_status == "exempted":
            # 限时豁免有效期内不转回 active，豁免到期后自动恢复评估
            if alert.exempt_until is not None and now < alert.exempt_until:
                alert.last_evaluated_date = stat_date
                alert.evidence_json = merged_evidence
                alert.save(update_fields=["last_evaluated_date", "evidence_json"])
                return "updated"
            # 豁免到期，按当前状态重新落位
            alert.exempt_until = None
            if is_hit:
                alert.status = "active"
                alert.consecutive_hit_days = max(2, int(alert.consecutive_hit_days or 0) + 1)
                alert.consecutive_clear_days = 0
            elif is_clear:
                alert.status = "active"
                alert.consecutive_hit_days = 0
                alert.consecutive_clear_days = 1
            else:
                alert.status = "observing"
                alert.consecutive_hit_days = 0
                alert.consecutive_clear_days = 0
            alert.last_evaluated_date = stat_date
            alert.evidence_json = merged_evidence
            alert.save()
            return "updated"

        if prev_status == "resolved":
            if not is_hit:
                alert.last_evaluated_date = stat_date
                alert.save(update_fields=["last_evaluated_date"])
                return "noop"
            alert.status = "observing"
            alert.consecutive_hit_days = 1
            alert.consecutive_clear_days = 0
            alert.last_evaluated_date = stat_date
            alert.evidence_json = merged_evidence
            alert.save()
            return "created_observing"

        if prev_status == "observing":
            if is_hit:
                alert.consecutive_hit_days = int(alert.consecutive_hit_days or 0) + 1
                alert.consecutive_clear_days = 0
                if alert.consecutive_hit_days >= 2:
                    alert.status = "active"
                    action = "promoted_active"
                else:
                    action = "updated"
            else:
                # 观察期一旦中断命中，立即清零连续命中计数防止跨日拼凑 2 天
                alert.consecutive_hit_days = 0
                alert.consecutive_clear_days = int(alert.consecutive_clear_days or 0) + 1
                if is_clear or alert.consecutive_clear_days >= 2:
                    alert.status = "resolved"
                    action = "resolved"
                else:
                    action = "updated"
            alert.last_evaluated_date = stat_date
            alert.evidence_json = merged_evidence
            alert.save()
            return action

        # prev_status == "active"
        if is_hit:
            alert.consecutive_hit_days = int(alert.consecutive_hit_days or 0) + 1
            alert.consecutive_clear_days = 0
            action = "updated"
        elif is_clear:
            alert.consecutive_hit_days = 0
            alert.consecutive_clear_days = int(alert.consecutive_clear_days or 0) + 1
            if alert.consecutive_clear_days >= 3:
                alert.status = "resolved"
                action = "resolved"
            else:
                action = "updated"
        else:
            # 处于滞回死区：既不加重也不推进解除计数（保持连续性要求）
            alert.consecutive_clear_days = 0
            action = "updated"

        alert.last_evaluated_date = stat_date
        alert.evidence_json = merged_evidence
        alert.save()
        return action


def evaluate_daily_alerts(
    site: Site | int,
    stat_date: date | str | None = None,
) -> dict[str, Any]:
    """
    执行全站每日口碑与低完读滞回告警评估（10M 规模安全分批查询）。
    缺测日（site health_status == 'missing'）直接跳过，绝不解除活动告警。
    """
    site_obj = _coerce_site(site)
    if site_obj is None:
        return {"evaluated_pages": 0, "skipped": True, "reason": "site_not_found"}

    eval_date = _coerce_date(stat_date, default_days_ago=1)
    if _is_site_day_missing(site_obj.id, eval_date):
        return {
            "site_id": site_obj.id,
            "stat_date": eval_date.isoformat(),
            "evaluated_pages": 0,
            "skipped": True,
            "reason": "missing_day",
        }

    settings_obj = AnalyticsSettings.get_for_site_readonly(site_obj)
    min_votes = max(1, int(getattr(settings_obj, "min_rating_votes", 20) or 20))
    observe_votes = max(1, int(getattr(settings_obj, "observe_rating_votes", 5) or 5))
    now = timezone.now()

    healthy_dates = get_recent_healthy_dates(site_obj.id, eval_date, target_days=30)

    # 1. 按 type_key 在 SQL 中计算 P90 高 PV 阈值，并只拉取高 PV 或存在可测阅读的候选页 ID
    type_keys = set(
        _eligible_daily_queryset_for_site(site_obj)
        .filter(stat_date__in=healthy_dates)
        .values_list("type_key", flat=True)
        .distinct()
    )
    type_thresholds: dict[str, dict[str, Any]] = {}
    candidate_pairs: dict[tuple[int, int], str] = {}  # (page_id, live_revision_id) -> type_key

    for tkey in sorted(type_keys):
        if not tkey:
            continue
        th_info = compute_type_high_pv_threshold(site_obj, tkey, eval_date, healthy_dates=healthy_dates)
        type_thresholds[tkey] = th_info
        th_val = int(th_info["threshold"])

        # 只在 PostgreSQL 侧拉取满足高 PV 门槛的公开页，严禁拉取全站长尾页面
        high_pv_rows = (
            _eligible_daily_queryset_for_site(site_obj, type_key=tkey)
            .filter(stat_date__in=healthy_dates, page__live_revision_id__isnull=False)
            .values("page_id", "page__live_revision_id")
            .annotate(org_pv=Sum("organic_pv"))
            .filter(org_pv__gte=th_val)
        )
        for row in high_pv_rows:
            candidate_pairs[(int(row["page_id"]), int(row["page__live_revision_id"]))] = tkey

    # 2. 加入近 30 个健康日内达到最低投票阈值的页面（SQL 分组过滤）
    rated_rows = (
        ArticleRating.objects.filter(
            site_id=site_obj.id,
            grade__in=tuple(GRADE_SCORES.keys()),
            first_eligible_day__in=healthy_dates,
            page__live=True,
            page__alias_of__isnull=True,
            page__live_revision_id=F("live_revision_id"),
        )
        .values("page_id", "live_revision_id")
        .annotate(cnt=Count("id"))
        .filter(cnt__gte=min_votes)
    )
    for row in rated_rows:
        candidate_pairs.setdefault((int(row["page_id"]), int(row["live_revision_id"])), "")

    # 3. 加入已有未完结告警的页面（确保即使掉出高 PV 也能走 3 日恢复流转）
    open_alerts = (
        AnalyticsAlert.objects.filter(
            site_id=site_obj.id,
            status__in=("observing", "active", "exempted"),
        )
        .values_list("page_id", "live_revision_id")
        .distinct()
    )
    for pid, rev_id in open_alerts:
        candidate_pairs.setdefault((int(pid), int(rev_id)), "")

    stats = {
        "site_id": site_obj.id,
        "stat_date": eval_date.isoformat(),
        "evaluated_pages": 0,
        "created_observing": 0,
        "promoted_active": 0,
        "resolved": 0,
        "skipped_duplicate": 0,
        "skipped_missing_page_day": 0,
    }

    pair_list = sorted(candidate_pairs.keys())
    chunk_size = 200
    for idx in range(0, len(pair_list), chunk_size):
        chunk = pair_list[idx : idx + chunk_size]
        chunk_pids = sorted({pid for pid, _ in chunk})
        page_rows = list(
            Page.objects.filter(id__in=chunk_pids).values_list("id", "path", "live", "alias_of_id", "live_revision_id")
        )
        page_meta = {pid: (path, live, alias_id, cur_rev) for pid, path, live, alias_id, cur_rev in page_rows}
        blocked_ids = get_public_blocked_page_ids([(pid, path) for pid, path, _, _, _ in page_rows])

        # 批量聚合该批次页面的 30 健康日流量/阅读与 eval_date 当日状态
        daily_aggs = {
            (int(r["page_id"]), int(r["live_revision_id"])): r
            for r in PageAnalyticsDaily.objects.filter(
                site_id=site_obj.id,
                page_id__in=chunk_pids,
                stat_date__in=healthy_dates,
            )
            .exclude(health_status="missing")
            .values("page_id", "live_revision_id")
            .annotate(
                org_pv=Sum("organic_pv"),
                pv=Sum("pv"),
                meas=Sum("measurable_views"),
                comp=Sum("completed_views"),
                bounce=Sum("short_bounce_views"),
            )
        }
        # 补充不限版本的同页近 30 健康日 organic_pv（同页版本刚切换时，30 日流量仍应归入页面级高 PV 判别）
        page_org_pv_30d = {
            int(r["page_id"]): {
                "org_pv": int(r["org_pv"] or 0),
                "meas": int(r["meas"] or 0),
                "comp": int(r["comp"] or 0),
                "bounce": int(r["bounce"] or 0),
                "type_key": r["type_key"] or "",
            }
            for r in PageAnalyticsDaily.objects.filter(
                site_id=site_obj.id,
                page_id__in=chunk_pids,
                stat_date__in=healthy_dates,
            )
            .exclude(health_status="missing")
            .values("page_id", "type_key")
            .annotate(
                org_pv=Sum("organic_pv"),
                meas=Sum("measurable_views"),
                comp=Sum("completed_views"),
                bounce=Sum("short_bounce_views"),
            )
        }

        page_day_missing = set(
            PageAnalyticsDaily.objects.filter(
                site_id=site_obj.id,
                page_id__in=chunk_pids,
                stat_date=eval_date,
                health_status="missing",
            ).values_list("page_id", flat=True)
        )

        # 批量聚合同批次的 ArticleRating（零用户身份返回）
        rating_buckets: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for r in (
            ArticleRating.objects.filter(
                site_id=site_obj.id,
                page_id__in=chunk_pids,
                grade__in=tuple(GRADE_SCORES.keys()),
                first_eligible_day__in=healthy_dates,
            )
            .values("page_id", "live_revision_id", "trust_tier", "grade")
            .annotate(cnt=Count("id"))
        ):
            rating_buckets[(int(r["page_id"]), int(r["live_revision_id"]))].append(r)

        for pid, rev_id in chunk:
            if pid in page_day_missing:
                stats["skipped_missing_page_day"] += 1
                continue

            p_info = page_meta.get(pid)
            is_public_current = bool(
                p_info is not None
                and p_info[1]
                and p_info[2] is None
                and p_info[3] == rev_id
                and pid not in blocked_ids
            )

            # 若页面已非当前公开版本/已回收，不应保留阻塞该已下线版本的 active 误报，但也不自动恢复 confirmed_demoted
            if not is_public_current:
                AnalyticsAlert.objects.filter(
                    site_id=site_obj.id,
                    page_id=pid,
                    live_revision_id=rev_id,
                    status__in=("observing", "active"),
                ).update(status="resolved", last_evaluated_date=eval_date)
                continue

            stats["evaluated_pages"] += 1
            tkey = candidate_pairs[(pid, rev_id)] or page_org_pv_30d.get(pid, {}).get("type_key", "")
            if tkey and tkey not in type_thresholds:
                type_thresholds[tkey] = compute_type_high_pv_threshold(
                    site_obj, tkey, eval_date, healthy_dates=healthy_dates
                )
            th_info = type_thresholds.get(tkey) or {"threshold": 500, "p90": None, "group_size": 0}
            high_pv_threshold = int(th_info["threshold"])

            rev_agg = daily_aggs.get((pid, rev_id))
            page_agg = page_org_pv_30d.get(pid, {})
            organic_pv_30d = int((rev_agg["org_pv"] if rev_agg else page_agg.get("org_pv")) or 0)
            measurable_30d = int((rev_agg["meas"] if rev_agg else page_agg.get("meas")) or 0)
            completed_30d = int((rev_agg["comp"] if rev_agg else page_agg.get("comp")) or 0)
            short_bounce_30d = int((rev_agg["bounce"] if rev_agg else page_agg.get("bounce")) or 0)

            completion_rate = (float(completed_30d) / float(measurable_30d)) if measurable_30d > 0 else None
            short_bounce_rate = (float(short_bounce_30d) / float(measurable_30d)) if measurable_30d > 0 else None

            rep = _build_reputation_metrics_from_agg_rows(
                agg_rows=rating_buckets.get((pid, rev_id), []),
                min_votes=min_votes,
                observe_votes=observe_votes,
                window_healthy_days=len(healthy_dates),
                as_of_date=eval_date,
            )

            is_high_pv = organic_pv_30d >= high_pv_threshold
            is_pv_below_80pct = organic_pv_30d < (0.8 * float(high_pv_threshold))

            base_evidence = {
                "stat_date": eval_date.isoformat(),
                "type_key": tkey,
                "organic_pv_30d": organic_pv_30d,
                "high_pv_threshold": high_pv_threshold,
                "high_pv_p90": th_info.get("p90"),
                "measurable_views_30d": measurable_30d,
                "completion_rate": round(completion_rate, 4) if completion_rate is not None else None,
                "short_bounce_rate": round(short_bounce_rate, 4) if short_bounce_rate is not None else None,
                "n_trusted": rep["n_trusted"],
                "d_trusted": rep["d_trusted"],
                "r_trusted": rep["r_trusted"],
                "r_all": rep["r_all"],
                "wilson_lower": rep["wilson_lower"],
                "is_early_observe": rep["is_early_observe"],
                "rule_version": REPUTATION_RULE_VERSION,
            }

            # (A) LOW_REPUTATION
            res_a = _evaluate_alert_transition(
                site_id=site_obj.id,
                page_id=pid,
                live_revision_id=rev_id,
                alert_kind="LOW_REPUTATION",
                stat_date=eval_date,
                is_hit=bool(rep["is_low_reputation"]),
                is_clear=bool(rep["is_recovery_satisfied"]),
                evidence=base_evidence,
                now=now,
            )
            if res_a in stats:
                stats[res_a] += 1

            # (B) HIGH_TRAFFIC_LOW_REPUTATION
            ht_low_rep_hit = bool(is_high_pv and rep["is_low_reputation"])
            ht_low_rep_clear = bool(rep["is_recovery_satisfied"] or is_pv_below_80pct)
            res_b = _evaluate_alert_transition(
                site_id=site_obj.id,
                page_id=pid,
                live_revision_id=rev_id,
                alert_kind="HIGH_TRAFFIC_LOW_REPUTATION",
                stat_date=eval_date,
                is_hit=ht_low_rep_hit,
                is_clear=ht_low_rep_clear,
                evidence=base_evidence,
                now=now,
            )
            if res_b in stats:
                stats[res_b] += 1

            # (C) HIGH_TRAFFIC_LOW_ENGAGEMENT（不依赖评价票数，独立行为标题党预警）
            low_eng_behavior = bool(
                measurable_30d >= 60
                and completion_rate is not None
                and short_bounce_rate is not None
                and (completion_rate < 0.35 or short_bounce_rate >= 0.55)
            )
            ht_low_eng_hit = bool(is_high_pv and low_eng_behavior)
            ht_low_eng_clear = bool(
                is_pv_below_80pct
                or (
                    measurable_30d >= 60
                    and completion_rate is not None
                    and short_bounce_rate is not None
                    and completion_rate >= 0.35
                    and short_bounce_rate < 0.55
                )
            )
            res_c = _evaluate_alert_transition(
                site_id=site_obj.id,
                page_id=pid,
                live_revision_id=rev_id,
                alert_kind="HIGH_TRAFFIC_LOW_ENGAGEMENT",
                stat_date=eval_date,
                is_hit=ht_low_eng_hit,
                is_clear=ht_low_eng_clear,
                evidence=base_evidence,
                now=now,
            )
            if res_c in stats:
                stats[res_c] += 1

    return stats


def _load_active_penalty_map(
    site_id: int,
    page_rev_pairs: list[tuple[int, int]],
    *,
    now: datetime,
) -> dict[tuple[int, int], set[str]]:
    """批量读取未豁免的活动告警集合，避免逐页查询。"""
    if not page_rev_pairs:
        return {}
    pids = sorted({pid for pid, _ in page_rev_pairs})
    wanted = set(page_rev_pairs)
    result: dict[tuple[int, int], set[str]] = defaultdict(set)
    rows = (
        AnalyticsAlert.objects.filter(
            site_id=site_id,
            page_id__in=pids,
            alert_kind__in=("LOW_REPUTATION", "HIGH_TRAFFIC_LOW_REPUTATION", "TRAFFIC_ANOMALY"),
        )
        .filter(
            Q(status="active")
            | Q(status="confirmed_demoted")
            | Q(status="exempted", exempt_until__lte=now)
            | Q(status="exempted", exempt_until__isnull=True)
        )
        .values_list("page_id", "live_revision_id", "alert_kind")
    )
    for pid, rev_id, kind in rows:
        key = (int(pid), int(rev_id))
        if key in wanted:
            result[key].add(kind)
    return result


def _compute_penalty_factor(
    *,
    active_kinds: set[str],
    measurable_views: int,
    completed_views: int,
    short_bounce_views: int,
) -> float:
    """
    计算热榜降权惩罚系数：
    - 正常：1.0
    - 活动低口碑（LOW_REPUTATION 或 HIGH_TRAFFIC_LOW_REPUTATION）：0.60
    - 活动 HIGH_TRAFFIC_LOW_REPUTATION 且 (完读样本>=60 且完读率<35% 或 短读率>=55%)：0.35
    """
    if not active_kinds:
        return 1.0
    has_ht_low_rep = "HIGH_TRAFFIC_LOW_REPUTATION" in active_kinds
    has_low_rep = "LOW_REPUTATION" in active_kinds or has_ht_low_rep
    if not has_low_rep:
        return 1.0

    if has_ht_low_rep and measurable_views >= 60:
        comp_rate = float(completed_views) / float(measurable_views)
        bounce_rate = float(short_bounce_views) / float(measurable_views)
        if comp_rate < 0.35 or bounce_rate >= 0.55:
            return 0.35
    return 0.60


def _sum_conversions_from_daily_rows(rows: list[dict[str, Any]]) -> dict[int, int]:
    """从已预筛选的 Top-200 日表明细汇总可比较的站内转化总数（绝不计入评价写入次数）。"""
    totals: dict[int, int] = defaultdict(int)
    allowed_conv_keys = {"step_complete", "doc_download", "repo_click", "demo_click", "code_copy", "mermaid_view"}
    for r in rows:
        cjson = r.get("conversions_json")
        if not isinstance(cjson, dict):
            continue
        pid = int(r["page_id"])
        for k, v in cjson.items():
            if k in allowed_conv_keys:
                try:
                    totals[pid] += max(0, int(v))
                except (TypeError, ValueError):
                    continue
    return totals


def _load_approved_comment_counts(page_ids: list[int], since_dt: datetime) -> dict[int, int]:
    """批量统计窗口内已批准公开可见的评论数量。"""
    if not page_ids:
        return {}
    try:
        from comments.models import Comment

        return {
            int(r["page_id"]): int(r["cnt"])
            for r in Comment.objects.filter(
                page_id__in=page_ids,
                status="active",
                is_publicly_visible=True,
                approved_version__isnull=False,
                created_at__gte=since_dt,
            )
            .values("page_id")
            .annotate(cnt=Count("id"))
        }
    except DatabaseError:
        return {}
    except Exception:
        return {}


def _build_snapshot_items_for_window(
    *,
    site: Site,
    board_kind: str,
    type_key: str,
    eval_date: date,
    window_days: int,
    now: datetime,
    min_votes: int,
    observe_votes: int,
) -> list[dict[str, Any]]:
    """针对单个 type_key 在 PostgreSQL 预筛选 Top-200 候选并完成组内归一化与降权计算。"""
    start_date = eval_date - timedelta(days=window_days - 1)
    healthy_dates = get_recent_healthy_dates(site.id, eval_date, target_days=window_days)
    if not healthy_dates:
        healthy_dates = [eval_date - timedelta(days=d) for d in range(window_days)]

    base_qs = _eligible_daily_queryset_for_site(site, type_key=type_key).filter(
        stat_date__gte=start_date,
        stat_date__lte=eval_date,
        page__live_revision_id__isnull=False,
    )

    # PostgreSQL 侧预筛选 Top-200（仅取当前页面 ID 与核心聚合金额）
    if board_kind == "reputation":
        cand_qs = (
            base_qs.filter(live_revision_id=F("page__live_revision_id"))
            .values("page_id", "page__live_revision_id", "page__path", "page__first_published_at")
            .annotate(
                pv=Sum("pv"),
                organic_pv=Sum("organic_pv"),
                visitor_days=Sum("daily_uv"),
                measurable_views=Sum("measurable_views"),
                valid_views=Sum("valid_views"),
                completed_views=Sum("completed_views"),
                short_bounce_views=Sum("short_bounce_views"),
                total_active_seconds=Sum("total_active_seconds"),
            )
            .filter(measurable_views__gte=20)
            .order_by("-completed_views", "-valid_views", "page_id")[:_MAX_CANDIDATES_PER_TYPE]
        )
    elif board_kind == "surging":
        surge_start = eval_date - timedelta(days=1)
        cand_qs = (
            _eligible_daily_queryset_for_site(site, type_key=type_key)
            .filter(stat_date__gte=surge_start, stat_date__lte=eval_date, page__live_revision_id__isnull=False)
            .values("page_id", "page__live_revision_id", "page__path", "page__first_published_at")
            .annotate(
                pv=Sum("pv"),
                organic_pv=Sum("organic_pv"),
                visitor_days=Sum("daily_uv"),
                measurable_views=Sum("measurable_views"),
                valid_views=Sum("valid_views"),
                completed_views=Sum("completed_views"),
                short_bounce_views=Sum("short_bounce_views"),
                total_active_seconds=Sum("total_active_seconds"),
            )
            .filter(valid_views__gt=0)
            .order_by("-valid_views", "-pv", "page_id")[:_MAX_CANDIDATES_PER_TYPE]
        )
    else:
        cand_qs = (
            base_qs.values("page_id", "page__live_revision_id", "page__path", "page__first_published_at")
            .annotate(
                pv=Sum("pv"),
                organic_pv=Sum("organic_pv"),
                visitor_days=Sum("daily_uv"),
                measurable_views=Sum("measurable_views"),
                valid_views=Sum("valid_views"),
                completed_views=Sum("completed_views"),
                short_bounce_views=Sum("short_bounce_views"),
                total_active_seconds=Sum("total_active_seconds"),
            )
            .filter(pv__gt=0)
            .order_by("-valid_views", "-visitor_days", "page_id")[:_MAX_CANDIDATES_PER_TYPE]
        )

    cand_rows = list(cand_qs)
    if not cand_rows:
        return []

    blocked_ids = get_public_blocked_page_ids([(int(r["page_id"]), r["page__path"]) for r in cand_rows])
    cand_rows = [r for r in cand_rows if int(r["page_id"]) not in blocked_ids]
    if not cand_rows:
        return []

    cand_pids = [int(r["page_id"]) for r in cand_rows]
    pair_list = [(int(r["page_id"]), int(r["page__live_revision_id"])) for r in cand_rows]

    pages_by_id = {p.id: p for p in Page.objects.filter(id__in=cand_pids)}
    penalty_map = _load_active_penalty_map(site.id, pair_list, now=now)

    # 剔除存在未处理 TRAFFIC_ANOMALY 的页面
    cand_rows = [
        r
        for r in cand_rows
        if "TRAFFIC_ANOMALY" not in penalty_map.get((int(r["page_id"]), int(r["page__live_revision_id"])), set())
    ]
    if not cand_rows:
        return []

    since_dt = datetime.combine(start_date, datetime.min.time())
    if timezone.is_naive(since_dt):
        since_dt = timezone.make_aware(since_dt)
    if board_kind == "surging":
        since_dt = now - timedelta(hours=6)
    comment_counts = _load_approved_comment_counts(cand_pids, since_dt)

    conv_rows = list(
        PageAnalyticsDaily.objects.filter(
            site_id=site.id,
            page_id__in=cand_pids,
            stat_date__gte=start_date if board_kind != "surging" else (eval_date - timedelta(days=1)),
            stat_date__lte=eval_date,
        )
        .exclude(health_status="missing")
        .values("page_id", "stat_date", "valid_views", "conversions_json")
    )
    conv_totals = _sum_conversions_from_daily_rows(conv_rows)

    surge_weighted_valid: dict[int, float] = defaultdict(float)
    if board_kind == "surging":
        decay_today = 1.0 / ((2.0 + 0.0) ** 1.3)
        decay_prev = 1.0 / ((2.0 + 24.0) ** 1.3)
        for cr in conv_rows:
            pid = int(cr["page_id"])
            vv = float(cr.get("valid_views") or 0)
            if cr["stat_date"] == eval_date:
                surge_weighted_valid[pid] += vv * decay_today
            else:
                surge_weighted_valid[pid] += vv * decay_prev

    dates_30 = get_recent_healthy_dates(site.id, eval_date, target_days=30)
    rating_buckets: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for rr in (
        ArticleRating.objects.filter(
            site_id=site.id,
            page_id__in=cand_pids,
            grade__in=tuple(GRADE_SCORES.keys()),
            first_eligible_day__in=dates_30,
        )
        .values("page_id", "live_revision_id", "trust_tier", "grade")
        .annotate(cnt=Count("id"))
    ):
        rating_buckets[(int(rr["page_id"]), int(rr["live_revision_id"]))].append(rr)

    pre_items: list[dict[str, Any]] = []
    max_base_metric = 0.0
    avg_duration_ref = 180.0
    dur_samples = [
        float(r["total_active_seconds"] or 0) / float(r["measurable_views"] or 1)
        for r in cand_rows
        if int(r["measurable_views"] or 0) > 0
    ]
    if dur_samples:
        avg_duration_ref = max(60.0, sum(dur_samples) / len(dur_samples))

    for r in cand_rows:
        pid = int(r["page_id"])
        rev_id = int(r["page__live_revision_id"])
        pv = int(r["pv"] or 0)
        uv = int(r["visitor_days"] or 0)
        meas = int(r["measurable_views"] or 0)
        valid_v = int(r["valid_views"] or 0)
        comp_v = int(r["completed_views"] or 0)
        bounce_v = int(r["short_bounce_views"] or 0)
        active_sec = int(r["total_active_seconds"] or 0)
        comments_n = int(comment_counts.get(pid, 0))
        convs_n = int(conv_totals.get(pid, 0))

        comp_rate = (float(comp_v) / float(meas)) if meas > 0 else 0.0
        bounce_rate = (float(bounce_v) / float(meas)) if meas > 0 else 0.0
        avg_dur = (float(active_sec) / float(meas)) if meas > 0 else 0.0
        rel_dur = min(1.0, avg_dur / avg_duration_ref)

        rep = _build_reputation_metrics_from_agg_rows(
            agg_rows=rating_buckets.get((pid, rev_id), []),
            min_votes=min_votes,
            observe_votes=observe_votes,
            window_healthy_days=len(dates_30),
            as_of_date=eval_date,
        )

        if board_kind == "surging":
            base_val = surge_weighted_valid.get(pid, 0.0) + (3.0 * min(comments_n, 20)) / (2.0 ** 1.3)
            observing = valid_v < 5
        elif board_kind == "reputation":
            rep_component = rep["r_trusted"] if rep["n_trusted"] >= min_votes else BAYESIAN_PRIOR_SCORE
            base_val = 0.40 * comp_rate + 0.20 * rel_dur + 0.15 * max(0.0, 1.0 - bounce_rate) + 0.25 * rep_component
            observing = bool(meas < 20 or rep["n_trusted"] < min_votes)
        else:
            base_val = float(valid_v) + 3.0 * min(comments_n, 20) + 2.0 * min(convs_n, 50) + 0.5 * float(comp_v)
            observing = uv < 20

        max_base_metric = max(max_base_metric, base_val)
        active_kinds = penalty_map.get((pid, rev_id), set())
        factor = _compute_penalty_factor(
            active_kinds=active_kinds,
            measurable_views=meas,
            completed_views=comp_v,
            short_bounce_views=bounce_v,
        )

        page_obj = pages_by_id.get(pid)
        first_pub = r.get("page__first_published_at")
        pre_items.append(
            {
                "page_id": pid,
                "live_revision_id": rev_id,
                "type_key": type_key,
                "title": page_obj.title if page_obj else f"Page #{pid}",
                "url": page_obj.url if page_obj else "",
                "base_val": base_val,
                "comp_rate": comp_rate,
                "bounce_rate": bounce_rate,
                "avg_dur": avg_dur,
                "factor": factor,
                "observing": observing,
                "pv": pv,
                "organic_pv": int(r["organic_pv"] or 0),
                "visitor_days": uv,
                "valid_views": valid_v,
                "measurable_views": meas,
                "completed_views": comp_v,
                "short_bounce_views": bounce_v,
                "comment_count": comments_n,
                "conversion_count": convs_n,
                "reputation_score": rep["r_trusted"],
                "trusted_votes": rep["n_trusted"],
                "active_alerts": sorted(active_kinds),
                "_first_pub_ts": first_pub.timestamp() if first_pub else 0.0,
            }
        )

    denom = max_base_metric if max_base_metric > 0 else 1.0
    items: list[dict[str, Any]] = []
    for it in pre_items:
        if board_kind == "reputation":
            raw_score = round(it["base_val"] * 100.0, 4)
        else:
            norm_base = it["base_val"] / denom
            raw_score = round(100.0 * (0.75 * norm_base + 0.25 * it["comp_rate"]), 4)
        final_score = round(raw_score * it["factor"], 4)
        items.append(
            {
                "page_id": it["page_id"],
                "live_revision_id": it["live_revision_id"],
                "type_key": it["type_key"],
                "title": it["title"],
                "url": it["url"],
                "raw_score": raw_score,
                "factor": it["factor"],
                "final_score": final_score,
                "observing": it["observing"],
                "pv": it["pv"],
                "organic_pv": it["organic_pv"],
                "visitor_days": it["visitor_days"],
                "valid_views": it["valid_views"],
                "measurable_views": it["measurable_views"],
                "completed_views": it["completed_views"],
                "short_bounce_views": it["short_bounce_views"],
                "completion_rate": round(it["comp_rate"], 4),
                "short_bounce_rate": round(it["bounce_rate"], 4),
                "avg_active_seconds": round(it["avg_dur"], 1),
                "comment_count": it["comment_count"],
                "conversion_count": it["conversion_count"],
                "reputation_score": it["reputation_score"],
                "trusted_votes": it["trusted_votes"],
                "active_alerts": it["active_alerts"],
                "_first_pub_ts": it["_first_pub_ts"],
            }
        )

    items.sort(
        key=lambda x: (
            1 if x["observing"] else 0,
            -x["final_score"],
            -x["measurable_views"],
            -x["_first_pub_ts"],
            x["page_id"],
        )
    )
    trimmed = items[:_MAX_SNAPSHOT_ITEMS]
    for rank_idx, entry in enumerate(trimmed, start=1):
        entry.pop("_first_pub_ts", None)
        entry["rank"] = rank_idx
    return trimmed


def compute_ranking_snapshots(
    site: Site | int,
    stat_date: date | str | None = None,
) -> dict[str, Any]:
    """
    计算并落库全站 4 类榜单快照（popular_7d / popular_30d / surging / reputation）。
    按 type_key 在 PostgreSQL 预筛选 Top-200 并归一化，再汇总全站 'all' 综合榜，
    显式保留 raw_score、factor 与 final_score 供后台对照查看。
    """
    site_obj = _coerce_site(site)
    if site_obj is None:
        return {"snapshots_written": 0, "reason": "site_not_found"}

    eval_date = _coerce_date(stat_date, default_days_ago=1)
    now = timezone.now()
    settings_obj = AnalyticsSettings.get_for_site_readonly(site_obj)
    min_votes = max(1, int(getattr(settings_obj, "min_rating_votes", 20) or 20))
    observe_votes = max(1, int(getattr(settings_obj, "observe_rating_votes", 5) or 5))

    eligible_types = _get_rank_eligible_type_keys()
    if not eligible_types:
        eligible_types = sorted(
            {
                tk
                for tk in PageAnalyticsDaily.objects.filter(site_id=site_obj.id)
                .values_list("type_key", flat=True)
                .distinct()
                if tk
            }
        )

    board_windows = {
        "surging": 2,
        "popular_7d": 7,
        "popular_30d": 30,
        "reputation": 30,
    }

    snapshots_written = 0
    board_summary: dict[str, dict[str, int]] = {}

    for board_kind in _RANK_BOARD_KINDS:
        window_days = board_windows[board_kind]
        combined_all: list[dict[str, Any]] = []
        board_summary[board_kind] = {}

        for tkey in eligible_types:
            caps = get_analytics_capabilities(tkey)
            if caps is not None and not getattr(caps, "rank_eligible", False):
                continue

            items = _build_snapshot_items_for_window(
                site=site_obj,
                board_kind=board_kind,
                type_key=tkey,
                eval_date=eval_date,
                window_days=window_days,
                now=now,
                min_votes=min_votes,
                observe_votes=observe_votes,
            )
            RankingSnapshot.objects.update_or_create(
                site=site_obj,
                board_kind=board_kind,
                type_key=tkey,
                stat_date=eval_date,
                defaults={
                    "rule_version": RANKING_RULE_VERSION,
                    "items_json": items,
                },
            )
            snapshots_written += 1
            board_summary[board_kind][tkey] = len(items)
            if board_kind != "reputation":
                combined_all.extend(dict(it) for it in items)

        # 深度干货口碑榜默认关闭跨类型混排（Spec §3.1），人气与飙升提供按类型归一后的全站总榜
        if board_kind != "reputation":
            combined_all.sort(
                key=lambda x: (
                    1 if x["observing"] else 0,
                    -x["final_score"],
                    -x["measurable_views"],
                    x["page_id"],
                )
            )
            all_trimmed = combined_all[:_MAX_SNAPSHOT_ITEMS]
            for idx, entry in enumerate(all_trimmed, start=1):
                entry["rank"] = idx
            RankingSnapshot.objects.update_or_create(
                site=site_obj,
                board_kind=board_kind,
                type_key="all",
                stat_date=eval_date,
                defaults={
                    "rule_version": RANKING_RULE_VERSION,
                    "items_json": all_trimmed,
                },
            )
            snapshots_written += 1
            board_summary[board_kind]["all"] = len(all_trimmed)

    return {
        "site_id": site_obj.id,
        "stat_date": eval_date.isoformat(),
        "snapshots_written": snapshots_written,
        "boards": board_summary,
    }
