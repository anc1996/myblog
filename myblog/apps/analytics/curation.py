"""
analytics.curation
第 2 期 Quality Score 评分引擎、每日精选自动候选生成、主编并发审批工作台与强核验投放服务。

核心业务与安全红线：
1. D3-A 自动入围门槛：
   近 30 个完整日健康日 >= min_healthy_days_in_30d（默认 25）、访客日 >= 100、可测阅读 >= 60、
   覆盖率 >= 60%、完读率 >= 50%、Quality Score >= 70；
   排除活动 HIGH_TRAFFIC_LOW_REPUTATION / TRAFFIC_ANOMALY、逾期指南与 14 天拒绝冷却期内记录；
   绝不覆盖已人工审批的决定；
2. 并发防重与审计留痕：
   所有审批/拒绝/撤回/提名/豁免/确认下榜服务均在事务内行锁校验 expected_version，
   冲突抛出 ConcurrencyConflictError (ValidationError 子类)，并写 CuratedRecommendationAction 留痕；
3. 投放读侧强核验主库（RecommendationService.for_placement）：
   仅返回 status="approved" 且在时间窗内的候选，并在读侧批量复核主库：
   Page.live=True、alias_of_id is None、live_revision_id 一致、未进入 get_public_blocked_page_ids()、
   归属当前站点（及指定栏目后代）、无未豁免活动 HIGH_TRAFFIC_LOW_REPUTATION 告警、指南未逾期。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import logging
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Sum
from django.utils import timezone
from wagtail.models import Page, Site

from analytics.collector import resolve_column_page_id, resolve_page_analytics_spec
from analytics.models import (
    AnalyticsAlert,
    AnalyticsSettings,
    ArticleRating,
    CuratedRecommendation,
    CuratedRecommendationAction,
    PageAnalyticsDaily,
)
from analytics.scoring import (
    GRADE_SCORES,
    _build_reputation_metrics_from_agg_rows,
    _coerce_date,
    _coerce_site,
    _eligible_daily_queryset_for_site,
    _load_active_penalty_map,
    _load_approved_comment_counts,
    get_recent_healthy_dates,
)
from analytics.settlement import invalidate_analytics_display_cache
from content_lifecycle.models import RecycleEntry
from content_lifecycle.public_access import get_public_blocked_page_ids
from discovery.registry import get_analytics_capabilities, get_type_adapter

logger = logging.getLogger(__name__)

QUALITY_RULE_VERSION = "v1.0"
VALID_PLACEMENTS = frozenset({"home_carousel", "home_guides", "home_projects", "column_featured"})
_ACTIVE_RECYCLE_STATUSES = ("recycled", "restoring")
_DEFAULT_COOLDOWN_DAYS = 14
_MAX_EXEMPT_DAYS = 14


class ConcurrencyConflictError(ValidationError):
    """版本并发冲突异常（继承 ValidationError 便于统一捕获）。"""


class PlacementItem(dict):
    """
    投放位条目字典（同时支持 dict 键访问与属性访问，适配模板与服务调用）。
    """

    def __getattr__(self, name: str) -> Any:
        if name == "id" and "page_id" in self:
            return self["page_id"]
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _check_curation_permission(actor: Any) -> None:
    if not actor or not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_active", False):
        raise PermissionDenied("必须由已登录且有效的编辑/管理员账号执行精选操作。")
    if getattr(actor, "is_superuser", False):
        return
    if actor.has_perm("analytics.approve_curatedrecommendation") or actor.has_perm(
        "analytics.change_curatedrecommendation"
    ):
        return
    raise PermissionDenied("当前用户无权执行精选推荐审批或提名操作。")


def _check_alert_permission(actor: Any) -> None:
    if not actor or not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_active", False):
        raise PermissionDenied("必须由已登录且有效的编辑/管理员账号执行告警治理操作。")
    if getattr(actor, "is_superuser", False):
        return
    if actor.has_perm("analytics.resolve_analyticsalert") or actor.has_perm("analytics.change_analyticsalert"):
        return
    raise PermissionDenied("当前用户无权处理数据分析降噪告警。")


def _get_guide_freshness_score(spec_page: Any, as_of_date: date) -> tuple[float, bool]:
    """
    计算指南时效分 F 与是否逾期阻断标记：
    - 非指南类型统一返回 (0.5, False)；
    - 指南缺 last_reviewed_at 或 >= 365 天返回 (0.0, True)；
    - 335..364 天临期提醒返回 (0.5, False)；
    - < 335 天健康返回 (1.0, False)。
    """
    if not hasattr(spec_page, "last_reviewed_at") and not hasattr(spec_page, "is_outdated"):
        return 0.5, False
    rev_at = getattr(spec_page, "last_reviewed_at", None)
    if rev_at is None:
        return 0.0, True
    rev_date = rev_at.date() if isinstance(rev_at, datetime) else rev_at
    diff_days = (as_of_date - rev_date).days
    if diff_days >= 365:
        return 0.0, True
    if diff_days >= 335:
        return 0.5, False
    return 1.0, False


def compute_quality_score(
    *,
    measurable_views: int,
    completed_views: int,
    short_bounce_views: int,
    total_active_seconds: int,
    visitor_days: int,
    comment_count: int = 0,
    reputation_score: float | None = None,
    trusted_votes: int = 0,
    min_rating_votes: int = 20,
    freshness_score: float = 0.5,
    base_completion_rate: float = 0.50,
    base_bounce_rate: float = 0.25,
    ref_active_seconds: float = 180.0,
) -> dict[str, Any]:
    """
    计算 D3-A 精选质量分：
    Quality Score = 100 * (0.35C + 0.20T + 0.15L + 0.20I + 0.10F)
    - C: 贝叶斯平滑完读率 (加 20 份同组基准)
    - T: 相对同组参考时长的活跃阅读时长分 (截断到 0..1)
    - L: 1 - 平滑短读离开率 (加 20 份同组基准)
    - I: 评论率与可信口碑融合 (trusted_votes < min_rating_votes 时评价权重为 0)
    - F: 时效分 (指南 <335d=1.0, 335~364d=0.5, 逾期/缺值=0.0; 博客/项目=0.5)
    """
    meas = max(0, int(measurable_views))
    comp = max(0, int(completed_views))
    bounce = max(0, int(short_bounce_views))
    active_sec = max(0, int(total_active_seconds))
    vdays = max(0, int(visitor_days))
    comments_n = max(0, int(comment_count))

    base_c = min(1.0, max(0.0, float(base_completion_rate)))
    base_b = min(1.0, max(0.0, float(base_bounce_rate)))
    ref_sec = max(30.0, float(ref_active_seconds))

    c_val = (float(comp) + 20.0 * base_c) / (float(meas) + 20.0)
    c_val = min(1.0, max(0.0, c_val))

    avg_sec = (float(active_sec) / float(meas)) if meas > 0 else 0.0
    t_val = min(1.0, max(0.0, avg_sec / ref_sec))

    smoothed_bounce = (float(bounce) + 20.0 * base_b) / (float(meas) + 20.0)
    l_val = min(1.0, max(0.0, 1.0 - smoothed_bounce))

    comment_rate = (float(min(comments_n, 50)) / float(max(1, vdays))) if vdays > 0 else 0.0
    comment_score = min(1.0, max(0.0, comment_rate / 0.02))

    if trusted_votes >= min_rating_votes and reputation_score is not None:
        rep_clamped = min(1.0, max(0.0, float(reputation_score)))
        i_val = 0.40 * comment_score + 0.60 * rep_clamped
    else:
        i_val = comment_score

    f_val = min(1.0, max(0.0, float(freshness_score)))

    score = 100.0 * (0.35 * c_val + 0.20 * t_val + 0.15 * l_val + 0.20 * i_val + 0.10 * f_val)
    return {
        "quality_score": round(score, 2),
        "C": round(c_val, 4),
        "T": round(t_val, 4),
        "L": round(l_val, 4),
        "I": round(i_val, 4),
        "F": round(f_val, 4),
        "comment_score": round(comment_score, 4),
        "avg_active_seconds": round(avg_sec, 1),
        "rule_version": QUALITY_RULE_VERSION,
    }


def _compute_type_baselines(site: Site, healthy_dates: list[date]) -> dict[str, dict[str, float]]:
    """按 type_key 在 PostgreSQL 汇总全组完读率、短读率与平均时长基准，绝不拉取单页明细列表。"""
    rows = (
        _eligible_daily_queryset_for_site(site)
        .filter(stat_date__in=healthy_dates, health_status="normal")
        .values("type_key")
        .annotate(
            meas=Sum("measurable_views"),
            comp=Sum("completed_views"),
            bounce=Sum("short_bounce_views"),
            active_sec=Sum("total_active_seconds"),
        )
    )
    baselines: dict[str, dict[str, float]] = {}
    for r in rows:
        tkey = r.get("type_key") or ""
        if not tkey:
            continue
        meas = int(r["meas"] or 0)
        if meas < 200:
            baselines[tkey] = {"base_c": 0.50, "base_b": 0.25, "ref_sec": 180.0}
        else:
            base_c = min(0.95, max(0.10, float(r["comp"] or 0) / float(meas)))
            base_b = min(0.85, max(0.05, float(r["bounce"] or 0) / float(meas)))
            ref_sec = max(60.0, float(r["active_sec"] or 0) / float(meas))
            baselines[tkey] = {"base_c": base_c, "base_b": base_b, "ref_sec": ref_sec}
    return baselines


def _target_positions_for_page(type_key: str, column_page_id: int | None) -> list[tuple[str, int | None]]:
    positions: list[tuple[str, int | None]] = [("home_carousel", None)]
    if type_key == "guide":
        positions.append(("home_guides", None))
    elif type_key == "project":
        positions.append(("home_projects", None))
    if column_page_id:
        positions.append(("column_featured", int(column_page_id)))
    return positions


def generate_curated_candidates(
    site: Site | int,
    stat_date: date | str | None = None,
) -> dict[str, Any]:
    """
    每日精选候选生成（D3-A）：
    1. 检查健康日 >= min_healthy_days_in_30d、访客日 >= 100、可测 >= 60、覆盖率 >= 60%、完读率 >= 50%、Quality Score >= 70；
    2. 排除非公开/别名/回收页、活动 HIGH_TRAFFIC_LOW_REPUTATION / TRAFFIC_ANOMALY / confirmed_demoted、逾期指南、14 天冷却期内记录；
    3. 仅新建或更新 status="pending" 的候选记录，绝不覆盖编辑已做出的 approved / rejected（冷却期内） / withdrawn 决定。
    """
    site_obj = _coerce_site(site)
    if site_obj is None:
        return {"candidates_created": 0, "candidates_updated": 0, "reason": "site_not_found"}

    eval_date = _coerce_date(stat_date, default_days_ago=1)
    start_30d = eval_date - timedelta(days=29)
    now = timezone.now()

    settings_obj = AnalyticsSettings.get_for_site_readonly(site_obj)
    min_healthy_days = max(1, int(getattr(settings_obj, "min_healthy_days_in_30d", 25) or 25))
    min_votes = max(1, int(getattr(settings_obj, "min_rating_votes", 20) or 20))
    observe_votes = max(1, int(getattr(settings_obj, "observe_rating_votes", 5) or 5))

    healthy_dates = get_recent_healthy_dates(site_obj.id, eval_date, target_days=30)
    type_baselines = _compute_type_baselines(site_obj, healthy_dates)

    # PostgreSQL 侧按 D3-A 核心门槛直接过滤：仅返回达标页面（千万级全站仅极少数高质页出线）
    cand_qs = (
        _eligible_daily_queryset_for_site(site_obj)
        .filter(
            stat_date__gte=start_30d,
            stat_date__lte=eval_date,
            health_status="normal",
            page__live_revision_id__isnull=False,
            live_revision_id=F("page__live_revision_id"),
        )
        .values("page_id", "live_revision_id", "type_key", "page__path")
        .annotate(
            healthy_days=Count("stat_date", distinct=True),
            visitor_days=Sum("daily_uv"),
            measurable_views=Sum("measurable_views"),
            completed_views=Sum("completed_views"),
            short_bounce_views=Sum("short_bounce_views"),
            total_active_seconds=Sum("total_active_seconds"),
            pv=Sum("pv"),
            organic_pv=Sum("organic_pv"),
        )
        .filter(
            healthy_days__gte=min_healthy_days,
            visitor_days__gte=100,
            measurable_views__gte=60,
            pv__gt=0,
        )
        .order_by("page_id")
    )

    summary = {
        "site_id": site_obj.id,
        "stat_date": eval_date.isoformat(),
        "qualified_pages": 0,
        "candidates_created": 0,
        "candidates_updated": 0,
        "skipped_blocked_or_alert": 0,
        "skipped_cooldown_or_decided": 0,
    }

    # 分批读取达标候选（每次最多 200 页），避免内存膨胀
    offset = 0
    batch_size = 200
    since_dt = datetime.combine(start_30d, datetime.min.time())
    if timezone.is_naive(since_dt):
        since_dt = timezone.make_aware(since_dt)

    while True:
        batch_rows = list(cand_qs[offset : offset + batch_size])
        if not batch_rows:
            break
        offset += batch_size

        # 在 Python 内核对比率条件（覆盖率 >= 60%, 完读率 >= 50%, 能力声明支持精选）
        pre_filtered = []
        for r in batch_rows:
            pv = int(r["pv"] or 0)
            meas = int(r["measurable_views"] or 0)
            comp = int(r["completed_views"] or 0)
            if pv <= 0 or (float(meas) / float(pv)) < 0.60:
                continue
            if meas <= 0 or (float(comp) / float(meas)) < 0.50:
                continue
            tkey = r.get("type_key") or ""
            caps = get_analytics_capabilities(tkey) if tkey else None
            if caps is not None and not getattr(caps, "curation_eligible", False):
                continue
            pre_filtered.append(r)

        if not pre_filtered:
            continue

        batch_pids = sorted({int(r["page_id"]) for r in pre_filtered})
        blocked_ids = get_public_blocked_page_ids([(int(r["page_id"]), r["page__path"]) for r in pre_filtered])
        pair_list = [(int(r["page_id"]), int(r["live_revision_id"])) for r in pre_filtered]
        penalty_map = _load_active_penalty_map(site_obj.id, pair_list, now=now)
        comment_counts = _load_approved_comment_counts(batch_pids, since_dt)
        pages_by_id = {p.id: p for p in Page.objects.filter(id__in=batch_pids).specific()}

        # 批量读取近 30 日各页所属栏目（取最近一条非空 column_page_id）
        col_map: dict[int, int | None] = {}
        for cr in (
            PageAnalyticsDaily.objects.filter(
                site_id=site_obj.id,
                page_id__in=batch_pids,
                column_page_id__isnull=False,
            )
            .order_by("page_id", "-stat_date")
            .values("page_id", "column_page_id")
        ):
            col_map.setdefault(int(cr["page_id"]), int(cr["column_page_id"]))

        # 批量聚合当前版本近 30 健康日评价
        rating_buckets: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for rr in (
            ArticleRating.objects.filter(
                site_id=site_obj.id,
                page_id__in=batch_pids,
                grade__in=tuple(GRADE_SCORES.keys()),
                first_eligible_day__in=healthy_dates,
            )
            .values("page_id", "live_revision_id", "trust_tier", "grade")
            .annotate(cnt=Count("id"))
        ):
            rating_buckets[(int(rr["page_id"]), int(rr["live_revision_id"]))].append(rr)

        for r in pre_filtered:
            pid = int(r["page_id"])
            rev_id = int(r["live_revision_id"])
            tkey = r.get("type_key") or ""

            if pid in blocked_ids:
                summary["skipped_blocked_or_alert"] += 1
                continue

            active_alerts = penalty_map.get((pid, rev_id), set())
            if "HIGH_TRAFFIC_LOW_REPUTATION" in active_alerts or "TRAFFIC_ANOMALY" in active_alerts:
                summary["skipped_blocked_or_alert"] += 1
                continue

            spec_page = pages_by_id.get(pid)
            if spec_page is None or getattr(spec_page, "is_recycled", False):
                summary["skipped_blocked_or_alert"] += 1
                continue
            if spec_page.live_revision_id != rev_id:
                summary["skipped_blocked_or_alert"] += 1
                continue

            freshness_f, is_guide_overdue = _get_guide_freshness_score(spec_page, eval_date)
            if is_guide_overdue:
                summary["skipped_blocked_or_alert"] += 1
                continue

            rep = _build_reputation_metrics_from_agg_rows(
                agg_rows=rating_buckets.get((pid, rev_id), []),
                min_votes=min_votes,
                observe_votes=observe_votes,
                window_healthy_days=len(healthy_dates),
                as_of_date=eval_date,
            )
            baseline = type_baselines.get(tkey) or {"base_c": 0.50, "base_b": 0.25, "ref_sec": 180.0}
            q_res = compute_quality_score(
                measurable_views=int(r["measurable_views"] or 0),
                completed_views=int(r["completed_views"] or 0),
                short_bounce_views=int(r["short_bounce_views"] or 0),
                total_active_seconds=int(r["total_active_seconds"] or 0),
                visitor_days=int(r["visitor_days"] or 0),
                comment_count=int(comment_counts.get(pid, 0)),
                reputation_score=rep["r_trusted"],
                trusted_votes=rep["n_trusted"],
                min_rating_votes=min_votes,
                freshness_score=freshness_f,
                base_completion_rate=baseline["base_c"],
                base_bounce_rate=baseline["base_b"],
                ref_active_seconds=baseline["ref_sec"],
            )
            if q_res["quality_score"] < 70.0:
                continue

            summary["qualified_pages"] += 1
            col_id = col_map.get(pid) or resolve_column_page_id(spec_page)
            evidence_snapshot = {
                "stat_date": eval_date.isoformat(),
                "type_key": tkey,
                "healthy_days": int(r["healthy_days"] or 0),
                "visitor_days": int(r["visitor_days"] or 0),
                "pv": int(r["pv"] or 0),
                "organic_pv": int(r["organic_pv"] or 0),
                "measurable_views": int(r["measurable_views"] or 0),
                "completed_views": int(r["completed_views"] or 0),
                "short_bounce_views": int(r["short_bounce_views"] or 0),
                "coverage_rate": round(float(r["measurable_views"]) / float(r["pv"]), 4),
                "completion_rate": round(float(r["completed_views"]) / float(r["measurable_views"]), 4),
                "comment_count": int(comment_counts.get(pid, 0)),
                "reputation": {
                    "n_trusted": rep["n_trusted"],
                    "d_trusted": rep["d_trusted"],
                    "r_trusted": rep["r_trusted"],
                    "wilson_lower": rep["wilson_lower"],
                },
                "quality_breakdown": q_res,
                "rule_version": QUALITY_RULE_VERSION,
            }

            for pos, pos_col_id in _target_positions_for_page(tkey, col_id):
                with transaction.atomic():
                    existing = (
                        CuratedRecommendation.objects.select_for_update()
                        .filter(
                            site_id=site_obj.id,
                            page_id=pid,
                            live_revision_id=rev_id,
                            position=pos,
                        )
                        .first()
                    )
                    if existing is None:
                        CuratedRecommendation.objects.create(
                            site=site_obj,
                            page_id=pid,
                            live_revision_id=rev_id,
                            position=pos,
                            column_page_id=pos_col_id,
                            status="pending",
                            quality_score=q_res["quality_score"],
                            evidence_json=evidence_snapshot,
                            version=1,
                        )
                        summary["candidates_created"] += 1
                        continue

                    if existing.rejected_cooldown_until is not None and existing.rejected_cooldown_until > now:
                        summary["skipped_cooldown_or_decided"] += 1
                        continue

                    # 绝不覆盖已人工批准、人工撤回或无到期冷却期的驳回决定
                    if existing.status in ("approved", "withdrawn"):
                        summary["skipped_cooldown_or_decided"] += 1
                        continue
                    if existing.status == "rejected":
                        if existing.rejected_cooldown_until is None or existing.rejected_cooldown_until > now:
                            summary["skipped_cooldown_or_decided"] += 1
                            continue
                        # 14 天冷却期已过：重新回到 pending 待审池
                        existing.status = "pending"
                        existing.rejected_cooldown_until = None

                    existing.column_page_id = pos_col_id
                    existing.quality_score = q_res["quality_score"]
                    existing.evidence_json = evidence_snapshot
                    existing.version = int(existing.version or 1) + 1
                    existing.save()
                    summary["candidates_updated"] += 1

    return summary


def _assert_page_public_and_unblocked_for_curation(
    *,
    site: Site,
    page: Page,
    live_revision_id: int,
) -> Any:
    """核验页面公开资格、版本号一致性、精选能力声明、指南时效与风险告警。"""
    spec_page = page.specific if hasattr(page, "specific") else page
    if not getattr(spec_page, "live", False) or getattr(spec_page, "alias_of_id", None) is not None:
        raise ValidationError("未发布或别名页面禁止进入精选推荐。")
    if getattr(spec_page, "is_recycled", False):
        raise ValidationError("已移入回收站的页面禁止进入精选推荐。")
    if not spec_page.live_revision_id or int(spec_page.live_revision_id) != int(live_revision_id):
        raise ValidationError("当前页面的正式发布版本已变更，不能审批或提名旧版本。")
    if site.root_page_id:
        root_path = site.root_page.path
        if not spec_page.path.startswith(root_path) or spec_page.depth <= site.root_page.depth:
            raise ValidationError("页面不属于当前站点子树，禁止跨站精选。")
    if spec_page.id in get_public_blocked_page_ids([(spec_page.id, spec_page.path)]):
        raise ValidationError("页面或祖先处于受限/未公开/回收状态，禁止精选投放。")

    caps, _type_key = resolve_page_analytics_spec(spec_page)
    if caps is None or caps.kind != "detail" or not getattr(caps, "curation_eligible", False):
        raise ValidationError("当前页面类型未声明精选资格 (curation_eligible=True)。")

    _, is_overdue = _get_guide_freshness_score(spec_page, timezone.localdate())
    if is_overdue:
        raise ValidationError("该专题指南已超过 365 天未技术复核或缺少复核日期，禁止精选投放。")

    now = timezone.now()
    blocking = (
        AnalyticsAlert.objects.filter(
            site_id=site.id,
            page_id=spec_page.id,
            live_revision_id=int(live_revision_id),
            alert_kind__in=("HIGH_TRAFFIC_LOW_REPUTATION", "TRAFFIC_ANOMALY"),
        )
        .filter(
            Q(status="active")
            | Q(status="confirmed_demoted")
            | Q(status="exempted", exempt_until__lte=now)
            | Q(status="exempted", exempt_until__isnull=True)
        )
        .exists()
    )
    if blocking:
        raise ValidationError("当前版本存在未豁免的高流量低口碑或异常流量告警，需先处理或限时豁免后才可批准/提名。")

    return spec_page


def approve_recommendation(
    recommendation_id: int,
    actor: Any,
    *,
    expected_version: int,
    priority: int = 0,
    is_pinned: bool = False,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    reason: str = "",
) -> CuratedRecommendation:
    """主编批准精选推荐（严格校验 expected_version 与公开/风险状态）。"""
    _check_curation_permission(actor)
    if starts_at is not None and expires_at is not None and expires_at <= starts_at:
        raise ValidationError("结束时间必须晚于开始时间。")

    with transaction.atomic():
        rec = (
            CuratedRecommendation.objects.select_for_update()
            .select_related("site", "page")
            .filter(id=int(recommendation_id))
            .first()
        )
        if rec is None:
            raise ValidationError("目标精选推荐记录不存在。")
        if int(rec.version) != int(expected_version):
            raise ConcurrencyConflictError("该精选记录已被他人更新，请刷新后重试。")

        _assert_page_public_and_unblocked_for_curation(
            site=rec.site,
            page=rec.page,
            live_revision_id=rec.live_revision_id,
        )

        rec.status = "approved"
        rec.priority = int(priority)
        rec.is_pinned = bool(is_pinned)
        rec.starts_at = starts_at
        rec.expires_at = expires_at
        rec.rejected_cooldown_until = None
        rec.version = int(rec.version) + 1
        rec.save()

        CuratedRecommendationAction.objects.create(
            recommendation=rec,
            actor=actor,
            action="approve",
            reason=(reason or "").strip()[:500],
            evidence_version=rec.version,
        )
        site_id = rec.site_id
        page_id = rec.page_id
        transaction.on_commit(lambda: invalidate_analytics_display_cache(page_ids=[page_id], site_id=site_id))
        return rec


def reject_recommendation(
    recommendation_id: int,
    actor: Any,
    *,
    expected_version: int,
    reason: str = "",
    cooldown_days: int = _DEFAULT_COOLDOWN_DAYS,
) -> CuratedRecommendation:
    """主编拒绝（暂不入选）精选候选，并设置 14 天冷却期防止次日重复入池。"""
    _check_curation_permission(actor)
    cd_days = max(1, int(cooldown_days or _DEFAULT_COOLDOWN_DAYS))

    with transaction.atomic():
        rec = (
            CuratedRecommendation.objects.select_for_update()
            .filter(id=int(recommendation_id))
            .first()
        )
        if rec is None:
            raise ValidationError("目标精选推荐记录不存在。")
        if int(rec.version) != int(expected_version):
            raise ConcurrencyConflictError("该精选记录已被他人更新，请刷新后重试。")

        rec.status = "rejected"
        rec.is_pinned = False
        rec.rejected_cooldown_until = timezone.now() + timedelta(days=cd_days)
        rec.version = int(rec.version) + 1
        rec.save()

        CuratedRecommendationAction.objects.create(
            recommendation=rec,
            actor=actor,
            action="reject",
            reason=(reason or "").strip()[:500],
            evidence_version=rec.version,
        )
        site_id = rec.site_id
        page_id = rec.page_id
        transaction.on_commit(lambda: invalidate_analytics_display_cache(page_ids=[page_id], site_id=site_id))
        return rec


def withdraw_recommendation(
    recommendation_id: int,
    actor: Any,
    *,
    expected_version: int,
    reason: str = "",
) -> CuratedRecommendation:
    """主编撤回已批准或待审的精选推荐位。"""
    _check_curation_permission(actor)

    with transaction.atomic():
        rec = (
            CuratedRecommendation.objects.select_for_update()
            .filter(id=int(recommendation_id))
            .first()
        )
        if rec is None:
            raise ValidationError("目标精选推荐记录不存在。")
        if int(rec.version) != int(expected_version):
            raise ConcurrencyConflictError("该精选记录已被他人更新，请刷新后重试。")

        rec.status = "withdrawn"
        rec.is_pinned = False
        rec.version = int(rec.version) + 1
        rec.save()

        CuratedRecommendationAction.objects.create(
            recommendation=rec,
            actor=actor,
            action="withdraw",
            reason=(reason or "").strip()[:500],
            evidence_version=rec.version,
        )
        site_id = rec.site_id
        page_id = rec.page_id
        transaction.on_commit(lambda: invalidate_analytics_display_cache(page_ids=[page_id], site_id=site_id))
        return rec


def nominate_recommendation(
    site: Site | int,
    page: Page | int,
    position: str,
    actor: Any,
    *,
    column_page_id: int | None = None,
    priority: int = 0,
    is_pinned: bool = False,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    reason: str = "",
    auto_approve: bool = False,
    expected_version: int | None = None,
) -> CuratedRecommendation:
    """
    编辑人工提名精选（用于发掘冷门佳作）：
    可越过 D3-A 流量与 Quality Score 门槛，但绝不越过公开资格、正式版本一致性、指南时效或活动风险告警。
    """
    _check_curation_permission(actor)
    if position not in VALID_PLACEMENTS:
        raise ValidationError(f"不支持的精选投放位：{position}")
    if starts_at is not None and expires_at is not None and expires_at <= starts_at:
        raise ValidationError("结束时间必须晚于开始时间。")

    site_obj = _coerce_site(site)
    if site_obj is None:
        raise ValidationError("目标站点不存在。")
    if isinstance(page, Page) and getattr(page, "pk", None):
        page_obj = Page.objects.filter(id=int(page.pk)).first()
    elif isinstance(page, Page):
        page_obj = page
    else:
        page_obj = Page.objects.filter(id=int(page)).first()
    if page_obj is None:
        raise ValidationError("目标页面不存在。")

    rev_id = int(page_obj.live_revision_id or 0)
    spec_page = _assert_page_public_and_unblocked_for_curation(
        site=site_obj,
        page=page_obj,
        live_revision_id=rev_id,
    )

    resolved_col_id = column_page_id
    if position == "column_featured" and not resolved_col_id:
        resolved_col_id = resolve_column_page_id(spec_page)

    # 同步快照当前近 30 日基础质量参考（即便流量较低也留存证据）
    eval_date = timezone.localdate() - timedelta(days=1)
    start_30d = eval_date - timedelta(days=29)
    agg = (
        PageAnalyticsDaily.objects.filter(
            site_id=site_obj.id,
            page_id=spec_page.id,
            live_revision_id=rev_id,
            stat_date__gte=start_30d,
            stat_date__lte=eval_date,
        )
        .exclude(health_status="missing")
        .aggregate(
            vdays=Sum("daily_uv"),
            meas=Sum("measurable_views"),
            comp=Sum("completed_views"),
            bounce=Sum("short_bounce_views"),
            active_sec=Sum("total_active_seconds"),
        )
    )
    freshness_f, _ = _get_guide_freshness_score(spec_page, eval_date)
    q_res = compute_quality_score(
        measurable_views=int(agg["meas"] or 0),
        completed_views=int(agg["comp"] or 0),
        short_bounce_views=int(agg["bounce"] or 0),
        total_active_seconds=int(agg["active_sec"] or 0),
        visitor_days=int(agg["vdays"] or 0),
        freshness_score=freshness_f,
    )
    evidence = {
        "nominated_by_id": getattr(actor, "id", None),
        "nominated_at": timezone.now().isoformat(),
        "reason": (reason or "").strip()[:500],
        "quality_breakdown": q_res,
        "manual_nomination": True,
    }

    with transaction.atomic():
        existing = (
            CuratedRecommendation.objects.select_for_update()
            .filter(
                site_id=site_obj.id,
                page_id=spec_page.id,
                live_revision_id=rev_id,
                position=position,
            )
            .first()
        )
        if existing is not None:
            if expected_version is not None and int(existing.version) != int(expected_version):
                raise ConcurrencyConflictError("该精选记录已被他人更新，请刷新后重试。")
            existing.column_page_id = resolved_col_id
            existing.status = "approved" if auto_approve else "pending"
            existing.priority = int(priority)
            existing.is_pinned = bool(is_pinned)
            existing.starts_at = starts_at
            existing.expires_at = expires_at
            existing.rejected_cooldown_until = None
            existing.quality_score = q_res["quality_score"]
            merged_ev = dict(existing.evidence_json or {})
            merged_ev.update(evidence)
            existing.evidence_json = merged_ev
            existing.version = int(existing.version) + 1
            existing.save()
            rec = existing
        else:
            if expected_version is not None and int(expected_version) != 0:
                raise ConcurrencyConflictError("目标精选记录尚未创建，expected_version 应为 0 或省略。")
            rec = CuratedRecommendation.objects.create(
                site=site_obj,
                page_id=spec_page.id,
                live_revision_id=rev_id,
                position=position,
                column_page_id=resolved_col_id,
                status="approved" if auto_approve else "pending",
                priority=int(priority),
                is_pinned=bool(is_pinned),
                starts_at=starts_at,
                expires_at=expires_at,
                quality_score=q_res["quality_score"],
                evidence_json=evidence,
                version=1,
            )

        CuratedRecommendationAction.objects.create(
            recommendation=rec,
            actor=actor,
            action="nominate",
            reason=(reason or "").strip()[:500],
            evidence_version=rec.version,
        )
        transaction.on_commit(
            lambda: invalidate_analytics_display_cache(page_ids=[spec_page.id], site_id=site_obj.id)
        )
        return rec


def _check_and_bump_alert_version(
    alert: AnalyticsAlert,
    *,
    expected_version: int | None,
) -> int:
    """
    校验并递增告警版本（同时兼容模型显式 version 字段与 evidence_json['_version'] 乐观锁）。
    """
    if hasattr(alert, "version"):
        cur_ver = int(getattr(alert, "version") or 1)
        if expected_version is not None and cur_ver != int(expected_version):
            raise ConcurrencyConflictError("该告警已被他人处理，请刷新后重试。")
        new_ver = cur_ver + 1
        setattr(alert, "version", new_ver)
        return new_ver

    ev = dict(alert.evidence_json or {})
    cur_ver = int(ev.get("_version") or 1)
    if expected_version is not None and cur_ver != int(expected_version):
        raise ConcurrencyConflictError("该告警已被他人处理，请刷新后重试。")
    new_ver = cur_ver + 1
    ev["_version"] = new_ver
    alert.evidence_json = ev
    return new_ver


def _log_alert_related_recommendation_actions(
    *,
    alert: AnalyticsAlert,
    actor: Any,
    action: str,
    reason: str,
    withdraw_open: bool = False,
) -> None:
    recs = list(
        CuratedRecommendation.objects.select_for_update().filter(
            site_id=alert.site_id,
            page_id=alert.page_id,
            live_revision_id=alert.live_revision_id,
        )
    )
    for rec in recs:
        if withdraw_open and rec.status in ("pending", "approved"):
            rec.status = "withdrawn"
            rec.is_pinned = False
            rec.version = int(rec.version) + 1
            rec.save()
        CuratedRecommendationAction.objects.create(
            recommendation=rec,
            actor=actor,
            action=action,
            reason=(reason or "").strip()[:500],
            evidence_version=rec.version,
        )


def exempt_alert(
    alert_id: int,
    actor: Any,
    *,
    expected_version: int | None = None,
    reason: str = "",
    days: int = _MAX_EXEMPT_DAYS,
) -> AnalyticsAlert:
    """
    主编对同站点同正式版本授予限时豁免（最长 14 天）。
    豁免不能绕过公开资格检查或严重异常流量风险（TRAFFIC_ANOMALY 不可豁免）。
    """
    _check_alert_permission(actor)
    days_int = int(days)
    if days_int < 1 or days_int > _MAX_EXEMPT_DAYS:
        raise ValidationError(f"告警限时豁免期限必须在 1 到 {_MAX_EXEMPT_DAYS} 天之间。")

    with transaction.atomic():
        alert = AnalyticsAlert.objects.select_for_update().filter(id=int(alert_id)).first()
        if alert is None:
            raise ValidationError("目标分析告警不存在。")
        if alert.alert_kind == "TRAFFIC_ANOMALY":
            raise ValidationError("重大流量异常告警 (TRAFFIC_ANOMALY) 不允许通过限时豁免放行。")

        new_ver = _check_and_bump_alert_version(alert, expected_version=expected_version)
        now = timezone.now()
        ev = dict(alert.evidence_json or {})
        ev.update(
            {
                "exempted_by_id": getattr(actor, "id", None),
                "exempt_reason": (reason or "").strip()[:500],
                "exempted_at": now.isoformat(),
                "exempt_days": days_int,
            }
        )
        alert.evidence_json = ev
        alert.status = "exempted"
        alert.exempt_until = now + timedelta(days=days_int)
        alert.save()

        _log_alert_related_recommendation_actions(
            alert=alert,
            actor=actor,
            action="exempt_alert",
            reason=f"[{alert.alert_kind} v{new_ver}] {reason}".strip(),
            withdraw_open=False,
        )
        transaction.on_commit(
            lambda: invalidate_analytics_display_cache(page_ids=[alert.page_id], site_id=alert.site_id)
        )
        return alert


def confirm_demote_alert(
    alert_id: int,
    actor: Any,
    *,
    expected_version: int | None = None,
    reason: str = "",
) -> AnalyticsAlert:
    """
    主编核查后确认该版本降权下榜（confirmed_demoted），并撤回该版本的在投/待审精选推荐。
    """
    _check_alert_permission(actor)

    with transaction.atomic():
        alert = AnalyticsAlert.objects.select_for_update().filter(id=int(alert_id)).first()
        if alert is None:
            raise ValidationError("目标分析告警不存在。")

        new_ver = _check_and_bump_alert_version(alert, expected_version=expected_version)
        now = timezone.now()
        ev = dict(alert.evidence_json or {})
        ev.update(
            {
                "demoted_by_id": getattr(actor, "id", None),
                "demote_reason": (reason or "").strip()[:500],
                "demoted_at": now.isoformat(),
            }
        )
        alert.evidence_json = ev
        alert.status = "confirmed_demoted"
        alert.exempt_until = None
        alert.save()

        _log_alert_related_recommendation_actions(
            alert=alert,
            actor=actor,
            action="confirm_demote_alert",
            reason=f"[{alert.alert_kind} v{new_ver}] {reason}".strip(),
            withdraw_open=True,
        )
        transaction.on_commit(
            lambda: invalidate_analytics_display_cache(page_ids=[alert.page_id], site_id=alert.site_id)
        )
        return alert


def _extract_badge_and_summary(spec_page: Any, type_key: str) -> tuple[str, str, str]:
    """零模型分支地提取卡片徽章、摘要与成文日期标签。"""
    badge = ""
    topic = getattr(spec_page, "topic", None)
    if topic is not None and getattr(topic, "name", ""):
        badge = str(topic.name)
    elif getattr(spec_page, "primary_language", ""):
        badge = str(spec_page.primary_language)
    else:
        adapter = get_type_adapter(type_key) if type_key else None
        if adapter is not None:
            try:
                badge = str(spec_page._meta.verbose_name)
            except Exception:
                badge = type_key
        else:
            badge = str(getattr(getattr(spec_page, "_meta", None), "verbose_name", "") or "精选内容")

    summary = str(getattr(spec_page, "summary", "") or "").strip()
    if not summary:
        intro_val = getattr(spec_page, "intro", None)
        if intro_val is not None:
            summary = str(getattr(intro_val, "raw_text", "") or "").strip()
            if not summary:
                try:
                    summary = str(intro_val).strip()
                except Exception:
                    summary = ""
    if not summary:
        summary = str(getattr(spec_page, "search_description", "") or spec_page.title or "").strip()

    authored_on = getattr(spec_page, "authored_on", None)
    date_label = authored_on.strftime("%Y-%m-%d") if authored_on else ""
    return badge, summary, date_label


class RecommendationService:
    """首页与栏目精选推荐位读侧强核验投放服务。"""

    @classmethod
    def for_placement(
        cls,
        site: Site | int | None,
        position: str,
        root_page_id: int | None = None,
        limit: int = 8,
        *,
        request: Any = None,
    ) -> list[PlacementItem]:
        """
        按投放位读取已批准（status='approved'）且在有效时间窗内的精选推荐：
        - 排序：is_pinned DESC, priority DESC, quality_score DESC；
        - 读侧批量强核验 PostgreSQL 主库：
          1. Page.live=True 且 alias_of_id is None；
          2. live_revision_id 与推荐记录完全一致；
          3. 未进入 get_public_blocked_page_ids()（排除自身/祖先未发布、受限、回收站）；
          4. 属于当前站点子树（以及指定的 root_page_id 后代）；
          5. 无未豁免的活动 HIGH_TRAFFIC_LOW_REPUTATION / TRAFFIC_ANOMALY 或 confirmed_demoted 告警；
          6. 若为指南则未超过 365 天复核期限。
        - 绝不拿未审批最新文章冒充精选；无合规记录返回 []。
        """
        if position not in VALID_PLACEMENTS:
            return []
        site_obj = _coerce_site(site)
        if site_obj is None:
            return []

        safe_limit = max(1, min(int(limit or 8), 50))
        now = timezone.now()
        today = timezone.localdate()

        qs = (
            CuratedRecommendation.objects.filter(
                site_id=site_obj.id,
                position=position,
                status="approved",
                page__live=True,
                page__alias_of__isnull=True,
            )
            .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
            .select_related("page")
            .order_by("-is_pinned", "-priority", "-quality_score", "-id")
        )

        site_root_path = ""
        site_root_depth = 0
        if site_obj.root_page_id:
            site_root_path = site_obj.root_page.path
            site_root_depth = site_obj.root_page.depth
            qs = qs.filter(page__path__startswith=site_root_path, page__depth__gt=site_root_depth)

        if root_page_id is not None:
            root_row = Page.objects.filter(id=int(root_page_id)).values_list("path", "depth").first()
            if not root_row:
                return []
            root_path, root_depth = root_row
            if site_root_path and not root_path.startswith(site_root_path):
                return []
            qs = qs.filter(page__path__startswith=root_path, page__depth__gt=root_depth)
            if position == "column_featured":
                qs = qs.filter(Q(column_page_id=int(root_page_id)) | Q(column_page_id__isnull=True))

        # 仅拉取有界候选窗（<= 4*limit），杜绝全表拉取
        fetch_cap = max(32, safe_limit * 4)
        try:
            candidates = list(qs[:fetch_cap])
        except DatabaseError as exc:
            logger.warning("RecommendationService query failed (table may not be migrated yet): %s", exc)
            return []

        if not candidates:
            return []

        # 1. 版本一致性初筛
        rev_matched = [
            rec
            for rec in candidates
            if rec.page.live_revision_id is not None
            and int(rec.page.live_revision_id) == int(rec.live_revision_id)
        ]
        if not rev_matched:
            return []

        # 2. 批量公开访问资格核验（穿透祖先限制/回收/未上线）
        blocked_ids = get_public_blocked_page_ids([(rec.page_id, rec.page.path) for rec in rev_matched])
        public_cands = [rec for rec in rev_matched if rec.page_id not in blocked_ids]
        if not public_cands:
            return []

        # 3. 批量风险告警核验（未豁免活动 HIGH_TRAFFIC_LOW_REPUTATION / TRAFFIC_ANOMALY 或 confirmed_demoted）
        cand_pids = sorted({rec.page_id for rec in public_cands})
        blocking_pairs = set(
            AnalyticsAlert.objects.filter(
                site_id=site_obj.id,
                page_id__in=cand_pids,
                alert_kind__in=("HIGH_TRAFFIC_LOW_REPUTATION", "TRAFFIC_ANOMALY"),
            )
            .filter(
                Q(status="active")
                | Q(status="confirmed_demoted")
                | Q(status="exempted", exempt_until__lte=now)
                | Q(status="exempted", exempt_until__isnull=True)
            )
            .values_list("page_id", "live_revision_id")
        )
        unblocked_cands = [
            rec for rec in public_cands if (rec.page_id, rec.live_revision_id) not in blocking_pairs
        ]
        if not unblocked_cands:
            return []

        # 4. 批量拉取具体子类模型实例，检查 is_recycled、能力声明与指南 365 天复核时效
        valid_pids = sorted({rec.page_id for rec in unblocked_cands})
        specific_pages = {p.id: p for p in Page.objects.filter(id__in=valid_pids).specific()}

        results: list[PlacementItem] = []
        seen_page_ids: set[int] = set()
        for rec in unblocked_cands:
            if rec.page_id in seen_page_ids:
                continue
            spec_page = specific_pages.get(rec.page_id)
            if spec_page is None or getattr(spec_page, "is_recycled", False):
                continue
            if not getattr(spec_page, "live", False) or getattr(spec_page, "alias_of_id", None) is not None:
                continue
            if int(getattr(spec_page, "live_revision_id", 0) or 0) != int(rec.live_revision_id):
                continue

            caps, type_key = resolve_page_analytics_spec(spec_page)
            if caps is None or caps.kind != "detail" or not getattr(caps, "curation_eligible", False):
                continue

            _, is_overdue = _get_guide_freshness_score(spec_page, today)
            if is_overdue:
                continue

            badge, summary, date_label = _extract_badge_and_summary(spec_page, type_key)
            try:
                url = (spec_page.get_url(request) if request is not None else spec_page.get_url()) or spec_page.url
            except Exception:
                url = getattr(spec_page, "url", "") or getattr(spec_page, "url_path", "")

            seen_page_ids.add(rec.page_id)
            results.append(
                PlacementItem(
                    {
                        "recommendation_id": rec.id,
                        "page_id": spec_page.id,
                        "live_revision_id": int(rec.live_revision_id),
                        "type_key": type_key,
                        "badge": badge,
                        "title": spec_page.title,
                        "url": url,
                        "summary": summary,
                        "date_label": date_label,
                        "quality_score": float(rec.quality_score or 0.0),
                        "is_pinned": bool(rec.is_pinned),
                        "priority": int(rec.priority or 0),
                        "page": spec_page,
                    }
                )
            )
            if len(results) >= safe_limit:
                break

        return results
