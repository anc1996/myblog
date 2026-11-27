"""
api.views
第一阶段核心 RESTful JSON API 视图集。

落实核心后端契约：
1. 搜索与筛选 API (GET /api/v1/search/)：
   - 支持关键词、类型、标签、成文年份、月份组合过滤；
   - 支持游标分页 (next_cursor) 与页码辅助转换 (page/page_size)；
   - SEARCH-MODE-02 规则（模式 A/B 指示与展开标记）；
   - Redis Epoch 动态代次缓存（发布/下架自动使旧代次缓存失效，零 TTL 滞后）；
   - Redis 滑动窗口限流（60 次/分钟/IP）。
2. 文章详情 API (GET /api/v1/posts/<slug>/)：
   - 双形态正文输出：结构化 blocks 积木字典 + 预渲染 safe_html 字符串；
   - 包含多作者、标签、配图（受控交付标识与直达 URL）及成文时间；
   - Redis Epoch 代次缓存 + 限流保护（120 次/分钟/IP）。
3. 文章列表 API (GET /api/v1/posts/)：
   - 针对前台列表与翻页设计的极简只读元数据接口。
"""

import hashlib
import json
import logging
from typing import Any
from django.core.cache import cache
from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils.safestring import mark_safe

from discovery.contracts import QuerySpec
from discovery.query import ContentQuery, _encode_cursor
from discovery.models import ContentDiscoveryEpoch, PublishedSearchDocument
from discovery.registry import get_adapter_by_public_type_slug, get_type_adapter, resolve_adapter_model
from content_lifecycle.access import AccessContext, AccessMode
from content_lifecycle.public_access import (
    filter_public_page_queryset,
    get_public_blocked_path_prefixes,
    resolve_public_site_for_request,
)
from .limiter import ratelimit_api

logger = logging.getLogger(__name__)


def _cors_json_response(data: dict, status: int = 200, extra_headers: dict | None = None) -> JsonResponse:
    """生成带基础 CORS 头的 JsonResponse"""
    response = JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response["Access-Control-Allow-Headers"] = "Content-Type, X-Requested-With"
    if extra_headers:
        for k, v in extra_headers.items():
            response[k] = v
    return response


@ratelimit_api(scope="search", max_requests=60, window_seconds=60)
def search_api(request):
    """
    全站搜索与组合筛选 JSON API
    GET /api/v1/search/
    """
    search_query = request.GET.get("query", "").strip()

    # 标签筛选
    tag_id_str = request.GET.get("tag", "").strip()
    tag_ids = [int(tag_id_str)] if tag_id_str.isdigit() else []

    # 类型筛选
    type_key = request.GET.get("type", "").strip()
    type_keys = [type_key] if type_key else []

    # 日期范围
    year_str = request.GET.get("year", "").strip()
    year = int(year_str) if year_str.isdigit() else None
    month_str = request.GET.get("month", "").strip()
    month = int(month_str) if month_str.isdigit() else None

    # 分页与模式控制
    cursor = request.GET.get("cursor", None)
    page_str = request.GET.get("page", "").strip()
    page_size_str = request.GET.get("page_size", "10").strip()
    page_size = max(1, min(int(page_size_str) if page_size_str.isdigit() else 10, 50))

    secondary = request.GET.get("secondary", "none").strip()
    expand = request.GET.get("expand", "auto").strip()

    # 当前全站 Epoch 代次
    current_epoch = ContentDiscoveryEpoch.get_current_epoch()
    site = resolve_public_site_for_request(request)
    if site is None:
        return _cors_json_response(
            {
                "status": "success",
                "epoch": current_epoch,
                "query": search_query,
                "total_count": 0,
                "page_size": page_size,
                "next_cursor": None,
                "cursor_reset": False,
                "expansion_used": False,
                "secondary_used": False,
                "show_expand_button": False,
                "items": [],
                "facets": {},
                "archive_groups": {},
            },
            extra_headers={"X-Cache": "MISS", "X-Epoch": str(current_epoch), "Cache-Control": "no-store"},
        )

    # 辅助机制：若前端传了传统页码 page（如 page=2）且未传 cursor，自动折算为 offset 游标
    if not cursor and page_str.isdigit() and int(page_str) > 1:
        page_num = int(page_str)
        calc_offset = (page_num - 1) * page_size
        cursor = _encode_cursor(current_epoch, calc_offset)

    # 1. 尝试读取 Redis Epoch 缓存
    # 将当前站点的实时公开权限指纹纳入缓存键，确保即便信号异常未能自增 epoch，
    # 任意页面的限制/回收/下架也会立刻反映到总数、分面、归档与分页结果中。
    from django.contrib.postgres.aggregates import StringAgg
    from django.db.models import CharField, Value
    from django.db.models.functions import Cast, Coalesce, Concat, MD5
    from wagtail.models import Page as WagtailPage

    blocked_prefixes = get_public_blocked_path_prefixes(site=site)
    public_qs = filter_public_page_queryset(
        WagtailPage.objects.all(),
        site=site,
        request=request,
    )
    members_digest = public_qs.aggregate(
        sig=MD5(
            Coalesce(
                StringAgg(
                    Concat(
                        Cast("id", CharField()),
                        Value(":"),
                        Coalesce(Cast("live_revision_id", CharField()), Value("")),
                    ),
                    delimiter=Value(","),
                    order_by="id",
                    output_field=CharField(),
                ),
                Value(""),
            )
        )
    )["sig"] or ""
    security_sig = hashlib.md5(
        ("|".join(blocked_prefixes) + f":{members_digest}").encode("utf-8")
    ).hexdigest()[:12]
    params_sorted = sorted(request.GET.items())
    param_hash = hashlib.md5(json.dumps(params_sorted).encode("utf-8")).hexdigest()
    cache_key = f"api:search:v3:s{site.id}:{current_epoch}:{security_sig}:{param_hash}"
    cached_payload = cache.get(cache_key)

    if cached_payload is not None:
        return _cors_json_response(
            cached_payload,
            extra_headers={"X-Cache": "HIT", "X-Epoch": str(current_epoch), "Cache-Control": "private, no-store"},
        )

    # 2. 执行只读快照检索 (ContentQuery)
    spec = QuerySpec(
        q=search_query or None,
        site_id=site.id,
        type_keys=type_keys,
        tag_ids=tag_ids,
        year=year,
        month=month,
        secondary=secondary,
        expand=expand,
        cursor=cursor,
        page_size=page_size,
        facets=["types", "tags", "years"],
    )

    access_context = AccessContext(user=None, mode=AccessMode.PUBLIC)
    result = ContentQuery.execute(spec, access_context)

    # SEARCH-MODE-02: 模式 A 命中少于 5 条时的手动展开判断
    a_count = len([it for it in result.items if it.match_mode == "A"])
    show_expand_button = (
        (1 <= a_count < 5)
        and not result.secondary_used
        and bool(search_query)
    )

    # 3. 序列化标准 JSON 负载
    items_data = []
    for it in result.items:
        items_data.append({
            "id": it.page_id,
            "title": it.title,
            "url": it.url,
            "authored_on": it.authored_on.strftime("%Y-%m-%d") if it.authored_on else None,
            "type_key": it.type_key,
            "summary": it.summary,
            "match_mode": it.match_mode,
            "highlights": [h.get("snippet", "") if isinstance(h, dict) else getattr(h, "snippet", "") for h in it.highlights],
            "tags": it.tags or [],
        })

    response_payload = {
        "status": "success",
        "epoch": current_epoch,
        "query": search_query,
        "total_count": result.total_count,
        "page_size": page_size,
        "next_cursor": result.next_cursor,
        "cursor_reset": result.cursor_reset,
        "expansion_used": result.expansion_used,
        "secondary_used": result.secondary_used,
        "show_expand_button": show_expand_button,
        "items": items_data,
        "facets": result.facets,
        "archive_groups": result.archive_groups,
    }

    # 写入 Redis 缓存（缓存 300 秒；若后台发布/回收自增 epoch，直接从命名空间瞬时失效）
    cache.set(cache_key, response_payload, timeout=300)

    return _cors_json_response(
        response_payload,
        extra_headers={"X-Cache": "MISS", "X-Epoch": str(current_epoch), "Cache-Control": "private, no-store"},
    )


def _parse_page_params(request) -> tuple[int, int]:
    page_str = request.GET.get("page", "1").strip()
    page_num = int(page_str) if page_str.isdigit() and int(page_str) > 0 else 1
    page_size_str = request.GET.get("page_size", "10").strip()
    page_size = max(1, min(int(page_size_str) if page_size_str.isdigit() else 10, 50))
    return page_num, page_size


def _serve_adapter_detail_api(
    request,
    *,
    type_key: str,
    slug: str,
    cache_prefix: str,
    not_found_message: str,
    raise_http404: bool = False,
    readonly_epoch: bool = False,
):
    """通用注册表详情 API：通过 TypeAdapter 动态解析模型与水合序列化，消除业务 Page 导入。"""
    adapter = get_type_adapter(type_key)
    model_cls = resolve_adapter_model(adapter) if adapter is not None else None
    if adapter is None or model_cls is None or not hasattr(adapter, "serialize_api_detail"):
        return _cors_json_response({"status": "error", "code": 404, "message": not_found_message}, status=404)

    site = resolve_public_site_for_request(request)
    if site is None:
        if raise_http404:
            raise Http404(not_found_message)
        return _cors_json_response(
            {"status": "error", "code": 404, "message": not_found_message},
            status=404,
            extra_headers={"Cache-Control": "no-store"},
        )

    current_epoch = (
        ContentDiscoveryEpoch.get_current_epoch_readonly()
        if readonly_epoch
        else ContentDiscoveryEpoch.get_current_epoch()
    )
    candidates = list(
        filter_public_page_queryset(
            model_cls.objects.filter(slug=slug),
            site=site,
            request=request,
        ).order_by("path")[:2]
    )
    if len(candidates) != 1:
        if raise_http404:
            raise Http404(not_found_message)
        return _cors_json_response(
            {"status": "error", "code": 404, "message": not_found_message},
            status=404,
            extra_headers={"Cache-Control": "no-store"},
        )
    page = candidates[0]

    if hasattr(page, "hydrate_from_mongo"):
        access_context = AccessContext(user=None, mode=AccessMode.PUBLIC)
        page.hydrate_from_mongo(access_context=access_context)

    payload = {
        "status": "success",
        "epoch": current_epoch,
        "data": adapter.serialize_api_detail(page, request=request),
    }
    return _cors_json_response(
        payload,
        extra_headers={"X-Cache": "BYPASS", "X-Epoch": str(current_epoch), "Cache-Control": "no-store"},
    )


def _serve_adapter_list_api(
    request,
    *,
    type_key: str,
    cache_key: str,
    cache_timeout: int = 180,
    envelope: str = "items",
    readonly_epoch: bool = False,
):
    """通用注册表列表 API：通过 TypeAdapter 动态构建查询与序列化条目。"""
    adapter = get_type_adapter(type_key)
    model_cls = resolve_adapter_model(adapter) if adapter is not None else None
    if adapter is None or model_cls is None or not hasattr(adapter, "serialize_api_list_item"):
        return _cors_json_response({"status": "error", "code": 404, "message": "未找到对应内容类型"}, status=404)

    site = resolve_public_site_for_request(request)
    if site is None:
        return _cors_json_response(
            {"status": "error", "code": 404, "message": "未找到对应站点"},
            status=404,
            extra_headers={"Cache-Control": "no-store"},
        )

    page_num, page_size = _parse_page_params(request)
    current_epoch = (
        ContentDiscoveryEpoch.get_current_epoch_readonly()
        if readonly_epoch
        else ContentDiscoveryEpoch.get_current_epoch()
    )
    if hasattr(adapter, "get_api_list_queryset"):
        qs = adapter.get_api_list_queryset(model_cls, request)
    else:
        qs = model_cls.objects.filter(live=True, is_recycled=False).order_by("-authored_on", "-id")
    qs = filter_public_page_queryset(qs, site=site, request=request)

    total_count = qs.count()
    offset = (page_num - 1) * page_size
    pages = qs[offset : offset + page_size]
    items = [adapter.serialize_api_list_item(p) for p in pages]
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
    has_next = offset + page_size < total_count
    has_prev = page_num > 1

    if envelope == "pagination":
        payload = {
            "status": "success",
            "epoch": current_epoch,
            "pagination": {
                "total_count": total_count,
                "page": page_num,
                "page_size": page_size,
                "total_pages": total_pages,
                "has_next": has_next,
                "has_prev": has_prev,
            },
            "data": items,
        }
    else:
        payload = {
            "status": "success",
            "epoch": current_epoch,
            "total_count": total_count,
            "current_page": page_num,
            "page_size": page_size,
            "total_pages": total_pages,
            "has_next": has_next,
            "has_prev": has_prev,
            "items": items,
        }

    return _cors_json_response(
        payload,
        extra_headers={"X-Cache": "BYPASS", "X-Epoch": str(current_epoch), "Cache-Control": "no-store"},
    )


@ratelimit_api(scope="post_detail", max_requests=120, window_seconds=60)
def post_detail_api(request, slug):
    """
    文章详情 JSON API（双形态积木输出）
    GET /api/v1/posts/<slug>/
    """
    return _serve_adapter_detail_api(
        request,
        type_key="blog_page",
        slug=slug,
        cache_prefix="api:post",
        not_found_message="文章不存在或已下架",
    )


@ratelimit_api(scope="post_list", max_requests=60, window_seconds=60)
def post_list_api(request):
    """
    文章列表分页 JSON API
    GET /api/v1/posts/
    """
    page_num, page_size = _parse_page_params(request)
    tag_slug = request.GET.get("tag", "").strip()
    current_epoch = ContentDiscoveryEpoch.get_current_epoch()
    cache_key = f"api:posts_list:{current_epoch}:p{page_num}_s{page_size}_t{tag_slug}"
    return _serve_adapter_list_api(request, type_key="blog_page", cache_key=cache_key, cache_timeout=180, envelope="items")


@ratelimit_api(scope="guide_list", max_requests=60, window_seconds=60)
def guides_list_api(request):
    """
    专题指南专栏列表 API
    GET /api/v1/guides/
    """
    page_num, page_size = _parse_page_params(request)
    topic_slug = request.GET.get("topic", "").strip()
    difficulty = request.GET.get("difficulty", "").strip()
    current_epoch = ContentDiscoveryEpoch.get_current_epoch()
    cache_key = f"api:guides_list:{current_epoch}:p{page_num}_s{page_size}_t{topic_slug}_d{difficulty}"
    return _serve_adapter_list_api(request, type_key="guide", cache_key=cache_key, cache_timeout=180, envelope="items")


@ratelimit_api(scope="guide_detail", max_requests=120, window_seconds=60)
def guide_detail_api(request, slug):
    """
    专题指南详情 API（双形态积木输出与技术复核告警）
    GET /api/v1/guides/<slug>/
    """
    return _serve_adapter_detail_api(
        request,
        type_key="guide",
        slug=slug,
        cache_prefix="api:guide",
        not_found_message="专栏指南不存在或已下架",
    )


@ratelimit_api(scope="archives", max_requests=120, window_seconds=60)
def timeline_archives_api(request):
    """
    全站多页面时间线归档 API (GET /api/v1/archives/)。
    支持按 type, tag, topic, year 过滤，返回年月分桶统计与时间线。
    支持弱 ETag 与 304 快速缓存协商。
    """
    from discovery.feeds import compute_public_timeline_archives
    from django.utils.cache import get_conditional_response
    from wagtail.models import Site

    site = Site.find_for_request(request)
    site_id = site.id if site else None

    type_slug = request.GET.get("type", "").strip() or None
    tag_slug = request.GET.get("tag", "").strip() or None
    topic_slug = request.GET.get("topic", "").strip() or None
    year_param = request.GET.get("year", "").strip()
    year_val = None
    if year_param.isdigit():
        year_val = int(year_param)

    from django.utils import timezone
    epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()
    site_today = timezone.localdate().isoformat()
    weak_etag = f'W/"archives:v1:s{site_id or 0}:e{epoch}:d{site_today}:t{type_slug or "all"}:tag{tag_slug or "all"}:top{topic_slug or "all"}:y{year_val or "all"}"'

    conditional_response = get_conditional_response(request, etag=weak_etag)
    if conditional_response is not None:
        conditional_response["ETag"] = weak_etag
        conditional_response["X-Epoch"] = str(epoch)
        conditional_response["Cache-Control"] = "public, max-age=60"
        return conditional_response

    data = compute_public_timeline_archives(
        site_id=site_id,
        type_slug=type_slug,
        tag_slug=tag_slug,
        topic_slug=topic_slug,
        year=year_val,
    )

    payload = {
        "status": "success",
        "epoch": epoch,
        "data": data,
    }
    resp = _cors_json_response(payload, extra_headers={"ETag": weak_etag, "X-Epoch": str(epoch)})
    resp["Cache-Control"] = "public, max-age=60"
    return resp


def archive_calendar_api(request):
    """
    按月日历归档统计接口 (GET /api/v1/archives/calendar/?month=YYYY-MM&type=blog)。
    仅读取 ArchiveDailyCount（最多 31 行日桶），并绑定代次/权限版本/站点日期的弱 ETag 与 Redis 短缓存。
    """
    from django.core.cache import cache
    from django.utils import timezone
    from django.utils.cache import get_conditional_response
    from wagtail.models import Site
    from discovery.archive_counts import get_archive_calendar_data
    from discovery.models import ArchiveCountState

    site = Site.find_for_request(request)
    site_id = site.id if site else None
    site_today = timezone.localdate()

    raw_month = (request.GET.get("month") or "").strip()
    target_year = site_today.year
    target_month = site_today.month
    if raw_month and "-" in raw_month:
        parts = raw_month.split("-", 1)
        if parts[0].isdigit() and parts[1].isdigit():
            y_v, m_v = int(parts[0]), int(parts[1])
            if 1970 <= y_v <= 2100 and 1 <= m_v <= 12:
                target_year, target_month = y_v, m_v
    else:
        if (request.GET.get("year") or "").isdigit():
            target_year = int(request.GET.get("year"))
        if (request.GET.get("month") or "").isdigit():
            m_v = int(request.GET.get("month"))
            if 1 <= m_v <= 12:
                target_month = m_v

    type_slug = (request.GET.get("type") or "").strip().lower() or None
    epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()
    active_gen, is_stale = ArchiveCountState.get_active_generation_readonly()
    month_key = f"{target_year:04d}-{target_month:02d}"
    weak_etag = (
        f'W/"archcal:v1:s{site_id or 0}:e{epoch}:g{active_gen}:'
        f'd{site_today.isoformat()}:m{month_key}:t{type_slug or "all"}"'
    )

    cond_resp = get_conditional_response(request, etag=weak_etag)
    if cond_resp is not None:
        cond_resp["ETag"] = weak_etag
        cond_resp["X-Epoch"] = str(epoch)
        cond_resp["Cache-Control"] = "public, max-age=60"
        return cond_resp

    cache_key = f"myblog:archcal:v1:s{site_id or 0}:e{epoch}:g{active_gen}:d{site_today.isoformat()}:m{month_key}:t{type_slug or 'all'}"
    cached_data = cache.get(cache_key)
    if cached_data is not None:
        resp = _cors_json_response(
            {"status": "success", "epoch": epoch, "data": cached_data},
            extra_headers={"ETag": weak_etag, "X-Epoch": str(epoch), "X-Cache": "HIT"},
        )
        resp["Cache-Control"] = "public, max-age=60"
        return resp

    cal_data = get_archive_calendar_data(
        site_id=site_id,
        year=target_year,
        month=target_month,
        type_slug=type_slug,
        base_url="/timeline/",
    )
    if not is_stale:
        cache.set(cache_key, cal_data, timeout=120)

    resp = _cors_json_response(
        {"status": "success", "epoch": epoch, "data": cal_data},
        extra_headers={"ETag": weak_etag, "X-Epoch": str(epoch), "X-Cache": "MISS"},
    )
    resp["Cache-Control"] = "public, max-age=60"
    return resp


@ratelimit_api(scope="project_list", max_requests=60, window_seconds=60)
def project_list_api(request):
    """
    开源作品集与项目列表 API
    GET /api/v1/projects/
    """
    page_num, page_size = _parse_page_params(request)
    status_filter = request.GET.get("status", "").strip()
    lang_filter = request.GET.get("language", "").strip()
    tag_slug = request.GET.get("tag", "").strip()
    current_epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()
    cache_key = f"api:projects_list:{current_epoch}:p{page_num}_s{page_size}_st{status_filter}_l{lang_filter}_t{tag_slug}"
    return _serve_adapter_list_api(
        request,
        type_key="project",
        cache_key=cache_key,
        cache_timeout=600,
        envelope="pagination",
        readonly_epoch=True,
    )


@ratelimit_api(scope="project_detail", max_requests=120, window_seconds=60)
def project_detail_api(request, slug):
    """
    开源作品集与项目详情 API
    GET /api/v1/projects/<slug>/
    """
    return _serve_adapter_detail_api(
        request,
        type_key="project",
        slug=slug,
        cache_prefix="api:project_detail",
        not_found_message="项目不存在或已下架",
        raise_http404=True,
        readonly_epoch=True,
    )


@ratelimit_api(scope="content_list", max_requests=60, window_seconds=60)
def content_list_api(request, type_slug):
    """通用已注册内容类型列表接口：GET /api/v1/content/<type_slug>/"""
    adapter = get_adapter_by_public_type_slug(type_slug)
    if adapter is None:
        return _cors_json_response({"status": "error", "code": 404, "message": "未知内容类型"}, status=404)
    page_num, page_size = _parse_page_params(request)
    tag_slug = request.GET.get("tag", "").strip()
    current_epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()
    cache_key = f"api:content_list:{adapter.type_key}:{current_epoch}:p{page_num}_s{page_size}_t{tag_slug}"
    return _serve_adapter_list_api(
        request,
        type_key=adapter.type_key,
        cache_key=cache_key,
        cache_timeout=180,
        envelope="items",
        readonly_epoch=True,
    )


@ratelimit_api(scope="content_detail", max_requests=120, window_seconds=60)
def content_detail_api(request, type_slug, slug):
    """通用已注册内容类型详情接口：GET /api/v1/content/<type_slug>/<slug>/"""
    adapter = get_adapter_by_public_type_slug(type_slug)
    if adapter is None:
        return _cors_json_response({"status": "error", "code": 404, "message": "未知内容类型"}, status=404)
    return _serve_adapter_detail_api(
        request,
        type_key=adapter.type_key,
        slug=slug,
        cache_prefix=f"api:content_detail:{adapter.type_key}",
        not_found_message="内容不存在或已下架",
        readonly_epoch=True,
    )


@ratelimit_api(scope="site_settings", max_requests=120, window_seconds=60)
def site_settings_api(request):
    """
    全站全局导航与站点设置只读 API (H-03 & H-07)
    GET /api/v1/site/settings/
    严格只读查询 filter(singleton_key=1)，严禁调用 .load()；
    菜单项关联页面批量核验公开资格，下架或回收页面自动优雅降级。
    """
    from site_settings.models import NavigationSettings, SocialMediaSettings, FooterSettings
    from wagtail.models import Page
    from django.utils.cache import get_conditional_response

    site = resolve_public_site_for_request(request)
    if site is None:
        return _cors_json_response(
            {"status": "error", "code": 404, "message": "未找到对应站点"},
            status=404,
            extra_headers={"Cache-Control": "no-store"},
        )
    site_id = site.id
    current_epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()

    nav_obj = NavigationSettings.objects.filter(singleton_key=1).first()
    social_obj = SocialMediaSettings.objects.filter(singleton_key=1).first()
    footer_obj = FooterSettings.objects.filter(singleton_key=1).first()

    # 1. 解析导航菜单并收集被引用页面
    raw_nav_items = []
    target_page_ids = set()
    if nav_obj and nav_obj.nav_items:
        for block in nav_obj.nav_items:
            val = block.value
            p_obj = val.get("target_page")
            p_id = getattr(p_obj, "id", None) if p_obj else None
            if p_id:
                target_page_ids.add(p_id)
            raw_nav_items.append({
                "label": val.get("label", ""),
                "badge": val.get("badge", ""),
                "target_page_id": p_id,
                "external_url": val.get("external_url", ""),
                "is_external": bool(val.get("is_external", False)),
            })

    # 批量核验页面可用性 (H-07)
    allowed_page_urls = {}
    if target_page_ids:
        pages_qs = filter_public_page_queryset(
            Page.objects.filter(id__in=target_page_ids),
            site=site,
            request=request,
        )
        for p in pages_qs:
            url = (p.get_url(request=request) if hasattr(p, "get_url") else p.url) or ""
            if url:
                allowed_page_urls[p.id] = url

    allowed_sig = hashlib.md5(
        "|".join(f"{pid}:{allowed_page_urls[pid]}" for pid in sorted(allowed_page_urls.keys())).encode("utf-8")
    ).hexdigest()[:12]
    weak_etag = f'W/"site_settings:v3:s{site_id}:e{current_epoch}:{allowed_sig}"'
    conditional_resp = get_conditional_response(request, etag=weak_etag)
    if conditional_resp is not None:
        conditional_resp["ETag"] = weak_etag
        conditional_resp["X-Epoch"] = str(current_epoch)
        conditional_resp["Cache-Control"] = "private, no-cache"
        return conditional_resp

    # 过滤并降级导航
    nav_items = []
    for item in raw_nav_items:
        ext_url = item["external_url"]
        p_id = item["target_page_id"]
        final_url = ""
        if ext_url:
            final_url = ext_url
        elif p_id and p_id in allowed_page_urls:
            final_url = allowed_page_urls[p_id]
        elif not p_id and not ext_url:
            final_url = ""
        else:
            # 关联的页面已被回收或下架，跳过该项 (优雅降级)
            continue

        nav_items.append({
            "label": item["label"],
            "badge": item["badge"],
            "url": final_url,
            "is_external": item["is_external"],
        })

    # 2. 解析社交媒体链接
    social_links = [
        {
            "platform": item["platform"],
            "label": item["label"],
            "url": item["url"],
        }
        for item in SocialMediaSettings.get_public_links_readonly(
            social_obj,
            fallback_to_defaults=False,
        )
    ]

    # 3. 解析页脚设置
    footer_data = {
        "copyright_text": footer_obj.copyright_text if footer_obj else "© 2026 myblog.",
        "icp_number": footer_obj.icp_number if footer_obj else "",
        "police_record_number": footer_obj.police_record_number if footer_obj else "",
        "about_text": footer_obj.about_text if footer_obj else "",
    }

    payload = {
        "status": "success",
        "epoch": current_epoch,
        "data": {
            "navigation": nav_items,
            "social_links": social_links,
            "footer": footer_data,
        },
    }
    resp = _cors_json_response(payload, extra_headers={"ETag": weak_etag, "X-Epoch": str(current_epoch)})
    resp["Cache-Control"] = "private, no-cache"
    return resp
