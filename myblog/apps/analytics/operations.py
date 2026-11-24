"""
analytics.operations
第 3 期作者产能与维护看板、工作流/评论审核效率统计与站内治理汇总服务。

核心边界：
1. 人物与权限严格分离：`people.Person` 仅用于内容署名，后台数据权限严格以
   `OwnershipPagePermissionTester` + `Person.linked_user` 为准，绝不按同名关联；
2. 遵循千万级容量红线：作者列表与待审/治理列表全部有界分页（单页 <= 20 条，深跳超 100 页返回 422），
   统计汇总在 PostgreSQL 聚合或有界窗口切片完成；
3. 质量指标仅供运营维护参考，绝不直接用于绩效考核、自动封禁或权限调整；治理视图绝不展示访客 IP。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import math
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.utils import timezone
from wagtail.models import Page, PageLogEntry, Site, TaskState, WorkflowState

from analytics.models import (
    AnalyticsAlert,
    AnalyticsSettings,
    ArticleRatingAggregate,
    CuratedRecommendation,
    PageAnalyticsDaily,
    PageAnalyticsTotal,
)
from analytics.permissions import (
    can_resolve_analytics_alerts,
    can_view_analytics_menu,
    can_view_site_analytics,
    filter_accessible_pages_for_analytics,
)
from analytics.services import get_project_tech_stack_heat
from content_lifecycle.models import RecycleBatch
from content_lifecycle.public_access import get_public_blocked_page_ids
from discovery.pagination import (
    MAX_SHALLOW_PAGE_CAP,
    PageWindowExceededError,
    build_count_meta,
    build_pagination_payload,
)


_STALE_UPDATE_DAYS = 90
_OVERDUE_GUIDE_DAYS = 365
_REVIEW_TIMEOUT_HOURS = 72.0


def _resolve_site(site_id: int | None = None) -> Site | None:
    if site_id is not None:
        s = Site.objects.filter(id=int(site_id)).select_related("root_page").first()
        if s is not None:
            return s
    return (
        Site.objects.filter(is_default_site=True).select_related("root_page").first()
        or Site.objects.select_related("root_page").order_by("id").first()
    )


def _percentile_sorted(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, int(math.ceil(len(sorted_vals) * pct)) - 1))
    return round(sorted_vals[idx], 2)


def _summarize_durations(hours_list: list[float]) -> dict[str, Any]:
    if not hours_list:
        return {
            "sample_count": 0,
            "mean_hours": None,
            "median_hours": None,
            "p90_hours": None,
            "timeout_count": 0,
        }
    s = sorted(hours_list)
    n = len(s)
    mean_h = round(sum(s) / float(n), 2)
    if n % 2 == 1:
        med_h = round(s[n // 2], 2)
    else:
        med_h = round((s[n // 2 - 1] + s[n // 2]) / 2.0, 2)
    p90_h = _percentile_sorted(s, 0.90)
    timeout_cnt = sum(1 for h in s if h > _REVIEW_TIMEOUT_HOURS)
    return {
        "sample_count": n,
        "mean_hours": mean_h,
        "median_hours": med_h,
        "p90_hours": p90_h,
        "timeout_count": timeout_cnt,
    }


def _bucket_wait_ages(created_times: list[datetime], now: datetime) -> dict[str, int]:
    lt_24h = 0
    d1_to_3 = 0
    gt_3d = 0
    for dt in created_times:
        if dt is None:
            continue
        hours = max(0.0, (now - dt).total_seconds() / 3600.0)
        if hours < 24.0:
            lt_24h += 1
        elif hours <= 72.0:
            d1_to_3 += 1
        else:
            gt_3d += 1
    return {
        "total": len(created_times),
        "lt_24h": lt_24h,
        "d1_to_3": d1_to_3,
        "gt_3d": gt_3d,
    }


def _collect_author_capacity_rows(
    user,
    *,
    site: Site | None,
    days: int,
    author_q: str,
    page_num: int,
    per_page: int,
) -> dict[str, Any]:
    from discovery.models import PublishedSearchAuthor, PublishedSearchDocument
    from discovery.registry import get_author_enabled_type_keys, list_review_cycle_type_keys
    from people.models import Person

    is_site_mgr = can_view_site_analytics(user)
    today = timezone.localdate()
    start_date = today - timedelta(days=days - 1)
    start_dt = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
    stale_cutoff = today - timedelta(days=_STALE_UPDATE_DAYS)
    overdue_cutoff = today - timedelta(days=_OVERDUE_GUIDE_DAYS)

    settings_obj = AnalyticsSettings.get_for_site_readonly(site)
    min_votes = max(1, int(getattr(settings_obj, "min_rating_votes", 20) or 20))
    rating_enabled = bool(getattr(settings_obj, "rating_enabled", False))

    # 确定人物列表作用域：全站主管按关键词检索 Person；受限作者仅限本人明确绑定的 Person
    if is_site_mgr:
        person_qs = Person.objects.filter(is_system_anonymous=False)
        if author_q:
            person_qs = person_qs.filter(Q(name__icontains=author_q) | Q(slug__icontains=author_q))
        person_qs = person_qs.order_by("initial_group_rank", "initial", "sort_key", "id")
    else:
        person_qs = Person.objects.filter(linked_user_id=user.id, is_system_anonymous=False)
        if author_q:
            person_qs = person_qs.filter(Q(name__icontains=author_q) | Q(slug__icontains=author_q))
        person_qs = person_qs.order_by("id")

    max_jump = min(MAX_SHALLOW_PAGE_CAP, max(1, (5000 + per_page - 1) // per_page))
    if page_num > max_jump:
        raise PageWindowExceededError(page_num, max_jump)

    probe_ids = list(person_qs.values_list("id", flat=True)[:5001])
    if len(probe_ids) > 5000:
        total_count = 5000
        count_kind = "lower_bound"
    else:
        total_count = len(probe_ids)
        count_kind = "exact"

    offset = (page_num - 1) * per_page
    slice_person_ids = probe_ids[offset : offset + per_page]
    has_next = len(probe_ids) > offset + per_page
    has_previous = page_num > 1 and bool(slice_person_ids)

    persons_by_id = {p.id: p for p in Person.objects.filter(id__in=slice_person_ids)}
    ordered_persons = [persons_by_id[pid] for pid in slice_person_ids if pid in persons_by_id]

    # 通过共享索引与注册表能力查询公开有效文档，彻底解除对具体 Page 模型的硬编码依赖
    author_types = get_author_enabled_type_keys()
    docs_qs = PublishedSearchDocument.objects.filter(
        is_live=True,
        is_recycled=False,
        index_generation=1,
    )
    if site is not None:
        if site.id:
            docs_qs = docs_qs.filter(Q(site_id=site.id) | Q(site_id__isnull=True))
        if site.root_page_id:
            docs_qs = docs_qs.filter(root_path__startswith=site.root_page.path, depth__gt=site.root_page.depth)

    author_docs_qs = docs_qs.filter(type_key__in=author_types) if author_types else docs_qs.none()
    candidate_rows = list(author_docs_qs.values_list("page_id", "root_path")[:5000])
    blocked_page_ids = get_public_blocked_page_ids(candidate_rows) if candidate_rows else set()
    eligible_page_ids = {pid for pid, _ in candidate_rows if pid not in blocked_page_ids}
    if not is_site_mgr and eligible_page_ids:
        eligible_page_ids = set(
            filter_accessible_pages_for_analytics(
                user,
                Page.objects.filter(id__in=eligible_page_ids),
            ).values_list("id", flat=True)
        )

    # 按 (person_id, page_id) 去重汇总作品关系与角色
    person_pages: dict[int, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    if slice_person_ids and eligible_page_ids:
        for row in PublishedSearchAuthor.objects.filter(
            person_id__in=slice_person_ids,
            document__index_generation=1,
            document__page_id__in=eligible_page_ids,
        ).values("person_id", "document__page_id", "role"):
            person_pages[int(row["person_id"])][int(row["document__page_id"])].add(row["role"] or "primary_author")

    all_touched_pids = sorted({pid for pmap in person_pages.values() for pid in pmap.keys()})

    # 提取页面基础元数据、近期再发布次数与质量/精选/评价指标
    page_meta: dict[int, dict[str, Any]] = {}
    if all_touched_pids:
        for p in Page.objects.filter(id__in=all_touched_pids).values("id", "title", "url_path", "first_published_at", "last_published_at", "live_revision_id"):
            page_meta[int(p["id"])] = dict(p)

    # 通过 PublishedSearchDocument 统一获取成文日期与技术复核元数据
    review_cycles = list_review_cycle_type_keys()
    doc_authored_map: dict[int, date] = {}
    review_meta_map: dict[int, tuple[date | None, int]] = {}
    if all_touched_pids:
        for dr in PublishedSearchDocument.objects.filter(
            index_generation=1,
            page_id__in=all_touched_pids,
        ).values("page_id", "type_key", "authored_on", "extra_data"):
            pid = int(dr["page_id"])
            if dr.get("authored_on"):
                doc_authored_map[pid] = dr["authored_on"]
            if dr["type_key"] in review_cycles:
                raw_lra = (dr.get("extra_data") or {}).get("last_reviewed_at")
                parsed_lra = None
                if raw_lra:
                    try:
                        parsed_lra = date.fromisoformat(str(raw_lra)[:10])
                    except ValueError:
                        parsed_lra = None
                review_meta_map[pid] = (parsed_lra, review_cycles[dr["type_key"]])

    # 真实发布日志统计窗口内首发/再发布事件（绝不按 authored_on 或草稿保存计数）
    pub_counts_by_pid: dict[int, int] = defaultdict(int)
    if all_touched_pids:
        for lr in (
            PageLogEntry.objects.filter(
                page_id__in=all_touched_pids,
                action="wagtail.publish",
                timestamp__gte=start_dt,
            )
            .values("page_id")
            .annotate(cnt=Count("id"))
        ):
            pub_counts_by_pid[int(lr["page_id"])] = int(lr["cnt"] or 0)

    # 汇总窗口流量与累计流量
    tot_by_pid: dict[int, dict[str, int]] = {}
    if site is not None and all_touched_pids:
        for tr in PageAnalyticsTotal.objects.filter(site=site, page_id__in=all_touched_pids).values(
            "page_id", "total_pv", "total_visitor_days", "total_valid_views", "total_completed_views"
        ):
            tot_by_pid[int(tr["page_id"])] = tr

    win_by_pid: dict[int, dict[str, int]] = defaultdict(lambda: {"pv": 0, "meas": 0, "comp": 0})
    if site is not None and all_touched_pids:
        for dr in (
            PageAnalyticsDaily.objects.filter(
                site=site,
                page_id__in=all_touched_pids,
                stat_date__gte=start_date,
                stat_date__lte=today,
            )
            .exclude(health_status="missing")
            .values("page_id")
            .annotate(
                pv=Sum("pv"),
                meas=Sum("measurable_views"),
                comp=Sum("completed_views"),
            )
        ):
            win_by_pid[int(dr["page_id"])] = {
                "pv": int(dr["pv"] or 0),
                "meas": int(dr["meas"] or 0),
                "comp": int(dr["comp"] or 0),
            }

    curated_pids: set[int] = set()
    if site is not None and all_touched_pids:
        curated_pids = set(
            CuratedRecommendation.objects.filter(
                site=site,
                page_id__in=all_touched_pids,
                status="approved",
            ).values_list("page_id", flat=True)
        )

    rating_by_pid: dict[int, dict[str, int]] = defaultdict(lambda: {"pos": 0, "total": 0})
    if site is not None and all_touched_pids and rating_enabled:
        for ag in ArticleRatingAggregate.objects.filter(site=site, page_id__in=all_touched_pids).values(
            "page_id", "live_revision_id", "excellent_count", "good_count", "pass_count", "poor_count"
        ):
            pid = int(ag["page_id"])
            pm = page_meta.get(pid)
            if pm and pm.get("live_revision_id") and int(pm["live_revision_id"]) == int(ag["live_revision_id"]):
                pos = int(ag["excellent_count"] or 0) + int(ag["good_count"] or 0)
                tot = pos + int(ag["pass_count"] or 0) + int(ag["poor_count"] or 0)
                rating_by_pid[pid]["pos"] += pos
                rating_by_pid[pid]["total"] += tot

    author_rows: list[dict[str, Any]] = []
    for person in ordered_persons:
        pmap = person_pages.get(person.id, {})
        pids = sorted(pmap.keys())
        page_count = len(pids)
        role_counts: dict[str, int] = defaultdict(int)
        first_pub_win = 0
        repub_win = 0
        stale_90d_count = 0
        overdue_guides = 0
        total_pv_sum = 0
        total_vdays_sum = 0
        win_meas_sum = 0
        win_comp_sum = 0
        curated_count = 0
        rating_pos = 0
        rating_tot = 0
        latest_pub_dt: datetime | None = None

        for pid in pids:
            for r_code in pmap[pid]:
                role_counts[r_code] += 1
            pm = page_meta.get(pid) or {}
            fp_dt = pm.get("first_published_at")
            lp_dt = pm.get("last_published_at")
            if fp_dt and fp_dt >= start_dt:
                first_pub_win += 1
            repub_win += pub_counts_by_pid.get(pid, 0)
            ref_dt = lp_dt or fp_dt
            if ref_dt is not None:
                if latest_pub_dt is None or ref_dt > latest_pub_dt:
                    latest_pub_dt = ref_dt
                if ref_dt.date() < stale_cutoff:
                    stale_90d_count += 1
            elif pid in doc_authored_map and doc_authored_map[pid] < stale_cutoff:
                stale_90d_count += 1

            if pid in review_meta_map:
                lra, cycle_days = review_meta_map[pid]
                if lra is None or lra <= (today - timedelta(days=cycle_days)):
                    overdue_guides += 1

            tr = tot_by_pid.get(pid)
            if tr:
                total_pv_sum += int(tr["total_pv"] or 0)
                total_vdays_sum += int(tr["total_visitor_days"] or 0)
            wr = win_by_pid.get(pid)
            if wr:
                win_meas_sum += wr["meas"]
                win_comp_sum += wr["comp"]
            if pid in curated_pids:
                curated_count += 1
            rt = rating_by_pid.get(pid)
            if rt:
                rating_pos += rt["pos"]
                rating_tot += rt["total"]

        avg_pv = round(total_pv_sum / float(page_count), 1) if page_count > 0 else 0.0
        avg_vdays = round(total_vdays_sum / float(page_count), 1) if page_count > 0 else 0.0
        completion_pct = round((win_comp_sum / float(win_meas_sum)) * 100.0, 1) if win_meas_sum > 0 else None
        curation_pct = round((curated_count / float(page_count)) * 100.0, 1) if page_count > 0 else None
        if not rating_enabled:
            favorable_pct = None
            rating_note = "未启用"
        elif rating_tot < min_votes:
            favorable_pct = None
            rating_note = f"观察中 ({rating_tot}/{min_votes})"
        else:
            favorable_pct = round((rating_pos / float(rating_tot)) * 100.0, 1)
            rating_note = f"有效 {rating_tot} 票"

        role_summary = ", ".join(f"{k}:{v}" for k, v in sorted(role_counts.items()))
        author_rows.append(
            {
                "person": person,
                "is_linked_account": bool(person.linked_user_id),
                "page_count": page_count,
                "role_summary": role_summary or "-",
                "first_published_win": first_pub_win,
                "republish_win": repub_win,
                "stale_90d_count": stale_90d_count,
                "overdue_guides": overdue_guides,
                "latest_pub_date": latest_pub_dt.date().isoformat() if latest_pub_dt else "-",
                "avg_pv": avg_pv,
                "avg_visitor_days": avg_vdays,
                "completion_pct": completion_pct,
                "curated_count": curated_count,
                "curation_pct": curation_pct,
                "favorable_pct": favorable_pct,
                "rating_note": rating_note,
                "rating_votes": rating_tot,
            }
        )

    # 全站逾期复核页面责任清单（按注册表声明 review_cycle_days 的页面类型动态汇总，最多取前 30 条）
    overdue_list: list[dict[str, Any]] = []
    overdue_candidates: list[tuple[date, int, dict[str, Any]]] = []
    if review_cycles:
        rev_rows = list(
            docs_qs.filter(type_key__in=list(review_cycles.keys())).values(
                "page_id", "type_key", "title", "url_path", "root_path", "extra_data"
            )[:1000]
        )
        rev_blocked = get_public_blocked_page_ids([(int(r["page_id"]), r["root_path"]) for r in rev_rows])
        rev_eligible_ids = {int(r["page_id"]) for r in rev_rows if int(r["page_id"]) not in rev_blocked}
        if not is_site_mgr and rev_eligible_ids:
            rev_eligible_ids = set(
                filter_accessible_pages_for_analytics(
                    user,
                    Page.objects.filter(id__in=rev_eligible_ids),
                ).values_list("id", flat=True)
            )
        for r in rev_rows:
            pid = int(r["page_id"])
            if pid not in rev_eligible_ids:
                continue
            cycle_days = review_cycles[r["type_key"]]
            cutoff = today - timedelta(days=cycle_days)
            raw_lra = (r.get("extra_data") or {}).get("last_reviewed_at")
            lra_date = None
            if raw_lra:
                try:
                    lra_date = date.fromisoformat(str(raw_lra)[:10])
                except ValueError:
                    lra_date = None
            if lra_date is None or lra_date <= cutoff:
                overdue_candidates.append((lra_date or date.min, pid, {**r, "_lra_date": lra_date}))
        overdue_candidates.sort(key=lambda item: (item[0], item[1]))
        overdue_candidates = overdue_candidates[:30]

    og_ids = [pid for _, pid, _ in overdue_candidates]
    og_authors: dict[int, list[str]] = defaultdict(list)
    if og_ids:
        for ga in (
            PublishedSearchAuthor.objects.filter(
                document__index_generation=1,
                document__page_id__in=og_ids,
            )
            .exclude(author_slug="anonymous")
            .select_related("document")
            .order_by("sort_order", "id")
        ):
            if ga.author_name:
                og_authors[int(ga.document.page_id)].append(f"{ga.author_name}({ga.role or 'author'})")
    for _, pid, g in overdue_candidates:
        owners = og_authors.get(pid, [])
        lra_date = g["_lra_date"]
        overdue_list.append(
            {
                "page_id": pid,
                "title": g["title"],
                "url_path": g["url_path"],
                "last_reviewed_at": lra_date.isoformat() if lra_date else "未记录",
                "days_overdue": (today - lra_date).days if lra_date else None,
                "assignees": "、".join(owners) if owners else "未分派维护人",
                "is_unassigned": not bool(owners),
            }
        )

    count_meta = build_count_meta(value=total_count, kind=count_kind)
    pagination = build_pagination_payload(
        mode="shallow",
        current_page=page_num,
        page_size=per_page,
        total_count=total_count,
        count_kind=count_kind,
        has_next=has_next,
        has_previous=has_previous,
        next_cursor=None,
        previous_cursor=None,
    )
    return {
        "rows": author_rows,
        "count_meta": count_meta,
        "pagination": pagination,
        "overdue_guides": overdue_list,
        "min_rating_votes": min_votes,
    }


def _collect_review_and_governance_summary(site: Site | None, *, days: int) -> dict[str, Any]:
    from comments.models import Comment, CommentReviewAction

    now = timezone.now()
    start_dt = now - timedelta(days=days)

    # 1. Wagtail 页面工作流审核积压与处理耗时
    pending_wf_times = list(
        WorkflowState.objects.filter(status=WorkflowState.STATUS_IN_PROGRESS)
        .values_list("created_at", flat=True)[:2000]
    )
    wf_backlog = _bucket_wait_ages(pending_wf_times, now)

    wf_durations: list[float] = []
    wf_approved = 0
    wf_rejected = 0
    for ts in (
        TaskState.objects.filter(
            finished_at__gte=start_dt,
            finished_at__isnull=False,
            started_at__isnull=False,
        )
        .values("status", "started_at", "finished_at")[:5000]
    ):
        st = str(ts["status"] or "")
        if st == TaskState.STATUS_APPROVED:
            wf_approved += 1
        elif st == TaskState.STATUS_REJECTED:
            wf_rejected += 1
        else:
            continue
        dur_h = max(0.0, (ts["finished_at"] - ts["started_at"]).total_seconds() / 3600.0)
        wf_durations.append(dur_h)

    wf_decided = wf_approved + wf_rejected
    wf_rejection_rate_pct = round((wf_rejected / float(wf_decided)) * 100.0, 1) if wf_decided > 0 else None
    wf_summary = {
        "backlog": wf_backlog,
        "turnaround": _summarize_durations(wf_durations),
        "approved_count": wf_approved,
        "rejected_count": wf_rejected,
        "rejection_rate_pct": wf_rejection_rate_pct,
    }

    # 2. 评论审核积压与处理耗时（基于 pending_submitted_at 与 CommentReviewAction）
    pending_comment_times = list(
        Comment.objects.filter(status="pending", pending_submitted_at__isnull=False)
        .values_list("pending_submitted_at", flat=True)[:2000]
    )
    cmt_backlog = _bucket_wait_ages(pending_comment_times, now)

    cmt_durations: list[float] = []
    cmt_approved = 0
    cmt_rejected = 0
    for act in (
        CommentReviewAction.objects.filter(created_at__gte=start_dt)
        .select_related("comment")
        .values("action", "new_status", "created_at", "comment__pending_submitted_at", "comment__created_at")[:5000]
    ):
        act_name = str(act["action"] or "").lower()
        new_st = str(act["new_status"] or "").lower()
        if "approve" in act_name or new_st == "active":
            cmt_approved += 1
        elif "reject" in act_name or new_st == "rejected":
            cmt_rejected += 1
        else:
            continue
        sub_dt = act.get("comment__pending_submitted_at") or act.get("comment__created_at")
        if sub_dt is not None and act["created_at"] >= sub_dt:
            cmt_durations.append((act["created_at"] - sub_dt).total_seconds() / 3600.0)

    cmt_decided = cmt_approved + cmt_rejected
    cmt_rejection_rate_pct = round((cmt_rejected / float(cmt_decided)) * 100.0, 1) if cmt_decided > 0 else None
    comment_summary = {
        "backlog": cmt_backlog,
        "turnaround": _summarize_durations(cmt_durations),
        "approved_count": cmt_approved,
        "rejected_count": cmt_rejected,
        "rejection_rate_pct": cmt_rejection_rate_pct,
    }

    # 3. 治理与风控汇总（无任何访客 IP 或请求轨迹）
    recycle_counts = {"recycled": 0, "restored": 0, "purged": 0}
    for rc in RecycleBatch.objects.filter(recycled_at__gte=start_dt).values("status").annotate(cnt=Count("id")):
        if rc["status"] in recycle_counts:
            recycle_counts[rc["status"]] = int(rc["cnt"] or 0)

    alert_counts: dict[str, int] = defaultdict(int)
    if site is not None:
        for ac in AnalyticsAlert.objects.filter(site=site).values("status").annotate(cnt=Count("id")):
            alert_counts[str(ac["status"])] = int(ac["cnt"] or 0)

    return {
        "workflow": wf_summary,
        "comments": comment_summary,
        "governance": {
            "recycle": recycle_counts,
            "alerts": dict(alert_counts),
        },
    }


def get_operations_dashboard_data(
    user,
    *,
    site_id: int | None = None,
    days: int = 30,
    author_q: str = "",
    page_num: int = 1,
    per_page: int = 20,
) -> dict[str, Any]:
    """
    第 3 期运营与审核工作台统一入口：
    - 全站主管/超管：全站作者产能、逾期指南清单、工作流/评论审核效率、治理统计、Project 技术栈热度；
    - 受限作者：仅见本人绑定 Person 的个人页面产出统计（不显示他人或全站审核治理）。
    """
    if not can_view_analytics_menu(user):
        raise PermissionDenied("当前用户无权访问数据分析运营工作台。")

    site = _resolve_site(site_id)
    all_sites = list(Site.objects.order_by("id"))
    days = 7 if int(days or 30) <= 7 else (90 if int(days or 30) >= 90 else 30)
    author_q = (author_q or "").strip()[:80]
    page_num = max(1, int(page_num or 1))
    per_page = max(1, min(int(per_page or 20), 50))

    is_site_mgr = can_view_site_analytics(user)
    can_gov = is_site_mgr or can_resolve_analytics_alerts(user)

    author_data = _collect_author_capacity_rows(
        user,
        site=site,
        days=days,
        author_q=author_q,
        page_num=page_num,
        per_page=per_page,
    )
    tech_stack = get_project_tech_stack_heat(site, days=30 if days >= 30 else 7) if is_site_mgr else {"languages": [], "tags": []}
    review_gov = _collect_review_and_governance_summary(site, days=days) if can_gov else None

    return {
        "site": site,
        "sites": all_sites,
        "is_site_manager": is_site_mgr,
        "can_view_governance": can_gov,
        "filters": {
            "site_id": site.id if site else None,
            "days": days,
            "author_q": author_q,
        },
        "authors": author_data,
        "tech_stack": tech_stack,
        "review_governance": review_gov,
    }
