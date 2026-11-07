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
from discovery.contracts import PublishedPayload, PublicListingCapabilities, TextSegment, TypeAdapterContract


class ProjectPageAdapter:
    type_key = "project"
    model_label = "portfolio.ProjectPage"
    schema_version = 1
    extractor_version = 1
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

        authors_data = []
        author_slugs = []
        if hasattr(page, "authors"):
            try:
                for a in page.authors.all():
                    person = getattr(a, "person", None)
                    if person:
                        p_name = getattr(person, "name", "")
                        p_id = getattr(person, "id", None)
                        p_uuid = str(getattr(person, "public_id", ""))
                        authors_data.append({
                            "id": p_id,
                            "name": p_name,
                            "public_id": p_uuid,
                            "role": getattr(a, "role", ""),
                        })
                        if p_id is not None:
                            author_slugs.append(str(p_id))
                        if p_name:
                            author_slugs.append(p_name.lower().replace(" ", "-"))
                        if p_uuid:
                            author_slugs.append(p_uuid)
            except Exception:
                pass

        extra_data = {
            "project_status": getattr(page, "project_status", "maintained"),
            "primary_language": getattr(page, "primary_language", "Python"),
            "star_count": getattr(page, "star_count", 0),
            "license_name": getattr(page, "license_name", "MIT"),
            "repo_url": getattr(page, "repo_url", ""),
            "demo_url": getattr(page, "demo_url", ""),
            "authors": authors_data,
            "author_slugs": author_slugs,
            "topic_slug": "",
            "topic_name": "",
            "last_published_at": page.last_published_at.isoformat() if getattr(page, "last_published_at", None) else None,
        }

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
        )
