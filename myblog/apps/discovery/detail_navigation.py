"""
全站详情页共享上下篇导航与同栏/同标签推荐只读查询服务 (detail_navigation.py)。

落实架构契约：
1. 共享层零具体业务页面导入：仅通过 discovery.registry.get_archive_enabled_type_keys()
   过滤内容详情页，自动排除 BlogIndexPage / GuideIndexPage / PortfolioIndexPage / TagIndexPage；
2. 站点归属双核验：以当前 Site.root_page.path 的 root_path__startswith 为准（排除嵌套子站点），
   防止跨站移动子树后投影 site_id 未同步导致的泄漏；
3. 公开资格前置：所有候选先过 apply_public_eligibility_filter 与 authored_on <= site_today，
   最终 ≤6 条记录再通过 wagtailcore.Page.get_url(request) 解析真实路径，零 MongoDB 读取。
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import DatabaseError
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from wagtail.models import Page, Site

from discovery.eligibility import apply_public_eligibility_filter
from discovery.facets import get_type_display_label
from discovery.models import PublishedSearchDocument, PublishedSearchTag
from discovery.registry import get_archive_enabled_type_keys

logger = logging.getLogger(__name__)


def resolve_site_readonly(request: Any = None, page: Any = None) -> Site | None:
    """
    纯只读解析并交叉核验当前请求与页面的所属 Wagtail Site。
    若请求站点与页面所属站点明确冲突，返回 None，防止跨站数据泄漏。
    """
    req_site = None
    if request is not None:
        try:
            req_site = Site.find_for_request(request)
        except Exception:
            req_site = None

    page_site = None
    if page is not None and hasattr(page, "get_site"):
        try:
            page_site = page.get_site()
        except Exception:
            page_site = None

    if req_site is not None and page_site is not None and req_site.id != page_site.id:
        return None

    resolved = req_site or page_site
    if resolved is None:
        try:
            resolved = Site.objects.select_related("root_page").filter(is_default_site=True).first()
        except DatabaseError:
            return None

    if resolved is None:
        return None

    root_page = getattr(resolved, "root_page", None)
    if root_page is None or not getattr(root_page, "path", None):
        return None

    page_path = getattr(page, "path", None) if page is not None else None
    if page_path and not str(page_path).startswith(root_page.path):
        return None

    return resolved


def _get_nested_site_root_paths(site: Site) -> list[str]:
    """找出挂在当前站点根路径之下的嵌套子站点根路径，避免查询越界到子站点。"""
    root_path = site.root_page.path
    nested: list[str] = []
    try:
        for other in Site.objects.exclude(id=site.id).select_related("root_page"):
            other_root = getattr(other, "root_page", None)
            other_path = getattr(other_root, "path", None) if other_root else None
            if other_path and other_path.startswith(root_path) and len(other_path) > len(root_path):
                nested.append(other_path)
    except DatabaseError:
        return []
    return nested


def get_detail_navigation_and_recommendations(
    page: Any,
    request: Any = None,
    *,
    include_recommendations: bool = True,
    rec_limit: int = 4,
) -> dict[str, Any]:
    """
    查询当前公开详情页的「上一篇 / 下一篇」及「同栏 / 同标签推荐」。
    仅服务公开、已发布、未回收、非别名且命中当前正式 Revision 投影的页面。
    """
    empty = {"prev_page": None, "next_page": None, "recommendations": []}

    if page is None:
        return empty
    page_id = getattr(page, "id", None)
    page_path = getattr(page, "path", None) or ""
    page_depth = int(getattr(page, "depth", 0) or 0)
    live_rev_id = getattr(page, "live_revision_id", None)

    if (
        not page_id
        or not page_path
        or not live_rev_id
        or not getattr(page, "live", False)
        or getattr(page, "is_recycled", False)
        or getattr(page, "alias_of_id", None) is not None
    ):
        return empty

    site = resolve_site_readonly(request, page)
    if site is None:
        return empty

    detail_type_keys = get_archive_enabled_type_keys()
    if not detail_type_keys:
        return empty

    site_root_path = site.root_page.path
    site_today = timezone.localdate()

    try:
        base_qs = PublishedSearchDocument.objects.filter(
            index_generation=1,
            is_live=True,
            is_recycled=False,
            type_key__in=detail_type_keys,
            authored_on__lte=site_today,
            root_path__startswith=site_root_path,
        )
        for nested_root in _get_nested_site_root_paths(site):
            base_qs = base_qs.exclude(root_path__startswith=nested_root)

        eligible_docs = apply_public_eligibility_filter(base_qs)

        # 当前页必须命中与自身 live_revision_id 严格一致的合格投影，不回退到旧投影或草稿
        current_doc = (
            eligible_docs.filter(
                page_id=page_id,
                effective_revision_id=live_rev_id,
            )
            .values("id", "page_id", "authored_on")
            .first()
        )
        if current_doc is None:
            return empty

        cur_date = current_doc["authored_on"]
        cur_id = int(current_doc["page_id"])
        has_direct_parent = len(page_path) > 4 and page_depth > 1
        parent_path = page_path[:-4] if has_direct_parent else ""

        prev_row: dict[str, Any] | None = None
        next_row: dict[str, Any] | None = None

        if has_direct_parent:
            sibling_qs = eligible_docs.filter(
                root_path__startswith=parent_path,
                depth=page_depth,
            ).exclude(page_id=page_id)

            prev_row = (
                sibling_qs.filter(
                    Q(authored_on__lt=cur_date)
                    | Q(authored_on=cur_date, page_id__lt=cur_id)
                )
                .order_by("-authored_on", "-page_id")
                .values("page_id", "title", "type_key", "authored_on")
                .first()
            )
            next_row = (
                sibling_qs.filter(
                    Q(authored_on__gt=cur_date)
                    | Q(authored_on=cur_date, page_id__gt=cur_id)
                )
                .order_by("authored_on", "page_id")
                .values("page_id", "title", "type_key", "authored_on")
                .first()
            )

        rec_rows: list[dict[str, Any]] = []
        if include_recommendations and rec_limit > 0:
            excluded_ids: set[int] = {int(page_id)}
            if prev_row is not None:
                excluded_ids.add(int(prev_row["page_id"]))
            if next_row is not None:
                excluded_ids.add(int(next_row["page_id"]))

            if has_direct_parent:
                col_rows = list(
                    eligible_docs.filter(
                        root_path__startswith=parent_path,
                        depth=page_depth,
                    )
                    .exclude(page_id__in=excluded_ids)
                    .order_by("-authored_on", "-page_id")
                    .values("page_id", "title", "type_key", "authored_on")[:rec_limit]
                )
                for r in col_rows:
                    r["source"] = "column"
                    r["source_label"] = "同栏推荐"
                    rec_rows.append(r)

            remaining = rec_limit - len(rec_rows)
            if remaining > 0:
                tag_ids = list(
                    PublishedSearchTag.objects.filter(document_id=current_doc["id"])
                    .values_list("tag_id", flat=True)
                )
                if tag_ids:
                    used_ids = excluded_ids | {int(r["page_id"]) for r in rec_rows}
                    has_shared_tag = Exists(
                        PublishedSearchTag.objects.filter(
                            document_id=OuterRef("pk"),
                            tag_id__in=tag_ids,
                        )
                    )
                    tag_rows = list(
                        eligible_docs.filter(has_shared_tag)
                        .exclude(page_id__in=used_ids)
                        .order_by("-authored_on", "-page_id")
                        .values("page_id", "title", "type_key", "authored_on")[:remaining]
                    )
                    for r in tag_rows:
                        r["source"] = "tag"
                        r["source_label"] = "同标签推荐"
                        rec_rows.append(r)

            remaining = rec_limit - len(rec_rows)
            if remaining > 0:
                used_ids = excluded_ids | {int(r["page_id"]) for r in rec_rows}
                site_rows = list(
                    eligible_docs.exclude(page_id__in=used_ids)
                    .order_by("-authored_on", "-page_id")
                    .values("page_id", "title", "type_key", "authored_on")[:remaining]
                )
                for r in site_rows:
                    r["source"] = "site"
                    r["source_label"] = "全站推荐"
                    rec_rows.append(r)

        # 仅对最终极少量目标页（<=6）批量查基础 Page 解析真实 URL，绝不查 Mongo 正文
        target_ids: list[int] = []
        for row in (prev_row, next_row, *rec_rows):
            if row is not None:
                pid = int(row["page_id"])
                if pid not in target_ids:
                    target_ids.append(pid)

        if not target_ids:
            return empty

        pages_by_id = {
            p.id: p
            for p in Page.objects.filter(
                id__in=target_ids,
                live=True,
                alias_of_id__isnull=True,
            )
        }

        def _serialize_item(row: dict[str, Any] | None) -> dict[str, Any] | None:
            if row is None:
                return None
            pid = int(row["page_id"])
            target = pages_by_id.get(pid)
            if target is None:
                return None
            target_site = target.get_site() if hasattr(target, "get_site") else None
            if target_site is not None and target_site.id != site.id:
                return None
            url = (target.get_url(request) if request is not None else target.url) or ""
            if not url:
                return None
            return {
                "page_id": pid,
                "title": target.title or row["title"],
                "url": url,
                "authored_on": row["authored_on"],
                "type_key": row["type_key"],
                "type_label": get_type_display_label(row["type_key"]),
                "source": row.get("source", ""),
                "source_label": row.get("source_label", ""),
            }

        return {
            "prev_page": _serialize_item(prev_row),
            "next_page": _serialize_item(next_row),
            "recommendations": [
                item
                for item in (_serialize_item(r) for r in rec_rows)
                if item is not None
            ],
        }
    except DatabaseError as exc:
        logger.debug("detail_navigation query degraded gracefully: %s", exc)
        return empty
