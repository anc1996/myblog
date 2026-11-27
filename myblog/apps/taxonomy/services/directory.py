"""
全站标签大厅目录与单标签跨类型详情聚合服务 (TagDirectoryService & TagDetailService)。

落实方案 B 分层索引与零写库只读契约：
1. TagDirectoryService:
   - 100% SQL 下推聚合与分页，严禁拉取全量标签进 Python 内存；
   - 公开 GET 全程纯只读，绝不执行 TagDirectoryMetadata 写库；
   - 默认「方案 B」精简总览：Top 20 热门标签 + A–Z/# 每字母最多 20 个，超出引导至单字母 60/页分页视图；
   - 单字母 / 全站分页网格 / 关键词搜索统一采用 60 条/页有界分页（直跳上限 50 页，越界安全钳位）；
2. TagDetailService:
   - 跨类型 Tab 分栏切换 (全部/博客/指南/项目，D2-A)；
   - 单快照 ContentQuery 统一执行，0 MongoDB 延迟；
   - 缺失友好导流 (BLK-05: 200 OK 友好建议，0 私有标签名称泄露)。
"""

from dataclasses import dataclass, field
from datetime import date
import logging
import math
from typing import Any
from urllib.parse import urlencode

from django.db.models import CharField, Count, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce, Lower
from django.urls import reverse
from taggit.models import Tag
from wagtail.models import Site

from content_lifecycle.access import AccessContext, AccessMode
from discovery.contracts import QueryResult, QuerySpec
from discovery.detail_navigation import _get_nested_site_root_paths
from discovery.eligibility import apply_public_eligibility_filter
from discovery.models import PublishedSearchDocument, PublishedSearchTag
from discovery.query import ContentQuery
from taxonomy.models import TagDirectoryMetadata, get_pinyin_initial

logger = logging.getLogger(__name__)

PER_LETTER_PREVIEW_LIMIT = 20
DIRECTORY_PAGE_SIZE = 60
MAX_DIRECT_JUMP_PAGE = 50
TOP_TAGS_LIMIT = 20
ALPHABET_BUCKETS = tuple(chr(c) for c in range(ord("A"), ord("Z") + 1)) + ("#",)


def _get_site_eligible_docs(request: Any):
    """构造当前站点与公开资格过滤后的 PublishedSearchDocument 查询集（排除嵌套子站点）。"""
    site = Site.find_for_request(request)
    eligible_docs = PublishedSearchDocument.objects.filter(is_live=True, is_recycled=False)
    if site is not None:
        root_page = getattr(site, "root_page", None)
        root_path = getattr(root_page, "path", None) if root_page else None
        if root_path:
            eligible_docs = eligible_docs.filter(site_id=site.id, root_path__startswith=root_path)
            for nested_root in _get_nested_site_root_paths(site):
                eligible_docs = eligible_docs.exclude(root_path__startswith=nested_root)
        else:
            eligible_docs = eligible_docs.filter(site_id=site.id)
    return site, apply_public_eligibility_filter(eligible_docs)


def _build_tag_detail_url(index_page: Any, request: Any, tag_slug: str) -> str:
    """构造带标签大厅根路径前缀的完整标签详情 URL，避免在子路径下退化为相对路径。"""
    base_url = (index_page.get_url(request) or "/tags/").rstrip("/")
    try:
        sub_path = index_page.reverse_subpage("detail", kwargs={"tag_slug": tag_slug}).lstrip("/")
        return f"{base_url}/{sub_path}"
    except Exception:
        return f"{base_url}/{tag_slug}/"


def _build_directory_url(
    base_url: str,
    *,
    letter: str = "",
    q: str = "",
    sort: str = "popular",
    view: str = "overview",
    page: int = 1,
) -> str:
    """构造干净的标签大厅查询 URL，自动省略默认值。"""
    params: list[tuple[str, str]] = []
    if letter:
        params.append(("letter", "other" if letter == "#" else letter))
    if q:
        params.append(("q", q))
    if view == "grid" and not letter and not q:
        params.append(("view", "grid"))
    if sort == "name":
        params.append(("sort", "name"))
    if page and int(page) > 1:
        params.append(("page", str(int(page))))
    if not params:
        return base_url
    return f"{base_url}?{urlencode(params)}"


def _build_page_numbers(current_page: int, max_reachable_page: int) -> list[int | None]:
    """生成带省略号（None）的页码窗口，例如 [1, None, 3, 4, 5, None, 10]。"""
    if max_reachable_page <= 1:
        return [1]
    pages_to_show: set[int] = {1, max_reachable_page}
    for p in range(max(1, current_page - 2), min(max_reachable_page, current_page + 2) + 1):
        pages_to_show.add(p)
    sorted_pages = sorted(pages_to_show)
    result: list[int | None] = []
    prev = 0
    for p in sorted_pages:
        if prev and p - prev > 1:
            result.append(None)
        result.append(p)
        prev = p
    return result


@dataclass
class TagDirectoryItem:
    """单个标签在大厅中的展示结构"""
    id: int
    name: str
    slug: str
    count: int
    initial: str
    initial_group_rank: int
    url: str
    description: str = ""


@dataclass
class TagDirectoryViewModel:
    """标签大厅视图模型数据结构"""
    total_count: int
    top_tags: list[TagDirectoryItem]
    grouped_tags: dict[str, list[TagDirectoryItem]]
    search_query: str = ""
    is_empty: bool = False
    canonical_url: str = ""
    full_url: str = ""
    site_total_count: int = 0
    filtered_count: int = 0
    view_mode: str = "overview"
    show_top_tags: bool = True
    active_letter: str = ""
    active_letter_param: str = ""
    active_sort: str = "popular"
    alphabet_counts: list[dict[str, Any]] = field(default_factory=list)
    group_totals: dict[str, int] = field(default_factory=dict)
    overview_groups: list[dict[str, Any]] = field(default_factory=list)
    page_items: list[TagDirectoryItem] = field(default_factory=list)
    pagination: dict[str, Any] = field(default_factory=dict)
    all_tags_url: str = ""
    reset_search_url: str = ""
    sort_popular_url: str = ""
    sort_name_url: str = ""
    view_overview_url: str = ""
    view_grid_url: str = ""


class TagDirectoryService:
    """全站标签大厅目录聚合服务 (模式一)"""

    @classmethod
    def _parse_params(cls, request: Any) -> dict[str, Any]:
        search_q = (request.GET.get("q") or "").strip()[:80]

        raw_letter = (request.GET.get("letter") or "").strip()
        if raw_letter.lower() == "other" or raw_letter == "#":
            active_letter = "#"
        elif len(raw_letter) == 1 and "a" <= raw_letter.lower() <= "z":
            active_letter = raw_letter.upper()
        else:
            active_letter = ""

        raw_view = (request.GET.get("view") or "").strip().lower()
        if active_letter:
            view_mode = "letter"
        elif search_q:
            view_mode = "search"
        elif raw_view == "grid":
            view_mode = "grid"
        else:
            view_mode = "overview"

        raw_sort = (request.GET.get("sort") or "").strip().lower()
        active_sort = "name" if raw_sort == "name" else "popular"

        raw_page = (request.GET.get("page") or "1").strip()
        try:
            requested_page = max(1, int(raw_page))
        except (ValueError, TypeError):
            requested_page = 1

        return {
            "q": search_q,
            "letter": active_letter,
            "letter_param": "other" if active_letter == "#" else active_letter,
            "view_mode": view_mode,
            "sort": active_sort,
            "page": requested_page,
        }

    @classmethod
    def _apply_letter_filter(cls, qs, letter: str):
        if not letter:
            return qs
        if letter == "#":
            # 兼容历史无 TagDirectoryMetadata 行：未落在 A-Z 中的均视为 #
            non_sharp_tag_ids = TagDirectoryMetadata.objects.exclude(initial="#").values("tag_id")
            return qs.exclude(tag_id__in=Subquery(non_sharp_tag_ids))
        letter_tag_ids = TagDirectoryMetadata.objects.filter(initial=letter).values("tag_id")
        return qs.filter(tag_id__in=Subquery(letter_tag_ids))

    @classmethod
    def _rows_to_items(
        cls,
        rows: list[dict[str, Any]],
        meta_by_tag_id: dict[int, TagDirectoryMetadata],
        index_page: Any,
        request: Any,
    ) -> list[TagDirectoryItem]:
        items: list[TagDirectoryItem] = []
        for r in rows:
            t_id = int(r["tag_id"])
            tag_name = r["tag_name"]
            meta = meta_by_tag_id.get(t_id)
            if meta is not None:
                initial = meta.initial or "#"
                group_rank = int(meta.initial_group_rank)
                description = meta.description or ""
            else:
                # 纯只读内存回退：绝不在 GET 期间写入 TagDirectoryMetadata
                initial = get_pinyin_initial(tag_name)
                group_rank = 0 if "A" <= initial <= "Z" else 1
                description = ""

            safe_slug = r["tag_slug"] or f"sym-{t_id}"
            items.append(
                TagDirectoryItem(
                    id=t_id,
                    name=tag_name,
                    slug=safe_slug,
                    count=int(r["count"]),
                    initial=initial,
                    initial_group_rank=group_rank,
                    url=_build_tag_detail_url(index_page, request, safe_slug),
                    description=description,
                )
            )
        return items

    @classmethod
    def get_tag_directory(cls, request: Any, index_page: Any) -> TagDirectoryViewModel:
        params = cls._parse_params(request)
        search_q = params["q"]
        active_letter = params["letter"]
        active_letter_param = params["letter_param"]
        view_mode = params["view_mode"]
        active_sort = params["sort"]
        requested_page = params["page"]

        base_url = index_page.get_url(request) or "/tags/"
        _, eligible_docs = _get_site_eligible_docs(request)

        # 1. 单次 SQL 聚合全站 A–Z + # 各字母有效标签数（不把全表标签拉进 Python 内存）
        meta_initial_sq = Subquery(
            TagDirectoryMetadata.objects.filter(tag_id=OuterRef("tag_id")).values("initial")[:1],
            output_field=CharField(),
        )
        alphabet_rows = list(
            PublishedSearchTag.objects.filter(document__in=eligible_docs)
            .annotate(bucket_initial=Coalesce(meta_initial_sq, Value("#")))
            .values("bucket_initial")
            .annotate(tag_count=Count("tag_id", distinct=True))
        )
        counts_by_letter: dict[str, int] = {}
        for row in alphabet_rows:
            bucket = row["bucket_initial"] if row["bucket_initial"] in ALPHABET_BUCKETS else "#"
            counts_by_letter[bucket] = counts_by_letter.get(bucket, 0) + int(row["tag_count"] or 0)

        site_total_count = sum(counts_by_letter.values())

        alphabet_counts: list[dict[str, Any]] = []
        for ch in ALPHABET_BUCKETS:
            cnt = counts_by_letter.get(ch, 0)
            alphabet_counts.append(
                {
                    "letter": ch,
                    "query_value": "other" if ch == "#" else ch,
                    "count": cnt,
                    "has_items": cnt > 0,
                    "is_active": active_letter == ch,
                    "url": _build_directory_url(
                        base_url,
                        letter=ch,
                        q="",
                        sort=active_sort,
                        view="overview",
                        page=1,
                    ),
                }
            )

        group_totals = {ch: counts_by_letter[ch] for ch in ALPHABET_BUCKETS if counts_by_letter.get(ch, 0) > 0}

        all_tags_url = _build_directory_url(base_url, letter="", q="", sort=active_sort, view="overview", page=1)
        reset_search_url = _build_directory_url(
            base_url,
            letter=active_letter,
            q="",
            sort=active_sort,
            view="grid" if view_mode == "grid" else "overview",
            page=1,
        )
        sort_popular_url = _build_directory_url(
            base_url, letter=active_letter, q=search_q, sort="popular", view=view_mode, page=1
        )
        sort_name_url = _build_directory_url(
            base_url, letter=active_letter, q=search_q, sort="name", view=view_mode, page=1
        )
        view_overview_url = _build_directory_url(base_url, letter="", q="", sort=active_sort, view="overview", page=1)
        view_grid_url = _build_directory_url(base_url, letter="", q="", sort=active_sort, view="grid", page=1)

        if site_total_count == 0:
            return TagDirectoryViewModel(
                total_count=0,
                top_tags=[],
                grouped_tags={},
                search_query=search_q,
                is_empty=True,
                canonical_url=base_url,
                full_url=_build_directory_url(
                    base_url, letter=active_letter, q=search_q, sort=active_sort, view=view_mode, page=1
                ),
                site_total_count=0,
                filtered_count=0,
                view_mode=view_mode,
                show_top_tags=(view_mode == "overview"),
                active_letter=active_letter,
                active_letter_param=active_letter_param,
                active_sort=active_sort,
                alphabet_counts=alphabet_counts,
                group_totals=group_totals,
                all_tags_url=all_tags_url,
                reset_search_url=reset_search_url,
                sort_popular_url=sort_popular_url,
                sort_name_url=sort_name_url,
                view_overview_url=view_overview_url,
                view_grid_url=view_grid_url,
            )

        # 2. 公共基础标签统计 QuerySet（仅下推 SQL 排序与有界切片）
        base_tag_qs = (
            PublishedSearchTag.objects.filter(document__in=eligible_docs)
            .values("tag_id", "tag_name", "tag_slug")
            .annotate(
                count=Count("document_id", distinct=True),
                sort_key=Lower("tag_name"),
            )
            .filter(count__gte=1)
        )

        order_fields = ("sort_key", "tag_id") if active_sort == "name" else ("-count", "sort_key", "tag_id")

        # 模式 1：方案 B 默认 A–Z 精简总览（Top 20 + 每非空字母最多 20 个，总量严格 <= 540）
        if view_mode == "overview":
            top_rows = list(base_tag_qs.order_by("-count", "sort_key", "tag_id")[:TOP_TAGS_LIMIT])
            bucket_rows_map: dict[str, list[dict[str, Any]]] = {}
            fetched_ids: set[int] = {int(r["tag_id"]) for r in top_rows}

            for ch in ALPHABET_BUCKETS:
                if counts_by_letter.get(ch, 0) <= 0:
                    continue
                rows = list(
                    cls._apply_letter_filter(base_tag_qs, ch)
                    .order_by(*order_fields)[:PER_LETTER_PREVIEW_LIMIT]
                )
                if rows:
                    bucket_rows_map[ch] = rows
                    fetched_ids.update(int(r["tag_id"]) for r in rows)

            meta_by_tag_id = {
                m.tag_id: m
                for m in TagDirectoryMetadata.objects.filter(tag_id__in=fetched_ids)
            }

            top_tags = cls._rows_to_items(top_rows, meta_by_tag_id, index_page, request)
            grouped_tags: dict[str, list[TagDirectoryItem]] = {}
            overview_groups: list[dict[str, Any]] = []

            for ch in ALPHABET_BUCKETS:
                rows = bucket_rows_map.get(ch)
                if not rows:
                    continue
                items = cls._rows_to_items(rows, meta_by_tag_id, index_page, request)
                grouped_tags[ch] = items
                total_in_group = counts_by_letter.get(ch, len(items))
                overview_groups.append(
                    {
                        "letter": ch,
                        "items": items,
                        "total_count": total_in_group,
                        "has_more": total_in_group > PER_LETTER_PREVIEW_LIMIT,
                        "more_url": _build_directory_url(
                            base_url,
                            letter=ch,
                            q="",
                            sort=active_sort,
                            view="overview",
                            page=1,
                        ),
                    }
                )

            return TagDirectoryViewModel(
                total_count=site_total_count,
                top_tags=top_tags,
                grouped_tags=grouped_tags,
                search_query=search_q,
                is_empty=False,
                canonical_url=base_url,
                full_url=_build_directory_url(
                    base_url, letter="", q="", sort=active_sort, view="overview", page=1
                ),
                site_total_count=site_total_count,
                filtered_count=site_total_count,
                view_mode="overview",
                show_top_tags=True,
                active_letter="",
                active_letter_param="",
                active_sort=active_sort,
                alphabet_counts=alphabet_counts,
                group_totals=group_totals,
                overview_groups=overview_groups,
                all_tags_url=all_tags_url,
                reset_search_url=reset_search_url,
                sort_popular_url=sort_popular_url,
                sort_name_url=sort_name_url,
                view_overview_url=view_overview_url,
                view_grid_url=view_grid_url,
            )

        # 模式 2：单字母分页 / 全站分页网格 / 搜索结果分页（SQL 下推 LIMIT 60 OFFSET ...）
        filtered_qs = base_tag_qs
        if active_letter:
            filtered_qs = cls._apply_letter_filter(filtered_qs, active_letter)
        if search_q:
            filtered_qs = filtered_qs.filter(tag_name__icontains=search_q)

        if search_q:
            filtered_count = filtered_qs.count()
        elif active_letter:
            filtered_count = counts_by_letter.get(active_letter, 0)
        else:
            filtered_count = site_total_count

        total_pages = max(1, math.ceil(filtered_count / DIRECTORY_PAGE_SIZE)) if filtered_count > 0 else 1
        max_jump_page = max(1, min(total_pages, MAX_DIRECT_JUMP_PAGE))
        current_page = max(1, min(requested_page, max_jump_page))
        offset = (current_page - 1) * DIRECTORY_PAGE_SIZE

        if filtered_count == 0:
            page_rows = []
        else:
            page_rows = list(
                filtered_qs.order_by(*order_fields)[offset : offset + DIRECTORY_PAGE_SIZE]
            )

        fetched_ids = {int(r["tag_id"]) for r in page_rows}
        meta_by_tag_id = (
            {m.tag_id: m for m in TagDirectoryMetadata.objects.filter(tag_id__in=fetched_ids)}
            if fetched_ids
            else {}
        )
        page_items = cls._rows_to_items(page_rows, meta_by_tag_id, index_page, request)

        # 保持向后兼容：在搜索/单字母态也将当前页按字母归入 grouped_tags，并填充 top_tags 切片
        grouped_tags = {}
        for item in page_items:
            grouped_tags.setdefault(item.initial, []).append(item)

        page_numbers = _build_page_numbers(current_page, max_jump_page)
        page_links = [
            {
                "number": p,
                "is_current": (p == current_page),
                "url": _build_directory_url(
                    base_url,
                    letter=active_letter,
                    q=search_q,
                    sort=active_sort,
                    view=view_mode,
                    page=p,
                )
                if p is not None
                else "",
            }
            for p in page_numbers
        ]
        has_previous = current_page > 1
        has_next = current_page < max_jump_page
        pagination = {
            "current_page": current_page,
            "total_pages": total_pages,
            "max_jump_page": max_jump_page,
            "page_size": DIRECTORY_PAGE_SIZE,
            "total_count": filtered_count,
            "start_index": (offset + 1) if page_items else 0,
            "end_index": (offset + len(page_items)) if page_items else 0,
            "has_previous": has_previous,
            "has_next": has_next,
            "prev_url": _build_directory_url(
                base_url,
                letter=active_letter,
                q=search_q,
                sort=active_sort,
                view=view_mode,
                page=current_page - 1,
            )
            if has_previous
            else "",
            "next_url": _build_directory_url(
                base_url,
                letter=active_letter,
                q=search_q,
                sort=active_sort,
                view=view_mode,
                page=current_page + 1,
            )
            if has_next
            else "",
            "page_numbers": page_numbers,
            "page_links": page_links,
            "is_window_capped": total_pages > MAX_DIRECT_JUMP_PAGE,
        }

        return TagDirectoryViewModel(
            total_count=filtered_count,
            top_tags=page_items[:TOP_TAGS_LIMIT],
            grouped_tags=grouped_tags,
            search_query=search_q,
            is_empty=(filtered_count == 0),
            canonical_url=base_url,
            full_url=_build_directory_url(
                base_url,
                letter=active_letter,
                q=search_q,
                sort=active_sort,
                view=view_mode,
                page=current_page,
            ),
            site_total_count=site_total_count,
            filtered_count=filtered_count,
            view_mode=view_mode,
            show_top_tags=False,
            active_letter=active_letter,
            active_letter_param=active_letter_param,
            active_sort=active_sort,
            alphabet_counts=alphabet_counts,
            group_totals=group_totals,
            page_items=page_items,
            pagination=pagination,
            all_tags_url=all_tags_url,
            reset_search_url=reset_search_url,
            sort_popular_url=sort_popular_url,
            sort_name_url=sort_name_url,
            view_overview_url=view_overview_url,
            view_grid_url=view_grid_url,
        )


class TagDetailService:
    """单标签跨类型检索与多维分栏服务 (模式二与模式三)"""

    @classmethod
    def get_tag_detail(cls, request: Any, index_page: Any, tag_slug: str) -> dict[str, Any]:
        clean_slug = tag_slug.strip()

        # 查找对应 Tag 对象
        tag_obj = Tag.objects.filter(slug=clean_slug).first()

        # 基础合法集合 (核验该标签在当前公开集合中是否拥有 >= 1 篇内容)
        site, eligible_docs = _get_site_eligible_docs(request)

        tag_id = tag_obj.id if tag_obj else None
        scope_total_count = 0
        if tag_id:
            scope_total_count = PublishedSearchTag.objects.filter(
                tag_id=tag_id, document__in=eligible_docs
            ).values("document_id").distinct().count()

        # 模式三：标签不存在或全站已无公开内容 -> 200 友好推荐导流页 (BLK-05 / 决策四-A)
        if not tag_obj or scope_total_count == 0:
            # 在当前有效标签集中提取前 5 个相似推荐标签 (纯以入参 clean_slug 为输入，绝不暴露私有标签)
            candidate_tags = (
                PublishedSearchTag.objects.filter(document__in=eligible_docs)
                .values("tag_name", "tag_slug")
                .annotate(count=Count("document_id", distinct=True))
                .filter(count__gte=1)
                .order_by("-count")[:10]
            )
            recommended = []
            for ct in candidate_tags:
                s_slug = ct["tag_slug"] or "sym"
                t_url = _build_tag_detail_url(index_page, request, s_slug)
                recommended.append({
                    "name": ct["tag_name"],
                    "slug": s_slug,
                    "count": ct["count"],
                    "url": t_url,
                })

            return {
                "mode": "missing",
                "tag_slug": clean_slug,
                "recommended_tags": recommended[:5],
                "canonical_url": index_page.get_url(request),
                "directory_url": index_page.get_url(request),
            }

        # 模式二：正常详情聚合页 (跨类型检索，纯只读读取元数据)
        tag_meta = TagDirectoryMetadata.objects.filter(tag=tag_obj).first()

        # 解析类型分栏 Tab (决策二-A)
        raw_type = request.GET.get("type", "all").strip().lower()
        type_keys = []
        if raw_type and raw_type != "all":
            type_keys = [raw_type]

        # 解析其他过滤与检索参数
        q_term = (request.GET.get("q") or "").strip()
        title_only = request.GET.get("title_only") in ("1", "true", "on", "yes")
        sort_param = request.GET.get("sort", "authored_desc" if not q_term else "relevance")

        date_from = None
        df_str = request.GET.get("date_from")
        if df_str:
            try:
                date_from = date.fromisoformat(df_str.strip())
            except (ValueError, TypeError):
                date_from = None

        date_to = None
        dt_str = request.GET.get("date_to")
        if dt_str:
            try:
                date_to = date.fromisoformat(dt_str.strip())
            except (ValueError, TypeError):
                date_to = None

        cursor = (request.GET.get("cursor") or "").strip() or None
        page_num = None
        seek_date = None
        seek_direction = "date"
        if cursor is None:
            if (request.GET.get("seek_direction") or "").strip().lower() == "oldest":
                seek_direction = "oldest"
            elif request.GET.get("seek_date"):
                try:
                    seek_date = date.fromisoformat(request.GET.get("seek_date").strip())
                except (ValueError, TypeError):
                    seek_date = None
            if seek_direction != "oldest" and seek_date is None and request.GET.get("page"):
                try:
                    p_val = int(str(request.GET.get("page")).strip())
                    if p_val >= 1:
                        page_num = p_val
                except (ValueError, TypeError):
                    page_num = None

        spec = QuerySpec(
            q=q_term or None,
            search_fields="title" if title_only else "all",
            tag_ids=[tag_id],
            type_keys=type_keys,
            site_id=site.id if site else None,
            date_from=date_from,
            date_to=date_to,
            sort=sort_param,
            facets=["types"],
            page_size=15,
            cursor=cursor,
            page=page_num,
            seek_date=seek_date,
            seek_direction=seek_direction,
            count_mode="bounded",
            include_listing_scope=False,
        )

        # 单快照调用全站检索核心 (0 MongoDB 网络调用)
        from discovery.pagination import PageWindowExceededError
        try:
            query_result: QueryResult = ContentQuery.execute(spec, AccessContext(mode=AccessMode.PUBLIC))
        except PageWindowExceededError:
            spec_fallback = QuerySpec(
                q=spec.q,
                search_fields=spec.search_fields,
                tag_ids=spec.tag_ids,
                type_keys=spec.type_keys,
                site_id=spec.site_id,
                date_from=spec.date_from,
                date_to=spec.date_to,
                sort=spec.sort,
                facets=spec.facets,
                page_size=spec.page_size,
                page=1,
                count_mode="bounded",
                include_listing_scope=False,
            )
            query_result = ContentQuery.execute(spec_fallback, AccessContext(mode=AccessMode.PUBLIC))

        # 组装类型分栏 Tab 计数 (BLK-03 / 决策二-A)
        from discovery.facets import get_type_display_label
        from discovery.registry import get_archive_enabled_type_keys

        preferred_order = {"blog_page": 0, "guide": 1, "project": 2}
        ordered_type_keys = sorted(
            get_archive_enabled_type_keys(),
            key=lambda tk: (preferred_order.get(tk, 50), tk),
        )
        types_facet = {item["key"]: item["count"] for item in query_result.facets.get("types", [])}
        if types_facet:
            all_count = sum(v for k, v in types_facet.items() if k != "navigation")
        else:
            all_count = int(query_result.total_count or 0)

        type_tabs = [
            {"slug": "all", "label": "全部", "count": all_count, "is_active": (raw_type == "all" or not raw_type)},
        ]
        for tk in ordered_type_keys:
            type_tabs.append({
                "slug": tk,
                "label": get_type_display_label(tk),
                "count": types_facet.get(tk, 0),
                "is_active": (raw_type == tk),
            })

        # 生成订阅链接 (BLK-06)
        feed_url = None
        try:
            feed_url = reverse("discovery:feed_tag", kwargs={"slug": clean_slug})
        except Exception:
            feed_url = f"/feeds/tag/{clean_slug}/"

        canonical_url = f"{index_page.get_url(request)}{clean_slug}/"

        return {
            "mode": "detail",
            "tag": tag_obj,
            "tag_meta": tag_meta,
            "view_model": query_result,
            "type_tabs": type_tabs,
            "active_type": raw_type or "all",
            "scope_total_count": scope_total_count,
            "feed_url": feed_url,
            "canonical_url": canonical_url,
            "directory_url": index_page.get_url(request),
            "active_filters": {
                "q": q_term,
                "title_only": title_only,
                "sort": sort_param,
                "type": raw_type or "all",
                "date_from": date_from.isoformat() if date_from else "",
                "date_to": date_to.isoformat() if date_to else "",
            },
        }
