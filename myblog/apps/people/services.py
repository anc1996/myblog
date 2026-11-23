"""
全站作者专栏大厅、远程联想与个人专栏聚合服务 (people.services)。

设计原则：
1. 单次只读快照 (REPEATABLE READ READ ONLY) 内完成活跃判定、分桶统计与 ContentQuery 查询；
2. 严格绑定当前 Wagtail Site，仅统计正式发布、未回收、未受限且 authored_on <= 站点当天的文章；
3. 零篇文章或不存在的作者统一返回友好 404 导流信息（不泄漏零文章私有档案或后台账号）；
4. 不在 GET 中写数据库、不水合 Mongo 正文，头像仅输出公开策略允许的资产网关地址。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
import logging
from typing import Any
from urllib.parse import urlencode
import uuid

from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.urls import reverse
from django.utils import timezone
from wagtail.models import Site

from content_lifecycle.access import AccessContext, AccessMode
from discovery.contracts import QuerySpec
from discovery.eligibility import apply_public_eligibility_filter
from discovery.facets import get_type_display_label
from discovery.models import ContentDiscoveryEpoch, PublishedSearchAuthor, PublishedSearchDocument
from discovery.pagination import PageWindowExceededError, build_count_meta, build_pagination_payload
from discovery.query import ContentQuery
from discovery.registry import (
    get_author_enabled_type_keys,
    get_public_type_slug_map,
    get_type_key_to_public_slug_map,
    list_type_adapters,
)
from media_library.models import VisibilityChoices
from people.author_identity import normalize_author_role, roles_for_filter_key
from people.models import Person

logger = logging.getLogger(__name__)

DIRECTORY_PAGE_SIZE = 48
DEFAULT_DETAIL_PAGE_SIZE = 20
MAX_DETAIL_PAGE_SIZE = 50
TRUE_VALUES = {"1", "true", "on", "yes"}
ALLOWED_SORTS = {"relevance", "authored_desc", "authored_asc"}

ROLE_FILTER_CHIPS = (
    ("all", "全部参与"),
    ("major", "主笔 / 主要作者"),
    ("co_author", "共同作者"),
    ("other_contrib", "技术审校 / 其他"),
)

SORT_LABELS = {
    "authored_desc": "成文日期：新 → 旧",
    "authored_asc": "成文日期：旧 → 新",
    "relevance": "按匹配相关度",
}


@contextmanager
def _readonly_snapshot():
    """与 ContentQuery 共用同一只读可重复读快照语义，避免并发更新时计数与列表撕裂。"""
    is_outer_testcase = False
    if connection.in_atomic_block:
        if all(getattr(b, "_from_testcase", False) for b in connection.atomic_blocks):
            is_outer_testcase = True
        elif getattr(connection, "_in_discovery_snapshot", False):
            yield
            return
        else:
            raise RuntimeError("people.services cannot run inside a foreign read-write atomic block.")

    should_manage_atomic = not connection.in_atomic_block
    atomic_ctx = transaction.atomic() if should_manage_atomic else transaction.atomic(savepoint=is_outer_testcase)
    with atomic_ctx:
        prev_flag = getattr(connection, "_in_discovery_snapshot", False)
        connection._in_discovery_snapshot = True
        try:
            if connection.vendor == "postgresql" and should_manage_atomic and not is_outer_testcase:
                with connection.cursor() as cur:
                    cur.execute("SET LOCAL TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL statement_timeout = '2000ms';")
            yield
        finally:
            connection._in_discovery_snapshot = prev_flag


def _safe_person_avatar_url(person: Person | None) -> str:
    """仅返回明确公开的头像网关链接，绝不暴露受限头像或账户头像。"""
    if person is None or not getattr(person, "avatar_id", None):
        return ""
    avatar = getattr(person, "avatar", None)
    if avatar is None:
        return ""
    if getattr(avatar, "visibility", None) != VisibilityChoices.PUBLIC:
        return ""
    return f"/media-gateway/image/{avatar.public_id}/"


def _get_site_eligible_author_docs(site: Site | None, site_today: date):
    """返回当前 Site 下所有满足公开资格、支持作者维度且成文日期不超过今天的文档集合。"""
    if site is None:
        return PublishedSearchDocument.objects.none()
    author_types = get_author_enabled_type_keys()
    if not author_types:
        return PublishedSearchDocument.objects.none()
    qs = PublishedSearchDocument.objects.filter(
        site_id=site.id,
        is_live=True,
        is_recycled=False,
        type_key__in=author_types,
        authored_on__lte=site_today,
    )
    return apply_public_eligibility_filter(qs)


def _get_author_type_order() -> list[tuple[str, str, str]]:
    """返回所有已声明接入作者维度的内容类型有序清单: [(type_key, public_slug, label), ...]。"""
    preferred_order = {"blog_page": 0, "guide": 1, "project": 2}
    entries: list[tuple[int, str, str, str]] = []
    for adapter in list_type_adapters():
        caps = getattr(adapter, "capabilities", None)
        if not caps or "author" not in (getattr(caps, "supported_dimensions", ()) or ()):
            continue
        tk = adapter.type_key
        pub_slug = getattr(caps, "public_type_slug", "") or tk
        label = get_type_display_label(tk)
        entries.append((preferred_order.get(tk, 50), tk, pub_slug, label))
    entries.sort(key=lambda item: (item[0], item[1]))
    return [(tk, pub_slug, label) for _, tk, pub_slug, label in entries]


def _plain_excerpt(text: str, max_len: int = 120) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


@dataclass
class AuthorCardVM:
    id: int
    name: str
    slug: str
    initial: str
    initial_group_rank: int
    sort_key: str
    avatar_url: str
    bio: str
    bio_excerpt: str
    total_count: int
    type_badges: list[dict[str, Any]]
    url: str
    is_anonymous: bool = False


def _build_active_author_cards(site: Site | None, site_today: date) -> list[AuthorCardVM]:
    """在当前只读快照内批量统计当前站点全部活跃作者卡片。"""
    eligible_docs = _get_site_eligible_author_docs(site, site_today)
    total_rows = list(
        PublishedSearchAuthor.objects.filter(document__in=eligible_docs)
        .values("person_id")
        .annotate(total=Count("document_id", distinct=True))
        .filter(total__gte=1)
    )
    if not total_rows:
        return []

    totals_by_pid = {int(r["person_id"]): int(r["total"]) for r in total_rows}
    by_type_rows = list(
        PublishedSearchAuthor.objects.filter(document__in=eligible_docs)
        .values("person_id", "document__type_key")
        .annotate(cnt=Count("document_id", distinct=True))
    )
    type_counts_map: dict[int, dict[str, int]] = {}
    for r in by_type_rows:
        pid = int(r["person_id"])
        tk = r["document__type_key"]
        type_counts_map.setdefault(pid, {})[tk] = int(r["cnt"] or 0)

    ordered_types = _get_author_type_order()
    persons = list(
        Person.objects.filter(pk__in=list(totals_by_pid.keys()))
        .select_related("avatar")
        .order_by("initial_group_rank", "initial", "sort_key", "id")
    )

    cards: list[AuthorCardVM] = []
    for p in persons:
        total_cnt = totals_by_pid.get(p.pk, 0)
        if total_cnt <= 0:
            continue
        p_types = type_counts_map.get(p.pk, {})
        badges = [
            {"type_key": tk, "slug": pub_slug, "label": label, "count": p_types.get(tk, 0)}
            for tk, pub_slug, label in ordered_types
            if p_types.get(tk, 0) > 0
        ]
        cards.append(
            AuthorCardVM(
                id=p.pk,
                name=p.name,
                slug=p.slug,
                initial=p.initial or "#",
                initial_group_rank=p.initial_group_rank,
                sort_key=p.sort_key or p.name.lower(),
                avatar_url=_safe_person_avatar_url(p),
                bio=p.bio or "",
                bio_excerpt=_plain_excerpt(p.bio, 110),
                total_count=total_cnt,
                type_badges=badges,
                url=reverse("people:author_detail", kwargs={"slug": p.slug}),
                is_anonymous=bool(p.is_system_anonymous),
            )
        )
    return cards


def get_author_directory_data(request) -> dict[str, Any]:
    """构建 `/authors/` 作者专栏大厅上下文数据。"""
    site = Site.find_for_request(request)
    site_today = timezone.localdate()
    q = (request.GET.get("q") or "").strip()[:100]
    raw_initial = (request.GET.get("initial") or "").strip().upper()
    if raw_initial and (len(raw_initial) != 1 or (raw_initial != "#" and not ("A" <= raw_initial <= "Z"))):
        raw_initial = ""

    try:
        page_num = max(1, int((request.GET.get("page") or "1").strip()))
    except (ValueError, TypeError):
        page_num = 1

    with _readonly_snapshot():
        all_cards = _build_active_author_cards(site, site_today)

    available_initials_set = {c.initial for c in all_cards}
    letters = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + ["#"]
    initial_nav = []
    for ch in letters:
        has_items = ch in available_initials_set
        params = {}
        if q:
            params["q"] = q
        if ch != raw_initial:
            params["initial"] = ch
        qs = urlencode(params)
        initial_nav.append({
            "letter": ch,
            "has_items": has_items,
            "is_active": raw_initial == ch,
            "url": f"{reverse('people:author_directory')}?{qs}" if qs else reverse("people:author_directory"),
        })

    show_top_authors = not q and not raw_initial and page_num == 1
    top_authors = (
        sorted(all_cards, key=lambda c: (-c.total_count, c.sort_key, c.id))[:12]
        if show_top_authors
        else []
    )

    filtered = all_cards
    if q:
        q_low = q.lower()
        filtered = [
            c for c in filtered
            if q_low in c.name.lower() or q_low in (c.bio or "").lower()
        ]
    if raw_initial:
        filtered = [c for c in filtered if c.initial == raw_initial]

    paginator = Paginator(filtered, DIRECTORY_PAGE_SIZE)
    page_obj = paginator.get_page(page_num)

    grouped_authors: list[dict[str, Any]] = []
    group_map: dict[str, list[AuthorCardVM]] = {}
    for card in page_obj.object_list:
        if card.initial not in group_map:
            group_map[card.initial] = []
            grouped_authors.append({"initial": card.initial, "items": group_map[card.initial]})
        group_map[card.initial].append(card)

    base_params = {}
    if q:
        base_params["q"] = q
    if raw_initial:
        base_params["initial"] = raw_initial
    if page_obj.number > 1:
        base_params["page"] = str(page_obj.number)
    dir_url = reverse("people:author_directory")
    canonical_full_url = f"{dir_url}?{urlencode(base_params)}" if base_params else dir_url

    def _page_url(target_page: int) -> str:
        p = dict(base_params)
        if target_page > 1:
            p["page"] = str(target_page)
        else:
            p.pop("page", None)
        qs = urlencode(p)
        return f"{dir_url}?{qs}" if qs else dir_url

    return {
        "total_active_authors": len(all_cards),
        "filtered_count": len(filtered),
        "search_query": q,
        "selected_initial": raw_initial,
        "initial_nav": initial_nav,
        "show_top_authors": show_top_authors,
        "top_authors": top_authors,
        "grouped_authors": grouped_authors,
        "page_obj": page_obj,
        "prev_page_url": _page_url(page_obj.previous_page_number()) if page_obj.has_previous() else "",
        "next_page_url": _page_url(page_obj.next_page_number()) if page_obj.has_next() else "",
        "canonical_url": dir_url,
        "full_url": canonical_full_url,
        "reset_url": dir_url,
    }


def get_author_lookup_data(request, *, limit: int = 20) -> dict[str, Any]:
    """为搜索框与筛选下拉提供活跃作者联想：仅返回 {value, label} 与 more。"""
    site = Site.find_for_request(request)
    site_today = timezone.localdate()
    raw_q = (request.GET.get("q") or "").strip()[:100]
    safe_limit = max(1, min(int(limit or 20), 20))

    with _readonly_snapshot():
        eligible_docs = _get_site_eligible_author_docs(site, site_today)
        active_qs = (
            PublishedSearchAuthor.objects.filter(document__in=eligible_docs)
            .values("person_id")
            .annotate(cnt=Count("document_id", distinct=True))
            .filter(cnt__gte=1)
        )
        counts_map = {int(r["person_id"]): int(r["cnt"]) for r in active_qs}
        if not counts_map:
            return {"items": [], "more": False}

        persons_qs = Person.objects.filter(pk__in=list(counts_map.keys()))
        if raw_q:
            persons_qs = persons_qs.filter(Q(name__icontains=raw_q) | Q(slug__icontains=raw_q))
            candidates = list(persons_qs.order_by("initial_group_rank", "initial", "sort_key", "id")[: safe_limit + 1])
        else:
            all_active = list(persons_qs)
            all_active.sort(key=lambda p: (-counts_map.get(p.pk, 0), p.sort_key or p.name.lower(), p.pk))
            candidates = all_active[: safe_limit + 1]

    has_more = len(candidates) > safe_limit
    items = [{"value": p.slug, "label": p.name} for p in candidates[:safe_limit]]
    return {"items": items, "more": has_more}


def resolve_author_for_detail(request, raw_slug: str) -> tuple[str, Any]:
    """
    解析作者专栏入口：
    - 返回 ("ok", Person)
    - 返回 ("redirect", canonical_slug)
    - 返回 ("missing", top_5_active_cards)
    """
    site = Site.find_for_request(request)
    site_today = timezone.localdate()
    cleaned = (raw_slug or "").strip()

    with _readonly_snapshot():
        person = Person.objects.select_related("avatar").filter(slug__iexact=cleaned).first()
        needs_redirect = False
        if person is not None:
            if person.slug != cleaned:
                needs_redirect = True
        else:
            try:
                uid = uuid.UUID(cleaned)
                person = Person.objects.select_related("avatar").filter(public_id=uid).first()
                if person is not None:
                    needs_redirect = True
            except (ValueError, TypeError):
                if cleaned.isdigit():
                    pk_val = int(cleaned)
                    if 1 <= pk_val <= 9223372036854775807:
                        person = Person.objects.select_related("avatar").filter(pk=pk_val).first()
                        if person is not None:
                            needs_redirect = True

        eligible_docs = _get_site_eligible_author_docs(site, site_today)
        is_active = False
        if person is not None:
            is_active = PublishedSearchAuthor.objects.filter(
                person_id=person.pk,
                document__in=eligible_docs,
            ).exists()

        if person is not None and is_active:
            if needs_redirect:
                return ("redirect", person.slug)
            return ("ok", person)

        active_cards = _build_active_author_cards(site, site_today)
        top_5 = sorted(active_cards, key=lambda c: (-c.total_count, c.sort_key, c.id))[:5]
        return ("missing", top_5)


def build_author_detail_url(
    base_url: str,
    base_state: dict[str, Any],
    *,
    set_params: dict[str, Any] | None = None,
    remove_keys: set[str] | None = None,
    reset_pagination: bool = True,
) -> str:
    """构造作者专栏主页组合筛选 URL，切换条件时保留其他已选参数并重置分页。"""
    state = {
        "type": base_state.get("type", ""),
        "role": base_state.get("role", ""),
        "q": base_state.get("q", ""),
        "title_only": base_state.get("title_only", False),
        "date_from": base_state.get("date_from", ""),
        "date_to": base_state.get("date_to", ""),
        "sort": base_state.get("sort", ""),
        "page_size": base_state.get("page_size"),
        "secondary": base_state.get("secondary", "none"),
        "cursor": base_state.get("cursor"),
        "page": base_state.get("page"),
        "seek_date": base_state.get("seek_date", ""),
        "seek_direction": base_state.get("seek_direction", ""),
    }
    if reset_pagination:
        state["cursor"] = None
        state["page"] = None
        state["seek_date"] = ""
        state["seek_direction"] = ""
    if remove_keys:
        for rk in remove_keys:
            if rk == "title_only":
                state[rk] = False
            elif rk in ("page_size", "cursor", "page"):
                state[rk] = None
            elif rk == "secondary":
                state[rk] = "none"
            else:
                state[rk] = ""
    if set_params:
        for sk, sv in set_params.items():
            state[sk] = sv

    pairs: list[tuple[str, str]] = []
    if state["type"] and state["type"] != "all":
        pairs.append(("type", state["type"]))
    if state["role"] and state["role"] != "all":
        pairs.append(("role", state["role"]))
    if state["q"]:
        pairs.append(("q", state["q"]))
    if state["title_only"]:
        pairs.append(("title_only", "1"))
    if state["date_from"]:
        pairs.append(("date_from", state["date_from"]))
    if state["date_to"]:
        pairs.append(("date_to", state["date_to"]))
    if state["sort"]:
        pairs.append(("sort", state["sort"]))
    if state["page_size"] and state["page_size"] != DEFAULT_DETAIL_PAGE_SIZE:
        pairs.append(("page_size", str(state["page_size"])))
    if state["secondary"] == "words":
        pairs.append(("secondary", "words"))
    if state["cursor"]:
        pairs.append(("cursor", state["cursor"]))
    elif state["seek_direction"] == "oldest":
        pairs.append(("seek_direction", "oldest"))
    elif state["seek_date"]:
        pairs.append(("seek_date", state["seek_date"]))
    elif state["page"] and int(state["page"]) > 1:
        pairs.append(("page", str(state["page"])))

    qs = urlencode(pairs)
    return f"{base_url}?{qs}" if qs else base_url


def get_author_detail_data(request, person: Person) -> tuple[dict[str, Any], int]:
    """
    在单一只读可重复读快照内，计算作者个人专栏的名片总篇数、跨类型 Tab 数量、
    角色筛选、混合搜索结果与千万级双模分页。
    返回 (context_dict, http_status)。
    """
    site = Site.find_for_request(request)
    site_id = site.id if site is not None else None
    site_today = timezone.localdate()
    canonical_url = reverse("people:author_detail", kwargs={"slug": person.slug})

    ordered_types = _get_author_type_order()
    pub_to_tk = {pub: tk for tk, pub, _ in ordered_types}
    tk_to_pub = {tk: pub for tk, pub, _ in ordered_types}
    author_type_keys = [tk for tk, _, _ in ordered_types]

    raw_type = (request.GET.get("type") or "").strip().lower()
    if raw_type in ("", "all"):
        selected_type_pub = "all"
        selected_type_keys = author_type_keys
    elif raw_type in pub_to_tk:
        selected_type_pub = raw_type
        selected_type_keys = [pub_to_tk[raw_type]]
    elif raw_type in tk_to_pub:
        selected_type_pub = tk_to_pub[raw_type]
        selected_type_keys = [raw_type]
    else:
        return (
            {
                "person": person,
                "canonical_url": canonical_url,
                "error_message": "不支持的内容类型筛选参数。",
            },
            400,
        )

    raw_role = (request.GET.get("role") or "").strip().lower()
    if raw_role in ("", "all"):
        selected_role = "all"
        effective_role = None
    elif roles_for_filter_key(raw_role):
        selected_role = raw_role
        effective_role = raw_role
    else:
        return (
            {
                "person": person,
                "canonical_url": canonical_url,
                "error_message": "不支持的角色筛选参数。",
            },
            400,
        )

    raw_q = (request.GET.get("q") or "").strip()[:200]
    title_only = (request.GET.get("title_only") or "").strip().lower() in TRUE_VALUES

    raw_df = (request.GET.get("date_from") or "").strip()
    raw_dt = (request.GET.get("date_to") or "").strip()
    parsed_df: date | None = None
    parsed_dt: date | None = None
    try:
        if raw_df:
            parsed_df = date.fromisoformat(raw_df)
        if raw_dt:
            parsed_dt = date.fromisoformat(raw_dt)
    except (ValueError, TypeError):
        return (
            {
                "person": person,
                "canonical_url": canonical_url,
                "error_message": "成文日期格式无效，请使用 YYYY-MM-DD 格式。",
            },
            400,
        )
    if parsed_df and parsed_dt and parsed_df > parsed_dt:
        return (
            {
                "person": person,
                "canonical_url": canonical_url,
                "error_message": "开始日期不能晚于结束日期。",
            },
            400,
        )

    raw_sort = (request.GET.get("sort") or "").strip().lower()
    explicit_sort = raw_sort if raw_sort in ALLOWED_SORTS else ""
    if not raw_q and explicit_sort == "relevance":
        explicit_sort = "authored_desc"
    effective_sort = explicit_sort or ("relevance" if raw_q else "authored_desc")

    secondary = (request.GET.get("secondary") or "none").strip()
    if secondary not in ("none", "words"):
        secondary = "none"

    cursor = (request.GET.get("cursor") or "").strip() or None
    page_num: int | None = None
    seek_date: date | None = None
    seek_direction = "date"
    if cursor is None:
        raw_dir = (request.GET.get("seek_direction") or "").strip().lower()
        if raw_dir == "oldest":
            seek_direction = "oldest"
        else:
            raw_sd = (request.GET.get("seek_date") or "").strip()
            if raw_sd:
                try:
                    seek_date = date.fromisoformat(raw_sd)
                except (ValueError, TypeError):
                    return (
                        {
                            "person": person,
                            "canonical_url": canonical_url,
                            "error_message": "定位日期格式无效，请使用 YYYY-MM-DD。",
                        },
                        400,
                    )
                if effective_sort == "relevance":
                    effective_sort = "authored_desc"
                    explicit_sort = "authored_desc"
        if seek_direction != "oldest" and seek_date is None:
            raw_page = (request.GET.get("page") or "").strip()
            if raw_page:
                try:
                    parsed_p = int(raw_page)
                    page_num = parsed_p if parsed_p >= 1 else 1
                except (ValueError, TypeError):
                    page_num = None

    raw_ps = (request.GET.get("page_size") or "").strip()
    try:
        page_size = max(1, min(int(raw_ps) if raw_ps else DEFAULT_DETAIL_PAGE_SIZE, MAX_DETAIL_PAGE_SIZE))
    except (ValueError, TypeError):
        page_size = DEFAULT_DETAIL_PAGE_SIZE

    base_state = {
        "type": selected_type_pub,
        "role": selected_role,
        "q": raw_q,
        "title_only": title_only,
        "date_from": parsed_df.isoformat() if parsed_df else "",
        "date_to": parsed_dt.isoformat() if parsed_dt else "",
        "sort": explicit_sort,
        "page_size": page_size,
        "secondary": secondary,
        "cursor": cursor,
        "page": page_num,
        "seek_date": seek_date.isoformat() if seek_date else "",
        "seek_direction": seek_direction if seek_direction == "oldest" else "",
    }

    access_context = AccessContext(user=request.user, mode=AccessMode.PUBLIC)
    effective_date_to = min(parsed_dt, site_today) if parsed_dt else site_today

    http_status = 200
    error_message: str | None = None
    result = None
    card_total_count = 0
    tab_counts: dict[str, int] = {}
    all_tab_count = 0
    role_of_doc: dict[int, tuple[str, str]] = {}

    with _readonly_snapshot():
        eligible_docs = _get_site_eligible_author_docs(site, site_today)
        card_total_count = PublishedSearchAuthor.objects.filter(
            person_id=person.pk,
            document__in=eligible_docs,
        ).values("document_id").distinct().count()

        main_spec = QuerySpec(
            q=raw_q or None,
            search_fields="title" if title_only else "all",
            site_id=site_id,
            type_keys=selected_type_keys,
            author_slugs=[person.slug],
            author_role=effective_role,
            date_from=parsed_df,
            date_to=effective_date_to,
            sort=effective_sort,
            secondary=secondary,
            cursor=cursor,
            page=page_num,
            page_size=page_size,
            seek_date=seek_date,
            seek_direction=seek_direction,
            count_mode="exact",
            use_keyset=True,
            facets=["types"] if not raw_q else [],
            compute_facets_flag=not bool(raw_q),
        )
        try:
            result = ContentQuery.execute(main_spec, access_context)
        except PageWindowExceededError as exc:
            http_status = 422
            error_message = (
                f"页码 {exc.requested_page} 超出直跳窗口上限（最多支持直跳第 {exc.max_jump_page} 页），"
                "请使用「下一页」游标或按成文日期定位。"
            )
        except ValueError:
            http_status = 400
            error_message = "无效的分页游标参数，请重置或返回第 1 页重试。"

        if not raw_q:
            if result is not None and "types" in (result.facets or {}):
                for item in result.facets["types"]:
                    tab_counts[item["key"]] = int(item["count"] or 0)
                all_tab_count = sum(tab_counts.get(tk, 0) for tk in author_type_keys)
            else:
                all_tab_count = card_total_count
        else:
            # 关键词检索时，A/B/C 门槛依赖各自候选集，对每个类型 Tab 精确计算实际会命中的条数
            all_spec = QuerySpec(
                q=raw_q,
                search_fields="title" if title_only else "all",
                site_id=site_id,
                type_keys=author_type_keys,
                author_slugs=[person.slug],
                author_role=effective_role,
                date_from=parsed_df,
                date_to=effective_date_to,
                sort=effective_sort,
                secondary=secondary,
                page_size=1,
                count_mode="exact",
                compute_facets_flag=False,
            )
            all_tab_count = (
                result.total_count
                if (result is not None and selected_type_pub == "all")
                else ContentQuery.execute(all_spec, access_context).total_count
            )
            for tk, pub_slug, _ in ordered_types:
                if result is not None and selected_type_pub == pub_slug:
                    tab_counts[tk] = result.total_count
                else:
                    t_spec = QuerySpec(
                        q=raw_q,
                        search_fields="title" if title_only else "all",
                        site_id=site_id,
                        type_keys=[tk],
                        author_slugs=[person.slug],
                        author_role=effective_role,
                        date_from=parsed_df,
                        date_to=effective_date_to,
                        sort=effective_sort,
                        secondary=secondary,
                        page_size=1,
                        count_mode="exact",
                        compute_facets_flag=False,
                    )
                    tab_counts[tk] = ContentQuery.execute(t_spec, access_context).total_count

        if result is not None and result.items:
            page_ids = [it.page_id for it in result.items]
            for row in PublishedSearchAuthor.objects.filter(
                document__page_id__in=page_ids,
                document__index_generation=1,
                author_slug=person.slug,
            ).values("document__page_id", "role"):
                _, r_label = normalize_author_role(row["role"])
                role_of_doc[int(row["document__page_id"])] = (row["role"], r_label)

    if result is not None:
        items = result.items
        total_count = result.total_count
        next_cursor = result.next_cursor
        previous_cursor = result.previous_cursor
        cursor_reset = result.cursor_reset
        expansion_used = result.expansion_used
        secondary_used = result.secondary_used
        pagination_payload = result.pagination
        count_meta = result.count_meta
    else:
        items = []
        total_count = 0
        next_cursor = None
        previous_cursor = None
        cursor_reset = False
        expansion_used = False
        secondary_used = False
        pagination_payload = build_pagination_payload(
            mode="shallow_page",
            current_page=None,
            page_size=page_size,
            total_count=0,
            count_kind="exact",
            has_next=False,
            has_previous=False,
            next_cursor=None,
            previous_cursor=None,
        )
        count_meta = build_count_meta(value=0, kind="exact")

    type_tabs = [
        {
            "key": "all",
            "label": "全部",
            "count": all_tab_count,
            "is_active": selected_type_pub == "all",
            "url": build_author_detail_url(canonical_url, base_state, set_params={"type": "all"}, reset_pagination=True),
        }
    ]
    for tk, pub_slug, label in ordered_types:
        type_tabs.append({
            "key": pub_slug,
            "label": label,
            "count": tab_counts.get(tk, 0),
            "is_active": selected_type_pub == pub_slug,
            "url": build_author_detail_url(canonical_url, base_state, set_params={"type": pub_slug}, reset_pagination=True),
        })

    role_chips = []
    for r_key, r_label in ROLE_FILTER_CHIPS:
        role_chips.append({
            "key": r_key,
            "label": r_label,
            "is_active": selected_role == r_key,
            "url": build_author_detail_url(canonical_url, base_state, set_params={"role": r_key}, reset_pagination=True),
        })

    a_count = len([it for it in items if it.match_mode == "A"])
    show_expand_button = (1 <= a_count < 5) and not secondary_used and bool(raw_q)
    show_expand_url = (
        build_author_detail_url(canonical_url, base_state, set_params={"secondary": "words"}, reset_pagination=True)
        if show_expand_button
        else ""
    )

    active_filter_pills: list[dict[str, Any]] = []
    if selected_type_pub != "all":
        type_label = get_type_display_label(pub_to_tk.get(selected_type_pub, selected_type_pub))
        active_filter_pills.append({
            "key": "type",
            "label": "类型",
            "value": type_label,
            "remove_url": build_author_detail_url(canonical_url, base_state, set_params={"type": "all"}, reset_pagination=True),
        })
    if selected_role != "all":
        role_map = dict(ROLE_FILTER_CHIPS)
        active_filter_pills.append({
            "key": "role",
            "label": "角色",
            "value": role_map.get(selected_role, normalize_author_role(selected_role)[1]),
            "remove_url": build_author_detail_url(canonical_url, base_state, set_params={"role": "all"}, reset_pagination=True),
        })
    if raw_q:
        active_filter_pills.append({
            "key": "q",
            "label": "关键词",
            "value": raw_q,
            "remove_url": build_author_detail_url(canonical_url, base_state, remove_keys={"q", "secondary"}, reset_pagination=True),
        })
    if title_only:
        active_filter_pills.append({
            "key": "title_only",
            "label": "范围",
            "value": "仅搜标题",
            "remove_url": build_author_detail_url(canonical_url, base_state, remove_keys={"title_only"}, reset_pagination=True),
        })
    if parsed_df or parsed_dt:
        active_filter_pills.append({
            "key": "date_range",
            "label": "成文日期",
            "value": f"{parsed_df.isoformat() if parsed_df else '不限'} ~ {parsed_dt.isoformat() if parsed_dt else '至今'}",
            "remove_url": build_author_detail_url(canonical_url, base_state, remove_keys={"date_from", "date_to"}, reset_pagination=True),
        })
    if explicit_sort:
        active_filter_pills.append({
            "key": "sort",
            "label": "排序",
            "value": SORT_LABELS.get(explicit_sort, explicit_sort),
            "remove_url": build_author_detail_url(canonical_url, base_state, remove_keys={"sort"}, reset_pagination=True),
        })

    display_items: list[dict[str, Any]] = []
    for it in items:
        _, role_label = role_of_doc.get(it.page_id, ("", ""))
        display_items.append({
            "item": it,
            "type_label": get_type_display_label(it.type_key),
            "author_role_label": role_label,
        })

    pagination_base_url = build_author_detail_url(canonical_url, base_state, reset_pagination=True)
    pagination_qs = pagination_base_url.split("?", 1)[1] if "?" in pagination_base_url else ""

    pagination_vm = {
        "items": items,
        "total_count": total_count,
        "count_meta": count_meta,
        "pagination": pagination_payload,
        "next_cursor": next_cursor,
        "previous_cursor": previous_cursor,
        "canonical_url": canonical_url,
        "query_string": pagination_qs,
        "active_filters": {"seek_date": seek_date.isoformat() if seek_date else ""},
        "error_message": error_message,
    }

    full_url = build_author_detail_url(canonical_url, base_state, reset_pagination=False)
    search_in_site_url = f"{reverse('search')}?{urlencode({'author': person.slug})}"

    return (
        {
            "person": person,
            "avatar_url": _safe_person_avatar_url(person),
            "public_email": (person.email or "") if not person.is_system_anonymous else "",
            "card_total_count": card_total_count,
            "rss_url": reverse("discovery:feed_author", kwargs={"slug": person.slug}),
            "atom_url": reverse("discovery:feed_atom_author", kwargs={"slug": person.slug}),
            "search_in_site_url": search_in_site_url,
            "directory_url": reverse("people:author_directory"),
            "canonical_url": canonical_url,
            "full_url": full_url,
            "selected_type": selected_type_pub,
            "selected_role": selected_role,
            "search_query": raw_q,
            "title_only": title_only,
            "date_from": parsed_df.isoformat() if parsed_df else "",
            "date_to": parsed_dt.isoformat() if parsed_dt else "",
            "effective_sort": effective_sort,
            "explicit_sort": explicit_sort,
            "type_tabs": type_tabs,
            "role_chips": role_chips,
            "active_filter_pills": active_filter_pills,
            "display_items": display_items,
            "items": items,
            "total_count": total_count,
            "next_cursor": next_cursor,
            "previous_cursor": previous_cursor,
            "cursor_reset": cursor_reset,
            "expansion_used": expansion_used,
            "secondary_used": secondary_used,
            "show_expand_button": show_expand_button,
            "show_expand_url": show_expand_url,
            "pagination_vm": pagination_vm,
            "error_message": error_message,
            "active_filters": {
                "type": selected_type_pub,
                "role": selected_role,
                "q": raw_q,
                "title_only": title_only,
                "date_from": parsed_df.isoformat() if parsed_df else "",
                "date_to": parsed_dt.isoformat() if parsed_dt else "",
                "sort": effective_sort,
                "explicit_sort": explicit_sort,
            },
        },
        http_status,
    )
