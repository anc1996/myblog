"""
analytics.wagtail_hooks
Wagtail 8.1 数据分析后台路由、权限注册、左侧菜单与页面列表快捷入口。
"""

from __future__ import annotations

from datetime import date, datetime
from urllib.parse import urlencode

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.template import engines
from django.urls import path, reverse, reverse_lazy
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from wagtail import hooks
from wagtail.admin.auth import require_admin_access
from wagtail.admin.menu import MenuItem
from wagtail.admin.ui.sidebar import LinkMenuItem as LinkMenuItemComponent
from wagtail.admin.widgets.button import ListingButton
from wagtail.models import Page, Site

from analytics.curation import (
    ConcurrencyConflictError,
    approve_recommendation,
    confirm_demote_alert,
    exempt_alert,
    nominate_recommendation,
    reject_recommendation,
    withdraw_recommendation,
)
from analytics.models import (
    ALERT_KIND_CHOICES,
    ALERT_STATUS_CHOICES,
    CURATION_POSITION_CHOICES,
    CURATION_STATUS_CHOICES,
    RANKING_BOARD_KIND_CHOICES,
    AnalyticsAlert,
    CuratedRecommendation,
    CuratedRecommendationAction,
    RankingSnapshot,
)
from analytics.permissions import (
    can_approve_curation,
    can_resolve_analytics_alerts,
    can_view_analytics_menu,
    can_view_page_analytics,
    can_view_site_analytics,
)
from analytics.services import (
    get_author_pages_analytics_list,
    get_page_analytics_detail,
    get_site_dashboard_data,
)
from discovery.pagination import (
    COUNT_PROBE_LIMIT,
    PageWindowExceededError,
    build_count_meta,
    build_pagination_payload,
    compute_max_jump_page,
)
from discovery.registry import list_analytics_adapters


def _parse_int_param(raw: str | None, default: int | None = None) -> int | None:
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _parse_bool_param(raw: str | None, default: bool = True) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _parse_optional_dt(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    dt = parse_datetime(text)
    if dt is None:
        d = parse_date(text)
        if d is not None:
            dt = datetime.combine(d, datetime.min.time())
    if dt is None:
        raise ValidationError(f"时间格式无效：{text}")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _extract_error_message(exc: Exception) -> str:
    if hasattr(exc, "messages") and exc.messages:
        return "；".join(str(m) for m in exc.messages)
    return str(exc) or "操作失败，请核对输入后重试。"


def _workbench_nav_context(user, active_tab: str) -> dict:
    can_site = can_view_site_analytics(user)
    can_curation = can_approve_curation(user)
    can_alerts = can_resolve_analytics_alerts(user)
    can_ops = can_view_analytics_menu(user)
    return {
        "active_tab": active_tab,
        "wb_nav": {
            "can_site": can_site,
            "can_curation": can_curation,
            "can_alerts": can_alerts,
            "can_rankings": bool(can_site or can_curation or can_alerts),
            "can_operations": can_ops,
        },
    }


def _resolve_admin_site(request, explicit_site_id: int | None = None):
    sites = list(Site.objects.select_related("root_page").order_by("-is_default_site", "id"))
    selected = None
    if explicit_site_id is not None:
        for s in sites:
            if int(s.id) == int(explicit_site_id):
                selected = s
                break
    if selected is None:
        req_site = Site.find_for_request(request)
        if req_site is not None:
            for s in sites:
                if int(s.id) == int(req_site.id):
                    selected = s
                    break
    if selected is None and sites:
        selected = sites[0]
    return selected, sites


def _paginate_queryset_bounded(qs, page_num: int, page_size: int = 20):
    safe_page = max(1, int(page_num or 1))
    max_jump = compute_max_jump_page(page_size)
    if safe_page > max_jump:
        raise PageWindowExceededError(safe_page, max_jump)

    probe_count = qs.order_by()[: COUNT_PROBE_LIMIT + 1].count()
    if probe_count > COUNT_PROBE_LIMIT:
        total_count = COUNT_PROBE_LIMIT
        count_kind = "lower_bound"
    else:
        total_count = probe_count
        count_kind = "exact"

    offset = (safe_page - 1) * page_size
    rows = list(qs[offset : offset + page_size + 1])
    has_next = len(rows) > page_size
    items = rows[:page_size]
    has_previous = safe_page > 1

    count_meta = build_count_meta(value=total_count, kind=count_kind)
    pagination = build_pagination_payload(
        mode="shallow",
        current_page=safe_page,
        page_size=page_size,
        total_count=total_count,
        count_kind=count_kind,
        has_next=has_next,
        has_previous=has_previous,
        next_cursor=None,
        previous_cursor=None,
    )
    return items, count_meta, pagination


def _resolve_type_labels_map() -> dict[str, str]:
    labels: dict[str, str] = {}
    for adapter in list_analytics_adapters():
        caps = getattr(adapter, "analytics_capabilities", None)
        if caps is None or caps.kind != "detail":
            continue
        tkey = adapter.type_key
        label = getattr(adapter, "model_label", "") or tkey
        if "." in label:
            try:
                model_cls = apps.get_model(label)
                label = str(model_cls._meta.verbose_name)
            except Exception:
                pass
        labels[tkey] = label
    return labels


@require_admin_access
def admin_analytics_dashboard_view(request):
    """全站数据分析大盘（仅限超级管理员或持有 `analytics.view_site_analytics` 权限者）。"""
    if not can_view_site_analytics(request.user):
        return HttpResponseForbidden("当前用户无权查看全站数据分析大盘。")

    site_id = _parse_int_param(request.GET.get("site_id"), default=None)
    if site_id is None:
        req_site = Site.find_for_request(request)
        if req_site is not None:
            site_id = req_site.id
    days = _parse_int_param(request.GET.get("days"), default=7) or 7
    type_key = (request.GET.get("type_key") or "").strip()
    include_today = _parse_bool_param(request.GET.get("include_today"), default=True)
    q = (request.GET.get("q") or "").strip()
    sort = (request.GET.get("sort") or "window_valid").strip()
    page_num = _parse_int_param(request.GET.get("p"), default=1) or 1
    cursor = (request.GET.get("cursor") or "").strip() or None

    try:
        data = get_site_dashboard_data(
            request.user,
            site_id=site_id,
            days=days,
            type_key=type_key,
            include_today=include_today,
            q=q,
            sort=sort,
            page_num=page_num,
            cursor=cursor,
        )
    except PageWindowExceededError as exc:
        return HttpResponse(
            f"请求的页码超出浅页直跳上限（最多第 {exc.max_jump_page} 页），请改用「下一页游标」或「定位文章」搜索。",
            status=422,
            content_type="text/plain; charset=utf-8",
        )
    return render(
        request,
        "analytics/admin/dashboard.html",
        {
            "dashboard": data,
            "is_author_scope": False,
            **_workbench_nav_context(request.user, "dashboard"),
        },
    )


@require_admin_access
def admin_analytics_my_pages_view(request):
    """受限作者个人数据分析页面列表（仅展示本人授权页面，绝不泄露全站或他人数据）。"""
    if not can_view_analytics_menu(request.user):
        return HttpResponseForbidden("当前用户无权访问页面数据分析。")

    days = _parse_int_param(request.GET.get("days"), default=7) or 7
    include_today = _parse_bool_param(request.GET.get("include_today"), default=True)
    q = (request.GET.get("q") or "").strip()
    page_num = _parse_int_param(request.GET.get("p"), default=1) or 1
    cursor = (request.GET.get("cursor") or "").strip() or None
    try:
        data = get_author_pages_analytics_list(
            request.user,
            days=days,
            include_today=include_today,
            q=q,
            page_num=page_num,
            cursor=cursor,
        )
    except PageWindowExceededError as exc:
        return HttpResponse(
            f"请求的页码超出浅页直跳上限（最多第 {exc.max_jump_page} 页），请改用「下一页游标」或「定位文章」搜索。",
            status=422,
            content_type="text/plain; charset=utf-8",
        )
    return render(
        request,
        "analytics/admin/dashboard.html",
        {
            "dashboard": data,
            "is_author_scope": True,
            **_workbench_nav_context(request.user, "mine"),
        },
    )


@require_admin_access
def admin_analytics_curation_view(request):
    """精选推荐审批与手工提名工作台（限超级管理员或 `analytics.approve_curatedrecommendation`）。"""
    if not can_approve_curation(request.user):
        return HttpResponseForbidden("当前用户无权访问精选推荐审批工作台。")

    raw_site_id = request.POST.get("site_id") if request.method == "POST" else request.GET.get("site_id")
    site_id = _parse_int_param(raw_site_id, default=None)
    site, sites = _resolve_admin_site(request, explicit_site_id=site_id)

    status_filter = ((request.POST.get("filter_status") if request.method == "POST" else request.GET.get("status")) or "pending").strip()
    if status_filter not in {"pending", "approved", "rejected", "withdrawn", "all"}:
        status_filter = "pending"
    position_filter = ((request.POST.get("filter_position") if request.method == "POST" else request.GET.get("position")) or "").strip()
    q = ((request.POST.get("filter_q") if request.method == "POST" else request.GET.get("q")) or "").strip()
    page_num = _parse_int_param(
        request.POST.get("filter_p") if request.method == "POST" else request.GET.get("p"),
        default=1,
    ) or 1

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        redirect_params = {}
        if site is not None:
            redirect_params["site_id"] = site.id
        if status_filter:
            redirect_params["status"] = status_filter
        if position_filter:
            redirect_params["position"] = position_filter
        if q:
            redirect_params["q"] = q
        if page_num > 1:
            redirect_params["p"] = page_num

        try:
            if action == "approve":
                rec_id = _parse_int_param(request.POST.get("recommendation_id"), default=0) or 0
                expected_version = _parse_int_param(request.POST.get("expected_version"), default=0) or 0
                priority = _parse_int_param(request.POST.get("priority"), default=0) or 0
                is_pinned = _parse_bool_param(request.POST.get("is_pinned"), default=False)
                starts_at = _parse_optional_dt(request.POST.get("starts_at"))
                expires_at = _parse_optional_dt(request.POST.get("expires_at"))
                reason = (request.POST.get("reason") or "").strip()
                rec = approve_recommendation(
                    rec_id,
                    request.user,
                    expected_version=expected_version,
                    priority=priority,
                    is_pinned=is_pinned,
                    starts_at=starts_at,
                    expires_at=expires_at,
                    reason=reason,
                )
                messages.success(request, f"已批准《{rec.page.title}》入选精选（v{rec.version}）。")
            elif action == "toggle_pin":
                rec_id = _parse_int_param(request.POST.get("recommendation_id"), default=0) or 0
                expected_version = _parse_int_param(request.POST.get("expected_version"), default=0) or 0
                target_pinned = _parse_bool_param(request.POST.get("target_pinned"), default=True)
                cur_rec = CuratedRecommendation.objects.filter(id=rec_id).first()
                rec = approve_recommendation(
                    rec_id,
                    request.user,
                    expected_version=expected_version,
                    priority=cur_rec.priority if cur_rec else 0,
                    is_pinned=target_pinned,
                    starts_at=cur_rec.starts_at if cur_rec else None,
                    expires_at=cur_rec.expires_at if cur_rec else None,
                    reason=(request.POST.get("reason") or "").strip(),
                )
                messages.success(
                    request,
                    f"已{'置顶' if rec.is_pinned else '取消置顶'}精选条目 #{rec.id}（v{rec.version}）。",
                )
            elif action == "reject":
                rec_id = _parse_int_param(request.POST.get("recommendation_id"), default=0) or 0
                expected_version = _parse_int_param(request.POST.get("expected_version"), default=0) or 0
                cooldown_days = _parse_int_param(request.POST.get("cooldown_days"), default=14) or 14
                reason = (request.POST.get("reason") or "").strip()
                rec = reject_recommendation(
                    rec_id,
                    request.user,
                    expected_version=expected_version,
                    reason=reason,
                    cooldown_days=cooldown_days,
                )
                messages.success(
                    request,
                    f"已将精选条目 #{rec.id} 设为暂不入选（{cooldown_days} 天冷却期，v{rec.version}）。",
                )
            elif action == "withdraw":
                rec_id = _parse_int_param(request.POST.get("recommendation_id"), default=0) or 0
                expected_version = _parse_int_param(request.POST.get("expected_version"), default=0) or 0
                reason = (request.POST.get("reason") or "").strip()
                rec = withdraw_recommendation(
                    rec_id,
                    request.user,
                    expected_version=expected_version,
                    reason=reason,
                )
                messages.success(request, f"已撤回精选条目 #{rec.id}（v{rec.version}）。")
            elif action == "nominate":
                nom_site_id = _parse_int_param(request.POST.get("site_id"), default=site.id if site else None)
                nom_page_id = _parse_int_param(request.POST.get("page_id"), default=None)
                position = (request.POST.get("position") or "home_carousel").strip()
                column_page_id = _parse_int_param(request.POST.get("column_page_id"), default=None)
                priority = _parse_int_param(request.POST.get("priority"), default=0) or 0
                is_pinned = _parse_bool_param(request.POST.get("is_pinned"), default=False)
                auto_approve = _parse_bool_param(request.POST.get("auto_approve"), default=False)
                starts_at = _parse_optional_dt(request.POST.get("starts_at"))
                expires_at = _parse_optional_dt(request.POST.get("expires_at"))
                reason = (request.POST.get("reason") or "").strip()
                if not nom_site_id or not nom_page_id:
                    raise ValidationError("请提供有效的站点与目标页面 Page ID。")
                rec = nominate_recommendation(
                    nom_site_id,
                    nom_page_id,
                    position,
                    request.user,
                    column_page_id=column_page_id,
                    priority=priority,
                    is_pinned=is_pinned,
                    starts_at=starts_at,
                    expires_at=expires_at,
                    reason=reason,
                    auto_approve=auto_approve,
                )
                messages.success(
                    request,
                    f"已完成 Page #{nom_page_id} 手工提名（状态：{rec.get_status_display()}，v{rec.version}）。",
                )
            else:
                messages.error(request, "不支持的精选操作类型。")
        except (ConcurrencyConflictError, ValidationError, PermissionDenied, ValueError, TypeError) as exc:
            messages.error(request, _extract_error_message(exc))

        base_url = reverse("analytics_admin_curation")
        if redirect_params:
            return redirect(f"{base_url}?{urlencode(redirect_params)}")
        return redirect(base_url)

    status_counts = {"pending": 0, "approved": 0, "rejected": 0, "withdrawn": 0, "all": 0}
    rows_data = []
    count_meta = build_count_meta(value=0, kind="exact")
    pagination = build_pagination_payload(
        mode="shallow",
        current_page=1,
        page_size=20,
        total_count=0,
        count_kind="exact",
        has_next=False,
        has_previous=False,
        next_cursor=None,
        previous_cursor=None,
    )

    if site is not None:
        for sc in (
            CuratedRecommendation.objects.filter(site_id=site.id)
            .values("status")
            .annotate(cnt=Count("id"))
        ):
            st = sc["status"]
            cnt = int(sc["cnt"] or 0)
            if st in status_counts:
                status_counts[st] = cnt
            status_counts["all"] += cnt

        qs = CuratedRecommendation.objects.filter(site_id=site.id).select_related("page", "site")
        if status_filter != "all":
            qs = qs.filter(status=status_filter)
        if position_filter:
            qs = qs.filter(position=position_filter)
        if q:
            if q.isdigit():
                qs = qs.filter(Q(page_id=int(q)) | Q(page__title__icontains=q))
            else:
                qs = qs.filter(page__title__icontains=q)

        qs = qs.order_by("-is_pinned", "-priority", "-quality_score", "-updated_at", "-id")
        try:
            page_items, count_meta, pagination = _paginate_queryset_bounded(qs, page_num, page_size=20)
        except PageWindowExceededError as exc:
            return HttpResponse(
                f"请求的页码超出浅页直跳上限（最多第 {exc.max_jump_page} 页），请使用关键词或状态筛选定位。",
                status=422,
                content_type="text/plain; charset=utf-8",
            )

        rec_ids = [item.id for item in page_items]
        actions_by_rec: dict[int, list] = {rid: [] for rid in rec_ids}
        if rec_ids:
            for act in (
                CuratedRecommendationAction.objects.filter(recommendation_id__in=rec_ids)
                .select_related("actor")
                .order_by("-created_at")
            ):
                bucket = actions_by_rec.get(act.recommendation_id)
                if bucket is not None and len(bucket) < 3:
                    bucket.append(act)

        now = timezone.now()
        for rec in page_items:
            ev = rec.evidence_json if isinstance(rec.evidence_json, dict) else {}
            comp_rate = ev.get("completion_rate")
            cov_rate = ev.get("coverage_rate")
            rep = ev.get("reputation") if isinstance(ev.get("reputation"), dict) else {}
            q_bd = ev.get("quality_breakdown") if isinstance(ev.get("quality_breakdown"), dict) else {}
            cooldown_active = bool(
                rec.rejected_cooldown_until is not None and rec.rejected_cooldown_until > now
            )
            rows_data.append(
                {
                    "rec": rec,
                    "completion_rate_pct": round(float(comp_rate) * 100.0, 1) if comp_rate is not None else None,
                    "coverage_rate_pct": round(float(cov_rate) * 100.0, 1) if cov_rate is not None else None,
                    "visitor_days": ev.get("visitor_days"),
                    "pv": ev.get("pv"),
                    "comment_count": ev.get("comment_count"),
                    "r_trusted": rep.get("r_trusted"),
                    "n_trusted": rep.get("n_trusted"),
                    "manual_nomination": bool(ev.get("manual_nomination")),
                    "breakdown": q_bd,
                    "cooldown_active": cooldown_active,
                    "recent_actions": actions_by_rec.get(rec.id, []),
                }
            )

    return render(
        request,
        "analytics/admin/curation.html",
        {
            "site": site,
            "sites": sites,
            "rows": rows_data,
            "status_counts": status_counts,
            "position_choices": CURATION_POSITION_CHOICES,
            "status_choices": CURATION_STATUS_CHOICES,
            "filters": {
                "site_id": site.id if site else None,
                "status": status_filter,
                "position": position_filter,
                "q": q,
            },
            "count_meta": count_meta,
            "pagination": pagination,
            **_workbench_nav_context(request.user, "curation"),
        },
    )


@require_admin_access
def admin_analytics_alerts_view(request):
    """低口碑与高浏览低完读降噪预警工作台（限超级管理员或 `analytics.resolve_analyticsalert`）。"""
    if not can_resolve_analytics_alerts(request.user):
        return HttpResponseForbidden("当前用户无权访问降噪预警工作台。")

    raw_site_id = request.POST.get("site_id") if request.method == "POST" else request.GET.get("site_id")
    site_id = _parse_int_param(raw_site_id, default=None)
    site, sites = _resolve_admin_site(request, explicit_site_id=site_id)

    status_filter = ((request.POST.get("filter_status") if request.method == "POST" else request.GET.get("status")) or "open").strip()
    allowed_statuses = {"open", "observing", "active", "exempted", "confirmed_demoted", "resolved", "all"}
    if status_filter not in allowed_statuses:
        status_filter = "open"
    kind_filter = ((request.POST.get("filter_kind") if request.method == "POST" else request.GET.get("alert_kind")) or "").strip()
    q = ((request.POST.get("filter_q") if request.method == "POST" else request.GET.get("q")) or "").strip()
    page_num = _parse_int_param(
        request.POST.get("filter_p") if request.method == "POST" else request.GET.get("p"),
        default=1,
    ) or 1

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        redirect_params = {}
        if site is not None:
            redirect_params["site_id"] = site.id
        if status_filter:
            redirect_params["status"] = status_filter
        if kind_filter:
            redirect_params["alert_kind"] = kind_filter
        if q:
            redirect_params["q"] = q
        if page_num > 1:
            redirect_params["p"] = page_num

        try:
            if action == "exempt":
                alert_id = _parse_int_param(request.POST.get("alert_id"), default=0) or 0
                expected_version = _parse_int_param(request.POST.get("expected_version"), default=None)
                days = _parse_int_param(request.POST.get("days"), default=7) or 7
                reason = (request.POST.get("reason") or "").strip()
                alert = exempt_alert(
                    alert_id,
                    request.user,
                    expected_version=expected_version,
                    reason=reason,
                    days=days,
                )
                messages.success(request, f"已为告警 #{alert.id} 设置 {days} 天限时豁免。")
            elif action == "confirm_demote":
                alert_id = _parse_int_param(request.POST.get("alert_id"), default=0) or 0
                expected_version = _parse_int_param(request.POST.get("expected_version"), default=None)
                reason = (request.POST.get("reason") or "").strip()
                alert = confirm_demote_alert(
                    alert_id,
                    request.user,
                    expected_version=expected_version,
                    reason=reason,
                )
                messages.success(request, f"已确认对告警 #{alert.id} 执行降权下榜。")
            else:
                messages.error(request, "不支持的告警处理操作。")
        except (ConcurrencyConflictError, ValidationError, PermissionDenied, ValueError, TypeError) as exc:
            messages.error(request, _extract_error_message(exc))

        base_url = reverse("analytics_admin_alerts")
        if redirect_params:
            return redirect(f"{base_url}?{urlencode(redirect_params)}")
        return redirect(base_url)

    status_counts = {
        "open": 0,
        "observing": 0,
        "active": 0,
        "exempted": 0,
        "confirmed_demoted": 0,
        "resolved": 0,
        "all": 0,
    }
    rows_data = []
    count_meta = build_count_meta(value=0, kind="exact")
    pagination = build_pagination_payload(
        mode="shallow",
        current_page=1,
        page_size=20,
        total_count=0,
        count_kind="exact",
        has_next=False,
        has_previous=False,
        next_cursor=None,
        previous_cursor=None,
    )

    if site is not None:
        for sc in (
            AnalyticsAlert.objects.filter(site_id=site.id)
            .values("status")
            .annotate(cnt=Count("id"))
        ):
            st = sc["status"]
            cnt = int(sc["cnt"] or 0)
            if st in status_counts:
                status_counts[st] = cnt
            if st in {"observing", "active", "exempted", "confirmed_demoted"}:
                status_counts["open"] += cnt
            status_counts["all"] += cnt

        qs = AnalyticsAlert.objects.filter(site_id=site.id).select_related("page", "site")
        if status_filter == "open":
            qs = qs.filter(status__in=("observing", "active", "exempted", "confirmed_demoted"))
        elif status_filter != "all":
            qs = qs.filter(status=status_filter)
        if kind_filter:
            qs = qs.filter(alert_kind=kind_filter)
        if q:
            if q.isdigit():
                qs = qs.filter(Q(page_id=int(q)) | Q(page__title__icontains=q))
            else:
                qs = qs.filter(page__title__icontains=q)

        qs = qs.order_by("-updated_at", "-id")
        try:
            page_items, count_meta, pagination = _paginate_queryset_bounded(qs, page_num, page_size=20)
        except PageWindowExceededError as exc:
            return HttpResponse(
                f"请求的页码超出浅页直跳上限（最多第 {exc.max_jump_page} 页），请使用关键词或状态筛选定位。",
                status=422,
                content_type="text/plain; charset=utf-8",
            )

        for alert in page_items:
            ev = alert.evidence_json if isinstance(alert.evidence_json, dict) else {}
            alert_version = int(getattr(alert, "version", None) or ev.get("_version") or 1)
            comp_rate = ev.get("completion_rate")
            bounce_rate = ev.get("short_bounce_rate")
            rows_data.append(
                {
                    "alert": alert,
                    "expected_version": alert_version,
                    "can_exempt": alert.alert_kind != "TRAFFIC_ANOMALY" and alert.status != "resolved",
                    "can_demote": alert.status not in {"confirmed_demoted", "resolved"},
                    "organic_pv_30d": ev.get("organic_pv_30d"),
                    "high_pv_threshold": ev.get("high_pv_threshold"),
                    "measurable_views_30d": ev.get("measurable_views_30d"),
                    "completion_rate_pct": round(float(comp_rate) * 100.0, 1) if comp_rate is not None else None,
                    "short_bounce_rate_pct": round(float(bounce_rate) * 100.0, 1) if bounce_rate is not None else None,
                    "n_trusted": ev.get("n_trusted"),
                    "d_trusted": ev.get("d_trusted"),
                    "r_trusted": ev.get("r_trusted"),
                    "wilson_lower": ev.get("wilson_lower"),
                    "exempt_reason": ev.get("exempt_reason") or "",
                    "demote_reason": ev.get("demote_reason") or "",
                }
            )

    return render(
        request,
        "analytics/admin/alerts.html",
        {
            "site": site,
            "sites": sites,
            "rows": rows_data,
            "status_counts": status_counts,
            "alert_kind_choices": ALERT_KIND_CHOICES,
            "alert_status_choices": ALERT_STATUS_CHOICES,
            "filters": {
                "site_id": site.id if site else None,
                "status": status_filter,
                "alert_kind": kind_filter,
                "q": q,
            },
            "count_meta": count_meta,
            "pagination": pagination,
            **_workbench_nav_context(request.user, "alerts"),
        },
    )


@require_admin_access
def admin_analytics_rankings_view(request):
    """全站榜单与降权系数预览工作台（展示原分、降权系数与最终分）。"""
    if not (
        can_view_site_analytics(request.user)
        or can_approve_curation(request.user)
        or can_resolve_analytics_alerts(request.user)
    ):
        return HttpResponseForbidden("当前用户无权访问榜单预览工作台。")

    site_id = _parse_int_param(request.GET.get("site_id"), default=None)
    site, sites = _resolve_admin_site(request, explicit_site_id=site_id)

    board_kind = (request.GET.get("board_kind") or "popular_7d").strip()
    valid_boards = {code for code, _ in RANKING_BOARD_KIND_CHOICES}
    if board_kind not in valid_boards:
        board_kind = "popular_7d"

    type_labels = _resolve_type_labels_map()
    if site is not None:
        snap_types = (
            RankingSnapshot.objects.filter(site_id=site.id)
            .values_list("type_key", flat=True)
            .distinct()
        )
        for tk in snap_types:
            if tk and tk != "all" and tk not in type_labels:
                type_labels[tk] = tk

    available_types = [
        {"type_key": tk, "label": label}
        for tk, label in sorted(type_labels.items(), key=lambda x: x[0])
    ]

    requested_type = (request.GET.get("type_key") or "").strip()
    if board_kind == "reputation":
        # 深度干货口碑榜不跨类型混排：未指定或选 all 时优先切到首个可用内容类型
        if not requested_type or requested_type == "all":
            type_key = available_types[0]["type_key"] if available_types else "all"
        else:
            type_key = requested_type
    else:
        type_key = requested_type or "all"

    requested_date = parse_date((request.GET.get("stat_date") or "").strip())

    snapshot = None
    recent_dates: list[date] = []
    items_data = []
    if site is not None:
        snap_qs = RankingSnapshot.objects.filter(
            site_id=site.id,
            board_kind=board_kind,
            type_key=type_key,
        ).order_by("-stat_date")
        recent_dates = list(snap_qs.values_list("stat_date", flat=True)[:14])
        if requested_date is not None:
            snapshot = snap_qs.filter(stat_date=requested_date).first()
        if snapshot is None:
            snapshot = snap_qs.first()

        if snapshot is not None and isinstance(snapshot.items_json, list):
            for raw_item in snapshot.items_json:
                if not isinstance(raw_item, dict):
                    continue
                factor = float(raw_item.get("factor", 1.0) or 1.0)
                comp_rate = raw_item.get("completion_rate")
                bounce_rate = raw_item.get("short_bounce_rate")
                item_tkey = str(raw_item.get("type_key") or "")
                items_data.append(
                    {
                        **raw_item,
                        "type_label": type_labels.get(item_tkey, item_tkey or "内容"),
                        "factor_rounded": round(factor, 2),
                        "is_penalized": factor < 0.999,
                        "completion_rate_pct": round(float(comp_rate) * 100.0, 1) if comp_rate is not None else None,
                        "short_bounce_rate_pct": round(float(bounce_rate) * 100.0, 1) if bounce_rate is not None else None,
                    }
                )

    return render(
        request,
        "analytics/admin/rankings.html",
        {
            "site": site,
            "sites": sites,
            "board_choices": RANKING_BOARD_KIND_CHOICES,
            "available_types": available_types,
            "recent_dates": recent_dates,
            "snapshot": snapshot,
            "items": items_data,
            "filters": {
                "site_id": site.id if site else None,
                "board_kind": board_kind,
                "type_key": type_key,
                "stat_date": snapshot.stat_date.isoformat() if snapshot else (requested_date.isoformat() if requested_date else ""),
            },
            **_workbench_nav_context(request.user, "rankings"),
        },
    )


_FULL_PAGE_DRAWER_TEMPLATE = """{% extends "wagtailadmin/base.html" %}
{% load static %}
{% block titletag %}{{ detail.page.title }} - 单篇数据分析{% endblock %}
{% block extra_css %}
{{ block.super }}
<link rel="stylesheet" href="{% static 'css/admin-workbench.css' %}?v=20261123c">
<link rel="stylesheet" href="{% static 'analytics/css/analytics-admin.css' %}?v=20261123b">
{% endblock %}
{% block content %}
{% include "wagtailadmin/shared/header.html" with title=detail.page.title subtitle="单篇数据分析与阅读漏斗" icon="site" %}
<div class="nice-padding">
    {% include "analytics/admin/page_drawer.html" %}
</div>
{% endblock %}
"""


@require_admin_access
def admin_analytics_page_drawer_view(request, page_id: int):
    """单篇页面数据分析详情 / 抽屉视图（支持全页渲染与 `?partial=1` 嵌入抽屉）。"""
    page = get_object_or_404(Page, id=page_id).specific
    if not can_view_page_analytics(request.user, page):
        return HttpResponseForbidden("当前用户无权查看该页面的数据分析。")

    detail = get_page_analytics_detail(request.user, page)
    is_partial = (
        request.GET.get("partial") in ("1", "true", "yes")
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )
    context = {
        "detail": detail,
        "is_partial": is_partial,
        "can_view_site": can_view_site_analytics(request.user),
    }
    if is_partial:
        return render(request, "analytics/admin/page_drawer.html", context)
    tpl = engines["django"].from_string(_FULL_PAGE_DRAWER_TEMPLATE)
    return HttpResponse(tpl.render(context, request))


@require_admin_access
def admin_analytics_operations_view(request):
    """第 3 期作者产能、工作流/评论审核效率、治理汇总与技术栈热度工作台。"""
    if not can_view_analytics_menu(request.user):
        return HttpResponseForbidden("当前用户无权访问运营与审核工作台。")

    from analytics.operations import get_operations_dashboard_data

    site_id = _parse_int_param(request.GET.get("site_id"), default=None)
    days = _parse_int_param(request.GET.get("days"), default=30) or 30
    author_q = (request.GET.get("author_q") or "").strip()
    page_num = _parse_int_param(request.GET.get("p"), default=1) or 1

    try:
        data = get_operations_dashboard_data(
            request.user,
            site_id=site_id,
            days=days,
            author_q=author_q,
            page_num=page_num,
        )
    except PageWindowExceededError as exc:
        return HttpResponse(
            f"请求的页码超出浅页直跳上限（最多第 {exc.max_jump_page} 页），请使用作者关键词筛选定位。",
            status=422,
            content_type="text/plain; charset=utf-8",
        )

    return render(
        request,
        "analytics/admin/operations.html",
        {
            "ops": data,
            **_workbench_nav_context(request.user, "operations"),
        },
    )


@require_admin_access
def admin_analytics_export_view(request):
    """第 3 期受控 CSV 导出入口（权限过滤、5,000 行上限、防公式注入、防小样本反推与审计留痕）。"""
    if request.method not in ("GET", "POST"):
        return HttpResponse(status=405)
    from analytics.exports import export_analytics_csv

    params = request.POST if request.method == "POST" else request.GET
    export_kind = (params.get("kind") or "page_summary").strip()
    site_id = _parse_int_param(params.get("site_id"), default=None)
    site, _ = _resolve_admin_site(request, explicit_site_id=site_id)
    filters = {
        "site_id": site.id if site else None,
        "days": _parse_int_param(params.get("days"), default=30) or 30,
        "type_key": (params.get("type_key") or "").strip(),
        "board_kind": (params.get("board_kind") or "popular_7d").strip(),
        "include_today": _parse_bool_param(params.get("include_today"), default=True),
        "q": (params.get("q") or "").strip(),
        "author_q": (params.get("author_q") or "").strip(),
        "sort": (params.get("sort") or "window_valid").strip(),
    }
    try:
        return export_analytics_csv(
            request.user,
            export_kind=export_kind,
            site=site,
            filters=filters,
        )
    except PermissionDenied as exc:
        return HttpResponseForbidden(str(exc) or "无权导出该报表。")
    except ValidationError as exc:
        return HttpResponse(
            _extract_error_message(exc),
            status=400,
            content_type="text/plain; charset=utf-8",
        )


class AnalyticsMenuItem(MenuItem):
    """按权限动态控制显示与目标入口的后台菜单项。"""

    def is_shown(self, request):
        return can_view_analytics_menu(getattr(request, "user", None))

    def render_component(self, request):
        user = getattr(request, "user", None)
        if can_view_site_analytics(user):
            target_url = reverse("analytics_admin_dashboard")
        elif can_approve_curation(user):
            target_url = reverse("analytics_admin_curation")
        elif can_resolve_analytics_alerts(user):
            target_url = reverse("analytics_admin_alerts")
        else:
            target_url = reverse("analytics_admin_my_pages")
        return LinkMenuItemComponent(
            self.name,
            self.label,
            target_url,
            icon_name=self.icon_name,
            classname=self.classname,
            attrs=self.attrs,
        )


@hooks.register("register_permissions")
def register_analytics_permissions():
    """向 Wagtail 角色管理暴露数据分析独立权限（不依赖 is_staff）。"""
    return Permission.objects.filter(
        content_type__app_label="analytics",
        codename__in=[
            "view_site_analytics",
            "view_page_analytics",
            "approve_curatedrecommendation",
            "resolve_analyticsalert",
        ],
    )


@hooks.register("register_admin_urls")
def register_analytics_admin_urls():
    return [
        path(
            "analytics/",
            admin_analytics_dashboard_view,
            name="analytics_admin_dashboard",
        ),
        path(
            "analytics/mine/",
            admin_analytics_my_pages_view,
            name="analytics_admin_my_pages",
        ),
        path(
            "analytics/curation/",
            admin_analytics_curation_view,
            name="analytics_admin_curation",
        ),
        path(
            "analytics/alerts/",
            admin_analytics_alerts_view,
            name="analytics_admin_alerts",
        ),
        path(
            "analytics/rankings/",
            admin_analytics_rankings_view,
            name="analytics_admin_rankings",
        ),
        path(
            "analytics/operations/",
            admin_analytics_operations_view,
            name="analytics_admin_operations",
        ),
        path(
            "analytics/export/",
            admin_analytics_export_view,
            name="analytics_admin_export",
        ),
        path(
            "analytics/page/<int:page_id>/",
            admin_analytics_page_drawer_view,
            name="analytics_admin_page_drawer",
        ),
    ]


@hooks.register("register_admin_menu_item")
def register_analytics_menu_item():
    return AnalyticsMenuItem(
        "数据分析",
        reverse_lazy("analytics_admin_dashboard"),
        name="analytics",
        icon_name="site",
        order=245,
    )


@hooks.register("register_page_listing_more_buttons")
def page_listing_analytics_button(page, user, next_url=None):
    """在 Wagtail 页面列表为已发布或作者有权的内容页追加「数据分析」入口按钮。"""
    if getattr(page, "depth", 0) <= 1:
        return
    is_published_or_owned = bool(
        getattr(page, "live", False)
        or getattr(page, "first_published_at", None) is not None
        or getattr(page, "owner_id", None) == getattr(user, "pk", None)
    )
    if not is_published_or_owned:
        return
    if not can_view_page_analytics(user, page):
        return
    yield ListingButton(
        "数据分析",
        url=reverse("analytics_admin_page_drawer", args=[page.id]),
        icon_name="site",
        priority=28,
        attrs={"title": f"查看《{page.title}》的数据分析"},
    )
