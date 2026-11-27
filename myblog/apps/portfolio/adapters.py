"""
portfolio.adapters
ProjectPage 与 PortfolioIndexPage 的统一内容发现与订阅适配器 (TypeAdapter)。

实现 TypeAdapterContract 接口，挂载至 discovery 搜索与 feeds 体系：
1. ProjectPageAdapter: 开启 feed_enabled=True 与 archive_enabled=True，public_type_slug="project"；
2. PortfolioIndexPageAdapter: 显式关闭 feed_enabled 与 archive_enabled，杜绝大厅页污染文章流。
"""

import re
from datetime import date
from typing import Any
from discovery.contracts import (
    AnalyticsCapabilities,
    PublishedAuthorRef,
    PublishedPayload,
    PublicListingCapabilities,
    TextSegment,
    TypeAdapterContract,
)
from people.attribution import extract_raw_author_refs_from_page


class ProjectPageAdapter:
    type_key = "project"
    model_label = "portfolio.ProjectPage"
    schema_version = 1
    extractor_version = 2
    allowed_filters = ["year", "month", "tag_ids", "authored_on", "project_status", "primary_language"]
    weight_profile = {
        "title": 1.0,
        "summary": 0.5,
        "body": 0.1,
    }
    capabilities = PublicListingCapabilities(
        public_type_slug="project",
        feed_enabled=True,
        archive_enabled=True,
        supported_dimensions=("type", "tag", "author"),
    )
    analytics_capabilities = AnalyticsCapabilities(
        kind="detail",
        metric_keys=("pv", "uv", "read_depth"),
        conversion_keys=("repo_click", "demo_click"),
        rating_enabled=True,
        rank_eligible=True,
        curation_eligible=True,
        body_markers=("article-start", "article-end"),
    )

    def extract(self, page: Any) -> PublishedPayload:
        if hasattr(page, "hydrate_from_mongo") and (not getattr(page, "body", None) or len(getattr(page, "body", [])) == 0):
            page.hydrate_from_mongo()

        title = getattr(page, "title", "")
        doc_uuid = str(getattr(page, "document_uuid", ""))
        authored_on = getattr(page, "authored_on", None) or date.today()
        url_path = page.get_url() if hasattr(page, "get_url") else getattr(page, "url_path", "")
        summary = str(getattr(page, "summary", "")).strip()

        body_text = ""
        body_val = getattr(page, "body", None)
        if body_val:
            body_text = self._extract_stream_text(body_val)

        if not summary:
            summary = body_text[:200]

        tag_ids = []
        tags_data = []
        if hasattr(page, "tags"):
            for t in page.tags.all():
                tag_ids.append(t.id)
                tags_data.append({"id": t.id, "name": t.name, "slug": t.slug})

        # 仅提取原始作者引用，由 discovery/projection.py 统一调用 people.attribution 解析与落库
        raw_author_refs = extract_raw_author_refs_from_page(page)
        author_refs = [
            PublishedAuthorRef(
                person_id=r["person_id"],
                role=str(r.get("role") or ""),
                sort_order=int(r["sort_order"]) if r.get("sort_order") is not None else idx,
            )
            for idx, r in enumerate(raw_author_refs)
        ]

        extra_data = {
            "project_status": getattr(page, "project_status", "maintained"),
            "project_status_label": page.get_project_status_display() if hasattr(page, "get_project_status_display") else getattr(page, "project_status", "maintained"),
            "primary_language": getattr(page, "primary_language", "Python"),
            "star_count": getattr(page, "star_count", 0),
            "license_name": getattr(page, "license_name", "MIT"),
            "repo_url": getattr(page, "repo_url", ""),
            "demo_url": getattr(page, "demo_url", ""),
            "authors": [],
            "topic_slug": "",
            "topic_name": "",
            "last_published_at": page.last_published_at.isoformat() if getattr(page, "last_published_at", None) else None,
        }
        featured_img = getattr(page, "featured_image", None)
        if featured_img is not None and getattr(featured_img, "public_id", None):
            extra_data["cover_public_id"] = str(featured_img.public_id)

        segments = [
            TextSegment(field_name="title", text=title),
        ]
        if summary:
            segments.append(TextSegment(field_name="summary", text=summary))
        if body_text:
            segments.append(TextSegment(field_name="body", text=body_text))

        return PublishedPayload(
            page_id=page.id,
            document_uuid=doc_uuid,
            type_key=self.type_key,
            title=title,
            url_path=url_path,
            authored_on=authored_on,
            summary=summary,
            text_segments=segments,
            tag_ids=tag_ids,
            tags_data=tags_data,
            author_refs=author_refs,
            extra_data=extra_data,
        )

    def _extract_stream_text(self, stream_value: Any) -> str:
        parts = []
        try:
            for child in stream_value:
                block = getattr(child, "block", None)
                if block and hasattr(block, "extract_text"):
                    ext = block.extract_text(child.value)
                    if ext and ext.plain_text:
                        parts.append(ext.plain_text)
                else:
                    v_str = str(getattr(child, "value", ""))
                    clean = re.sub(r"<[^>]+>", " ", v_str)
                    clean = re.sub(r"\s+", " ", clean).strip()
                    if clean:
                        parts.append(clean)
        except Exception:
            v_str = str(stream_value)
            clean = re.sub(r"<[^>]+>", " ", v_str)
            parts.append(clean.strip())
        return " \n".join(parts)

    def get_api_list_queryset(self, model_cls: Any, request: Any):
        qs = model_cls.objects.filter(live=True, is_recycled=False).order_by("-authored_on", "-id")
        status_filter = request.GET.get("status", "").strip()
        lang_filter = request.GET.get("language", "").strip()
        tag_slug = request.GET.get("tag", "").strip()
        if status_filter:
            qs = qs.filter(project_status=status_filter)
        if lang_filter:
            qs = qs.filter(primary_language__iexact=lang_filter)
        if tag_slug:
            qs = qs.filter(tags__slug=tag_slug)
        return qs.select_related("featured_image").prefetch_related("tags", "authors__person")

    def serialize_api_list_item(self, page: Any) -> dict[str, Any]:
        authors = [{"id": a.person.id, "name": a.person.name, "role": a.role} for a in page.authors.all() if a.person]
        tags = [{"id": t.id, "name": t.name, "slug": t.slug} for t in page.tags.all()]
        cover_data = None
        if page.featured_image:
            cover_data = {
                "id": page.featured_image.id,
                "public_id": str(page.featured_image.public_id),
                "title": page.featured_image.title,
                "url": f"/media-gateway/image/{page.featured_image.public_id}/",
            }
        return {
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
            "authors": authors,
            "tags": tags,
        }

    def serialize_api_detail(self, page: Any, request: Any = None) -> dict[str, Any]:
        from django.utils.safestring import mark_safe
        from content_blocks.blocks.rich_text import sanitize_block_value_for_public
        from content_lifecycle.public_access import filter_public_pages, resolve_public_site_for_request

        site = resolve_public_site_for_request(request, page=page)
        block_context = {"request": request, "page": page, "is_preview": False}

        authors_data = []
        from people.models import extract_person_bio_text

        for a in page.authors.select_related("person").all():
            if a.person:
                authors_data.append({
                    "id": a.person.id,
                    "name": a.person.name,
                    "role": a.role,
                    "bio": extract_person_bio_text(a.person.bio),
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
                if hasattr(child.block, "render_clean"):
                    rendered_chunk = child.block.render_clean(block_val, context=block_context).html
                else:
                    rendered_chunk = child.render(context=block_context)
                if rendered_chunk:
                    rendered_html_parts.append(rendered_chunk)
                prep_val = child.block.get_prep_value(block_val) if hasattr(child.block, "get_prep_value") else str(block_val)
                prep_val = sanitize_block_value_for_public(prep_val, context=block_context)
                if block_type == "featured_posts" and isinstance(prep_val, dict):
                    raw_posts = [p for p in (block_val.get("posts") or []) if p is not None]
                    allowed_posts = filter_public_pages(raw_posts, site=site, request=request) if site is not None else []
                    prep_val = dict(prep_val)
                    prep_val["posts"] = [int(p.id) for p in allowed_posts]
                blocks_data.append({
                    "type": block_type,
                    "value": prep_val,
                })

        page_url = (page.get_url(request=request) if hasattr(page, "get_url") else page.url) or page.url

        return {
            "id": page.id,
            "title": page.title,
            "slug": page.slug,
            "url": page_url,
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
            "safe_html": mark_safe("\n".join(rendered_html_parts)),
        }


class PortfolioIndexPageAdapter:
    type_key = "portfolio_index"
    model_label = "portfolio.PortfolioIndexPage"
    schema_version = 1
    extractor_version = 1
    allowed_filters = ["year", "month"]
    weight_profile = {
        "title": 1.0,
        "intro": 0.4,
    }
    capabilities = PublicListingCapabilities(
        public_type_slug="portfolio_index",
        feed_enabled=False,
        archive_enabled=False,
        supported_dimensions=(),
    )
    analytics_capabilities = AnalyticsCapabilities(
        kind="entry",
        entry_kind="portfolio_index",
        metric_keys=("pv", "uv", "card_exposure", "entry_click"),
    )

    def extract(self, page: Any) -> PublishedPayload:
        title = getattr(page, "title", "")
        doc_uuid = str(getattr(page, "document_uuid", ""))
        url_path = page.get_url() if hasattr(page, "get_url") else getattr(page, "url_path", "")
        intro_text = str(getattr(page, "intro", "")).strip()

        segments = [
            TextSegment(field_name="title", text=title),
        ]
        if intro_text:
            segments.append(TextSegment(field_name="intro", text=intro_text))

        return PublishedPayload(
            page_id=page.id,
            document_uuid=doc_uuid,
            type_key=self.type_key,
            title=title,
            url_path=url_path,
            authored_on=date.today(),
            summary=intro_text[:200],
            text_segments=segments,
            tag_ids=[],
            tags_data=[],
            extra_data={"intro": intro_text},
        )
