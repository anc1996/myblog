"""
全站层级列表与多维检索核心服务 (IndexListingService)。

落实 RFC 规范与 B-01/B-03/D1~D4 闭环：
1. 单入口服务：接收 Page 与 Request，生成开箱即用的 ListingViewModel；
2. 缓存 Site 实例至 request._wagtail_site，实现 base.html 模板渲染 0 延迟 SQL；
3. 显式支持 Wagtail 8.1 后台预览模式 (is_preview)，新建未落库时返回空模型避免 404；
4. 规范化组装 QuerySpec 并通过 ContentQuery.execute 单快照安全交付。
"""

import logging
from urllib.parse import urlencode
from typing import Any
from wagtail.models import Site

from django.http import Http404
from discovery.contracts import QuerySpec
from discovery.query import ContentQuery
from discovery.listing.contracts import ListingPolicy, ListingFilters, ListingViewModel
from discovery.listing.params import parse_listing_filters
from content_lifecycle.access import AccessContext, AccessMode

logger = logging.getLogger(__name__)


class IndexListingService:
    """全站层级列表与多维检索核心应用服务"""

    @classmethod
    def build(cls, index_page: Any, request: Any) -> ListingViewModel:
        """针对指定的 IndexPage 实例与当前请求构建统一的 ListingViewModel"""
        is_preview = bool(
            getattr(request, "_is_listing_preview", False)
            or getattr(request, "is_preview", False)
            or getattr(request, "in_preview_panel", False)
        )

        # Wagtail 8.1 后台新建预览特殊防御 (B-01): self.pk 为 None 时直接返回空模型
        if is_preview and not getattr(index_page, "pk", None):
            return ListingViewModel(
                items=[],
                total_count=0,
                facets={},
                is_preview=True,
                is_empty=True,
                canonical_url="",
            )

        # 解析并缓存站点实例 (满足 V20 快照退出后零延迟查询)
        site = None
        try:
            site = Site.find_for_request(request)
            if site:
                request._wagtail_site = site
        except Exception:
            site = None

        # 获取页面能力声明策略
        if hasattr(index_page, "get_listing_policy"):
            policy: ListingPolicy = index_page.get_listing_policy(request)
        else:
            policy = ListingPolicy(type_keys=("blog_page",))

        # 解析并清洗 GET 参数
        filters: ListingFilters = parse_listing_filters(request, policy)

        # 组装公共查询规范 (QuerySpec)
        site_id = site.id if site else None
        spec = QuerySpec(
            q=filters.q or None,
            search_fields="title" if filters.title_only else "all",
            type_keys=list(policy.type_keys),
            site_id=site_id,
            root_page_id=index_page.pk,
            tree_scope="descendants" if filters.include_descendants else "children",
            include_root=False,
            include_listing_scope=True,
            date_from=filters.date_from,
            date_to=filters.date_to,
            sort=filters.sort,
            facets=list(policy.facet_keys),
            page_size=filters.page_size,
            cursor=filters.cursor,
            navigation_cursor=filters.navigation_cursor,
            extra_filters=filters.extra_filters,
        )

        # 计算规范化 URL 与分页查询参数
        try:
            canonical_url = index_page.get_url(request) if hasattr(index_page, "get_url") else getattr(index_page, "url_path", "")
        except Exception:
            canonical_url = getattr(index_page, "url_path", "")

        # 调用全站统一查询引擎 (单快照执行，0 MongoDB 网络访问)
        try:
            query_result = ContentQuery.execute(spec, AccessContext(mode=AccessMode.PUBLIC))
        except Http404:
            if is_preview:
                return ListingViewModel(
                    items=[],
                    total_count=0,
                    facets={},
                    is_preview=True,
                    is_empty=True,
                    canonical_url=canonical_url,
                )
            raise
        except Exception as exc:
            logger.exception("IndexListingService ContentQuery failed for page %s: %s", getattr(index_page, "pk", None), exc)
            return ListingViewModel(
                items=[],
                total_count=0,
                facets={},
                is_empty=True,
                http_status=500,
                error_message="检索服务暂时不可用，请稍后重试。",
            )

        active_filters: dict[str, Any] = {
            "q": filters.q,
            "title_only": filters.title_only,
            "include_descendants": filters.include_descendants,
            "date_from": filters.date_from.isoformat() if filters.date_from else "",
            "date_to": filters.date_to.isoformat() if filters.date_to else "",
            "sort": filters.sort,
            **filters.extra_filters,
        }

        # 过滤掉空值以生成纯净的 QueryString
        clean_params = {k: v for k, v in active_filters.items() if v not in ("", None, False)}
        if filters.include_descendants != policy.descendants_default:
            clean_params["include_descendants"] = "1" if filters.include_descendants else "0"
        else:
            clean_params.pop("include_descendants", None)

        query_string = urlencode(clean_params)

        return ListingViewModel(
            items=query_result.items,
            total_count=query_result.total_count,
            facets=query_result.facets,
            next_cursor=query_result.next_cursor,
            previous_cursor=query_result.previous_cursor,
            navigation_items=query_result.navigation_items,
            navigation_total_count=query_result.navigation_total_count,
            canonical_url=canonical_url,
            active_filters=active_filters,
            query_string=query_string,
            is_empty=(query_result.total_count == 0),
            http_status=200,
            cursor_reset=query_result.cursor_reset,
            is_preview=is_preview,
            corpus_epoch=query_result.corpus_epoch,
        )
