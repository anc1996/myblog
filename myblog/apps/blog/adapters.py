"""
BlogPage 与 BlogIndexPage 的统一内容发现适配器 (TypeAdapter)。

实现 TypeAdapterContract 接口，从有效发布快照中提取统一数据载荷 PublishedPayload，
严格遵循面向契约架构，绝不让 discovery 反向依赖 blog 模型。
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


class BlogPageAdapter:
    type_key = "blog_page"
    model_label = "blog.BlogPage"
    schema_version = 1
    extractor_version = 1
    allowed_filters = ["year", "month", "tag_ids", "authored_on"]
    weight_profile = {
        "title": 1.0,
        "intro": 0.4,
        "body": 0.1,
    }
    capabilities = PublicListingCapabilities(
        public_type_slug="blog",
        feed_enabled=True,
        archive_enabled=True,
        supported_dimensions=("type", "tag", "author"),
    )
    analytics_capabilities = AnalyticsCapabilities(
        kind="detail",
        metric_keys=("pv", "uv", "read_depth", "code_copy", "mermaid_view"),
        rating_enabled=True,
        rank_eligible=True,
        curation_eligible=True,
        body_markers=("article-start", "article-end"),
    )

    def extract(self, page: Any) -> PublishedPayload:
        # 方案 A【彻底脱水模式】：若 Page 处于脱水状态，在提取搜索文本前先从 MongoDB 水合
        if hasattr(page, "hydrate_from_mongo") and (not getattr(page, "body", None) or len(getattr(page, "body", [])) == 0):
            page.hydrate_from_mongo()

        title = getattr(page, "title", "")
        doc_uuid = str(getattr(page, "document_uuid", ""))
        authored_on = getattr(page, "authored_on", None)
        if not authored_on:
            authored_on = date.today()
        url_path = page.get_url() if hasattr(page, "get_url") else getattr(page, "url_path", "")

        # 提取 intro 文字
        intro_text = ""
        intro_val = getattr(page, "intro", None)
        if intro_val:
            intro_text = self._extract_stream_text(intro_val)

        # 提取 body 文字
        body_text = ""
        body_val = getattr(page, "body", None)
        if body_val:
            body_text = self._extract_stream_text(body_val)

        # 提取 tags
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
            "authors": [],
            "last_published_at": page.last_published_at.isoformat() if getattr(page, "last_published_at", None) else None,
            "topic_slug": "",
            "topic_name": "",
        }

        # 生成摘要
        summary = intro_text[:200] if intro_text else body_text[:200]

        segments = [
            TextSegment(field_name="title", text=title),
        ]
        if intro_text:
            segments.append(TextSegment(field_name="intro", text=intro_text))
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
        qs = model_cls.objects.filter(live=True, is_recycled=False).order_by("-authored_on", "-first_published_at")
        tag_slug = request.GET.get("tag", "").strip()
        if tag_slug:
            qs = qs.filter(tags__slug=tag_slug)
        return qs.select_related("featured_image").prefetch_related("tags", "authors__person")

    def serialize_api_list_item(self, page: Any) -> dict[str, Any]:
        authors = [{"id": a.person.id, "name": a.person.name, "role": a.role} for a in page.authors.all() if a.person]
        tags = [{"id": t.id, "name": t.name, "slug": t.slug} for t in page.tags.all()]
        return {
            "id": page.id,
            "title": page.title,
            "slug": page.slug,
            "url": page.url,
            "authored_on": page.authored_on.strftime("%Y-%m-%d") if page.authored_on else None,
            "cover_url": page.featured_image.file.url if page.featured_image else None,
            "authors": authors,
            "tags": tags,
        }

    def serialize_api_detail(self, page: Any) -> dict[str, Any]:
        blocks_data = []
        for b in page.body:
            val = b.block.get_prep_value(b.value) if hasattr(b.block, "get_prep_value") else b.value
            blocks_data.append({"type": b.block_type, "value": val})

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

        return {
            "id": page.id,
            "title": page.title,
            "slug": page.slug,
            "url": page.url,
            "authored_on": page.authored_on.strftime("%Y-%m-%d") if page.authored_on else None,
            "intro": str(page.intro) if page.intro else "",
            "featured_image": cover_data,
            "authors": authors_data,
            "tags": tags_data,
            "blocks": blocks_data,
            "safe_html": str(page.body),
        }


class BlogIndexPageAdapter:
    type_key = "blog_index"
    model_label = "blog.BlogIndexPage"
    schema_version = 1
    extractor_version = 1
    allowed_filters = ["year", "month", "authored_on"]
    weight_profile = {
        "title": 1.0,
        "intro": 0.4,
    }
    capabilities = PublicListingCapabilities(
        public_type_slug="blog_index",
        feed_enabled=False,
        archive_enabled=False,
        supported_dimensions=(),
    )
    analytics_capabilities = AnalyticsCapabilities(
        kind="entry",
        entry_kind="blog_index",
        metric_keys=("pv", "uv", "card_exposure", "entry_click"),
    )

    def extract(self, page: Any) -> PublishedPayload:
        title = getattr(page, "title", "")
        doc_uuid = str(getattr(page, "document_uuid", ""))
        authored_on = getattr(page, "authored_on", None)
        if not authored_on:
            authored_on = date.today()
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
            authored_on=authored_on,
            summary=intro_text[:200],
            text_segments=segments,
            tag_ids=[],
            tags_data=[],
            extra_data={"intro": intro_text},
        )
