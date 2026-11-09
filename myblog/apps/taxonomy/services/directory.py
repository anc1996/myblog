"""
全站标签大厅目录与单标签跨类型详情聚合服务 (TagDirectoryService & TagDetailService)。

落实 RFC 规范与 D1~D4 / BLK-01~06 闭环：
1. TagDirectoryService:
   - 仅统计拥有 >=1 篇正式发布公开内容的有效标签；
   - 提取 Top 20 热门推荐标签云 (按引用数降序)；
   - A-Z 首字母导航分组与 # 组 (使用 initial_group_rank 彻底解决 ASCII 逆序丢数，BLK-04)；
   - 标签名即时搜索过滤；
2. TagDetailService:
   - 跨类型 Tab 分栏切换 (全部/博客/指南/项目，D2-A)；
   - 单快照 ContentQuery 统一执行，0 MongoDB 延迟；
   - 缺失友好导流 (BLK-05: 200 OK 友好建议，0 私有标签名称泄露)。
"""

from dataclasses import dataclass, field
from datetime import date
import logging
from typing import Any
from django.db.models import Count
from django.urls import reverse
from taggit.models import Tag
from wagtail.models import Site

from discovery.contracts import QuerySpec, QueryResult
from discovery.query import ContentQuery
from discovery.models import PublishedSearchDocument, PublishedSearchTag
from discovery.eligibility import apply_public_eligibility_filter
from content_lifecycle.access import AccessContext, AccessMode
from taxonomy.models import TagDirectoryMetadata

logger = logging.getLogger(__name__)


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


class TagDirectoryService:
    """全站标签大厅目录聚合服务 (模式一)"""

    @classmethod
    def get_tag_directory(cls, request: Any, index_page: Any) -> TagDirectoryViewModel:
        site = Site.find_for_request(request)
        search_q = (request.GET.get("q") or "").strip()

        # 1. 基础合法候选集 (仅限已上线、未回收)
        eligible_docs = PublishedSearchDocument.objects.filter(is_live=True, is_recycled=False)
        if site:
            eligible_docs = eligible_docs.filter(site_id=site.id)
        eligible_docs = apply_public_eligibility_filter(eligible_docs)

        # 2. 仅聚合拥有 >= 1 篇正式发布公开内容的有效标签 (决策四-A)
        tag_counts_qs = (
            PublishedSearchTag.objects.filter(document__in=eligible_docs)
            .values("tag_id", "tag_name", "tag_slug")
            .annotate(count=Count("document_id", distinct=True))
            .filter(count__gte=1)
        )

        if search_q:
            tag_counts_qs = tag_counts_qs.filter(tag_name__icontains=search_q)

        tag_records = list(tag_counts_qs)
        if not tag_records:
            return TagDirectoryViewModel(
                total_count=0,
                top_tags=[],
                grouped_tags={},
                search_query=search_q,
                is_empty=True,
                canonical_url=index_page.get_url(request),
            )

        # 3. 批量读取与关联 TagDirectoryMetadata
        tag_ids = [r["tag_id"] for r in tag_records]
        meta_dict = {
            m.tag_id: m
            for m in TagDirectoryMetadata.objects.filter(tag_id__in=tag_ids)
        }
        tags_by_id = {t.id: t for t in Tag.objects.filter(id__in=tag_ids)}

        directory_items: list[TagDirectoryItem] = []
        for r in tag_records:
            t_id = r["tag_id"]
            t_obj = tags_by_id.get(t_id)
            meta = meta_dict.get(t_id)
            if not meta and t_obj:
                meta = TagDirectoryMetadata.get_or_create_for_tag(t_obj)

            initial = meta.initial if meta else "#"
            group_rank = meta.initial_group_rank if meta else (0 if "A" <= initial <= "Z" else 1)
            description = meta.description if meta else ""

            # 安全反向解析 URL (纯符号 slug 容错兜底，BLK-04)
            safe_slug = r["tag_slug"]
            if not safe_slug:
                safe_slug = f"sym-{t_id}"

            try:
                tag_url = index_page.reverse_subpage("detail", kwargs={"tag_slug": safe_slug})
            except Exception:
                tag_url = f"{index_page.get_url(request)}{safe_slug}/"

            directory_items.append(
                TagDirectoryItem(
                    id=t_id,
                    name=r["tag_name"],
                    slug=safe_slug,
                    count=r["count"],
                    initial=initial,
                    initial_group_rank=group_rank,
                    url=tag_url,
                    description=description,
                )
            )

        # 4. Top 20 热门推荐标签云 (决策三-A: 按内容数量降序取前 20)
        top_tags = sorted(directory_items, key=lambda x: (-x.count, x.name))[:20]

        # 5. A-Z 首字母分组排序 (BLK-04: initial_group_rank 保证 A-Z 在前，# 在最后)
        sorted_by_initial = sorted(
            directory_items,
            key=lambda x: (x.initial_group_rank, x.initial, x.name.lower(), x.id),
        )

        grouped_tags: dict[str, list[TagDirectoryItem]] = {}
        for item in sorted_by_initial:
            grouped_tags.setdefault(item.initial, []).append(item)

        return TagDirectoryViewModel(
            total_count=len(directory_items),
            top_tags=top_tags,
            grouped_tags=grouped_tags,
            search_query=search_q,
            is_empty=False,
            canonical_url=index_page.get_url(request),
        )


class TagDetailService:
    """单标签跨类型检索与多维分栏服务 (模式二与模式三)"""

    @classmethod
    def get_tag_detail(cls, request: Any, index_page: Any, tag_slug: str) -> dict[str, Any]:
        site = Site.find_for_request(request)
        clean_slug = tag_slug.strip()

        # 查找对应 Tag 对象
        tag_obj = Tag.objects.filter(slug=clean_slug).first()

        # 基础合法集合 (核验该标签在当前公开集合中是否拥有 >= 1 篇内容)
        eligible_docs = PublishedSearchDocument.objects.filter(is_live=True, is_recycled=False)
        if site:
            eligible_docs = eligible_docs.filter(site_id=site.id)
        eligible_docs = apply_public_eligibility_filter(eligible_docs)

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
                try:
                    t_url = index_page.reverse_subpage("detail", kwargs={"tag_slug": s_slug})
                except Exception:
                    t_url = f"{index_page.get_url(request)}{s_slug}/"
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

        # 模式二：正常详情聚合页 (跨类型检索)
        tag_meta = TagDirectoryMetadata.get_or_create_for_tag(tag_obj)

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
            include_listing_scope=False,
        )

        # 单快照调用全站检索核心 (0 MongoDB 网络调用)
        query_result: QueryResult = ContentQuery.execute(spec, AccessContext(mode=AccessMode.PUBLIC))

        # 组装类型分栏 Tab 计数 (BLK-03 / 决策二-A)
        types_facet = {item["key"]: item["count"] for item in query_result.facets.get("types", [])}
        all_count = sum(types_facet.values()) if not q_term else len(query_result.items)
        blog_count = types_facet.get("blog_page", 0)
        guide_count = types_facet.get("guide", 0)
        project_count = types_facet.get("project", 0)

        type_tabs = [
            {"slug": "all", "label": "全部", "count": all_count, "is_active": (raw_type == "all" or not raw_type)},
            {"slug": "blog_page", "label": "博客文章", "count": blog_count, "is_active": (raw_type == "blog_page")},
            {"slug": "guide", "label": "技术指南", "count": guide_count, "is_active": (raw_type == "guide")},
            {"slug": "project", "label": "开源项目", "count": project_count, "is_active": (raw_type == "project")},
        ]

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
