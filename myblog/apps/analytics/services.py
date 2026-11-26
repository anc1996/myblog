"""
analytics.services
全站数据分析大盘与单篇页面明细查询聚合服务（只读统计，绝不反向修改页面状态）。

核心口径与安全保证：
1. 严格权限隔离：所有入口先校验 `can_view_site_analytics` 或 `can_view_page_analytics`，
   页面列表强制经过 `filter_accessible_pages_for_analytics` 过滤；
2. 区分缺测、未覆盖、当日暂估与真实 0：绝不将 `missing / partial / uncovered` 缺测日
   当成 0 计入完读率、有效阅读率或均值的分母；
3. 明确区分“访客日（跨日每日 UV 之和）”与“单日站点/单页 UV”，绝不将多日或多页 UV
   混算为跨日独立访客；同一页面同日多正式版本（`live_revision_id`）按单日去重最大口径合并 UV；
4. 零具体页面模型硬编码：类型名称与能力声明全部通过 `discovery.registry` 动态解析。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import re
from typing import Any

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone
from wagtail.models import Page, Site

from analytics.models import (
    AnalyticsSettings,
    PageAnalyticsDaily,
    PageAnalyticsTotal,
    ScopeAnalyticsDaily,
)
from analytics.permissions import (
    _is_page_recycled,
    can_view_analytics_menu,
    can_view_page_analytics,
    can_view_site_analytics,
    filter_accessible_pages_for_analytics,
)
from content_lifecycle.public_access import get_public_blocked_page_ids
from discovery.pagination import (
    COUNT_PROBE_LIMIT,
    PageWindowExceededError,
    build_count_meta,
    build_pagination_payload,
    compute_max_jump_page,
    try_summary_count_from_archive_daily,
)
from discovery.registry import (
    get_analytics_capabilities,
    get_entry_analytics,
    get_type_adapter,
    list_analytics_adapters,
)

_HEALTH_PRIORITY = {"normal": 1, "partial": 2, "missing": 3}
_STATUS_LABELS = {
    "normal": "正常",
    "partial": "部分缺测",
    "missing": "缺测区间",
    "uncovered": "未覆盖",
}
_SOURCE_LABELS = {
    "home": "站内首页",
    "listing": "站内栏目/列表",
    "search": "站内搜索",
    "tag": "全站标签",
    "author": "作者专栏",
    "curated": "精选推荐位",
    "ranking": "站内榜单",
    "external_search": "外部搜索引擎",
    "external_social": "外部社交媒体",
    "external_other": "其他外链站点",
    "direct": "直接访问 / 未知来源",
    "unknown": "直接访问 / 未知来源",
}


def _resolve_site(site_id: int | None = None) -> Site | None:
    if site_id is not None:
        site = Site.objects.filter(id=site_id).first()
        if site is not None:
            return site
    return Site.objects.filter(is_default_site=True).first() or Site.objects.order_by("id").first()


def _load_settings_snapshot(site: Site | None) -> dict[str, Any]:
    defaults = {
        "collection_enabled": True,
        "public_pv_display_enabled": False,
        "rating_enabled": False,
        "ranking_enabled": False,
        "min_rating_votes": 20,
        "observe_rating_votes": 5,
        "high_pv_min": 300,
        "min_healthy_days_in_30d": 25,
    }
    if site is None:
        return defaults
    try:
        settings_obj = AnalyticsSettings.get_for_site_readonly(site)
        return {
            "collection_enabled": bool(getattr(settings_obj, "collection_enabled", True)),
            "public_pv_display_enabled": bool(getattr(settings_obj, "public_pv_display_enabled", False)),
            "rating_enabled": bool(getattr(settings_obj, "rating_enabled", False)),
            "ranking_enabled": bool(getattr(settings_obj, "ranking_enabled", False)),
            "min_rating_votes": int(getattr(settings_obj, "min_rating_votes", 20) or 20),
            "observe_rating_votes": int(getattr(settings_obj, "observe_rating_votes", 5) or 5),
            "high_pv_min": int(getattr(settings_obj, "high_pv_min", 300) or 300),
            "min_healthy_days_in_30d": int(getattr(settings_obj, "min_healthy_days_in_30d", 25) or 25),
        }
    except Exception:
        return defaults


def _resolve_type_label(type_key: str, *, page: Page | None = None) -> str:
    if type_key:
        adapter = get_type_adapter(type_key)
        if adapter is not None:
            model_label = getattr(adapter, "model_label", "")
            if model_label and "." in model_label:
                try:
                    model_cls = apps.get_model(model_label)
                    return str(model_cls._meta.verbose_name)
                except Exception:
                    pass
            return model_label or type_key
    if page is not None:
        try:
            return str(page.specific_class._meta.verbose_name)
        except Exception:
            pass
    return type_key or "内容页面"


def _resolve_type_key_for_page(page: Page) -> str:
    spec_cls = getattr(page, "specific_class", None) or type(page)
    model_label = f"{spec_cls._meta.app_label}.{spec_cls.__name__}"
    for adapter in list_analytics_adapters():
        if getattr(adapter, "model_label", "").lower() == model_label.lower():
            return getattr(adapter, "type_key", "")
    return getattr(spec_cls._meta, "model_name", "")


def _resolve_detail_content_type_ids(type_key: str = "") -> list[int]:
    ct_ids: list[int] = []
    for adapter in list_analytics_adapters():
        caps = getattr(adapter, "analytics_capabilities", None)
        if not caps or caps.kind != "detail":
            continue
        if type_key and getattr(adapter, "type_key", "") != type_key:
            continue
        model_label = getattr(adapter, "model_label", "")
        if "." in model_label:
            app_label, model_name = model_label.split(".", 1)
            ct = ContentType.objects.filter(app_label=app_label, model=model_name.lower()).first()
            if ct is not None:
                ct_ids.append(ct.id)
    return ct_ids


def _apply_page_search_filter(queryset, q: str, type_key: str = ""):
    ct_ids = _resolve_detail_content_type_ids(type_key)
    if ct_ids:
        queryset = queryset.filter(content_type_id__in=ct_ids)
    clean_q = (q or "").strip()
    if clean_q:
        cond = Q(title__icontains=clean_q) | Q(url_path__icontains=clean_q) | Q(slug__icontains=clean_q)
        if clean_q.isdigit():
            cond |= Q(id=int(clean_q))
        queryset = queryset.filter(cond)
    return queryset


def _resolve_entry_label(entry_kind: str) -> str:
    base_kind = entry_kind.split(":", 1)[0]
    spec = get_entry_analytics(base_kind)
    if spec is not None:
        labels = {
            "home": "网站首页",
            "author_detail": "作者专栏",
            "search": "站内搜索",
            "tag_index": "全站标签",
        }
        if base_kind in labels:
            return labels[base_kind]
        return getattr(spec, "model_label", "") or entry_kind
    for adapter in list_analytics_adapters():
        caps = getattr(adapter, "analytics_capabilities", None)
        if caps and getattr(caps, "entry_kind", "") == base_kind:
            return _resolve_type_label(getattr(adapter, "type_key", ""))
    fallback = {
        "home": "网站首页",
        "blog_index": "博客栏目",
        "guide_index": "专题指南栏目",
        "portfolio_index": "开源作品栏目",
        "tag_index": "全站标签页",
        "author_detail": "作者专栏",
        "search": "站内搜索页",
    }
    return fallback.get(base_kind, entry_kind)


def _safe_rate(numerator: int | float, denominator: int | float) -> float | None:
    if denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 4)


def _safe_pct(rate: float | None) -> float | None:
    if rate is None:
        return None
    return round(rate * 100.0, 2)


def _merge_health(current: str | None, incoming: str | None) -> str:
    cur = current if current in _HEALTH_PRIORITY else "normal"
    inc = incoming if incoming in _HEALTH_PRIORITY else "normal"
    return cur if _HEALTH_PRIORITY[cur] >= _HEALTH_PRIORITY[inc] else inc


def _build_coverage_warning(
    *,
    total_window_days: int,
    healthy_days: int,
    partial_days: list[str],
    missing_days: list[str],
    uncovered_days: list[str],
    include_today: bool,
    today_in_window: bool,
    min_healthy_days_in_30d: int = 25,
) -> dict[str, Any]:
    messages: list[str] = []
    if missing_days:
        messages.append(
            f"窗口内存在 {len(missing_days)} 个缺测日（{', '.join(missing_days[:5])}"
            f"{' 等' if len(missing_days) > 5 else ''}），已标记为缺测区间，未作为 0 计入比率分母。"
        )
    if partial_days:
        messages.append(
            f"窗口内存在 {len(partial_days)} 个部分缺测日（{', '.join(partial_days[:5])}"
            f"{' 等' if len(partial_days) > 5 else ''}），建议按健康日口径复核趋势。"
        )
    if uncovered_days:
        messages.append(
            f"窗口内有 {len(uncovered_days)} 天尚未覆盖采集数据（未纳入健康日分母）。"
        )
    if include_today and today_in_window:
        messages.append("今日数据尚未完成日终封账，当前显示为当日待结算 / 暂估口径。")
    if total_window_days >= 30 and healthy_days < min_healthy_days_in_30d:
        messages.append(
            f"近 30 日健康日共 {healthy_days} 天，低于站点阈值 {min_healthy_days_in_30d} 天，精选/排行评估需留意缺测影响。"
        )

    return {
        "total_window_days": total_window_days,
        "healthy_days": healthy_days,
        "partial_days": partial_days,
        "missing_days": missing_days,
        "uncovered_days": uncovered_days,
        "partial_count": len(partial_days),
        "missing_count": len(missing_days),
        "uncovered_count": len(uncovered_days),
        "has_warning": bool(messages),
        "messages": messages,
    }


def _extract_conversions_funnel_and_sources(conversions_json: Any) -> tuple[dict[str, int | None], dict[str, int]]:
    """
    从 `PageAnalyticsDaily.conversions_json` 安全提取阅读漏斗与来源分桶（兼容多种紧凑键名）。
    未采集到的细分层级保持为 None，不伪造为 0。
    """
    funnel: dict[str, int | None] = {
        "scroll_25": None,
        "scroll_50": None,
        "scroll_90": None,
        "end_reached": None,
        "short_body": None,
    }
    sources: dict[str, int] = {}
    if not isinstance(conversions_json, dict):
        return funnel, sources

    funnel_src = conversions_json.get("funnel") if isinstance(conversions_json.get("funnel"), dict) else conversions_json
    key_aliases = {
        "scroll_25": ("scroll_25", "scroll_25_views", "pct_25"),
        "scroll_50": ("scroll_50", "scroll_50_views", "pct_50"),
        "scroll_90": ("scroll_90", "scroll_90_views", "pct_90"),
        "end_reached": ("end_reached", "end_reached_views", "reached_end"),
        "short_body": ("short_body", "short_body_views", "is_short_body"),
    }
    for target_key, aliases in key_aliases.items():
        for alias in aliases:
            if alias in funnel_src and funnel_src[alias] is not None:
                try:
                    funnel[target_key] = max(0, int(funnel_src[alias]))
                except (TypeError, ValueError):
                    pass
                break

    raw_sources = conversions_json.get("sources") or conversions_json.get("source_counts")
    if isinstance(raw_sources, dict):
        for k, v in raw_sources.items():
            try:
                val = int(v)
            except (TypeError, ValueError):
                continue
            if val >= 0:
                sources[str(k)] = val
    for k, v in conversions_json.items():
        if isinstance(k, str) and k.startswith("src_"):
            try:
                val = int(v)
            except (TypeError, ValueError):
                continue
            if val >= 0:
                ch_key = k[4:]
                sources[ch_key] = sources.get(ch_key, 0) + val
    return funnel, sources


_TYPE_CONVERSION_KEYS = (
    "code_copy",
    "mermaid_view",
    "step_reach",
    "step_complete",
    "doc_attempt",
    "doc_download",
    "repo_click",
    "demo_click",
)


def _extract_type_conversion_counts(conversions_json: Any) -> dict[str, int]:
    """提取第 3 期各类型专属转化计数（非负整数）。"""
    counts: dict[str, int] = {}
    if not isinstance(conversions_json, dict):
        return counts
    for key in _TYPE_CONVERSION_KEYS:
        raw = conversions_json.get(key)
        if raw is None:
            continue
        try:
            val = int(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            counts[key] = val
    return counts


def _build_page_type_conversions_summary(
    specific_page: Any,
    type_key: str,
    conv_totals: dict[str, int],
    measurable_30d: int,
    today: date,
) -> dict[str, Any]:
    """按页面类型构建第 3 期专属转化与元信息（Blog/Guide/Project）。"""
    has_body = hasattr(specific_page, "body")
    visible_chars = 0
    step_count = 0
    code_blocks = 0
    mermaid_blocks = 0
    doc_blocks = 0
    if has_body:
        try:
            if hasattr(specific_page, "hydrate_from_mongo") and (
                not getattr(specific_page, "body", None) or len(getattr(specific_page, "body", [])) == 0
            ):
                specific_page.hydrate_from_mongo()
        except Exception:
            pass
        body_val = getattr(specific_page, "body", None) or []
        for blk in body_val:
            btype = getattr(blk, "block_type", "")
            bval = getattr(blk, "value", None)
            if btype == "step":
                step_count += 1
            elif btype == "code":
                code_blocks += 1
            elif btype == "mermaid":
                mermaid_blocks += 1
            elif btype == "document":
                doc_blocks += 1
            elif btype == "attachment_list" and isinstance(bval, dict):
                doc_blocks += len(bval.get("items") or [])
            if btype in ("markdown", "rich_text", "paragraph", "step", "lead_in"):
                txt = ""
                if btype == "markdown":
                    txt = str(bval or "")
                    txt = re.sub(r"```[\s\S]*?```", " ", txt)
                elif btype == "rich_text":
                    txt = re.sub(r"<[^>]+>", " ", str(getattr(bval, "source", bval) or ""))
                elif isinstance(bval, dict):
                    txt = re.sub(r"<[^>]+>", " ", str(bval.get("title") or "") + " " + str(bval.get("content") or bval.get("overview") or ""))
                visible_chars += len(re.sub(r"\s+", "", txt))

    result: dict[str, Any] = {
        "type_key": type_key,
        "visible_chars": visible_chars,
        "is_long_article": visible_chars >= 1500,
        "items": [],
        "meta": {},
    }
    if type_key in ("blog_page", "blog"):
        cc = conv_totals.get("code_copy", 0)
        mv = conv_totals.get("mermaid_view", 0)
        result["items"] = [
            {"key": "code_copy", "label": "代码复制次数", "count": cc, "rate_pct": _safe_pct(_safe_rate(cc, measurable_30d))},
            {"key": "mermaid_view", "label": "Mermaid 查看次数", "count": mv, "rate_pct": _safe_pct(_safe_rate(mv, measurable_30d))},
        ]
        result["meta"] = {
            "long_article_threshold": 1500,
            "code_blocks": code_blocks,
            "mermaid_blocks": mermaid_blocks,
            "char_rule_version": "v1_no_code",
        }
    elif type_key in ("guide", "guide_page"):
        sr = conv_totals.get("step_reach", 0)
        sc = conv_totals.get("step_complete", 0)
        da = conv_totals.get("doc_attempt", 0)
        dd = conv_totals.get("doc_download", 0)
        step_completion_pct = (
            round((sc / float(measurable_30d * step_count)) * 100.0, 1)
            if measurable_30d > 0 and step_count > 0
            else None
        )
        rev_at = getattr(specific_page, "last_reviewed_at", None)
        if rev_at is None:
            freshness_status = "missing"
            freshness_label = "缺失复核日期"
            days_since_review = None
        else:
            rev_d = rev_at.date() if isinstance(rev_at, datetime) else rev_at
            days_since_review = max(0, (today - rev_d).days)
            if days_since_review >= 365:
                freshness_status = "overdue"
                freshness_label = f"已逾期（{days_since_review} 天未复核）"
            elif days_since_review >= 335:
                freshness_status = "expiring_soon"
                freshness_label = f"临期提醒（{days_since_review} 天前复核）"
            else:
                freshness_status = "healthy"
                freshness_label = f"时效健康（{days_since_review} 天前复核）"
        result["items"] = [
            {"key": "step_reach", "label": "步骤触达数", "count": sr, "rate_pct": None},
            {"key": "step_complete", "label": "显式完成此步数", "count": sc, "rate_pct": step_completion_pct},
            {"key": "doc_attempt", "label": "课件点击尝试", "count": da, "rate_pct": _safe_pct(_safe_rate(da, measurable_30d))},
            {"key": "doc_download", "label": "网关授权成功下载（合并 Range）", "count": dd, "rate_pct": _safe_pct(_safe_rate(dd, da if da > 0 else measurable_30d))},
        ]
        result["meta"] = {
            "step_count": step_count,
            "doc_blocks": doc_blocks,
            "freshness_status": freshness_status,
            "freshness_label": freshness_label,
            "days_since_review": days_since_review,
        }
    elif type_key in ("project", "project_page"):
        rc = conv_totals.get("repo_click", 0)
        dc = conv_totals.get("demo_click", 0)
        has_repo = bool(str(getattr(specific_page, "repo_url", "") or "").strip())
        has_demo = bool(str(getattr(specific_page, "demo_url", "") or "").strip())
        result["items"] = [
            {"key": "repo_click", "label": "代码仓库点击", "count": rc, "rate_pct": _safe_pct(_safe_rate(rc, measurable_30d)) if has_repo else None},
            {"key": "demo_click", "label": "在线演示点击", "count": dc, "rate_pct": _safe_pct(_safe_rate(dc, measurable_30d)) if has_demo else None},
        ]
        result["meta"] = {
            "primary_language": str(getattr(specific_page, "primary_language", "") or ""),
            "has_repo": has_repo,
            "has_demo": has_demo,
            "star_count_display_only": getattr(specific_page, "star_count", 0),
        }
    return result


def _aggregate_page_daily_rows(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """
    将 `PageAnalyticsDaily` 原始明细按 `(page_id, stat_date)` 先做同日多版本合并（UV 取单日 Max，
    PV 与互动指标按版本累加；`missing` 行不混入健康分母），再聚合为单页汇总。
    """
    by_page_date: dict[tuple[int, date], dict[str, Any]] = {}
    for row in rows:
        pid = row["page_id"]
        sdate = row["stat_date"]
        health = row.get("health_status") or "normal"
        key = (pid, sdate)
        slot = by_page_date.setdefault(
            key,
            {
                "page_id": pid,
                "stat_date": sdate,
                "type_key": row.get("type_key") or "",
                "pv": 0,
                "daily_uv": 0,
                "measurable_views": 0,
                "valid_views": 0,
                "completed_views": 0,
                "short_bounce_views": 0,
                "total_active_seconds": 0,
                "organic_pv": 0,
                "health_status": health,
            },
        )
        if row.get("type_key"):
            slot["type_key"] = row["type_key"]
        slot["health_status"] = _merge_health(slot["health_status"], health)
        if health == "missing":
            continue
        slot["pv"] += int(row.get("pv") or 0)
        slot["daily_uv"] = max(slot["daily_uv"], int(row.get("daily_uv") or 0))
        slot["measurable_views"] += int(row.get("measurable_views") or 0)
        slot["valid_views"] += int(row.get("valid_views") or 0)
        slot["completed_views"] += int(row.get("completed_views") or 0)
        slot["short_bounce_views"] += int(row.get("short_bounce_views") or 0)
        slot["total_active_seconds"] += int(row.get("total_active_seconds") or 0)
        slot["organic_pv"] += int(row.get("organic_pv") or 0)

    by_page: dict[int, dict[str, Any]] = {}
    for day_item in by_page_date.values():
        pid = day_item["page_id"]
        agg = by_page.setdefault(
            pid,
            {
                "page_id": pid,
                "type_key": day_item["type_key"],
                "pv": 0,
                "visitor_days": 0,
                "measurable_views": 0,
                "valid_views": 0,
                "completed_views": 0,
                "short_bounce_views": 0,
                "total_active_seconds": 0,
                "organic_pv": 0,
                "healthy_days": 0,
                "missing_days": 0,
                "partial_days": 0,
            },
        )
        if day_item["type_key"]:
            agg["type_key"] = day_item["type_key"]
        health = day_item["health_status"]
        if health == "missing":
            agg["missing_days"] += 1
            continue
        if health == "partial":
            agg["partial_days"] += 1
        else:
            agg["healthy_days"] += 1
        agg["pv"] += day_item["pv"]
        agg["visitor_days"] += day_item["daily_uv"]
        agg["measurable_views"] += day_item["measurable_views"]
        agg["valid_views"] += day_item["valid_views"]
        agg["completed_views"] += day_item["completed_views"]
        agg["short_bounce_views"] += day_item["short_bounce_views"]
        agg["total_active_seconds"] += day_item["total_active_seconds"]
        agg["organic_pv"] += day_item["organic_pv"]
    return by_page


def _parse_safe_search_terms(scope_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    解析 D5 安全热门搜索词与无结果词（仅展示命中安全词表的规范词条，未命中词条仅保留总计）。
    """
    hot_map: dict[str, dict[str, Any]] = {}
    zero_map: dict[str, dict[str, Any]] = {}
    unlisted_count = 0

    for row in scope_rows:
        if (row.get("health_status") or "normal") == "missing":
            continue
        raw_key = str(row.get("scope_key") or "").strip()
        pv = int(row.get("pv") or 0)
        daily_uv = int(row.get("daily_uv") or 0)
        clicks = int(row.get("entry_clicks") or 0)
        if not raw_key:
            continue

        lowered = raw_key.lower()
        if lowered in {"unknown", "__unknown__", "unlisted", "other"} or lowered.startswith("unknown:"):
            unlisted_count += pv
            continue

        is_zero = False
        term = raw_key
        for prefix in ("zero:", "no_result:", "empty:"):
            if lowered.startswith(prefix):
                is_zero = True
                term = raw_key[len(prefix):].strip()
                break
        if not is_zero:
            for prefix in ("hit:", "term:"):
                if lowered.startswith(prefix):
                    term = raw_key[len(prefix):].strip()
                    break
        if not term:
            unlisted_count += pv
            continue

        target = zero_map if is_zero else hot_map
        entry = target.setdefault(
            term,
            {"term": term, "pv": 0, "visitor_days": 0, "clicks": 0},
        )
        entry["pv"] += pv
        entry["visitor_days"] += daily_uv
        entry["clicks"] += clicks

    hot_terms = sorted(hot_map.values(), key=lambda x: (-x["pv"], -x["clicks"], x["term"]))[:20]
    zero_result_terms = sorted(zero_map.values(), key=lambda x: (-x["pv"], x["term"]))[:20]
    return {
        "hot_terms": hot_terms,
        "zero_result_terms": zero_result_terms,
        "unlisted_count": unlisted_count,
    }


_CURSOR_SALT = "myblog.analytics.dashboard.cursor.v1"
_CURSOR_MAX_AGE = 12 * 3600


def _encode_analytics_cursor(payload: dict[str, Any]) -> str:
    return signing.dumps(payload, salt=_CURSOR_SALT, compress=True)


def _decode_analytics_cursor(token: str | None, expected_sort: str) -> dict[str, Any] | None:
    if not token or not isinstance(token, str) or len(token) > 1024:
        return None
    try:
        data = signing.loads(token, salt=_CURSOR_SALT, max_age=_CURSOR_MAX_AGE)
    except signing.BadSignature:
        return None
    if not isinstance(data, dict) or data.get("sort") != expected_sort:
        return None
    if data.get("dir") not in ("next", "prev"):
        return None
    return data


def _bounded_count(qs, *, site_id: int | None = None, type_key: str = "", allow_archive_summary: bool = False) -> tuple[int, str]:
    """
    千万级安全有界计数：最多探测 COUNT_PROBE_LIMIT + 1 条；
    超出上限时优先尝试 ArchiveDailyCount 预聚合汇总，否则返回 lower_bound 下限。
    """
    probe = len(list(qs.order_by().values_list("pk", flat=True)[: COUNT_PROBE_LIMIT + 1]))
    if probe <= COUNT_PROBE_LIMIT:
        return probe, "exact"
    if allow_archive_summary:
        tkeys = [type_key] if type_key else []
        summary = try_summary_count_from_archive_daily(
            site_id=site_id,
            type_keys=tkeys,
            date_from=None,
            date_to=None,
        )
        if summary is not None and summary >= probe:
            return summary, "summary"
    return COUNT_PROBE_LIMIT, "lower_bound"


def _build_tuple_lt_q(fields_and_vals: list[tuple[str, Any]]) -> Q:
    """构造字典序 (a, b, c, ...) < (va, vb, vc, ...) 的等价 SQL Q 条件，用于 Keyset 深翻。"""
    overall = Q()
    prefix_eq = Q()
    for fname, val in fields_and_vals:
        overall |= prefix_eq & Q(**{f"{fname}__lt": val})
        prefix_eq &= Q(**{fname: val})
    return overall


def _build_tuple_gt_q(fields_and_vals: list[tuple[str, Any]]) -> Q:
    """构造字典序 (a, b, c, ...) > (va, vb, vc, ...) 的等价 SQL Q 条件，用于向上回翻游标。"""
    overall = Q()
    prefix_eq = Q()
    for fname, val in fields_and_vals:
        overall |= prefix_eq & Q(**{f"{fname}__gt": val})
        prefix_eq &= Q(**{fname: val})
    return overall


def _hydrate_page_rows_by_slice(
    *,
    site: Site,
    accessible_pages_qs,
    slice_ids: list[int],
    start_date: date,
    end_date: date,
    today: date,
    precomputed_window: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """仅针对当前页 <= 20 个 page_id 批量拉取 Page 对象、窗口汇总、今日 PV 与历史累计总表。"""
    if not slice_ids:
        return []

    pages_by_id = {
        p.id: p
        for p in accessible_pages_qs.filter(id__in=slice_ids).specific()
    }
    totals_by_page = {
        row["page_id"]: row
        for row in PageAnalyticsTotal.objects.filter(
            site=site, page_id__in=slice_ids
        ).values("page_id", "total_pv", "total_visitor_days", "last_settled_date")
    }
    today_pv_map = {
        row["page_id"]: int(row["pv"] or 0)
        for row in PageAnalyticsDaily.objects.filter(
            site=site, page_id__in=slice_ids, stat_date=today
        )
        .exclude(health_status="missing")
        .values("page_id")
        .annotate(pv=Sum("pv"))
    }

    window_map = dict(precomputed_window or {})
    missing_win_ids = [pid for pid in slice_ids if pid not in window_map]
    if missing_win_ids:
        daily_slice_rows = list(
            PageAnalyticsDaily.objects.filter(
                site=site,
                page_id__in=missing_win_ids,
                stat_date__gte=start_date,
                stat_date__lte=end_date,
            ).values(
                "page_id",
                "stat_date",
                "type_key",
                "pv",
                "daily_uv",
                "measurable_views",
                "valid_views",
                "completed_views",
                "short_bounce_views",
                "total_active_seconds",
                "organic_pv",
                "health_status",
            )
        )
        window_map.update(_aggregate_page_daily_rows(daily_slice_rows))

    items: list[dict[str, Any]] = []
    for pid in slice_ids:
        page_obj = pages_by_id.get(pid)
        if page_obj is None:
            continue
        item = window_map.get(pid, {})
        tot = totals_by_page.get(pid, {})
        tk = item.get("type_key") or _resolve_type_key_for_page(page_obj)
        measurable = int(item.get("measurable_views") or 0)
        valid = int(item.get("valid_views") or 0)
        completed = int(item.get("completed_views") or 0)
        short_bounce = int(item.get("short_bounce_views") or 0)
        active_sec = int(item.get("total_active_seconds") or 0)
        win_pv = int(item.get("pv") or 0)
        vis_days = int(item.get("visitor_days") or 0)
        v_rate = _safe_rate(valid, measurable)
        c_rate = _safe_rate(completed, measurable)
        b_rate = _safe_rate(short_bounce, measurable)
        avg_sec = round(active_sec / measurable, 1) if measurable > 0 else None
        items.append(
            {
                "page_id": pid,
                "title": page_obj.title,
                "url_path": getattr(page_obj, "url_path", "") or "",
                "type_key": tk,
                "type_label": _resolve_type_label(tk, page=page_obj),
                "live": bool(getattr(page_obj, "live", False)),
                "is_recycled": _is_page_recycled(page_obj),
                "today_pv": today_pv_map.get(pid, 0),
                "window_pv": win_pv,
                "visitor_days": vis_days,
                "total_pv": int(tot.get("total_pv") or win_pv),
                "total_visitor_days": int(tot.get("total_visitor_days") or vis_days),
                "measurable_views": measurable,
                "valid_views": valid,
                "completed_views": completed,
                "valid_read_rate_pct": _safe_pct(v_rate),
                "completion_rate_pct": _safe_pct(c_rate),
                "short_bounce_rate_pct": _safe_pct(b_rate),
                "avg_active_seconds": avg_sec,
                "last_settled_date": tot.get("last_settled_date"),
            }
        )
    return items


def get_site_dashboard_data(
    user,
    *,
    site_id: int | None = None,
    days: int = 7,
    type_key: str = "",
    include_today: bool = True,
    q: str = "",
    sort: str = "window_valid",
    page_num: int = 1,
    cursor: str | None = None,
    per_page: int = 20,
) -> dict[str, Any]:
    """
    获取全站数据分析大盘聚合数据。
    仅超级用户或具备 `analytics.view_site_analytics` 权限的用户可调用。
    """
    if not can_view_site_analytics(user):
        raise PermissionDenied("当前用户无权查看全站数据分析大盘。")

    site = _resolve_site(site_id)
    settings_snapshot = _load_settings_snapshot(site)
    days = max(1, min(int(days or 7), 365))
    type_key = (type_key or "").strip()

    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    end_date = today if include_today else yesterday
    start_date = end_date - timedelta(days=days - 1)
    query_start = min(start_date, yesterday)
    query_end = max(end_date, today)

    all_sites = list(Site.objects.order_by("id").values("id", "site_name", "hostname", "is_default_site"))
    available_types = [
        {
            "type_key": getattr(adapter, "type_key", ""),
            "label": _resolve_type_label(getattr(adapter, "type_key", "")),
        }
        for adapter in list_analytics_adapters()
        if getattr(adapter, "analytics_capabilities", None)
        and adapter.analytics_capabilities.kind == "detail"
    ]

    q = (q or "").strip()[:120]
    sort = (sort or "window_valid").strip().lower()
    if sort not in ("window_valid", "total_pv", "latest"):
        sort = "window_valid"
    page_num = max(1, int(page_num or 1))
    per_page = max(1, min(int(per_page or 20), 100))
    max_jump_page = compute_max_jump_page(per_page)
    if not cursor and page_num > max_jump_page:
        raise PageWindowExceededError(page_num, max_jump_page)
    if site is None:
        return {
            "site": None,
            "sites": all_sites,
            "available_types": available_types,
            "filters": {
                "site_id": None,
                "days": days,
                "type_key": type_key,
                "include_today": include_today,
                "q": q,
                "sort": sort,
                "start_date": start_date,
                "end_date": end_date,
            },
            "settings": settings_snapshot,
            "summary": {},
            "trend_series": [],
            "type_breakdown": [],
            "entry_stats": [],
            "safe_search_terms": {"hot_terms": [], "zero_result_terms": [], "unlisted_count": 0, "zero_result_total": 0},
            "coverage_warning": _build_coverage_warning(
                total_window_days=days,
                healthy_days=0,
                partial_days=[],
                missing_days=[],
                uncovered_days=[],
                include_today=include_today,
                today_in_window=include_today,
                min_healthy_days_in_30d=settings_snapshot["min_healthy_days_in_30d"],
            ),
            "top_pages": [],
            "count_meta": build_count_meta(value=0, kind="exact"),
            "pages_pagination": build_pagination_payload(
                mode="shallow_page",
                current_page=1,
                page_size=per_page,
                total_count=0,
                count_kind="exact",
                has_next=False,
                has_previous=False,
                next_cursor=None,
                previous_cursor=None,
            ),
        }

    # 1. 查询范围统计表（站级或按内容类型）
    scope_kind_primary = "type" if type_key else "site"
    scope_qs = ScopeAnalyticsDaily.objects.filter(
        site=site,
        scope_kind=scope_kind_primary,
        stat_date__gte=query_start,
        stat_date__lte=query_end,
    )
    if type_key:
        scope_qs = scope_qs.filter(scope_key=type_key)

    scope_by_date: dict[date, dict[str, Any]] = {}
    for row in scope_qs.values("stat_date", "pv", "daily_uv", "health_status"):
        sdate = row["stat_date"]
        health = row.get("health_status") or "normal"
        existing = scope_by_date.get(sdate)
        if existing is None:
            scope_by_date[sdate] = {
                "pv": int(row.get("pv") or 0),
                "daily_uv": int(row.get("daily_uv") or 0),
                "health_status": health,
            }
        else:
            existing["pv"] = max(existing["pv"], int(row.get("pv") or 0))
            existing["daily_uv"] = max(existing["daily_uv"], int(row.get("daily_uv") or 0))
            existing["health_status"] = _merge_health(existing["health_status"], health)

    # 2. 权限过滤下推至 SQL 子查询（绝不把全站页面 ID 拉入 Python 内存）
    accessible_pages_qs = filter_accessible_pages_for_analytics(user, Page.objects.all())
    page_daily_qs = PageAnalyticsDaily.objects.filter(
        site=site,
        page__in=accessible_pages_qs,
        stat_date__gte=query_start,
        stat_date__lte=query_end,
    )
    if type_key:
        page_daily_qs = page_daily_qs.filter(type_key=type_key)

    # 如果站点范围表尚未写入某些日期，仅将详情汇总作 PV 兜底参考（绝不把跨页 UV 相加冒充站点 UV）
    page_pv_by_date: dict[date, dict[str, Any]] = {}
    for row in (
        page_daily_qs.values("stat_date")
        .annotate(
            pv=Sum("pv", filter=~Q(health_status="missing")),
            missing_cnt=Count("id", filter=Q(health_status="missing")),
            partial_cnt=Count("id", filter=Q(health_status="partial")),
            normal_cnt=Count("id", filter=Q(health_status="normal")),
        )
    ):
        sdate = row["stat_date"]
        if int(row.get("missing_cnt") or 0) > 0:
            health = "missing"
        elif int(row.get("partial_cnt") or 0) > 0:
            health = "partial"
        else:
            health = "normal"
        page_pv_by_date[sdate] = {"pv": int(row.get("pv") or 0), "health_status": health}

    # 3. 构造窗口逐日趋势与健康日覆盖统计
    trend_series: list[dict[str, Any]] = []
    healthy_days = 0
    partial_days: list[str] = []
    missing_days: list[str] = []
    uncovered_days: list[str] = []
    window_pv_sum = 0
    window_covered_pv_days = 0
    window_visitor_days_sum = 0
    window_covered_uv_days = 0

    for offset in range(days):
        cur_date = start_date + timedelta(days=offset)
        date_str = cur_date.isoformat()
        is_provisional = cur_date == today
        scope_row = scope_by_date.get(cur_date)
        fallback_row = page_pv_by_date.get(cur_date)

        if scope_row is not None:
            health = scope_row["health_status"]
            if health == "missing":
                missing_days.append(date_str)
                day_pv = None
                day_uv = None
            else:
                if health == "partial":
                    partial_days.append(date_str)
                elif not is_provisional:
                    healthy_days += 1
                day_pv = scope_row["pv"]
                day_uv = scope_row["daily_uv"]
                window_pv_sum += day_pv
                window_covered_pv_days += 1
                window_visitor_days_sum += day_uv
                window_covered_uv_days += 1
        elif fallback_row is not None:
            health = fallback_row["health_status"]
            if health == "missing":
                missing_days.append(date_str)
                day_pv = None
                day_uv = None
            else:
                if health == "partial":
                    partial_days.append(date_str)
                elif not is_provisional:
                    healthy_days += 1
                day_pv = fallback_row["pv"]
                day_uv = None
                window_pv_sum += day_pv
                window_covered_pv_days += 1
        else:
            health = "uncovered"
            uncovered_days.append(date_str)
            day_pv = None
            day_uv = None

        status_label = "当日暂估" if (is_provisional and health in ("normal", "partial")) else _STATUS_LABELS.get(health, health)
        trend_series.append(
            {
                "date": date_str,
                "pv": day_pv,
                "daily_uv": day_uv,
                "health_status": health,
                "status_label": status_label,
                "is_provisional": is_provisional,
            }
        )

    max_pv_in_trend = max((item["pv"] or 0 for item in trend_series), default=0)
    for item in trend_series:
        item["pv_bar_pct"] = (
            round((item["pv"] / max_pv_in_trend) * 100, 1)
            if (item["pv"] is not None and max_pv_in_trend > 0)
            else 0
        )

    # 今日 / 昨日 PV 与站点 UV
    def _pick_day_metric(target_date: date) -> tuple[int | None, int | None, str]:
        sr = scope_by_date.get(target_date)
        if sr is not None:
            if sr["health_status"] == "missing":
                return None, None, "missing"
            return sr["pv"], sr["daily_uv"], sr["health_status"]
        fr = page_pv_by_date.get(target_date)
        if fr is not None:
            if fr["health_status"] == "missing":
                return None, None, "missing"
            return fr["pv"], None, fr["health_status"]
        return None, None, "uncovered"

    today_pv, site_today_uv, today_health = _pick_day_metric(today)
    yesterday_pv, site_yesterday_uv, yesterday_health = _pick_day_metric(yesterday)

    # 4. 窗口内页面级阅读质量（SQL 单次聚合，绝不在 Python 循环千万级明细）
    win_daily_qs = page_daily_qs.filter(stat_date__gte=start_date, stat_date__lte=end_date).exclude(
        health_status="missing"
    )
    win_agg = win_daily_qs.aggregate(
        pv=Sum("pv"),
        measurable=Sum("measurable_views"),
        valid=Sum("valid_views"),
        completed=Sum("completed_views"),
        short_bounce=Sum("short_bounce_views"),
        active_sec=Sum("total_active_seconds"),
    )
    total_measurable = int(win_agg.get("measurable") or 0)
    total_valid = int(win_agg.get("valid") or 0)
    total_completed = int(win_agg.get("completed") or 0)
    total_short_bounce = int(win_agg.get("short_bounce") or 0)
    total_active_sec = int(win_agg.get("active_sec") or 0)
    total_detail_pv = int(win_agg.get("pv") or 0)

    valid_read_rate = _safe_rate(total_valid, total_measurable)
    completion_rate = _safe_rate(total_completed, total_measurable)
    short_bounce_rate = _safe_rate(total_short_bounce, total_measurable)
    avg_active_seconds = (
        round(total_active_sec / total_measurable, 1) if total_measurable > 0 else None
    )
    coverage_rate = _safe_rate(total_measurable, total_detail_pv)

    summary = {
        "today_pv": today_pv,
        "today_health": today_health,
        "yesterday_pv": yesterday_pv,
        "yesterday_health": yesterday_health,
        "window_pv": window_pv_sum if window_covered_pv_days > 0 else None,
        "visitor_days": window_visitor_days_sum if window_covered_uv_days > 0 else None,
        "site_today_uv": site_today_uv,
        "site_yesterday_uv": site_yesterday_uv,
        "measurable_views": total_measurable,
        "valid_read_rate": valid_read_rate,
        "valid_read_rate_pct": _safe_pct(valid_read_rate),
        "completion_rate": completion_rate,
        "completion_rate_pct": _safe_pct(completion_rate),
        "avg_active_seconds": avg_active_seconds,
        "short_bounce_rate": short_bounce_rate,
        "short_bounce_rate_pct": _safe_pct(short_bounce_rate),
        "coverage_rate": coverage_rate,
        "coverage_rate_pct": _safe_pct(coverage_rate),
    }

    # 5. 按内容类型 PV 占比
    type_scope_rows = list(
        ScopeAnalyticsDaily.objects.filter(
            site=site,
            scope_kind="type",
            stat_date__gte=start_date,
            stat_date__lte=end_date,
        )
        .exclude(health_status="missing")
        .values("scope_key")
        .annotate(pv=Sum("pv"), visitor_days=Sum("daily_uv"))
    )
    type_map: dict[str, dict[str, Any]] = {}
    if type_scope_rows:
        for row in type_scope_rows:
            tk = row["scope_key"]
            type_map[tk] = {
                "type_key": tk,
                "label": _resolve_type_label(tk),
                "pv": int(row["pv"] or 0),
                "visitor_days": int(row["visitor_days"] or 0),
            }
    else:
        for row in win_daily_qs.values("type_key").annotate(pv=Sum("pv"), visitor_days=Sum("daily_uv")):
            tk = row["type_key"] or "unknown"
            type_map[tk] = {
                "type_key": tk,
                "label": _resolve_type_label(tk),
                "pv": int(row["pv"] or 0),
                "visitor_days": int(row["visitor_days"] or 0),
            }

    total_type_pv = sum(t["pv"] for t in type_map.values())
    type_breakdown = sorted(type_map.values(), key=lambda x: (-x["pv"], x["type_key"]))
    for t in type_breakdown:
        share = _safe_rate(t["pv"], total_type_pv)
        t["share"] = share
        t["share_pct"] = _safe_pct(share) or 0.0

    # 6. 入口导流统计
    entry_scope_rows = list(
        ScopeAnalyticsDaily.objects.filter(
            site=site,
            scope_kind="entry",
            stat_date__gte=start_date,
            stat_date__lte=end_date,
        )
        .exclude(health_status="missing")
        .values("scope_key")
        .annotate(pv=Sum("pv"), visitor_days=Sum("daily_uv"), entry_clicks=Sum("entry_clicks"))
    )
    entry_stats = []
    zero_result_total = 0
    for row in entry_scope_rows:
        ek = row["scope_key"]
        epv = int(row["pv"] or 0)
        if ek == "search_zero_result":
            zero_result_total += epv
            continue
        clicks = int(row["entry_clicks"] or 0)
        ctr = _safe_rate(clicks, epv)
        entry_stats.append(
            {
                "entry_kind": ek,
                "label": _resolve_entry_label(ek),
                "pv": epv,
                "visitor_days": int(row["visitor_days"] or 0),
                "entry_clicks": clicks,
                "ctr": ctr,
                "ctr_pct": _safe_pct(ctr),
            }
        )
    entry_stats.sort(key=lambda x: (-x["pv"], -x["entry_clicks"], x["entry_kind"]))

    # 7. D5 安全搜索词与无结果词
    search_scope_rows = list(
        ScopeAnalyticsDaily.objects.filter(
            site=site,
            scope_kind="safe_search_term",
            stat_date__gte=start_date,
            stat_date__lte=end_date,
        ).values("scope_key", "pv", "daily_uv", "entry_clicks", "health_status")
    )
    safe_search_terms = _parse_safe_search_terms(search_scope_rows)
    safe_search_terms["zero_result_total"] = zero_result_total

    # 8. 千万级混合分页（有界计数 <= 5000 + 浅页直跳 <= 100 页 + 双向 Keyset 游标，只拉取当前页 <= 20 条）
    site_pages_qs = (
        accessible_pages_qs.filter(path__startswith=site.root_page.path)
        if site.root_page_id
        else accessible_pages_qs
    )
    candidate_pages_qs = _apply_page_search_filter(site_pages_qs, q, type_key)

    # 若携带搜索关键词，一律搜索全站所有可访问详情页（包含 0 PV 页面）
    effective_sort = "latest" if q else sort
    cursor_data = _decode_analytics_cursor(cursor, effective_sort) if cursor else None
    page_mode = "keyset" if cursor_data is not None else "shallow_page"
    current_page: int | None = None if page_mode == "keyset" else page_num

    precomputed_window: dict[int, dict[str, Any]] = {}
    slice_ids: list[int] = []
    has_next = False
    has_previous = False
    next_cursor: str | None = None
    previous_cursor: str | None = None

    if effective_sort == "latest":
        total_count, count_kind = _bounded_count(
            candidate_pages_qs,
            site_id=site.id,
            type_key=type_key,
            allow_archive_summary=not bool(q),
        )
        ordered_qs = candidate_pages_qs.order_by("-id")
        if cursor_data is not None:
            c_id = int(cursor_data["k"][0])
            if cursor_data["dir"] == "next":
                rows_plus = list(ordered_qs.filter(id__lt=c_id).values_list("id", flat=True)[: per_page + 1])
                has_next = len(rows_plus) > per_page
                slice_ids = rows_plus[:per_page]
                has_previous = bool(slice_ids) and candidate_pages_qs.filter(id__gt=slice_ids[0]).exists()
            else:
                rows_plus = list(candidate_pages_qs.order_by("id").filter(id__gt=c_id).values_list("id", flat=True)[: per_page + 1])
                has_previous = len(rows_plus) > per_page
                slice_ids = list(reversed(rows_plus[:per_page]))
                has_next = bool(slice_ids) and candidate_pages_qs.filter(id__lt=slice_ids[-1]).exists()
        else:
            offset = (page_num - 1) * per_page
            rows_plus = list(ordered_qs.values_list("id", flat=True)[offset : offset + per_page + 1])
            has_next = len(rows_plus) > per_page
            slice_ids = rows_plus[:per_page]
            has_previous = page_num > 1 and bool(slice_ids)
        if has_next and slice_ids:
            next_cursor = _encode_analytics_cursor({"sort": "latest", "dir": "next", "k": [slice_ids[-1]]})
        if has_previous and slice_ids:
            previous_cursor = _encode_analytics_cursor({"sort": "latest", "dir": "prev", "k": [slice_ids[0]]})

    elif effective_sort == "total_pv":
        tot_base_qs = PageAnalyticsTotal.objects.filter(site=site, page__in=candidate_pages_qs)
        total_count, count_kind = _bounded_count(tot_base_qs)
        if cursor_data is not None:
            c_pv, c_pid = int(cursor_data["k"][0]), int(cursor_data["k"][1])
            if cursor_data["dir"] == "next":
                lt_q = _build_tuple_lt_q([("total_pv", c_pv), ("page_id", c_pid)])
                rows_plus = list(
                    tot_base_qs.filter(lt_q).order_by("-total_pv", "-page_id").values_list("page_id", "total_pv")[: per_page + 1]
                )
                has_next = len(rows_plus) > per_page
                chosen = rows_plus[:per_page]
                has_previous = bool(chosen) and tot_base_qs.filter(
                    _build_tuple_gt_q([("total_pv", chosen[0][1]), ("page_id", chosen[0][0])])
                ).exists()
            else:
                gt_q = _build_tuple_gt_q([("total_pv", c_pv), ("page_id", c_pid)])
                rows_plus = list(
                    tot_base_qs.filter(gt_q).order_by("total_pv", "page_id").values_list("page_id", "total_pv")[: per_page + 1]
                )
                has_previous = len(rows_plus) > per_page
                chosen = list(reversed(rows_plus[:per_page]))
                has_next = bool(chosen) and tot_base_qs.filter(
                    _build_tuple_lt_q([("total_pv", chosen[-1][1]), ("page_id", chosen[-1][0])])
                ).exists()
        else:
            offset = (page_num - 1) * per_page
            rows_plus = list(
                tot_base_qs.order_by("-total_pv", "-page_id").values_list("page_id", "total_pv")[offset : offset + per_page + 1]
            )
            has_next = len(rows_plus) > per_page
            chosen = rows_plus[:per_page]
            has_previous = page_num > 1 and bool(chosen)
        slice_ids = [r[0] for r in chosen]
        if has_next and chosen:
            next_cursor = _encode_analytics_cursor({"sort": "total_pv", "dir": "next", "k": [int(chosen[-1][1]), int(chosen[-1][0])]})
        if has_previous and chosen:
            previous_cursor = _encode_analytics_cursor({"sort": "total_pv", "dir": "prev", "k": [int(chosen[0][1]), int(chosen[0][0])]})

    else:  # window_valid（在 PostgreSQL GROUP BY + Keyset HAVING 上执行，绝不拉全表 ID）
        grouped_qs = (
            win_daily_qs.values("page_id")
            .annotate(
                win_valid=Sum("valid_views"),
                win_comp=Sum("completed_views"),
                win_pv=Sum("pv"),
                win_meas=Sum("measurable_views"),
                win_bounce=Sum("short_bounce_views"),
                win_sec=Sum("total_active_seconds"),
                win_uv=Max("daily_uv"),
            )
        )
        total_count, count_kind = _bounded_count(grouped_qs)
        if cursor_data is not None:
            kv, kc, kp, kpid = [int(x) for x in cursor_data["k"]]
            if cursor_data["dir"] == "next":
                lt_q = _build_tuple_lt_q([("win_valid", kv), ("win_comp", kc), ("win_pv", kp), ("page_id", kpid)])
                rows_plus = list(
                    grouped_qs.filter(lt_q).order_by("-win_valid", "-win_comp", "-win_pv", "-page_id")[: per_page + 1]
                )
                has_next = len(rows_plus) > per_page
                chosen = rows_plus[:per_page]
                if chosen:
                    first_r = chosen[0]
                    gt_q = _build_tuple_gt_q([
                        ("win_valid", int(first_r["win_valid"] or 0)),
                        ("win_comp", int(first_r["win_comp"] or 0)),
                        ("win_pv", int(first_r["win_pv"] or 0)),
                        ("page_id", int(first_r["page_id"])),
                    ])
                    has_previous = grouped_qs.filter(gt_q).exists()
            else:
                gt_q = _build_tuple_gt_q([("win_valid", kv), ("win_comp", kc), ("win_pv", kp), ("page_id", kpid)])
                rows_plus = list(
                    grouped_qs.filter(gt_q).order_by("win_valid", "win_comp", "win_pv", "page_id")[: per_page + 1]
                )
                has_previous = len(rows_plus) > per_page
                chosen = list(reversed(rows_plus[:per_page]))
                if chosen:
                    last_r = chosen[-1]
                    lt_q = _build_tuple_lt_q([
                        ("win_valid", int(last_r["win_valid"] or 0)),
                        ("win_comp", int(last_r["win_comp"] or 0)),
                        ("win_pv", int(last_r["win_pv"] or 0)),
                        ("page_id", int(last_r["page_id"])),
                    ])
                    has_next = grouped_qs.filter(lt_q).exists()
        else:
            offset = (page_num - 1) * per_page
            rows_plus = list(
                grouped_qs.order_by("-win_valid", "-win_comp", "-win_pv", "-page_id")[offset : offset + per_page + 1]
            )
            has_next = len(rows_plus) > per_page
            chosen = rows_plus[:per_page]
            has_previous = page_num > 1 and bool(chosen)

        for r in chosen:
            pid = int(r["page_id"])
            slice_ids.append(pid)
            precomputed_window[pid] = {
                "page_id": pid,
                "type_key": "",
                "pv": int(r["win_pv"] or 0),
                "visitor_days": int(r["win_uv"] or 0),
                "measurable_views": int(r["win_meas"] or 0),
                "valid_views": int(r["win_valid"] or 0),
                "completed_views": int(r["win_comp"] or 0),
                "short_bounce_views": int(r["win_bounce"] or 0),
                "total_active_seconds": int(r["win_sec"] or 0),
            }
        if has_next and chosen:
            lr = chosen[-1]
            next_cursor = _encode_analytics_cursor({
                "sort": "window_valid",
                "dir": "next",
                "k": [int(lr["win_valid"] or 0), int(lr["win_comp"] or 0), int(lr["win_pv"] or 0), int(lr["page_id"])],
            })
        if has_previous and chosen:
            fr = chosen[0]
            previous_cursor = _encode_analytics_cursor({
                "sort": "window_valid",
                "dir": "prev",
                "k": [int(fr["win_valid"] or 0), int(fr["win_comp"] or 0), int(fr["win_pv"] or 0), int(fr["page_id"])],
            })

    top_pages = _hydrate_page_rows_by_slice(
        site=site,
        accessible_pages_qs=accessible_pages_qs,
        slice_ids=slice_ids,
        start_date=start_date,
        end_date=end_date,
        today=today,
        precomputed_window=precomputed_window,
    )
    count_meta = build_count_meta(value=total_count, kind=count_kind)
    pagination_payload = build_pagination_payload(
        mode=page_mode,
        current_page=current_page,
        page_size=per_page,
        total_count=total_count,
        count_kind=count_kind,
        has_next=has_next,
        has_previous=has_previous,
        next_cursor=next_cursor,
        previous_cursor=previous_cursor,
    )

    coverage_warning = _build_coverage_warning(
        total_window_days=days,
        healthy_days=healthy_days,
        partial_days=partial_days,
        missing_days=missing_days,
        uncovered_days=uncovered_days,
        include_today=include_today,
        today_in_window=(start_date <= today <= end_date),
        min_healthy_days_in_30d=settings_snapshot["min_healthy_days_in_30d"],
    )

    return {
        "site": site,
        "sites": all_sites,
        "available_types": available_types,
        "filters": {
            "site_id": site.id,
            "days": days,
            "type_key": type_key,
            "include_today": include_today,
            "q": q,
            "sort": sort,
            "start_date": start_date,
            "end_date": end_date,
        },
        "settings": settings_snapshot,
        "summary": summary,
        "trend_series": trend_series,
        "type_breakdown": type_breakdown,
        "entry_stats": entry_stats,
        "safe_search_terms": safe_search_terms,
        "coverage_warning": coverage_warning,
        "top_pages": top_pages,
        "count_meta": count_meta,
        "pages_pagination": pagination_payload,
    }


def get_page_analytics_detail(user, page: Page | int) -> dict[str, Any]:
    """
    获取单篇页面数据分析详情与 5 档阅读漏斗、来源构成与健康覆盖状态。
    严格调用 `can_view_page_analytics(user, page)` 校验访问权限。
    """
    if isinstance(page, int):
        page_obj = Page.objects.filter(id=page).first()
    else:
        page_obj = page

    if page_obj is None or not can_view_page_analytics(user, page_obj):
        raise PermissionDenied("当前用户无权查看该页面的数据分析。")

    specific_page = page_obj.specific if hasattr(page_obj, "specific") else page_obj
    site = None
    try:
        site = specific_page.get_site()
    except Exception:
        site = None
    if site is None:
        site = _resolve_site()

    settings_snapshot = _load_settings_snapshot(site)
    type_key = _resolve_type_key_for_page(specific_page)
    type_label = _resolve_type_label(type_key, page=specific_page)
    caps = get_analytics_capabilities(type_key) if type_key else None

    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    start_30d = today - timedelta(days=29)
    start_7d = today - timedelta(days=6)

    total_obj = (
        PageAnalyticsTotal.objects.filter(site=site, page=specific_page).first()
        if site is not None
        else None
    )
    daily_qs = (
        PageAnalyticsDaily.objects.filter(
            site=site,
            page=specific_page,
            stat_date__gte=start_30d,
            stat_date__lte=today,
        )
        if site is not None
        else PageAnalyticsDaily.objects.none()
    )

    day_map: dict[date, dict[str, Any]] = {}
    funnel_totals: dict[str, int] = {
        "scroll_25": 0,
        "scroll_50": 0,
        "scroll_90": 0,
        "end_reached": 0,
        "short_body": 0,
    }
    funnel_seen: dict[str, bool] = {k: False for k in funnel_totals}
    source_channels: dict[str, int] = {}
    conv_totals: dict[str, int] = {k: 0 for k in _TYPE_CONVERSION_KEYS}

    for row in daily_qs.values(
        "stat_date",
        "live_revision_id",
        "type_key",
        "pv",
        "daily_uv",
        "measurable_views",
        "valid_views",
        "completed_views",
        "short_bounce_views",
        "total_active_seconds",
        "organic_pv",
        "conversions_json",
        "health_status",
    ):
        if row.get("type_key"):
            type_key = row["type_key"]
            type_label = _resolve_type_label(type_key, page=specific_page)
            caps = get_analytics_capabilities(type_key)
        sdate = row["stat_date"]
        health = row.get("health_status") or "normal"
        slot = day_map.setdefault(
            sdate,
            {
                "date": sdate,
                "pv": 0,
                "daily_uv": 0,
                "measurable_views": 0,
                "valid_views": 0,
                "completed_views": 0,
                "short_bounce_views": 0,
                "total_active_seconds": 0,
                "organic_pv": 0,
                "health_status": health,
            },
        )
        slot["health_status"] = _merge_health(slot["health_status"], health)
        if health == "missing":
            continue
        slot["pv"] += int(row.get("pv") or 0)
        slot["daily_uv"] = max(slot["daily_uv"], int(row.get("daily_uv") or 0))
        slot["measurable_views"] += int(row.get("measurable_views") or 0)
        slot["valid_views"] += int(row.get("valid_views") or 0)
        slot["completed_views"] += int(row.get("completed_views") or 0)
        slot["short_bounce_views"] += int(row.get("short_bounce_views") or 0)
        slot["total_active_seconds"] += int(row.get("total_active_seconds") or 0)
        slot["organic_pv"] += int(row.get("organic_pv") or 0)

        f_data, s_data = _extract_conversions_funnel_and_sources(row.get("conversions_json"))
        for fk, fv in f_data.items():
            if fv is not None:
                funnel_seen[fk] = True
                funnel_totals[fk] += fv
        for sk, sv in s_data.items():
            source_channels[sk] = source_channels.get(sk, 0) + sv
        for ck, cv in _extract_type_conversion_counts(row.get("conversions_json")).items():
            conv_totals[ck] = conv_totals.get(ck, 0) + cv

    healthy_days_30d = 0
    partial_days: list[str] = []
    missing_days: list[str] = []
    uncovered_days: list[str] = []
    daily_trend: list[dict[str, Any]] = []

    pv_7d = 0
    pv_30d = 0
    visitor_days_7d = 0
    visitor_days_30d = 0
    covered_7d = 0
    covered_30d = 0
    measurable_30d = 0
    valid_30d = 0
    completed_30d = 0
    short_bounce_30d = 0
    active_sec_30d = 0
    organic_pv_30d = 0

    for offset in range(30):
        cur_date = start_30d + timedelta(days=offset)
        date_str = cur_date.isoformat()
        is_provisional = cur_date == today
        slot = day_map.get(cur_date)

        if slot is None:
            health = "uncovered"
            uncovered_days.append(date_str)
            daily_trend.append(
                {
                    "date": date_str,
                    "pv": None,
                    "daily_uv": None,
                    "valid_views": None,
                    "completed_views": None,
                    "avg_active_seconds": None,
                    "health_status": health,
                    "status_label": _STATUS_LABELS[health],
                    "is_provisional": is_provisional,
                }
            )
            continue

        health = slot["health_status"]
        if health == "missing":
            missing_days.append(date_str)
            daily_trend.append(
                {
                    "date": date_str,
                    "pv": None,
                    "daily_uv": None,
                    "valid_views": None,
                    "completed_views": None,
                    "avg_active_seconds": None,
                    "health_status": health,
                    "status_label": _STATUS_LABELS[health],
                    "is_provisional": is_provisional,
                }
            )
            continue

        if health == "partial":
            partial_days.append(date_str)
        elif not is_provisional:
            healthy_days_30d += 1

        covered_30d += 1
        pv_30d += slot["pv"]
        visitor_days_30d += slot["daily_uv"]
        measurable_30d += slot["measurable_views"]
        valid_30d += slot["valid_views"]
        completed_30d += slot["completed_views"]
        short_bounce_30d += slot["short_bounce_views"]
        active_sec_30d += slot["total_active_seconds"]
        organic_pv_30d += slot["organic_pv"]

        if cur_date >= start_7d:
            covered_7d += 1
            pv_7d += slot["pv"]
            visitor_days_7d += slot["daily_uv"]

        day_avg_sec = (
            round(slot["total_active_seconds"] / slot["measurable_views"], 1)
            if slot["measurable_views"] > 0
            else None
        )
        status_label = "当日暂估" if is_provisional else _STATUS_LABELS.get(health, health)
        daily_trend.append(
            {
                "date": date_str,
                "pv": slot["pv"],
                "daily_uv": slot["daily_uv"],
                "valid_views": slot["valid_views"],
                "completed_views": slot["completed_views"],
                "avg_active_seconds": day_avg_sec,
                "health_status": health,
                "status_label": status_label,
                "is_provisional": is_provisional,
            }
        )

    today_slot = day_map.get(today)
    yesterday_slot = day_map.get(yesterday)
    today_pv = (
        today_slot["pv"]
        if (today_slot is not None and today_slot["health_status"] != "missing")
        else None
    )
    today_uv = (
        today_slot["daily_uv"]
        if (today_slot is not None and today_slot["health_status"] != "missing")
        else None
    )
    yesterday_pv = (
        yesterday_slot["pv"]
        if (yesterday_slot is not None and yesterday_slot["health_status"] != "missing")
        else None
    )
    yesterday_uv = (
        yesterday_slot["daily_uv"]
        if (yesterday_slot is not None and yesterday_slot["health_status"] != "missing")
        else None
    )

    valid_read_rate = _safe_rate(valid_30d, measurable_30d)
    completion_rate = _safe_rate(completed_30d, measurable_30d)
    short_bounce_rate = _safe_rate(short_bounce_30d, measurable_30d)
    avg_active_seconds = round(active_sec_30d / measurable_30d, 1) if measurable_30d > 0 else None
    coverage_rate = _safe_rate(measurable_30d, pv_30d)

    # 5 档阅读漏斗：可测阅读 -> 25% -> 50% -> 90% -> 正文末端
    def _funnel_stage(label: str, count: int | None, *, available: bool) -> dict[str, Any]:
        ratio = _safe_rate(count, measurable_30d) if (available and count is not None) else None
        return {
            "label": label,
            "count": count if available else None,
            "available": available,
            "rate": ratio,
            "rate_pct": _safe_pct(ratio),
        }

    funnel_stages = [
        _funnel_stage("可测阅读", measurable_30d, available=True),
        _funnel_stage("正文 25%", funnel_totals["scroll_25"], available=funnel_seen["scroll_25"]),
        _funnel_stage("正文 50%", funnel_totals["scroll_50"], available=funnel_seen["scroll_50"]),
        _funnel_stage("正文 90%", funnel_totals["scroll_90"], available=funnel_seen["scroll_90"]),
        _funnel_stage("正文末端", funnel_totals["end_reached"], available=funnel_seen["end_reached"]),
    ]

    curated_or_rank_pv = max(0, pv_30d - organic_pv_30d)
    organic_share = _safe_rate(organic_pv_30d, pv_30d)
    channel_items = []
    total_channel_pv = sum(source_channels.values())
    for key, cnt in sorted(source_channels.items(), key=lambda kv: (-kv[1], kv[0])):
        share = _safe_rate(cnt, total_channel_pv)
        channel_items.append(
            {
                "key": key,
                "label": _SOURCE_LABELS.get(key, key),
                "pv": cnt,
                "share_pct": _safe_pct(share),
            }
        )

    coverage_warning = _build_coverage_warning(
        total_window_days=30,
        healthy_days=healthy_days_30d,
        partial_days=partial_days,
        missing_days=missing_days,
        uncovered_days=uncovered_days,
        include_today=True,
        today_in_window=True,
        min_healthy_days_in_30d=settings_snapshot["min_healthy_days_in_30d"],
    )

    type_conversions = _build_page_type_conversions_summary(
        specific_page,
        type_key,
        conv_totals,
        measurable_30d,
        today,
    )

    return {
        "page": {
            "id": specific_page.id,
            "title": specific_page.title,
            "url_path": getattr(specific_page, "url_path", "") or "",
            "document_uuid": str(getattr(specific_page, "document_uuid", "") or ""),
            "type_key": type_key,
            "type_label": type_label,
            "live": bool(getattr(specific_page, "live", False)),
            "live_revision_id": getattr(specific_page, "live_revision_id", None),
            "is_recycled": _is_page_recycled(specific_page),
            "supports_read_depth": bool(caps and "read_depth" in getattr(caps, "metric_keys", ())),
        },
        "site": site,
        "settings": settings_snapshot,
        "summary": {
            "today_pv": today_pv,
            "yesterday_pv": yesterday_pv,
            "pv_7d": pv_7d if covered_7d > 0 else None,
            "pv_30d": pv_30d if covered_30d > 0 else None,
            "total_pv": int(total_obj.total_pv) if total_obj else (pv_30d if covered_30d > 0 else 0),
            "today_uv": today_uv,
            "yesterday_uv": yesterday_uv,
            "visitor_days_7d": visitor_days_7d if covered_7d > 0 else None,
            "visitor_days_30d": visitor_days_30d if covered_30d > 0 else None,
            "total_visitor_days": int(total_obj.total_visitor_days) if total_obj else (visitor_days_30d if covered_30d > 0 else 0),
            "measurable_views_30d": measurable_30d,
            "valid_views_30d": valid_30d,
            "completed_views_30d": completed_30d,
            "short_bounce_views_30d": short_bounce_30d,
            "valid_read_rate": valid_read_rate,
            "valid_read_rate_pct": _safe_pct(valid_read_rate),
            "completion_rate": completion_rate,
            "completion_rate_pct": _safe_pct(completion_rate),
            "short_bounce_rate": short_bounce_rate,
            "short_bounce_rate_pct": _safe_pct(short_bounce_rate),
            "avg_active_seconds": avg_active_seconds,
            "coverage_rate": coverage_rate,
            "coverage_rate_pct": _safe_pct(coverage_rate),
            "last_settled_date": total_obj.last_settled_date if total_obj else None,
        },
        "funnel": {
            "stages": funnel_stages,
            "completed_views": completed_30d,
            "short_body_views": funnel_totals["short_body"] if funnel_seen["short_body"] else None,
        },
        "sources": {
            "organic_pv": organic_pv_30d,
            "curated_or_rank_pv": curated_or_rank_pv,
            "organic_share": organic_share,
            "organic_share_pct": _safe_pct(organic_share),
            "channels": channel_items,
        },
        "health_coverage": {
            "healthy_days_30d": healthy_days_30d,
            "min_healthy_days_in_30d": settings_snapshot["min_healthy_days_in_30d"],
            "meets_threshold": healthy_days_30d >= settings_snapshot["min_healthy_days_in_30d"],
            "warning": coverage_warning,
        },
        "type_conversions": type_conversions,
        "daily_trend": list(reversed(daily_trend)),
    }


def get_project_tech_stack_heat(
    site: Site | None,
    *,
    days: int = 30,
) -> dict[str, Any]:
    """
    第 3 期技术栈热度统计（按 primary_language 与技术标签分组）。
    通过 TypeAdapter 注册表与 PublishedSearchDocument/PublishedSearchTag 统一查询，
    不反向 import 具体项目页面模型，且不把 star_count 纳入站内流量。
    """
    if site is None:
        return {"languages": [], "tags": []}

    from discovery.models import PublishedSearchDocument, PublishedSearchTag
    from discovery.registry import list_analytics_adapters

    tech_type_keys = [
        adapter.type_key
        for adapter in list_analytics_adapters()
        if (
            "repo_click" in (getattr(adapter.analytics_capabilities, "conversion_keys", ()) or ())
            or "demo_click" in (getattr(adapter.analytics_capabilities, "conversion_keys", ()) or ())
        )
    ]
    if not tech_type_keys:
        return {"languages": [], "tags": []}

    days = 7 if int(days or 30) <= 7 else 30
    today = timezone.localdate()
    end_date = today - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)

    proj_qs = PublishedSearchDocument.objects.filter(
        is_live=True,
        is_recycled=False,
        index_generation=1,
        type_key__in=tech_type_keys,
    )
    if site.id:
        proj_qs = proj_qs.filter(Q(site_id=site.id) | Q(site_id__isnull=True))
    if site.root_page_id:
        proj_qs = proj_qs.filter(root_path__startswith=site.root_page.path, depth__gt=site.root_page.depth)

    # 按有界批次过滤祖先受限/回收页，避免大表内存膨胀
    proj_rows = list(proj_qs.values_list("page_id", "root_path", "extra_data")[:1000])
    if not proj_rows:
        return {"languages": [], "tags": []}
    blocked = get_public_blocked_page_ids([(pid, path) for pid, path, _ in proj_rows])
    eligible_map: dict[int, str] = {
        int(pid): (str((extra or {}).get("primary_language") or "").strip() or "未标注语言")
        for pid, _, extra in proj_rows
        if pid not in blocked
    }
    if not eligible_map:
        return {"languages": [], "tags": []}

    eligible_ids = sorted(eligible_map.keys())
    tags_by_pid: dict[int, list[str]] = defaultdict(list)
    for pid, tag_name in PublishedSearchTag.objects.filter(
        document__page_id__in=eligible_ids,
        document__index_generation=1,
    ).values_list("document__page_id", "tag_name"):
        if tag_name:
            tags_by_pid[int(pid)].append(str(tag_name))

    metrics_by_pid: dict[int, dict[str, int]] = defaultdict(lambda: {"valid": 0, "meas": 0, "repo": 0, "demo": 0})
    for dr in (
        PageAnalyticsDaily.objects.filter(
            site=site,
            page_id__in=eligible_ids,
            stat_date__gte=start_date,
            stat_date__lte=end_date,
        )
        .exclude(health_status="missing")
        .values("page_id", "valid_views", "measurable_views", "conversions_json")
    ):
        pid = int(dr["page_id"])
        m = metrics_by_pid[pid]
        m["valid"] += int(dr.get("valid_views") or 0)
        m["meas"] += int(dr.get("measurable_views") or 0)
        convs = _extract_type_conversion_counts(dr.get("conversions_json"))
        m["repo"] += convs.get("repo_click", 0)
        m["demo"] += convs.get("demo_click", 0)

    lang_buckets: dict[str, dict[str, Any]] = {}
    tag_buckets: dict[str, dict[str, Any]] = {}
    for pid, lang in eligible_map.items():
        pm = metrics_by_pid.get(pid, {"valid": 0, "meas": 0, "repo": 0, "demo": 0})
        lb = lang_buckets.setdefault(lang, {"name": lang, "project_count": 0, "valid_views": 0, "measurable_views": 0, "repo_clicks": 0, "demo_clicks": 0})
        lb["project_count"] += 1
        lb["valid_views"] += pm["valid"]
        lb["measurable_views"] += pm["meas"]
        lb["repo_clicks"] += pm["repo"]
        lb["demo_clicks"] += pm["demo"]

        for tname in tags_by_pid.get(pid, []):
            tb = tag_buckets.setdefault(tname, {"name": tname, "project_count": 0, "valid_views": 0, "measurable_views": 0, "repo_clicks": 0, "demo_clicks": 0})
            tb["project_count"] += 1
            tb["valid_views"] += pm["valid"]
            tb["measurable_views"] += pm["meas"]
            tb["repo_clicks"] += pm["repo"]
            tb["demo_clicks"] += pm["demo"]

    def _finalize(buckets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        items = []
        for b in buckets.values():
            clicks = b["repo_clicks"] + b["demo_clicks"]
            ctr = _safe_rate(clicks, b["measurable_views"])
            items.append({**b, "total_clicks": clicks, "click_rate_pct": _safe_pct(ctr)})
        items.sort(key=lambda x: (-x["valid_views"], -x["total_clicks"], -x["project_count"], x["name"]))
        return items[:20]

    return {
        "days": days,
        "languages": _finalize(lang_buckets),
        "tags": _finalize(tag_buckets),
    }


def get_author_pages_analytics_list(
    user,
    *,
    days: int = 7,
    include_today: bool = True,
    q: str = "",
    page_num: int = 1,
    cursor: str | None = None,
    per_page: int = 20,
) -> dict[str, Any]:
    """
    受限作者个人数据分析列表（仅返回通过 `filter_accessible_pages_for_analytics` 的本人授权页面，
    遵循千万级有界计数与混合分页协议，绝不拉全表或泄露他人数据）。
    """
    if not can_view_analytics_menu(user):
        raise PermissionDenied("当前用户无权访问页面数据分析。")

    site = _resolve_site()
    days = max(1, min(int(days or 7), 365))
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    end_date = today if include_today else yesterday
    start_date = end_date - timedelta(days=days - 1)

    q = (q or "").strip()[:120]
    page_num = max(1, int(page_num or 1))
    per_page = max(1, min(int(per_page or 20), 100))
    max_jump_page = compute_max_jump_page(per_page)
    if not cursor and page_num > max_jump_page:
        raise PageWindowExceededError(page_num, max_jump_page)

    accessible_qs = filter_accessible_pages_for_analytics(user, Page.objects.all())
    filtered_qs = _apply_page_search_filter(accessible_qs, q)
    total_count, count_kind = _bounded_count(filtered_qs)

    cursor_data = _decode_analytics_cursor(cursor, "author_latest") if cursor else None
    page_mode = "keyset" if cursor_data is not None else "shallow_page"
    current_page: int | None = None if page_mode == "keyset" else page_num

    slice_ids: list[int] = []
    has_next = False
    has_previous = False
    if cursor_data is not None:
        c_id = int(cursor_data["k"][0])
        if cursor_data["dir"] == "next":
            rows_plus = list(filtered_qs.order_by("-id").filter(id__lt=c_id).values_list("id", flat=True)[: per_page + 1])
            has_next = len(rows_plus) > per_page
            slice_ids = rows_plus[:per_page]
            has_previous = bool(slice_ids) and filtered_qs.filter(id__gt=slice_ids[0]).exists()
        else:
            rows_plus = list(filtered_qs.order_by("id").filter(id__gt=c_id).values_list("id", flat=True)[: per_page + 1])
            has_previous = len(rows_plus) > per_page
            slice_ids = list(reversed(rows_plus[:per_page]))
            has_next = bool(slice_ids) and filtered_qs.filter(id__lt=slice_ids[-1]).exists()
    else:
        offset = (page_num - 1) * per_page
        rows_plus = list(filtered_qs.order_by("-id").values_list("id", flat=True)[offset : offset + per_page + 1])
        has_next = len(rows_plus) > per_page
        slice_ids = rows_plus[:per_page]
        has_previous = page_num > 1 and bool(slice_ids)

    next_cursor = (
        _encode_analytics_cursor({"sort": "author_latest", "dir": "next", "k": [slice_ids[-1]]})
        if (has_next and slice_ids)
        else None
    )
    previous_cursor = (
        _encode_analytics_cursor({"sort": "author_latest", "dir": "prev", "k": [slice_ids[0]]})
        if (has_previous and slice_ids)
        else None
    )
    pages_items = (
        _hydrate_page_rows_by_slice(
            site=site,
            accessible_pages_qs=accessible_qs,
            slice_ids=slice_ids,
            start_date=start_date,
            end_date=end_date,
            today=today,
        )
        if site is not None
        else []
    )
    count_meta = build_count_meta(value=total_count, kind=count_kind)
    pagination_payload = build_pagination_payload(
        mode=page_mode,
        current_page=current_page,
        page_size=per_page,
        total_count=total_count,
        count_kind=count_kind,
        has_next=has_next,
        has_previous=has_previous,
        next_cursor=next_cursor,
        previous_cursor=previous_cursor,
    )
    return {
        "site": site,
        "filters": {
            "days": days,
            "include_today": include_today,
            "q": q,
            "sort": "latest",
            "start_date": start_date,
            "end_date": end_date,
        },
        "top_pages": pages_items,
        "count_meta": count_meta,
        "pages_pagination": pagination_payload,
    }


def get_batch_public_pv_readonly(
    site: Any,
    page_rev_pairs: list[tuple[int, int]],
) -> dict[int, int] | None:
    """
    只读批量读取列表页公开累计阅读量（1 次 SQL + 1 次 Redis HMGET，零 N+1，绝不写库）：
    - 若站点关闭 public_pv_display_enabled 或无有效站点，返回 None（前端隐藏浏览量）；
    - 与详情页 analytics_public_stats 使用同一合并口径：max(今日已落库+Redis增量, 累计+Redis增量)。
    """
    from django.db import DatabaseError
    from analytics.collector import _get_redis_client, _resolve_prefix

    if not page_rev_pairs:
        return {}

    site_id = site.id if isinstance(site, Site) else site
    if not site_id:
        try:
            default_site = Site.objects.filter(is_default_site=True).only("id").first()
            site_id = default_site.id if default_site else None
        except DatabaseError:
            return None
    if not site_id:
        return None

    ana_settings = AnalyticsSettings.get_for_site_readonly(site_id)
    if not getattr(ana_settings, "public_pv_display_enabled", True):
        return None

    unique_pids = sorted({int(pid) for pid, _ in page_rev_pairs if pid})
    if not unique_pids:
        return {}

    today = timezone.localdate()
    settled_totals: dict[int, int] = {}
    settled_today: dict[int, int] = {}
    try:
        settled_totals = {
            int(pid): int(pv or 0)
            for pid, pv in PageAnalyticsTotal.objects.filter(
                site_id=site_id,
                page_id__in=unique_pids,
            ).values_list("page_id", "total_pv")
        }
        settled_today = {
            int(row["page_id"]): int(row["total"] or 0)
            for row in PageAnalyticsDaily.objects.filter(
                site_id=site_id,
                page_id__in=unique_pids,
                stat_date=today,
            )
            .exclude(health_status="missing")
            .values("page_id")
            .annotate(total=Sum("pv"))
        }
    except DatabaseError:
        return None

    unsettled_by_pid: dict[int, int] = {}
    valid_pairs = [(int(pid), int(rev)) for pid, rev in page_rev_pairs if pid and int(rev or 0) > 0]
    if valid_pairs:
        try:
            client = _get_redis_client(write=False)
            if client is not None:
                prefix = _resolve_prefix()
                active_key = f"{prefix}:analytics:active:pv:{site_id}:{today.isoformat()}"
                fields = [f"{pid}|{rev}|pv" for pid, rev in valid_pairs]
                raw_vals = client.hmget(active_key, fields)
                for (pid, _), raw_val in zip(valid_pairs, raw_vals):
                    if raw_val is not None:
                        try:
                            unsettled_by_pid[pid] = unsettled_by_pid.get(pid, 0) + max(0, int(raw_val))
                        except (TypeError, ValueError):
                            pass
        except Exception:
            pass

    out: dict[int, int] = {}
    for pid in unique_pids:
        delta = unsettled_by_pid.get(pid, 0)
        today_pv = settled_today.get(pid, 0) + delta
        total_pv = max(today_pv, settled_totals.get(pid, 0) + delta)
        out[pid] = total_pv
    return out
