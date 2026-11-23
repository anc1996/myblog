"""
analytics.exports
第 3 期受控 CSV 导出服务（权限过滤、5,000 行上限、公式注入防护、防小样本反推、审计留痕）。
"""

from __future__ import annotations

import csv
import io
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.utils import timezone
from wagtail.models import Site

from analytics.models import AnalyticsExportAudit, AnalyticsSettings, RankingSnapshot
from analytics.operations import get_operations_dashboard_data
from analytics.permissions import (
    can_approve_curation,
    can_resolve_analytics_alerts,
    can_view_analytics_menu,
    can_view_site_analytics,
)
from analytics.services import get_author_pages_analytics_list, get_site_dashboard_data

MAX_EXPORT_ROWS = 5000
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_ALLOWED_EXPORT_KINDS = frozenset({
    "page_summary",
    "ranking_snapshot",
    "author_capacity",
    "review_efficiency",
})


def sanitize_csv_cell(value: Any) -> str:
    """防止 Excel / Calc CSV 公式注入，统一剥离换行并转义危险前缀。"""
    if value is None:
        return ""
    text = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _log_export_audit(
    *,
    actor,
    site: Site | None,
    export_kind: str,
    filters: dict[str, Any],
    row_count: int,
    status: str,
    reason: str = "",
) -> None:
    # 严禁将原始访客键或未经白名单的搜索词写入审计，这里仅保存脱敏参数摘要
    safe_filters = {
        k: v
        for k, v in (filters or {}).items()
        if k in {"site_id", "days", "type_key", "board_kind", "include_today", "sort", "stat_date"} and v not in (None, "")
    }
    AnalyticsExportAudit.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        site=site,
        export_kind=export_kind,
        filters_json=safe_filters,
        row_count=max(0, int(row_count)),
        status=status,
        reason=(reason or "")[:250],
    )


def export_analytics_csv(
    user,
    *,
    export_kind: str,
    site: Site | None = None,
    filters: dict[str, Any] | None = None,
) -> HttpResponse:
    """
    生成受控 CSV 响应并写入 `AnalyticsExportAudit`。
    - 权限不足抛出 `PermissionDenied` 并记录拒绝审计；
    - 超过 `MAX_EXPORT_ROWS` 抛出 `ValidationError` 并记录拒绝审计；
    - 绝不导出访客键、IP、UA 或原始搜索词；四档评价低于最小样本阈值时不输出比例防反推。
    """
    filters = dict(filters or {})
    kind = str(export_kind or "").strip()
    if kind not in _ALLOWED_EXPORT_KINDS:
        raise ValidationError(f"不支持的导出报表类型：{kind}")

    site_id = site.id if site is not None else filters.get("site_id")
    if site is None and site_id:
        site = Site.objects.filter(id=int(site_id)).first()
    if site is None:
        site = Site.objects.filter(is_default_site=True).first() or Site.objects.order_by("id").first()

    # 权限矩阵校验
    if kind == "page_summary":
        if not can_view_analytics_menu(user):
            _log_export_audit(actor=user, site=site, export_kind=kind, filters=filters, row_count=0, status="rejected", reason="permission_denied")
            raise PermissionDenied("无权导出页面分析明细。")
    elif kind == "ranking_snapshot":
        if not (can_view_site_analytics(user) or can_approve_curation(user) or can_resolve_analytics_alerts(user)):
            _log_export_audit(actor=user, site=site, export_kind=kind, filters=filters, row_count=0, status="rejected", reason="permission_denied")
            raise PermissionDenied("无权导出全站榜单快照。")
    elif kind in ("author_capacity", "review_efficiency"):
        if not can_view_site_analytics(user):
            _log_export_audit(actor=user, site=site, export_kind=kind, filters=filters, row_count=0, status="rejected", reason="permission_denied")
            raise PermissionDenied("仅全站数据分析权限可导出运营与审核报表。")

    headers: list[str] = []
    rows: list[list[Any]] = []

    if kind == "page_summary":
        days = int(filters.get("days") or 7)
        q = str(filters.get("q") or "").strip()
        type_key = str(filters.get("type_key") or "").strip()
        include_today = bool(filters.get("include_today", True))
        sort = str(filters.get("sort") or "window_valid")
        headers = [
            "Page ID", "标题", "类型", "状态", "窗口PV", "窗口访客日",
            "窗口有效阅读", "窗口完读", "累计PV", "累计访客日", "URL",
        ]
        # 按页拉取（上限 5000 行），避免全站一次拖库
        per_page = 100
        page_idx = 1
        while True:
            if can_view_site_analytics(user):
                res = get_site_dashboard_data(
                    user,
                    site_id=site.id if site else None,
                    days=days,
                    type_key=type_key,
                    include_today=include_today,
                    q=q,
                    sort=sort,
                    page_num=page_idx,
                    per_page=per_page,
                )
            else:
                res = get_author_pages_analytics_list(
                    user,
                    days=days,
                    include_today=include_today,
                    q=q,
                    page_num=page_idx,
                    per_page=per_page,
                )
            batch = res.get("top_pages") or []
            for item in batch:
                rows.append([
                    item.get("page_id"),
                    item.get("title"),
                    item.get("type_label"),
                    "回收站" if item.get("is_recycled") else ("已发布" if item.get("live") else "草稿"),
                    item.get("window_pv"),
                    item.get("window_visitor_days"),
                    item.get("window_valid_views"),
                    item.get("window_completed_views"),
                    item.get("total_pv"),
                    item.get("total_visitor_days"),
                    item.get("url_path"),
                ])
                if len(rows) > MAX_EXPORT_ROWS:
                    _log_export_audit(
                        actor=user, site=site, export_kind=kind, filters=filters,
                        row_count=len(rows), status="rejected", reason="max_rows_exceeded",
                    )
                    raise ValidationError(f"导出行数超过单次上限（最多 {MAX_EXPORT_ROWS} 行），请缩小搜索或类型筛选范围。")
            if not res.get("pages_pagination", {}).get("has_next") or page_idx >= 50:
                break
            page_idx += 1

    elif kind == "ranking_snapshot":
        board_kind = str(filters.get("board_kind") or "popular_7d")
        type_key = str(filters.get("type_key") or "all")
        snap = (
            RankingSnapshot.objects.filter(site=site, board_kind=board_kind, type_key=type_key)
            .order_by("-stat_date")
            .first()
        )
        headers = ["排名", "Page ID", "标题", "类型", "原分", "降权系数", "最终分", "有效样本", "状态"]
        if snap and isinstance(snap.items_json, list):
            for it in snap.items_json[:MAX_EXPORT_ROWS]:
                if not isinstance(it, dict):
                    continue
                rows.append([
                    it.get("rank"),
                    it.get("page_id"),
                    it.get("title"),
                    it.get("type_key"),
                    it.get("raw_score"),
                    it.get("factor"),
                    it.get("final_score"),
                    it.get("sample_size"),
                    "观察中" if it.get("is_observing") else "正式入榜",
                ])

    elif kind == "author_capacity":
        days = int(filters.get("days") or 30)
        author_q = str(filters.get("author_q") or "").strip()
        settings_obj = AnalyticsSettings.get_for_site_readonly(site)
        min_votes = max(1, int(getattr(settings_obj, "min_rating_votes", 20) or 20))
        headers = [
            "Person ID", "姓名", "已关联后台账号", "作品数", "角色分布",
            "窗口首发", "窗口再发布", "90日未更新", "逾期指南",
            "篇均PV", "篇均访客日", "加权完读率%", "精选数", "好评率%(防反推门槛保护)",
        ]
        page_idx = 1
        while True:
            op_data = get_operations_dashboard_data(
                user,
                site_id=site.id if site else None,
                days=days,
                author_q=author_q,
                page_num=page_idx,
                per_page=50,
            )
            authors_block = op_data["authors"]
            for ar in authors_block["rows"]:
                # 防小样本反推：总票数不足阈值时只输出保护标记，绝不泄露具体好评率
                safe_fav = ar["favorable_pct"] if (ar["favorable_pct"] is not None and ar["rating_votes"] >= min_votes) else ar["rating_note"]
                rows.append([
                    ar["person"].id,
                    ar["person"].name,
                    "是" if ar["is_linked_account"] else "否",
                    ar["page_count"],
                    ar["role_summary"],
                    ar["first_published_win"],
                    ar["republish_win"],
                    ar["stale_90d_count"],
                    ar["overdue_guides"],
                    ar["avg_pv"],
                    ar["avg_visitor_days"],
                    ar["completion_pct"] if ar["completion_pct"] is not None else "-",
                    ar["curated_count"],
                    safe_fav,
                ])
                if len(rows) > MAX_EXPORT_ROWS:
                    _log_export_audit(
                        actor=user, site=site, export_kind=kind, filters=filters,
                        row_count=len(rows), status="rejected", reason="max_rows_exceeded",
                    )
                    raise ValidationError(f"导出行数超过单次上限（最多 {MAX_EXPORT_ROWS} 行）。")
            if not authors_block["pagination"].get("has_next") or page_idx >= 100:
                break
            page_idx += 1

    elif kind == "review_efficiency":
        days = int(filters.get("days") or 30)
        op_data = get_operations_dashboard_data(
            user,
            site_id=site.id if site else None,
            days=days,
        )
        rg = op_data.get("review_governance") or {}
        wf = rg.get("workflow") or {}
        cm = rg.get("comments") or {}
        rc = (rg.get("governance") or {}).get("recycle") or {}
        headers = [
            "审核域", "待审积压", "小于24h", "1至3天", "大于3天",
            "终态样本", "平均耗时(h)", "中位数(h)", "P90(h)", "超时(>72h)", "驳回率%",
        ]
        for label, d in (("页面工作流", wf), ("读者评论", cm)):
            bl = d.get("backlog") or {}
            tn = d.get("turnaround") or {}
            rows.append([
                label,
                bl.get("total", 0),
                bl.get("lt_24h", 0),
                bl.get("d1_to_3", 0),
                bl.get("gt_3d", 0),
                tn.get("sample_count", 0),
                tn.get("mean_hours") if tn.get("mean_hours") is not None else "-",
                tn.get("median_hours") if tn.get("median_hours") is not None else "-",
                tn.get("p90_hours") if tn.get("p90_hours") is not None else "-",
                tn.get("timeout_count", 0),
                d.get("rejection_rate_pct") if d.get("rejection_rate_pct") is not None else "-",
            ])
        rows.append([
            "回收站治理批次",
            rc.get("recycled", 0),
            "-", "-", "-",
            rc.get("restored", 0) + rc.get("purged", 0),
            "-", "-", "-", "-",
            f"恢复:{rc.get('restored', 0)}/销毁:{rc.get('purged', 0)}",
        ])

    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM 方便 Excel 正确识别中文
    writer = csv.writer(buf)
    writer.writerow([sanitize_csv_cell(h) for h in headers])
    for row in rows:
        writer.writerow([sanitize_csv_cell(col) for col in row])

    _log_export_audit(
        actor=user,
        site=site,
        export_kind=kind,
        filters=filters,
        row_count=len(rows),
        status="completed",
    )

    stamp = timezone.localdate().strftime("%Y%m%d")
    resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="myblog_{kind}_{stamp}.csv"'
    resp["X-Content-Type-Options"] = "nosniff"
    return resp
