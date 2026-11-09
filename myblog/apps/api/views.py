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

    qs = GuidePage.objects.filter(live=True, is_recycled=False).order_by("-last_reviewed_at", "-first_published_at")
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
            "cover_url": f"/media-gateway/image/{g.featured_image.public_id}/" if g.featured_image else None,
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
            num = str(val.get("step_number") or len(steps_toc) + 1)
            steps_toc.append({
                "step_number": num,
                "title": str(val.get("title") or f"步骤 {num}"),
                "anchor": f"step-{num}",
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
        for r in page.related_guides.select_related("related_page").all()
        if r.related_page.live and not getattr(r.related_page.specific, "is_recycled", False)
    ]

    cover_data = None
    if page.featured_image:
        cover_data = {
            "id": page.featured_image.id,
            "title": page.featured_image.title,
            "public_id": str(page.featured_image.public_id),
            "url": f"/media-gateway/image/{page.featured_image.public_id}/",
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

    epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()
    weak_etag = f'W/"archives:v1:s{site_id or 0}:e{epoch}:t{type_slug or "all"}:tag{tag_slug or "all"}:top{topic_slug or "all"}:y{year_val or "all"}"'

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


@ratelimit_api(scope="project_list", max_requests=60, window_seconds=60)
def project_list_api(request):
    """
    开源作品集与项目列表 API
    GET /api/v1/projects/
    """
    from portfolio.models import ProjectPage

    page_str = request.GET.get("page", "1").strip()
    page_num = int(page_str) if page_str.isdigit() and int(page_str) > 0 else 1

    page_size_str = request.GET.get("page_size", "10").strip()
    page_size = max(1, min(int(page_size_str) if page_size_str.isdigit() else 10, 50))

    status_filter = request.GET.get("status", "").strip()
    lang_filter = request.GET.get("language", "").strip()
    tag_slug = request.GET.get("tag", "").strip()

    current_epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()
    cache_key = f"api:projects_list:{current_epoch}:p{page_num}_s{page_size}_st{status_filter}_l{lang_filter}_t{tag_slug}"
    cached_payload = cache.get(cache_key)

    if cached_payload is not None:
        return _cors_json_response(cached_payload, extra_headers={"X-Cache": "HIT", "X-Epoch": str(current_epoch)})

    qs = ProjectPage.objects.filter(live=True, is_recycled=False).order_by("-authored_on", "-id")
    if status_filter:
        qs = qs.filter(project_status=status_filter)
    if lang_filter:
        qs = qs.filter(primary_language__iexact=lang_filter)
    if tag_slug:
        qs = qs.filter(tags__slug=tag_slug)

    total_count = qs.count()
    offset = (page_num - 1) * page_size
    pages = qs.select_related("featured_image").prefetch_related("tags", "authors__person")[offset : offset + page_size]

    items = []
    for p in pages:
        authors = [{"id": a.person.id, "name": a.person.name, "role": a.role} for a in p.authors.all() if a.person]
        tags = [{"id": t.id, "name": t.name, "slug": t.slug} for t in p.tags.all()]
        cover_data = None
        if p.featured_image:
            cover_data = {
                "id": p.featured_image.id,
                "public_id": str(p.featured_image.public_id),
                "title": p.featured_image.title,
                "url": f"/media-gateway/image/{p.featured_image.public_id}/",
            }
        items.append({
            "id": p.id,
            "title": p.title,
            "slug": p.slug,
            "url": p.url,
            "project_status": p.project_status,
            "project_status_label": p.get_project_status_display(),
            "summary": p.summary,
            "primary_language": p.primary_language,
            "star_count": p.star_count,
            "license_name": p.license_name,
            "repo_url": p.repo_url,
            "demo_url": p.demo_url,
            "started_on": p.started_on.strftime("%Y-%m-%d") if p.started_on else None,
            "completed_on": p.completed_on.strftime("%Y-%m-%d") if p.completed_on else None,
            "authored_on": p.authored_on.strftime("%Y-%m-%d") if p.authored_on else None,
            "featured_image": cover_data,
            "authors": authors,
            "tags": tags,
        })

    payload = {
        "status": "success",
        "epoch": current_epoch,
        "pagination": {
            "total_count": total_count,
            "page": page_num,
            "page_size": page_size,
            "total_pages": (total_count + page_size - 1) // page_size if total_count > 0 else 1,
            "has_next": offset + page_size < total_count,
            "has_prev": page_num > 1,
        },
        "data": items,
    }
    cache.set(cache_key, payload, timeout=600)
    return _cors_json_response(payload, extra_headers={"X-Cache": "MISS", "X-Epoch": str(current_epoch)})


@ratelimit_api(scope="project_detail", max_requests=120, window_seconds=60)
def project_detail_api(request, slug):
    """
    开源作品集与项目详情 API
    GET /api/v1/projects/<slug>/
    """
    from portfolio.models import ProjectPage

    current_epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()
    cache_key = f"api:project_detail:{current_epoch}:{slug}"
    cached_payload = cache.get(cache_key)

    if cached_payload is not None:
        return _cors_json_response(cached_payload, extra_headers={"X-Cache": "HIT", "X-Epoch": str(current_epoch)})

    page = get_object_or_404(ProjectPage, slug=slug, live=True, is_recycled=False)
    page.hydrate_from_mongo()

    authors_data = []
    for a in page.authors.select_related("person").all():
        if a.person:
            authors_data.append({
                "id": a.person.id,
                "name": a.person.name,
                "role": a.role,
                "bio": a.person.bio,
            })

    tags_data = [{"id": t.id, "name": t.name, "slug": t.slug} for t in page.tags.all()]

    cover_data = None
    if page.featured_image:
        cover_data = {
            "id": page.featured_image.id,
            "public_id": str(page.featured_image.public_id),
            "title": page.featured_image.title,
            "url": f"/media-gateway/image/{page.featured_image.public_id}/",
        }

    blocks_data = []
    rendered_html_parts = []
    if page.body:
        for child in page.body:
            block_type = child.block_type
            block_val = child.value
            rendered_chunk = ""
            if hasattr(child.block, "render_clean"):
                res = child.block.render_clean(block_val)
                rendered_chunk = res.html
            else:
                rendered_chunk = child.render()
            rendered_html_parts.append(rendered_chunk)
            blocks_data.append({
                "type": block_type,
                "value": child.block.get_prep_value(block_val) if hasattr(child.block, "get_prep_value") else str(block_val),
            })

    safe_html_str = mark_safe("\n".join(rendered_html_parts))

    project_payload = {
        "status": "success",
        "epoch": current_epoch,
        "data": {
            "id": page.id,
            "title": page.title,
            "slug": page.slug,
            "url": page.url,
            "project_status": page.project_status,
            "project_status_label": page.get_project_status_display(),
            "summary": page.summary,
            "primary_language": page.primary_language,
            "star_count": page.star_count,
            "license_name": page.license_name,
            "repo_url": page.repo_url,
            "demo_url": page.demo_url,
            "started_on": page.started_on.strftime("%Y-%m-%d") if page.started_on else None,
            "completed_on": page.completed_on.strftime("%Y-%m-%d") if page.completed_on else None,
            "authored_on": page.authored_on.strftime("%Y-%m-%d") if page.authored_on else None,
            "featured_image": cover_data,
            "authors": authors_data,
            "tags": tags_data,
            "blocks": blocks_data,
            "safe_html": safe_html_str,
        },
    }

    cache.set(cache_key, project_payload, timeout=600)
    return _cors_json_response(project_payload, extra_headers={"X-Cache": "MISS", "X-Epoch": str(current_epoch)})


@ratelimit_api(scope="site_settings", max_requests=120, window_seconds=60)
def site_settings_api(request):
    """
    全站全局导航与站点设置只读 API (H-03 & H-07)
    GET /api/v1/site/settings/
    严格只读查询 filter(singleton_key=1)，严禁调用 .load()；
    菜单项关联页面批量核验公开资格，下架或回收页面自动优雅降级。
    """
    from site_settings.models import NavigationSettings, SocialMediaSettings, FooterSettings
    from content_lifecycle.public_access import get_public_blocked_page_ids
    from wagtail.models import Site, Page
    from django.utils.cache import get_conditional_response

    site = Site.find_for_request(request)
    site_id = site.id if site else 0
    current_epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()

    weak_etag = f'W/"site_settings:v1:s{site_id}:e{current_epoch}"'
    conditional_resp = get_conditional_response(request, etag=weak_etag)
    if conditional_resp is not None:
        conditional_resp["ETag"] = weak_etag
        conditional_resp["X-Epoch"] = str(current_epoch)
        conditional_resp["Cache-Control"] = "public, max-age=120"
        return conditional_resp

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
        pages_qs = Page.objects.filter(id__in=target_page_ids, live=True).exclude(live_revision=None)
        candidate_records = list(pages_qs.values_list("id", "path"))
        blocked_ids = get_public_blocked_page_ids(candidate_records)
        for p in pages_qs:
            if p.id not in blocked_ids and getattr(p, "alias_of_id", None) is None:
                allowed_page_urls[p.id] = p.url

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
    social_links = []
    if social_obj and social_obj.social_links:
        for block in social_obj.social_links:
            val = block.value
            social_links.append({
                "platform": val.get("platform", "custom"),
                "label": val.get("label", ""),
                "url": val.get("url", ""),
            })

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
    resp["Cache-Control"] = "public, max-age=120"
    return resp
