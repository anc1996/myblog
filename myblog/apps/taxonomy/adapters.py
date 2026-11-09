"""
taxonomy.adapters
全站标签大厅页 (TagIndexPage) 内容发现适配器。
"""

from datetime import date
from typing import Any
from discovery.contracts import PublicListingCapabilities, PublishedPayload, TextSegment


class TagIndexPageAdapter:
    type_key = "tag_index"
    model_label = "taxonomy.TagIndexPage"
    schema_version = 1
    extractor_version = 1
    allowed_filters = []
    weight_profile = {"title": 1.0, "intro": 0.4}
    capabilities = PublicListingCapabilities(
        public_type_slug="tag_index",
        feed_enabled=False,
        archive_enabled=False,
        supported_dimensions=(),
    )

    def extract(self, page: Any) -> PublishedPayload:
        title = getattr(page, "title", "全站标签大厅")
        doc_uuid = str(getattr(page, "document_uuid", ""))
        url_path = page.get_url() if hasattr(page, "get_url") else getattr(page, "url_path", "")
        intro_text = str(getattr(page, "intro", "")).strip()
        segments = [TextSegment(field_name="title", text=title)]
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
            extra_data={"intro": intro_text},
        )
