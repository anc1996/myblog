"""
guides.adapters
GuidePage 与 GuideIndexPage 的统一内容发现适配器 (TypeAdapter)。

实现 TypeAdapterContract 接口，挂载至 discovery 搜索体系。
发布时自动从 MongoDB 水合正文并分块生成单列 GIN 词向量。
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


class GuidePageAdapter:
    type_key = "guide"
    model_label = "guides.GuidePage"
    schema_version = 1
    extractor_version = 1
    allowed_filters = ["year", "month", "tag_ids", "authored_on", "topic_slug", "difficulty"]
    weight_profile = {
        "title": 1.0,
        "summary": 0.4,
        "body": 0.1,
    }
    capabilities = PublicListingCapabilities(
        public_type_slug="guide",
        feed_enabled=True,
        archive_enabled=True,
        supported_dimensions=("type", "tag", "author", "topic"),
    )
    analytics_capabilities = AnalyticsCapabilities(
        kind="detail",
        metric_keys=("pv", "uv", "read_depth"),
        conversion_keys=("step_reach", "step_complete", "doc_attempt", "doc_download"),
        rating_enabled=True,
        rank_eligible=True,
        curation_eligible=True,
        body_markers=("article-start", "article-end"),
        review_cycle_days=365,
    )

    def extract(self, page: Any) -> PublishedPayload:
        if hasattr(page, "hydrate_from_mongo") and (not getattr(page, "body", None) or len(getattr(page, "body", [])) == 0):
            page.hydrate_from_mongo()

        title = getattr(page, "title", "")
        doc_uuid = str(getattr(page, "document_uuid", ""))
        authored_on = getattr(page, "authored_on", None) or date.today()
        url_path = page.get_url() if hasattr(page, "get_url") else getattr(page, "url_path", "")
        summary = str(getattr(page, "summary", "")).strip()

        # 提取专栏正文文字 (步骤、代码、说明)
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

        # 提取 topic
        topic_slug = ""
        topic_name = ""
        topic = getattr(page, "topic", None)
        if topic:
            topic_slug = getattr(topic, "slug", "")
            topic_name = getattr(topic, "name", "")

        extra_data = {
            "authors": [],
            "topic_slug": topic_slug,
            "topic_name": topic_name,
            "difficulty": getattr(page, "difficulty", "intermediate"),
            "difficulty_label": page.get_difficulty_display() if hasattr(page, "get_difficulty_display") else getattr(page, "difficulty", "intermediate"),
            "last_reviewed_at": page.last_reviewed_at.isoformat() if getattr(page, "last_reviewed_at", None) else None,
            "estimated_minutes": getattr(page, "estimated_minutes", None),
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
        qs = model_cls.objects.filter(live=True, is_recycled=False).order_by("-last_reviewed_at", "-first_published_at")
        topic_slug = request.GET.get("topic", "").strip()
        difficulty = request.GET.get("difficulty", "").strip()
        if topic_slug:
            qs = qs.filter(topic__slug=topic_slug)
        if difficulty:
            qs = qs.filter(difficulty=difficulty)
        return qs.select_related("topic", "featured_image").prefetch_related("tags", "authors__person")

    def serialize_api_list_item(self, page: Any) -> dict[str, Any]:
        is_outdated, _, _ = page.is_outdated()
        authors = [{"id": a.person.id, "name": a.person.name, "role": a.role} for a in page.authors.all() if a.person]
        tags = [{"id": t.id, "name": t.name, "slug": t.slug} for t in page.tags.all()]
        return {
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
            "summary": page.summary,
            "cover_url": f"/media-gateway/image/{page.featured_image.public_id}/" if page.featured_image else None,
            "authors": authors,
            "tags": tags,
        }

    def serialize_api_detail(self, page: Any) -> dict[str, Any]:
        blocks_data = []
        steps_toc = []
        for b in page.body:
            val = b.block.get_prep_value(b.value) if hasattr(b.block, "get_prep_value") else b.value
            blocks_data.append({"type": b.block_type, "value": val})
            if b.block_type == "step":
                num = str(val.get("step_number") or len(steps_toc) + 1)
                steps_toc.append({
                    "step_number": num,
                    "title": str(val.get("title") or f"步骤 {num}"),
                    "anchor": f"step-{num}",
                })

        is_outdated, _, warning_msg = page.is_outdated()
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

        return {
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
            "safe_html": str(page.body),
            "related_guides": related_data,
        }


class GuideIndexPageAdapter:
    type_key = "guide_index"
    model_label = "guides.GuideIndexPage"
    schema_version = 1
    extractor_version = 1
    allowed_filters = ["year", "month"]
    weight_profile = {
        "title": 1.0,
        "intro": 0.4,
    }
    capabilities = PublicListingCapabilities(
        public_type_slug="guide_index",
        feed_enabled=False,
        archive_enabled=False,
        supported_dimensions=(),
    )
    analytics_capabilities = AnalyticsCapabilities(
        kind="entry",
        entry_kind="guide_index",
        metric_keys=("pv", "uv", "card_exposure", "entry_click"),
    )

    def extract(self, page: Any) -> PublishedPayload:
        title = getattr(page, "title", "")
        doc_uuid = str(getattr(page, "document_uuid", ""))
        url_path = page.get_url() if hasattr(page, "get_url") else getattr(page, "url_path", "")
        intro_text = str(getattr(page, "intro", "")).strip()
        featured_topic_data = {}
        ft = getattr(page, "featured_topic", None)
        if ft:
            featured_topic_data = {
                "name": getattr(ft, "name", ""),
                "slug": getattr(ft, "slug", ""),
                "description": getattr(ft, "description", ""),
            }

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
            extra_data={"intro": intro_text, "featured_topic": featured_topic_data},
        )
