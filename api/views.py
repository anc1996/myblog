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
from content_lifecycle.access import AccessContext, AccessMode
from blog.models import BlogPage
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

    # 辅助机制：若前端传了传统页码 page（如 page=2）且未传 cursor，自动折算为 offset 游标
    if not cursor and page_str.isdigit() and int(page_str) > 1:
        page_num = int(page_str)
        calc_offset = (page_num - 1) * page_size
        cursor = _encode_cursor(current_epoch, calc_offset)

    # 1. 尝试读取 Redis Epoch 缓存
    params_sorted = sorted(request.GET.items())
    param_hash = hashlib.md5(json.dumps(params_sorted).encode("utf-8")).hexdigest()
    cache_key = f"api:search:{current_epoch}:{param_hash}"
    cached_payload = cache.get(cache_key)

    if cached_payload is not None:
        return _cors_json_response(cached_payload, extra_headers={"X-Cache": "HIT", "X-Epoch": str(current_epoch)})

    # 2. 执行只读快照检索 (ContentQuery)
    spec = QuerySpec(
        q=search_query or None,
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

    access_context = AccessContext(user=request.user, mode=AccessMode.PUBLIC)
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

    return _cors_json_response(response_payload, extra_headers={"X-Cache": "MISS", "X-Epoch": str(current_epoch)})


@ratelimit_api(scope="post_detail", max_requests=120, window_seconds=60)
def post_detail_api(request, slug):
    """
    文章详情 JSON API（双形态积木输出）
    GET /api/v1/posts/<slug>/
    """
    current_epoch = ContentDiscoveryEpoch.get_current_epoch()
    cache_key = f"api:post:{current_epoch}:{slug}"
    cached_payload = cache.get(cache_key)

    if cached_payload is not None:
        return _cors_json_response(cached_payload, extra_headers={"X-Cache": "HIT", "X-Epoch": str(current_epoch)})

    # 查询已发布的正式页面
    page = BlogPage.objects.filter(slug=slug, live=True).first()
    if not page or getattr(page, "is_recycled", False):
        return _cors_json_response(
            {"status": "error", "code": 404, "message": "文章不存在或已下架"},
            status=404,
        )

    # 从 MongoDB 精准水合正式生效版本
    access_context = AccessContext(user=request.user, mode=AccessMode.PUBLIC)
    page.hydrate_from_mongo(access_context=access_context)

    # 1. 结构化积木列表 (blocks)
    blocks_data = []
    for b in page.body:
        val = b.block.get_prep_value(b.value) if hasattr(b.block, "get_prep_value") else b.value
        blocks_data.append({
            "type": b.block_type,
            "value": val,
        })

    # 2. 预渲染安全 HTML (safe_html)
    safe_html_str = str(page.body)
    intro_html_str = str(page.intro) if page.intro else ""

    # 3. 关联元数据 (作者、标签、封面)
    authors_data = []
    for a in page.authors.select_related("person").all():
        if a.person:
            authors_data.append({
                "id": a.person.id,
                "name": a.person.name,
                "role": a.role or "作者",
                "bio": a.person.bio or "",
                "avatar_url": a.person.avatar.file.url if getattr(a.person, "avatar", None) else None,
            })

    tags_data = [{"id": t.id, "name": t.name, "slug": t.slug} for t in page.tags.all()]

    cover_data = None
    if page.featured_image:
        cover_data = {
            "id": page.featured_image.id,
            "title": page.featured_image.title,
            "public_id": str(page.featured_image.public_id),
            "url": page.featured_image.file.url,
            "width": page.featured_image.width,
            "height": page.featured_image.height,
        }

    post_payload = {
        "status": "success",
        "epoch": current_epoch,
        "data": {
            "id": page.id,
            "title": page.title,
            "slug": page.slug,
            "url": page.url,
            "authored_on": page.authored_on.strftime("%Y-%m-%d") if page.authored_on else None,
            "intro": intro_html_str,
            "featured_image": cover_data,
            "authors": authors_data,
            "tags": tags_data,
            "blocks": blocks_data,
            "safe_html": safe_html_str,
        },
    }

    cache.set(cache_key, post_payload, timeout=600)
    return _cors_json_response(post_payload, extra_headers={"X-Cache": "MISS", "X-Epoch": str(current_epoch)})


@ratelimit_api(scope="post_list", max_requests=60, window_seconds=60)
def post_list_api(request):
    """
    文章列表分页 JSON API
    GET /api/v1/posts/
    """
    page_str = request.GET.get("page", "1").strip()
    page_num = int(page_str) if page_str.isdigit() and int(page_str) > 0 else 1

    page_size_str = request.GET.get("page_size", "10").strip()
    page_size = max(1, min(int(page_size_str) if page_size_str.isdigit() else 10, 50))

    tag_slug = request.GET.get("tag", "").strip()

    current_epoch = ContentDiscoveryEpoch.get_current_epoch()
    cache_key = f"api:posts_list:{current_epoch}:p{page_num}_s{page_size}_t{tag_slug}"
    cached_payload = cache.get(cache_key)

    if cached_payload is not None:
        return _cors_json_response(cached_payload, extra_headers={"X-Cache": "HIT", "X-Epoch": str(current_epoch)})

    qs = BlogPage.objects.filter(live=True).order_by("-authored_on", "-first_published_at")
    if tag_slug:
        qs = qs.filter(tags__slug=tag_slug)

    total_count = qs.count()
    offset = (page_num - 1) * page_size
    pages = qs.select_related("featured_image").prefetch_related("tags", "authors__person")[offset : offset + page_size]

    items = []
    for p in pages:
        authors = [{"id": a.person.id, "name": a.person.name, "role": a.role} for a in p.authors.all() if a.person]
        tags = [{"id": t.id, "name": t.name, "slug": t.slug} for t in p.tags.all()]
        items.append({
            "id": p.id,
            "title": p.title,
            "slug": p.slug,
            "url": p.url,
            "authored_on": p.authored_on.strftime("%Y-%m-%d") if p.authored_on else None,
            "cover_url": p.featured_image.file.url if p.featured_image else None,
            "authors": authors,
            "tags": tags,
        })

    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
    has_next = page_num < total_pages
    has_prev = page_num > 1

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

    cache.set(cache_key, payload, timeout=180)
    return _cors_json_response(payload, extra_headers={"X-Cache": "MISS", "X-Epoch": str(current_epoch)})


@ratelimit_api(scope="guide_list", max_requests=60, window_seconds=60)
def guides_list_api(request):
    """
    专题指南专栏列表 API
    GET /api/v1/guides/
    """
    from guides.models import GuidePage, GuideTopic

    page_str = request.GET.get("page", "1").strip()
    page_num = int(page_str) if page_str.isdigit() and int(page_str) > 0 else 1

    page_size_str = request.GET.get("page_size", "10").strip()
    page_size = max(1, min(int(page_size_str) if page_size_str.isdigit() else 10, 50))

    topic_slug = request.GET.get("topic", "").strip()
    difficulty = request.GET.get("difficulty", "").strip()

    current_epoch = ContentDiscoveryEpoch.get_current_epoch()
    cache_key = f"api:guides_list:{current_epoch}:p{page_num}_s{page_size}_t{topic_slug}_d{difficulty}"
    cached_payload = cache.get(cache_key)

    if cached_payload is not None:
        return _cors_json_response(cached_payload, extra_headers={"X-Cache": "HIT", "X-Epoch": str(current_epoch)})

    qs = GuidePage.objects.filter(live=True).order_by("-last_reviewed_at", "-first_published_at")
    if topic_slug:
        qs = qs.filter(topic__slug=topic_slug)
    if difficulty:
        qs = qs.filter(difficulty=difficulty)

    total_count = qs.count()
    offset = (page_num - 1) * page_size
    pages = qs.select_related("topic", "featured_image").prefetch_related("tags", "authors__person")[offset : offset + page_size]

    items = []
    for g in pages:
        is_outdated, _, warning_msg = g.is_outdated()
        authors = [{"id": a.person.id, "name": a.person.name, "role": a.role} for a in g.authors.all() if a.person]
        tags = [{"id": t.id, "name": t.name, "slug": t.slug} for t in g.tags.all()]
        items.append({
            "id": g.id,
            "title": g.title,
            "slug": g.slug,
            "url": g.url,
            "topic": {"name": g.topic.name, "slug": g.topic.slug} if g.topic else None,
            "difficulty": g.difficulty,
            "difficulty_label": g.get_difficulty_display(),
            "estimated_minutes": g.estimated_minutes,
            "authored_on": g.authored_on.strftime("%Y-%m-%d") if g.authored_on else None,
            "last_reviewed_at": g.last_reviewed_at.strftime("%Y-%m-%d") if g.last_reviewed_at else None,
            "is_outdated": is_outdated,
            "summary": g.summary,
            "cover_url": g.featured_image.file.url if g.featured_image else None,
            "authors": authors,
            "tags": tags,
        })

    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
    has_next = page_num < total_pages
    has_prev = page_num > 1

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

    cache.set(cache_key, payload, timeout=180)
    return _cors_json_response(payload, extra_headers={"X-Cache": "MISS", "X-Epoch": str(current_epoch)})


@ratelimit_api(scope="guide_detail", max_requests=120, window_seconds=60)
def guide_detail_api(request, slug):
    """
    专题指南详情 API（双形态积木输出与技术复核告警）
    GET /api/v1/guides/<slug>/
    """
    from guides.models import GuidePage

    current_epoch = ContentDiscoveryEpoch.get_current_epoch()
    cache_key = f"api:guide:{current_epoch}:{slug}"
    cached_payload = cache.get(cache_key)

    if cached_payload is not None:
        return _cors_json_response(cached_payload, extra_headers={"X-Cache": "HIT", "X-Epoch": str(current_epoch)})

    page = GuidePage.objects.filter(slug=slug, live=True).first()
    if not page or getattr(page, "is_recycled", False):
        return _cors_json_response(
            {"status": "error", "code": 404, "message": "专栏指南不存在或已下架"},
            status=404,
        )

    access_context = AccessContext(user=request.user, mode=AccessMode.PUBLIC)
    page.hydrate_from_mongo(access_context=access_context)

    # 1. 结构化积木列表 (blocks)
    blocks_data = []
    steps_toc = []
    for b in page.body:
        val = b.block.get_prep_value(b.value) if hasattr(b.block, "get_prep_value") else b.value
        blocks_data.append({
            "type": b.block_type,
            "value": val,
        })
        if b.block_type == "step":
            steps_toc.append({
                "step_number": val.get("step_number"),
                "title": val.get("title"),
            })

    safe_html_str = str(page.body)
    is_outdated, _, warning_msg = page.is_outdated()

    authors_data = []
    for a in page.authors.select_related("person").all():
        if a.person:
            authors_data.append({
                "id": a.person.id,
                "name": a.person.name,
                "role": a.role,
                "bio": a.person.bio or "",
            })

    tags_data = [{"id": t.id, "name": t.name, "slug": t.slug} for t in page.tags.all()]
    related_data = [
        {"id": r.related_page.id, "title": r.related_page.title, "url": r.related_page.url}
        for r in page.related_guides.all()
    ]

    cover_data = None
    if page.featured_image:
        cover_data = {
            "id": page.featured_image.id,
            "title": page.featured_image.title,
            "public_id": str(page.featured_image.public_id),
            "url": page.featured_image.file.url,
        }

    guide_payload = {
        "status": "success",
        "epoch": current_epoch,
        "data": {
            "id": page.id,
            "title": page.title,
            "slug": page.slug,
            "url": page.url,
            "topic": {"name": page.topic.name, "slug": page.topic.slug} if page.topic else None,
            "difficulty": page.difficulty,
            "difficulty_label": page.get_difficulty_display(),
            "estimated_minutes": page.estimated_minutes,
            "authored_on": page.authored_on.strftime("%Y-%m-%d") if page.authored_on else None,
            "last_reviewed_at": page.last_reviewed_at.strftime("%Y-%m-%d") if page.last_reviewed_at else None,
            "is_outdated": is_outdated,
            "outdated_warning": warning_msg if is_outdated else None,
            "summary": page.summary,
            "featured_image": cover_data,
            "authors": authors_data,
            "tags": tags_data,
            "steps_toc": steps_toc,
            "blocks": blocks_data,
            "safe_html": safe_html_str,
            "related_guides": related_data,
        },
    }

    cache.set(cache_key, guide_payload, timeout=600)
    return _cors_json_response(guide_payload, extra_headers={"X-Cache": "MISS", "X-Epoch": str(current_epoch)})
