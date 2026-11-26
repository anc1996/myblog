from datetime import date, timedelta
import logging
import re
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.db import DatabaseError
from django.db.models import Count, Q
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from taggit.models import Tag
from wagtail.models import Site

from content_lifecycle.access import AccessContext, AccessMode
from discovery.contracts import QuerySpec
from discovery.detail_navigation import _get_nested_site_root_paths
from discovery.eligibility import apply_public_eligibility_filter
from discovery.facets import (
    _tag_name_priority,
    get_type_display_label,
)
from discovery.models import PublishedSearchAuthor, PublishedSearchDocument, PublishedSearchTag
from discovery.navigation import (
    build_tag_detail_url,
    get_site_directory_tree,
    resolve_site_for_navigation,
    resolve_tag_index_page,
)
from discovery.pagination import (
    PageWindowExceededError,
    build_count_meta,
    build_pagination_payload,
)
from discovery.query import ContentQuery
from discovery.registry import get_archive_enabled_type_keys, get_author_enabled_type_keys
from people.author_identity import normalize_author_role, roles_for_filter_key
from people.models import Person

logger = logging.getLogger(__name__)


TRUE_VALUES = {"1", "true", "on", "yes"}
ALLOWED_SORTS = {"relevance", "authored_desc", "authored_asc"}
ALLOWED_DAYS = {"7", "30", "365"}
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
MAX_AUTHOR_FILTERS = 10
_VALID_SEARCH_AUTHOR_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

AUTHOR_ROLE_OPTIONS = (
    ("", "全部角色"),
    ("major", "主笔 / 主要作者"),
    ("co_author", "共同作者"),
    ("other_contrib", "技术审校 / 其他"),
)

AUTHOR_ROLE_LABELS = {
    "major": "主笔 / 主要作者",
    "co_author": "共同作者",
    "other_contrib": "技术审校 / 其他",
    "primary_author": "主要作者",
    "core_dev": "核心研发",
    "reviewer": "技术审校",
    "translator": "翻译",
    "editor": "责任编辑",
    "other": "其他贡献",
    "anonymous": "佚名",
}

UNIFIED_TYPE_OPTIONS = (
    ("", "全部"),
    ("blog_page", "博客文章"),
    ("guide", "专题指南"),
    ("project", "开源项目"),
    ("navigation", "栏目导航页"),
)

DAYS_OPTIONS = (
    ("", "全部时间"),
    ("7", "近 7 天"),
    ("30", "近 30 天"),
    ("365", "近 1 年"),
)

SORT_LABELS = {
    "relevance": "按相关度",
    "authored_desc": "成文日期：新 → 旧",
    "authored_asc": "成文日期：旧 → 新",
}
MAX_SIDEBAR_TAG_FACETS = 20


def _query_public_tag_items(
    q: str,
    *,
    site: Site | None,
    limit: int = 25,
    exclude_nested_sites: bool = False,
) -> list[dict[str, Any]]:
    """只读聚合当前站点公开已发布文档的标签计数（大小写去重并排除草稿/回收/受限内容）。"""
    eligible_docs = PublishedSearchDocument.objects.filter(
        is_live=True,
        is_recycled=False,
        index_generation=1,
    )
    if site is not None:
        eligible_docs = eligible_docs.filter(site_id=site.id)
        if exclude_nested_sites and getattr(site, "root_page", None) and getattr(site.root_page, "path", None):
            eligible_docs = eligible_docs.filter(root_path__startswith=site.root_page.path)
            for nested_path in _get_nested_site_root_paths(site):
                eligible_docs = eligible_docs.exclude(root_path__startswith=nested_path)
    eligible_docs = apply_public_eligibility_filter(eligible_docs)

    tag_qs = PublishedSearchTag.objects.filter(document__in=eligible_docs)
    if q:
        tag_qs = tag_qs.filter(tag_name__icontains=q)

    rows = list(
        tag_qs.values("tag_id", "tag_name", "tag_slug")
        .annotate(count=Count("document_id", distinct=True))
        .order_by("-count", "tag_name")[:120]
    )
    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        raw_name = (r["tag_name"] or "").strip()
        norm_key = raw_name.lower() or f"id:{r['tag_id']}"
        slug_val = (r["tag_slug"] or "").strip() or str(r["tag_id"])
        cnt = int(r["count"] or 0)
        prio = _tag_name_priority(raw_name, cnt)
        if norm_key not in merged:
            merged[norm_key] = {
                "value": slug_val,
                "name": raw_name,
                "count": cnt,
                "_prio": prio,
            }
        else:
            cur = merged[norm_key]
            cur["count"] += cnt
            if prio > cur["_prio"]:
                cur["_prio"] = prio
                cur["name"] = raw_name
                cur["value"] = slug_val

    return sorted(merged.values(), key=lambda x: (-x["count"], x["name"].lower()))[:limit]


@never_cache
@require_GET
def tag_lookup_view(request):
    """全站搜索页标签远程联想接口：只读返回当前站点公开已发布内容的活跃标签（最多 25 条）。"""
    q = (request.GET.get("q") or "").strip()[:80]
    site = Site.find_for_request(request)
    items = _query_public_tag_items(q, site=site, limit=25)
    return JsonResponse({
        "items": [
            {"value": it["value"], "label": f"#{it['name']} ({it['count']})"}
            for it in items
        ]
    })


@require_GET
def quick_search_view(request):
    """
    全站 Ctrl+K 快捷搜索只读接口（B-04）：
    按当前站点安全返回最多 5 条栏目、5 条文章与 5 条标签；
    任何 DatabaseError 或运行时异常均安全降级为空列表，不跨站且不泄漏受限/草稿/回收内容。
    """
    raw_q = request.GET.get("q")
    q = (str(raw_q) if raw_q is not None else "").replace("\x00", "").strip()[:80]

    directories: list[dict[str, Any]] = []
    articles: list[dict[str, Any]] = []
    tags: list[dict[str, Any]] = []

    try:
        site = resolve_site_for_navigation(request)
    except Exception:
        logger.warning("quick_search_view failed to resolve site", exc_info=True)
        site = None

    if site is not None:
        # 1. 栏目匹配：空查询返回前 5 个一级主栏目；非空查询展平目录树按标题大小写不敏感子串匹配
        try:
            tree = get_site_directory_tree(request, site=site)
            if not q:
                for node in tree[:5]:
                    directories.append({
                        "title": str(node.get("title") or ""),
                        "url": str(node.get("url") or ""),
                        "article_count": int(node.get("article_count") or 0),
                    })
            else:
                q_lower = q.lower()
                queue = list(tree)
                while queue:
                    node = queue.pop(0)
                    title = str(node.get("title") or "")
                    if q_lower in title.lower():
                        directories.append({
                            "title": title,
                            "url": str(node.get("url") or ""),
                            "article_count": int(node.get("article_count") or 0),
                        })
                        if len(directories) >= 5:
                            break
                    for child in node.get("children") or []:
                        queue.append(child)
        except DatabaseError:
            logger.debug("quick_search_view directories degraded on DatabaseError", exc_info=True)
            directories = []
        except Exception:
            logger.warning("quick_search_view directories degraded gracefully", exc_info=True)
            directories = []

        # 2. 文章与标签匹配：仅在 q 非空时执行，空查询直接返回 [] 避免无意义全站查询
        if q:
            try:
                archive_type_keys = get_archive_enabled_type_keys()
                if archive_type_keys:
                    result = ContentQuery.execute(
                        QuerySpec(
                            q=q,
                            site_id=site.id,
                            type_keys=archive_type_keys,
                            page_size=5,
                            count_mode="bounded",
                            compute_facets_flag=False,
                        ),
                        AccessContext(mode=AccessMode.PUBLIC),
                    )
                    for it in result.items[:5]:
                        articles.append({
                            "title": str(it.title or ""),
                            "url": str(it.url or ""),
                            "type_label": get_type_display_label(it.type_key),
                            "authored_on": it.authored_on.isoformat() if it.authored_on else "",
                        })
            except DatabaseError:
                logger.debug("quick_search_view articles degraded on DatabaseError", exc_info=True)
                articles = []
            except Exception:
                logger.warning("quick_search_view articles degraded gracefully", exc_info=True)
                articles = []

            try:
                tag_items = _query_public_tag_items(
                    q,
                    site=site,
                    limit=5,
                    exclude_nested_sites=True,
                )
                tag_index = resolve_tag_index_page(request, site=site)
                tag_base_url = ""
                if tag_index is not None:
                    try:
                        tag_base_url = tag_index.get_url(request) or tag_index.url or ""
                    except Exception:
                        tag_base_url = getattr(tag_index, "url", "") or ""
                try:
                    search_base = reverse("search")
                except NoReverseMatch:
                    search_base = "/search/"

                for it in tag_items[:5]:
                    slug_val = str(it["value"] or "").strip()
                    detail_url = build_tag_detail_url(
                        tag_index,
                        slug_val,
                        base_url=tag_base_url,
                        request=request,
                    )
                    if not detail_url:
                        detail_url = f"{search_base}?{urlencode({'tag': slug_val})}"
                    tags.append({
                        "name": str(it["name"] or ""),
                        "slug": slug_val,
                        "url": detail_url,
                        "count": int(it["count"] or 0),
                    })
            except DatabaseError:
                logger.debug("quick_search_view tags degraded on DatabaseError", exc_info=True)
                tags = []
            except Exception:
                logger.warning("quick_search_view tags degraded gracefully", exc_info=True)
                tags = []

    response = JsonResponse({
        "q": q,
        "directories": directories,
        "articles": articles,
        "tags": tags,
    })
    response["Cache-Control"] = "no-store"
    return response


def _parse_tag_tokens(request) -> list[str]:
    """解析多选 tag 参数（支持重复 ?tag=a&tag=b 与逗号分隔 ?tag=a,b）"""
    tokens: list[str] = []
    for raw in request.GET.getlist("tag"):
        for part in str(raw).split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in tokens:
                tokens.append(cleaned)
    return tokens


def _parse_author_slugs(request) -> tuple[list[str], bool]:
    """解析多选 ?author= 参数（最多 10 项），校验规范 slug 语法。"""
    slugs: list[str] = []
    for raw in request.GET.getlist("author"):
        for part in str(raw).split(","):
            cleaned = part.strip().lower()
            if not cleaned:
                continue
            if len(cleaned) > 96 or not _VALID_SEARCH_AUTHOR_SLUG_RE.fullmatch(cleaned):
                return [], True
            if cleaned not in slugs:
                slugs.append(cleaned)
            if len(slugs) > MAX_AUTHOR_FILTERS:
                return [], True
    return slugs, False


def _resolve_active_author_options(request, slugs: list[str]) -> list[dict[str, str]]:
    """仅解析当前站点有公开内容（或系统佚名）的作者名称，未知/零篇 slug 不泄漏隐私名称。"""
    if not slugs:
        return []
    site = Site.find_for_request(request)
    author_types = get_author_enabled_type_keys()
    eligible_docs = PublishedSearchDocument.objects.filter(
        is_live=True,
        is_recycled=False,
        type_key__in=author_types,
        authored_on__lte=timezone.localdate(),
    )
    if site is not None:
        eligible_docs = eligible_docs.filter(site_id=site.id)
    eligible_docs = apply_public_eligibility_filter(eligible_docs)

    active_pids = set(
        PublishedSearchAuthor.objects.filter(
            author_slug__in=slugs,
            document__in=eligible_docs,
        ).values_list("person_id", flat=True)
    )
    persons_by_slug = {
        p.slug: p.name
        for p in Person.objects.filter(slug__in=slugs, pk__in=active_pids)
    }
    if "anonymous" in slugs and "anonymous" not in persons_by_slug:
        anon_name = Person.objects.filter(slug="anonymous", is_system_anonymous=True).values_list("name", flat=True).first()
        if anon_name:
            persons_by_slug["anonymous"] = anon_name

    return [
        {
            "slug": s,
            "label": persons_by_slug.get(s) or f"未匹配作者 ({s})",
        }
        for s in slugs
    ]


def _resolve_tag_tokens(raw_tokens: list[str]) -> tuple[list[int], list[dict[str, Any]], bool]:
    """
    将 tag_id 或小写 tag_slug/名称解析为规范化标签分组与展开后的所有 tag_ids。
    返回: (all_tag_ids, selected_groups, has_unmatched)
    """
    if not raw_tokens:
        return [], [], False

    all_tag_ids: list[int] = []
    selected_groups: list[dict[str, Any]] = []
    seen_norm_keys: set[str] = set()
    has_unmatched = False

    for tok in raw_tokens:
        matched_rows: list[dict[str, Any]] = []
        if tok.isdigit():
            tid = int(tok)
            base_row = (
                PublishedSearchTag.objects.filter(tag_id=tid)
                .values("tag_id", "tag_name", "tag_slug")
                .first()
            )
            if not base_row:
                tag_obj = Tag.objects.filter(id=tid).values("id", "name", "slug").first()
                if tag_obj:
                    base_row = {
                        "tag_id": tag_obj["id"],
                        "tag_name": tag_obj["name"],
                        "tag_slug": tag_obj["slug"],
                    }
            if base_row:
                t_name = (base_row["tag_name"] or "").strip()
                matched_rows = list(
                    PublishedSearchTag.objects.filter(tag_name__iexact=t_name)
                    .values("tag_id", "tag_name", "tag_slug")
                    .distinct()
                )
                if not any(r["tag_id"] == tid for r in matched_rows):
                    matched_rows.insert(0, base_row)
            else:
                matched_rows = [{"tag_id": tid, "tag_name": f"#{tid}", "tag_slug": str(tid)}]
        else:
            pub_matches = list(
                PublishedSearchTag.objects.filter(
                    Q(tag_slug__iexact=tok) | Q(tag_name__iexact=tok)
                )
                .values("tag_id", "tag_name", "tag_slug")
                .distinct()
            )
            tag_matches = list(
                Tag.objects.filter(Q(slug__iexact=tok) | Q(name__iexact=tok))
                .values("id", "name", "slug")
            )
            matched_names = {
                (r["tag_name"] or "").strip() for r in pub_matches if r.get("tag_name")
            } | {
                (r["name"] or "").strip() for r in tag_matches if r.get("name")
            }
            if matched_names:
                name_q = Q()
                for mname in matched_names:
                    name_q |= Q(tag_name__iexact=mname)
                matched_rows = list(
                    PublishedSearchTag.objects.filter(name_q)
                    .values("tag_id", "tag_name", "tag_slug")
                    .distinct()
                )
                for r in tag_matches:
                    if not any(m["tag_id"] == r["id"] for m in matched_rows):
                        matched_rows.append({
                            "tag_id": r["id"],
                            "tag_name": r["name"],
                            "tag_slug": r["slug"],
                        })

        if not matched_rows:
            has_unmatched = True
            continue

        # 选出最优显示名称与规范 URL token
        best_row = max(
            matched_rows,
            key=lambda r: _tag_name_priority((r.get("tag_name") or "").strip(), 1),
        )
        display_name = (best_row.get("tag_name") or "").strip() or tok
        norm_key = display_name.lower()
        group_ids: list[int] = []
        for r in matched_rows:
            rid = int(r["tag_id"])
            if rid not in group_ids:
                group_ids.append(rid)
            if rid not in all_tag_ids:
                all_tag_ids.append(rid)

        if norm_key in seen_norm_keys:
            continue
        seen_norm_keys.add(norm_key)

        canonical_token = (best_row.get("tag_slug") or "").strip() or str(best_row["tag_id"])
        selected_groups.append({
            "token": canonical_token,
            "raw_token": tok,
            "norm_key": norm_key,
            "label": display_name,
            "tag_ids": group_ids,
        })

    return all_tag_ids, selected_groups, has_unmatched


def _build_search_url(
    base_url: str,
    base_state: dict[str, Any],
    *,
    set_params: dict[str, Any] | None = None,
    remove_keys: set[str] | None = None,
    remove_tag_norm_key: str | None = None,
    toggle_tag: dict[str, Any] | None = None,
    remove_author_slug: str | None = None,
    toggle_author: str | None = None,
    reset_pagination: bool = True,
) -> str:
    """基于当前状态构造多维组合筛选 URL，绝不误清已有其他筛选条件"""
    state: dict[str, Any] = {
        "query": base_state.get("query", ""),
        "title_only": base_state.get("title_only", False),
        "type": base_state.get("type", ""),
        "author_role": base_state.get("author_role", ""),
        "year": base_state.get("year"),
        "month": base_state.get("month"),
        "date_from": base_state.get("date_from", ""),
        "date_to": base_state.get("date_to", ""),
        "days": base_state.get("days", ""),
        "sort": base_state.get("sort", ""),
        "page_size": base_state.get("page_size"),
        "secondary": base_state.get("secondary", "none"),
        "expand": base_state.get("expand", "auto"),
        "cursor": base_state.get("cursor"),
        "page": base_state.get("page"),
        "seek_date": base_state.get("seek_date", ""),
        "seek_direction": base_state.get("seek_direction", ""),
    }
    tag_groups = [dict(g) for g in base_state.get("tag_groups", [])]
    author_slugs = list(base_state.get("author_slugs", []))

    if reset_pagination:
        state["cursor"] = None
        state["page"] = None
        state["seek_date"] = ""
        state["seek_direction"] = ""

    if remove_keys:
        for rk in remove_keys:
            if rk == "tag":
                tag_groups = []
            elif rk == "author":
                author_slugs = []
            elif rk in ("title_only",):
                state[rk] = False
            elif rk in ("year", "month", "page_size", "cursor", "page"):
                state[rk] = None
            elif rk == "secondary":
                state[rk] = "none"
            elif rk == "expand":
                state[rk] = "auto"
            else:
                state[rk] = ""

    if remove_tag_norm_key is not None:
        tag_groups = [g for g in tag_groups if g["norm_key"] != remove_tag_norm_key]

    if remove_author_slug is not None:
        author_slugs = [s for s in author_slugs if s != remove_author_slug]

    if toggle_author is not None:
        a_slug = str(toggle_author).strip().lower()
        if a_slug in author_slugs:
            author_slugs = [s for s in author_slugs if s != a_slug]
        elif a_slug and len(author_slugs) < MAX_AUTHOR_FILTERS:
            author_slugs.append(a_slug)

    if toggle_tag is not None:
        t_norm = (toggle_tag.get("name") or "").strip().lower()
        t_ids = set(toggle_tag.get("tag_ids") or ([toggle_tag["id"]] if toggle_tag.get("id") else []))
        exists = any(
            g["norm_key"] == t_norm or bool(set(g.get("tag_ids", [])) & t_ids)
            for g in tag_groups
        )
        if exists:
            tag_groups = [
                g for g in tag_groups
                if g["norm_key"] != t_norm and not (set(g.get("tag_ids", [])) & t_ids)
            ]
        else:
            t_token = (toggle_tag.get("slug") or "").strip() or str(toggle_tag.get("id") or "")
            if t_token:
                tag_groups.append({
                    "token": t_token,
                    "norm_key": t_norm,
                    "label": toggle_tag.get("name") or t_token,
                    "tag_ids": list(t_ids),
                })

    if set_params:
        for sk, sv in set_params.items():
            state[sk] = sv

    pairs: list[tuple[str, str]] = []
    if state["query"]:
        pairs.append(("query", state["query"]))
    if state["title_only"]:
        pairs.append(("title_only", "1"))
    if state["type"]:
        pairs.append(("type", state["type"]))
    for a_slug in author_slugs:
        pairs.append(("author", a_slug))
    if state["author_role"]:
        pairs.append(("author_role", state["author_role"]))
    for g in tag_groups:
        pairs.append(("tag", g["token"]))
    if state["year"]:
        pairs.append(("year", str(state["year"])))
    if state["month"]:
        pairs.append(("month", str(state["month"])))
    if state["days"]:
        pairs.append(("days", str(state["days"])))
    else:
        if state["date_from"]:
            pairs.append(("date_from", state["date_from"]))
        if state["date_to"]:
            pairs.append(("date_to", state["date_to"]))
    if state["sort"]:
        pairs.append(("sort", state["sort"]))
    if state["page_size"] and state["page_size"] != DEFAULT_PAGE_SIZE:
        pairs.append(("page_size", str(state["page_size"])))
    if state["secondary"] == "words":
        pairs.append(("secondary", "words"))
    if state["expand"] in ("off", "on"):
        pairs.append(("expand", state["expand"]))
    if state["cursor"]:
        pairs.append(("cursor", state["cursor"]))
    elif state["seek_direction"] == "oldest":
        pairs.append(("seek_direction", "oldest"))
    elif state["seek_date"]:
        pairs.append(("seek_date", state["seek_date"]))
    elif state["page"] and int(state["page"]) > 1:
        pairs.append(("page", str(state["page"])))

    qs = urlencode(pairs, doseq=True)
    return f"{base_url}?{qs}" if qs else base_url


def search(request):
    """全站统一中文搜索与组合筛选入口"""
    author_discovery_enabled = getattr(settings, "AUTHOR_DISCOVERY_ENABLED", True)
    current_site = Site.find_for_request(request)
    effective_site_id = current_site.id if current_site is not None else -1
    canonical_url = reverse("search")
    raw_query = request.GET.get("query")
    if raw_query is None:
        raw_query = request.GET.get("q", "")
    search_query = str(raw_query).strip()[:200]

    title_only_raw = (request.GET.get("title_only") or "").strip().lower()
    title_only = title_only_raw in TRUE_VALUES

    raw_tag_tokens = _parse_tag_tokens(request)
    tag_ids, selected_tag_groups, has_unmatched_tag = _resolve_tag_tokens(raw_tag_tokens)
    effective_tag_ids = tag_ids if tag_ids else ([-1] if has_unmatched_tag else [])
    active_tag_ids_set = set(tag_ids)
    active_tag_norm_keys = {g["norm_key"] for g in selected_tag_groups}

    if author_discovery_enabled:
        selected_author_slugs, bad_author_param = _parse_author_slugs(request)
        raw_author_role = (request.GET.get("author_role") or "").strip().lower()
        if raw_author_role in ("", "all"):
            selected_author_role = ""
            bad_role_param = False
        elif roles_for_filter_key(raw_author_role):
            selected_author_role = raw_author_role
            bad_role_param = False
        else:
            selected_author_role = ""
            bad_role_param = True
        selected_author_options = _resolve_active_author_options(request, selected_author_slugs)
    else:
        selected_author_slugs, bad_author_param = [], False
        selected_author_role = ""
        bad_role_param = False
        selected_author_options = []
    active_author_slugs_set = set(selected_author_slugs)

    type_key = request.GET.get("type", "").strip()
    type_keys = [type_key] if type_key else []

    year_str = request.GET.get("year", "").strip()
    year = int(year_str) if year_str.isdigit() else None

    month_str = request.GET.get("month", "").strip()
    month = int(month_str) if month_str.isdigit() and 1 <= int(month_str) <= 12 else None

    # 日期范围与快捷天数 (7 / 30 / 365)
    raw_date_from = (request.GET.get("date_from") or "").strip()
    raw_date_to = (request.GET.get("date_to") or "").strip()
    parsed_date_from: date | None = None
    parsed_date_to: date | None = None
    if raw_date_from:
        try:
            parsed_date_from = date.fromisoformat(raw_date_from)
        except (ValueError, TypeError):
            parsed_date_from = None
    if raw_date_to:
        try:
            parsed_date_to = date.fromisoformat(raw_date_to)
        except (ValueError, TypeError):
            parsed_date_to = None

    raw_days = (request.GET.get("days") or "").strip()
    days = raw_days if raw_days in ALLOWED_DAYS else ""

    effective_date_from = parsed_date_from
    effective_date_to = parsed_date_to
    if days and effective_date_from is None:
        effective_date_from = timezone.localdate() - timedelta(days=int(days))

    raw_sort = (request.GET.get("sort") or "").strip().lower()
    explicit_sort = raw_sort if raw_sort in ALLOWED_SORTS else ""
    effective_sort = explicit_sort or ("relevance" if search_query else "authored_desc")

    secondary = (request.GET.get("secondary") or "none").strip()
    if secondary not in ("none", "words"):
        secondary = "none"
    expand = (request.GET.get("expand") or "auto").strip()
    if expand not in ("auto", "off", "on"):
        expand = "auto"

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
                    seek_date = None
        if seek_direction != "oldest" and seek_date is None:
            raw_page = (request.GET.get("page") or "").strip()
            if raw_page:
                try:
                    parsed_p = int(raw_page)
                    page_num = parsed_p if parsed_p >= 1 else 1
                except (ValueError, TypeError):
                    page_num = None

    raw_page_size = (request.GET.get("page_size") or "").strip()
    try:
        page_size = max(1, min(int(raw_page_size) if raw_page_size else DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE))
    except (ValueError, TypeError):
        page_size = DEFAULT_PAGE_SIZE

    base_state = {
        "query": search_query,
        "title_only": title_only,
        "type": type_key,
        "tag_groups": selected_tag_groups,
        "author_slugs": selected_author_slugs,
        "author_role": selected_author_role,
        "year": year,
        "month": month,
        "date_from": parsed_date_from.isoformat() if parsed_date_from else "",
        "date_to": parsed_date_to.isoformat() if parsed_date_to else "",
        "days": days,
        "sort": explicit_sort,
        "page_size": page_size,
        "secondary": secondary,
        "expand": expand,
        "cursor": cursor,
        "page": page_num,
        "seek_date": seek_date.isoformat() if seek_date else "",
        "seek_direction": seek_direction if seek_direction == "oldest" else "",
    }

    spec = QuerySpec(
        q=search_query or None,
        search_fields="title" if title_only else "all",
        site_id=effective_site_id,
        type_keys=type_keys,
        tag_ids=effective_tag_ids,
        author_slugs=selected_author_slugs,
        author_role=selected_author_role or None,
        year=year,
        month=month,
        date_from=effective_date_from,
        date_to=effective_date_to,
        sort=effective_sort,
        secondary=secondary,
        expand=expand,
        cursor=cursor,
        page=page_num,
        page_size=page_size,
        seek_date=seek_date,
        seek_direction=seek_direction,
        count_mode="bounded",
        facets=["types", "tags", "years", "authors"] if author_discovery_enabled else ["types", "tags", "years"],
    )

    access_context = AccessContext(
        user=request.user,
        mode=AccessMode.PUBLIC,
    )

    http_status = 200
    error_message: str | None = None
    result = None

    if bad_author_param:
        http_status = 400
        error_message = "无效的作者筛选参数（仅支持规范 Slug，最多同时选 10 位作者）。"
    elif bad_role_param:
        http_status = 400
        error_message = "无效的作者贡献角色参数。"
    else:
        try:
            result = ContentQuery.execute(spec, access_context)
        except PageWindowExceededError as exc:
            http_status = 422
            error_message = (
                f"页码 {exc.requested_page} 超出直跳窗口上限（最多支持直跳第 {exc.max_jump_page} 页），"
                "请使用「下一页」游标或按成文日期定位。"
            )
        except ValueError:
            http_status = 400
            error_message = "无效的分页游标参数，请重置或返回第 1 页重试。"

    if result is not None:
        items = result.items
        total_count = result.total_count
        facets = result.facets
        archive_groups = result.archive_groups
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
        facets = {}
        archive_groups = []
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
            count_kind="lower_bound",
            has_next=False,
            has_previous=False,
            next_cursor=None,
            previous_cursor=None,
        )
        count_meta = build_count_meta(value=0, kind="lower_bound")

    # SEARCH-MODE-02: 模式 A 命中少于 5 条时的手动展开判断
    a_count = len([it for it in items if it.match_mode == "A"])
    show_expand_button = (
        (1 <= a_count < 5)
        and not secondary_used
        and bool(search_query)
    )
    show_expand_url = (
        _build_search_url(
            canonical_url,
            base_state,
            set_params={"secondary": "words"},
            reset_pagination=True,
        )
        if show_expand_button
        else ""
    )

    # 构建用于 _listing_pagination.html 的非分页查询串
    pagination_base_url = _build_search_url(canonical_url, base_state, reset_pagination=True)
    pagination_query_string = pagination_base_url.split("?", 1)[1] if "?" in pagination_base_url else ""

    pagination_vm = {
        "items": items,
        "total_count": total_count,
        "count_meta": count_meta,
        "pagination": pagination_payload,
        "next_cursor": next_cursor,
        "previous_cursor": previous_cursor,
        "canonical_url": canonical_url,
        "query_string": pagination_query_string,
        "active_filters": {
            "seek_date": seek_date.isoformat() if seek_date else "",
        },
        "error_message": error_message,
    }

    # 1. 内容类型选项（全部 / 博客文章 / 专题指南 / 开源项目 / 栏目导航页）
    raw_type_counts = {item["key"]: int(item["count"]) for item in facets.get("types", [])}
    all_types_count = sum(v for k, v in raw_type_counts.items() if k != "navigation")
    type_options: list[dict[str, Any]] = []
    for t_key, t_label in UNIFIED_TYPE_OPTIONS:
        if t_key == "":
            t_count = all_types_count
            is_active = not type_key
            t_url = _build_search_url(canonical_url, base_state, set_params={"type": ""}, reset_pagination=True)
        else:
            t_count = raw_type_counts.get(t_key, 0)
            is_active = type_key == t_key
            next_type = "" if is_active else t_key
            t_url = _build_search_url(canonical_url, base_state, set_params={"type": next_type}, reset_pagination=True)
        type_options.append({
            "key": t_key,
            "label": t_label,
            "count": t_count,
            "is_active": is_active,
            "url": t_url,
        })

    # 2. 热门标签分面（基于大小写归一化结果，支持多选 toggle，侧栏仅展示前 20 项）
    tag_facet_items: list[dict[str, Any]] = []
    for tg in facets.get("tags", []):
        tg_ids = set(tg.get("tag_ids") or [tg["id"]])
        tg_norm = (tg.get("name") or "").strip().lower()
        is_active = bool(tg_ids & active_tag_ids_set) or (tg_norm in active_tag_norm_keys)
        tag_facet_items.append({
            "id": tg["id"],
            "tag_ids": list(tg_ids),
            "name": tg["name"],
            "slug": tg.get("slug") or str(tg["id"]),
            "count": tg["count"],
            "is_active": is_active,
            "url": _build_search_url(canonical_url, base_state, toggle_tag=tg, reset_pagination=True),
        })
    total_tag_facets_count = len(tag_facet_items)
    has_more_tags = total_tag_facets_count > MAX_SIDEBAR_TAG_FACETS
    tag_facet_items = tag_facet_items[:MAX_SIDEBAR_TAG_FACETS]

    # 3. 年份归档分面（支持 toggle）
    year_facet_items: list[dict[str, Any]] = []
    for yr in facets.get("years", []):
        yr_val = yr["year"]
        is_active = year == yr_val
        next_yr = None if is_active else yr_val
        year_facet_items.append({
            "year": yr_val,
            "count": yr["count"],
            "is_active": is_active,
            "url": _build_search_url(canonical_url, base_state, set_params={"year": next_yr}, reset_pagination=True),
        })

    # 3b. 作者分面（支持多选 toggle，按规范 slug 去重，展示当前 Person.name）
    author_facet_items: list[dict[str, Any]] = []
    for au in facets.get("authors", []):
        a_slug = au["slug"]
        author_facet_items.append({
            "slug": a_slug,
            "label": au["label"],
            "count": au["count"],
            "is_anonymous": bool(au.get("is_anonymous", False)),
            "is_active": a_slug in active_author_slugs_set,
            "url": _build_search_url(canonical_url, base_state, toggle_author=a_slug, reset_pagination=True),
        })

    # 4. 快捷时间范围胶囊（全部时间 / 近 7 天 / 近 30 天 / 近 1 年）
    days_options: list[dict[str, Any]] = []
    for d_key, d_label in DAYS_OPTIONS:
        if d_key == "":
            is_active = not days and not parsed_date_from and not parsed_date_to
            d_url = _build_search_url(
                canonical_url,
                base_state,
                remove_keys={"days", "date_from", "date_to"},
                reset_pagination=True,
            )
        else:
            is_active = days == d_key
            if is_active:
                d_url = _build_search_url(
                    canonical_url,
                    base_state,
                    remove_keys={"days", "date_from", "date_to"},
                    reset_pagination=True,
                )
            else:
                d_url = _build_search_url(
                    canonical_url,
                    base_state,
                    set_params={"days": d_key, "date_from": "", "date_to": ""},
                    reset_pagination=True,
                )
        days_options.append({
            "key": d_key,
            "label": d_label,
            "is_active": is_active,
            "url": d_url,
        })

    # 5. 已选筛选条件胶囊列表 (active_filter_pills)
    active_filter_pills: list[dict[str, Any]] = []
    if search_query:
        active_filter_pills.append({
            "key": "query",
            "label": "关键词",
            "value": search_query,
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"query", "secondary"}, reset_pagination=True
            ),
        })
    if title_only:
        active_filter_pills.append({
            "key": "title_only",
            "label": "范围",
            "value": "仅搜标题",
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"title_only"}, reset_pagination=True
            ),
        })
    if type_key:
        active_filter_pills.append({
            "key": "type",
            "label": "类型",
            "value": get_type_display_label(type_key),
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"type"}, reset_pagination=True
            ),
        })
    for au_opt in selected_author_options:
        active_filter_pills.append({
            "key": "author",
            "label": "作者",
            "value": au_opt["label"],
            "remove_url": _build_search_url(
                canonical_url,
                base_state,
                remove_author_slug=au_opt["slug"],
                reset_pagination=True,
            ),
        })
    if selected_author_role:
        active_filter_pills.append({
            "key": "author_role",
            "label": "角色",
            "value": AUTHOR_ROLE_LABELS.get(selected_author_role, normalize_author_role(selected_author_role)[1]),
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"author_role"}, reset_pagination=True
            ),
        })
    for tg_grp in selected_tag_groups:
        active_filter_pills.append({
            "key": "tag",
            "label": "标签",
            "value": f"#{tg_grp['label']}",
            "remove_url": _build_search_url(
                canonical_url,
                base_state,
                remove_tag_norm_key=tg_grp["norm_key"],
                reset_pagination=True,
            ),
        })
    if year:
        active_filter_pills.append({
            "key": "year",
            "label": "年份",
            "value": f"{year} 年",
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"year", "month"}, reset_pagination=True
            ),
        })
    if days:
        days_label_map = dict(DAYS_OPTIONS)
        active_filter_pills.append({
            "key": "days",
            "label": "时间",
            "value": days_label_map.get(days, f"近 {days} 天"),
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"days"}, reset_pagination=True
            ),
        })
    elif parsed_date_from or parsed_date_to:
        df_txt = parsed_date_from.isoformat() if parsed_date_from else "不限"
        dt_txt = parsed_date_to.isoformat() if parsed_date_to else "至今"
        active_filter_pills.append({
            "key": "date_range",
            "label": "成文日期",
            "value": f"{df_txt} ~ {dt_txt}",
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"date_from", "date_to"}, reset_pagination=True
            ),
        })
    if explicit_sort:
        active_filter_pills.append({
            "key": "sort",
            "label": "排序",
            "value": SORT_LABELS.get(explicit_sort, explicit_sort),
            "remove_url": _build_search_url(
                canonical_url, base_state, remove_keys={"sort"}, reset_pagination=True
            ),
        })

    # 6. 为结果卡片构造友好的类型中文名与标签跳转链接
    item_page_ids = [it.page_id for it in items]
    doc_authors_map: dict[int, list[dict[str, Any]]] = {}
    if author_discovery_enabled and item_page_ids:
        raw_au_rows = list(
            PublishedSearchAuthor.objects.filter(
                document__page_id__in=item_page_ids,
                document__site_id=effective_site_id,
                document__index_generation=1,
            )
            .order_by("sort_order", "id")
            .values("document__page_id", "person_id", "author_name", "author_slug")
        )
        live_names = {
            p["id"]: p["name"]
            for p in Person.objects.filter(id__in={r["person_id"] for r in raw_au_rows}).values("id", "name")
        }
        for r in raw_au_rows:
            pid_page = int(r["document__page_id"])
            slug_val = r["author_slug"]
            try:
                au_url = reverse("people:author_detail", kwargs={"slug": slug_val})
            except NoReverseMatch:
                au_url = f"/authors/{slug_val}/"
            doc_authors_map.setdefault(pid_page, []).append({
                "name": live_names.get(r["person_id"], r["author_name"]),
                "slug": slug_val,
                "url": au_url,
                "is_anonymous": slug_val == "anonymous",
            })

    display_items: list[dict[str, Any]] = []
    for it in items:
        tag_links = []
        for t in (it.tags or []):
            t_obj = {
                "id": t["id"],
                "tag_ids": [t["id"]],
                "name": t["name"],
                "slug": t.get("slug") or str(t["id"]),
            }
            tag_links.append({
                "id": t["id"],
                "name": t["name"],
                "slug": t.get("slug") or str(t["id"]),
                "is_active": (t["id"] in active_tag_ids_set) or ((t["name"] or "").strip().lower() in active_tag_norm_keys),
                "url": _build_search_url(canonical_url, base_state, toggle_tag=t_obj, reset_pagination=True),
            })
        display_items.append({
            "item": it,
            "type_label": get_type_display_label(it.type_key),
            "tag_links": tag_links,
            "authors": doc_authors_map.get(it.page_id, []),
        })

    selected_tag_tokens = [g["token"] for g in selected_tag_groups]
    selected_tag_options = [
        {"value": g["token"], "label": f"#{g['label']}"}
        for g in selected_tag_groups
    ]
    active_filters_state = {
        "query": search_query,
        "title_only": title_only,
        "type": type_key,
        "tags": selected_tag_tokens,
        "tag_options": selected_tag_options,
        "authors": selected_author_slugs,
        "author_options": selected_author_options,
        "author_role": selected_author_role,
        "year": year_str if year else "",
        "days": days,
        "date_from": parsed_date_from.isoformat() if parsed_date_from else "",
        "date_to": parsed_date_to.isoformat() if parsed_date_to else "",
        "sort": effective_sort,
        "explicit_sort": explicit_sort,
        "seek_date": seek_date.isoformat() if seek_date else "",
    }

    try:
        author_lookup_url = reverse("people:author_lookup")
    except NoReverseMatch:
        author_lookup_url = "/authors/lookup/"
    try:
        tag_lookup_url = reverse("search_tags_lookup")
    except NoReverseMatch:
        tag_lookup_url = "/search/tags-lookup/"

    tag_directory_url = ""
    try:
        from discovery.navigation import resolve_tag_index_page
        tag_index_page = resolve_tag_index_page(request)
        if tag_index_page is not None:
            tag_directory_url = tag_index_page.get_url(request) or tag_index_page.url or ""
    except Exception:
        tag_directory_url = ""

    # 仅在真正执行了搜索查询且查询成功时提供搜索统计上下文，避免参数报错请求或空浏览污染搜索词统计
    has_search_scope_filters = bool(
        title_only
        or type_key
        or tag_ids
        or has_unmatched_tag
        or selected_author_slugs
        or selected_author_role
        or year
        or month
        or days
        or parsed_date_from
        or parsed_date_to
    )
    analytics_search_executed = bool(result is not None and http_status == 200 and search_query)
    analytics_search_query = search_query if analytics_search_executed else ""
    analytics_search_total = total_count if analytics_search_executed else None
    analytics_search_scoped = has_search_scope_filters if analytics_search_executed else False

    context = {
        "search_query": search_query,
        "title_only": title_only,
        "query_result": result,
        "items": items,
        "display_items": display_items,
        "total_count": total_count,
        "facets": facets,
        "archive_groups": archive_groups,
        "next_cursor": next_cursor,
        "previous_cursor": previous_cursor,
        "cursor_reset": cursor_reset,
        "expansion_used": expansion_used,
        "secondary_used": secondary_used,
        "show_expand_button": show_expand_button,
        "show_expand_url": show_expand_url,
        "selected_type": type_key,
        "selected_tag": selected_tag_tokens[0] if selected_tag_tokens else "",
        "selected_tag_tokens": selected_tag_tokens,
        "selected_tag_options": selected_tag_options,
        "tag_lookup_url": tag_lookup_url,
        "tag_directory_url": tag_directory_url,
        "total_tag_facets_count": total_tag_facets_count,
        "has_more_tags": has_more_tags,
        "selected_author_slugs": selected_author_slugs,
        "selected_author_options": selected_author_options,
        "selected_author_role": selected_author_role,
        "author_role_options": AUTHOR_ROLE_OPTIONS,
        "author_lookup_url": author_lookup_url,
        "author_discovery_enabled": author_discovery_enabled,
        "selected_year": str(year) if year else "",
        "selected_days": days,
        "date_from": parsed_date_from.isoformat() if parsed_date_from else "",
        "date_to": parsed_date_to.isoformat() if parsed_date_to else "",
        "effective_sort": effective_sort,
        "type_options": type_options,
        "tag_facet_items": tag_facet_items,
        "author_facet_items": author_facet_items,
        "year_facet_items": year_facet_items,
        "days_options": days_options,
        "active_filter_pills": active_filter_pills,
        "reset_url": canonical_url,
        "canonical_url": canonical_url,
        "pagination_vm": pagination_vm,
        "error_message": error_message,
        "http_status": http_status,
        "analytics_search_query": analytics_search_query,
        "analytics_search_total": analytics_search_total,
        "analytics_search_scoped": analytics_search_scoped,
    }

    is_ajax = bool(
        request.headers.get("X-Listing-Response") == "fragment-v1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.GET.get("format") == "partial"
    )

    if is_ajax:
        html_fragment = render_to_string(
            "search/partials/_search_content.html",
            context,
            request=request,
        )
        full_url = _build_search_url(canonical_url, base_state, reset_pagination=False)
        return JsonResponse(
            {
                "html": html_fragment,
                "canonical_url": canonical_url,
                "full_url": full_url,
                "total_count": total_count,
                "cursor_reset": cursor_reset,
                "facets": facets,
                "next_cursor": next_cursor,
                "previous_cursor": previous_cursor,
                "error_message": error_message,
                "active_filters": active_filters_state,
            },
            status=http_status,
        )

    return TemplateResponse(
        request,
        "search/search.html",
        context,
        status=http_status,
    )
