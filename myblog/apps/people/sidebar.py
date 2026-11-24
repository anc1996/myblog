"""
全站详情页右侧「作者名片卡」纯只读聚合服务 (people.sidebar)。

落实架构契约：
1. 共享层零具体页面模型导入，仅通过 discovery.registry 与 PublishedSearchDocument/Author 聚合；
2. 仅认当前正式 Revision 的有效署名；系统「佚名」不渲染作者卡片、热力图或 Tab；
3. 本月创作热力图直接以单条 SQL 按 authored_on 分组统计（最多 31 行，零额外缓存，消除撤销延迟）；
4. 「作者发言」仅在非佚名 + 已绑定激活后台账号 + show_public_comments=True 时才调用 comments 安全聚合。
"""

from __future__ import annotations

import calendar
from datetime import date
import logging
from typing import Any

from django.conf import settings
from django.db import DatabaseError
from django.db.models import Count, Exists, OuterRef
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from wagtail.models import Page, Revision

from comments.services import get_public_comments_for_user
from discovery.detail_navigation import _get_nested_site_root_paths, resolve_site_readonly
from discovery.eligibility import apply_public_eligibility_filter
from discovery.facets import get_type_display_label
from discovery.models import PublishedSearchAuthor, PublishedSearchDocument
from discovery.registry import get_author_enabled_type_keys
from media_library.models import VisibilityChoices
from people.attribution import (
    extract_raw_author_refs_from_revision_content,
    resolve_author_attributions,
)
from people.author_identity import normalize_author_role
from people.models import Person

logger = logging.getLogger(__name__)

_ROLE_PRIORITY = {
    "primary_author": 0,
    "core_dev": 1,
    "co_author": 2,
    "editor": 3,
    "translator": 4,
    "reviewer": 5,
    "other": 6,
}
_HEATMAP_ROLES = ("primary_author", "core_dev", "co_author")


def _safe_avatar_url(person: Person | None) -> str:
    if person is None or not getattr(person, "avatar_id", None):
        return ""
    av = getattr(person, "avatar", None)
    if av is None or getattr(av, "visibility", None) != VisibilityChoices.PUBLIC:
        return ""
    return f"/media-gateway/image/{av.public_id}/"


def _author_profile_url(slug: str) -> str:
    if not getattr(settings, "AUTHOR_DISCOVERY_ENABLED", True):
        return ""
    try:
        return reverse("people:author_detail", kwargs={"slug": slug})
    except NoReverseMatch:
        return f"/authors/{slug}/"


def _compute_heat_level(count: int, max_count: int) -> int:
    if count <= 0:
        return 0
    denom = max(int(max_count or 1), 3)
    ratio = count / denom
    if ratio >= 0.75:
        return 4
    if ratio >= 0.50:
        return 3
    if ratio >= 0.25:
        return 2
    return 1


def _build_month_heatmap(person_id: int, eligible_docs, site_today: date) -> dict[str, Any]:
    month_start = date(site_today.year, site_today.month, 1)
    rows = (
        PublishedSearchAuthor.objects.filter(
            person_id=person_id,
            role__in=_HEATMAP_ROLES,
            document__in=eligible_docs.filter(
                authored_on__gte=month_start,
                authored_on__lte=site_today,
            ),
        )
        .values("document__authored_on")
        .annotate(cnt=Count("document_id", distinct=True))
    )
    day_counts: dict[date, int] = {
        r["document__authored_on"]: int(r["cnt"] or 0)
        for r in rows
        if r["document__authored_on"] is not None
    }
    max_count = max(day_counts.values()) if day_counts else 0
    month_total = sum(day_counts.values())
    active_days = sum(1 for c in day_counts.values() if c > 0)

    cal = calendar.Calendar(firstweekday=0)
    weeks: list[list[dict[str, Any]]] = []
    for week_dates in cal.monthdatescalendar(site_today.year, site_today.month):
        week_cells: list[dict[str, Any]] = []
        for d in week_dates:
            in_month = (d.year == site_today.year and d.month == site_today.month)
            is_future = in_month and d > site_today
            cnt = day_counts.get(d, 0) if (in_month and not is_future) else 0
            week_cells.append({
                "day": d.day if in_month else 0,
                "date": d.isoformat(),
                "in_month": in_month,
                "is_future": is_future,
                "is_today": d == site_today,
                "count": cnt,
                "heat_level": _compute_heat_level(cnt, max_count) if (in_month and not is_future) else 0,
            })
        weeks.append(week_cells)

    return {
        "year": site_today.year,
        "month": site_today.month,
        "month_label": f"{site_today.year}年{site_today.month}月",
        "month_total": month_total,
        "active_days": active_days,
        "weeks": weeks,
    }


def build_author_sidebar_card(page: Any, request: Any = None) -> dict[str, Any] | None:
    """
    构建详情页右侧「作者名片卡」展示数据；预览态、非公开页或仅「佚名」署名时返回 None。
    """
    if page is None:
        return None
    if request is not None and (
        getattr(request, "is_preview", False) or getattr(request, "in_preview_panel", False)
    ):
        return None

    page_id = getattr(page, "id", None)
    live_rev_id = getattr(page, "live_revision_id", None)
    if (
        not page_id
        or not live_rev_id
        or not getattr(page, "live", False)
        or getattr(page, "is_recycled", False)
        or getattr(page, "alias_of_id", None) is not None
        or not hasattr(page, "authors")
    ):
        return None

    site = resolve_site_readonly(request, page)
    if site is None:
        return None

    author_types = get_author_enabled_type_keys()
    if not author_types:
        return None

    site_root_path = site.root_page.path
    site_today = timezone.localdate()

    try:
        base_docs = PublishedSearchDocument.objects.filter(
            index_generation=1,
            is_live=True,
            is_recycled=False,
            type_key__in=author_types,
            authored_on__lte=site_today,
            root_path__startswith=site_root_path,
        )
        for nested_root in _get_nested_site_root_paths(site):
            base_docs = base_docs.exclude(root_path__startswith=nested_root)
        eligible_docs = apply_public_eligibility_filter(base_docs)

        # 1. 解析当前正式版本署名（优先 PublishedSearchAuthor，若暂缺则按 live_revision 回退）
        current_doc_id = (
            eligible_docs.filter(page_id=page_id, effective_revision_id=live_rev_id)
            .values_list("id", flat=True)
            .first()
        )
        raw_entries: list[tuple[int, str, str, int]] = []
        if current_doc_id is not None:
            for idx, r in enumerate(
                PublishedSearchAuthor.objects.filter(document_id=current_doc_id)
                .order_by("sort_order", "id")
                .values("person_id", "role", "sort_order")
            ):
                canon_role, role_label = normalize_author_role(r["role"])
                raw_entries.append((int(r["person_id"]), canon_role, role_label, int(r["sort_order"] or idx)))
        else:
            live_rev = getattr(page, "live_revision", None)
            if live_rev is None or getattr(live_rev, "id", None) != live_rev_id:
                live_rev = Revision.objects.filter(id=live_rev_id).first()
            raw_refs = extract_raw_author_refs_from_revision_content(
                getattr(live_rev, "content", None) if live_rev is not None else None
            )
            try:
                resolved = resolve_author_attributions(
                    raw_refs,
                    revision_id=live_rev_id,
                    allow_anonymous=True,
                )
            except RuntimeError:
                resolved = []
            for a in resolved:
                raw_entries.append((int(a.person_id), a.role, a.role_label, int(a.sort_order)))

        if not raw_entries:
            return None

        person_ids = list({pid for pid, _, _, _ in raw_entries})
        persons_map = {
            p.pk: p
            for p in Person.objects.select_related("avatar", "linked_user").filter(pk__in=person_ids)
        }

        # 按 Person 去重并剔除系统内置「佚名」
        dedup_entries: list[dict[str, Any]] = []
        seen_pids: set[int] = set()
        for seq, (pid, canon_role, role_label, sort_order) in enumerate(raw_entries):
            if pid in seen_pids:
                continue
            person = persons_map.get(pid)
            if person is None or person.is_system_anonymous or person.slug == "anonymous":
                continue
            seen_pids.add(pid)
            dedup_entries.append({
                "person": person,
                "role": canon_role,
                "role_label": role_label,
                "priority": _ROLE_PRIORITY.get(canon_role, 6),
                "order": (sort_order, seq),
            })

        if not dedup_entries:
            return None

        dedup_entries.sort(key=lambda x: (x["priority"], x["order"]))
        lead_item = dedup_entries[0]
        lead_person: Person = lead_item["person"]

        # 2. 本月创作热力图（零缓存直查 SQL）
        heatmap = _build_month_heatmap(lead_person.pk, eligible_docs, site_today)

        # 3. 「作者好文」：跨支持作者维度的类型取最近 5 篇（排除当前页），真实 Page 解析 URL
        art_rows = list(
            eligible_docs.filter(
                Exists(
                    PublishedSearchAuthor.objects.filter(
                        document_id=OuterRef("pk"),
                        person_id=lead_person.pk,
                    )
                )
            )
            .exclude(page_id=page_id)
            .order_by("-authored_on", "-page_id")
            .values("page_id", "title", "type_key", "authored_on")[:5]
        )
        recent_articles: list[dict[str, Any]] = []
        if art_rows:
            art_ids = [int(r["page_id"]) for r in art_rows]
            pages_by_id = {
                p.id: p
                for p in Page.objects.filter(id__in=art_ids, live=True, alias_of_id__isnull=True)
            }
            for r in art_rows:
                target = pages_by_id.get(int(r["page_id"]))
                if target is None:
                    continue
                t_site = target.get_site() if hasattr(target, "get_site") else None
                if t_site is not None and t_site.id != site.id:
                    continue
                url = (target.get_url(request) if request is not None else target.url) or ""
                if not url:
                    continue
                recent_articles.append({
                    "page_id": int(r["page_id"]),
                    "title": target.title or r["title"],
                    "url": url,
                    "authored_on": r["authored_on"],
                    "type_label": get_type_display_label(r["type_key"]),
                })

        # 4. 「作者发言」：仅当非佚名 + 已绑定激活后台账号 + 显式开启 show_public_comments 时聚合
        linked_user = getattr(lead_person, "linked_user", None)
        linked_active = bool(lead_person.linked_user_id and linked_user and getattr(linked_user, "is_active", False))
        show_comments_tab = bool(linked_active and lead_person.show_public_comments)
        recent_comments = (
            get_public_comments_for_user(
                lead_person.linked_user_id,
                site,
                request=request,
                limit=5,
            )
            if show_comments_tab
            else []
        )

        # 5. 其他合作作者紧凑徽章
        other_authors: list[dict[str, Any]] = []
        for co in dedup_entries[1:]:
            cp: Person = co["person"]
            other_authors.append({
                "name": cp.name,
                "slug": cp.slug,
                "url": _author_profile_url(cp.slug),
                "avatar_url": _safe_avatar_url(cp),
                "initials": (cp.name or "作").strip()[:1].upper(),
                "role_label": co["role_label"],
            })

        return {
            "name": lead_person.name,
            "slug": lead_person.slug,
            "url": _author_profile_url(lead_person.slug),
            "avatar_url": _safe_avatar_url(lead_person),
            "initials": (lead_person.name or "作").strip()[:1].upper(),
            "role_label": lead_item["role_label"],
            "bio": (lead_person.bio or "").strip(),
            "is_verified": linked_active,
            "verified_label": "站点核验署名" if linked_active else "",
            "heatmap": heatmap,
            "recent_articles": recent_articles,
            "show_comments_tab": show_comments_tab,
            "recent_comments": recent_comments,
            "other_authors": other_authors,
        }
    except DatabaseError as exc:
        logger.debug("build_author_sidebar_card degraded gracefully: %s", exc)
        return None
