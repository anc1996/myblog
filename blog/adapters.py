"""
BlogPage 与 BlogIndexPage 的统一内容发现适配器 (TypeAdapter)。

实现 TypeAdapterContract 接口，从有效发布快照中提取统一数据载荷 PublishedPayload，
严格遵循面向契约架构，绝不让 discovery 反向依赖 blog 模型。
"""

import re
from datetime import date
from typing import Any
from discovery.contracts import PublishedPayload, TextSegment, TypeAdapterContract


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

    def extract(self, page: Any) -> PublishedPayload:
        title = getattr(page, "title", "")
        doc_uuid = str(getattr(page, "document_uuid", ""))
        authored_on = getattr(page, "authored_on", None)
        if not authored_on:
            authored_on = date.today()
        url_path = getattr(page, "url_path", "")

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

    def extract(self, page: Any) -> PublishedPayload:
        title = getattr(page, "title", "")
        doc_uuid = str(getattr(page, "document_uuid", ""))
        authored_on = getattr(page, "authored_on", None)
        if not authored_on:
            authored_on = date.today()
        url_path = getattr(page, "url_path", "")
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
        )
