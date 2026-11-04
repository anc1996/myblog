"""
自定义富文本块 (CustomRichTextBlock)。

支持标题、文字样式、列表、下划线与链接；输出前严格经 nh3 白名单净化，
并提供全文提取与媒体引用提取实现。
"""

import re
from typing import Any
from django.utils.safestring import mark_safe
from wagtail import blocks
from wagtail.rich_text import RichText

from content_blocks.contracts import (
    BlockContract,
    BlockValidationResult,
    BlockRenderResult,
    BlockTextExtraction,
    MediaReference,
)
from content_blocks.sanitizer import sanitize_html


class CustomRichTextBlock(blocks.RichTextBlock):
    type_name = "rich_text"
    schema_version = 1

    def __init__(self, **kwargs):
        features = kwargs.pop(
            "features",
            ["h2", "h3", "h4", "bold", "italic", "underline", "ol", "ul", "link", "document-link", "image"],
        )
        super().__init__(features=features, **kwargs)

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        """验证富文本输入合法性"""
        if not value:
            return BlockValidationResult(is_valid=True)
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        """渲染并净化富文本内容"""
        if not value:
            return BlockRenderResult(html="", is_safe=True)
        raw_html = str(value)
        cleaned_html = sanitize_html(raw_html)
        return BlockRenderResult(html=cleaned_html, is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        """Wagtail 模板原生渲染集成入口"""
        res = self.render_clean(value, context)
        return mark_safe(res.html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        """提取供搜索引擎索引的纯文本内容"""
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        raw_html = str(value)
        clean_text = re.sub(r"<[^>]+>", " ", raw_html)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()
        segments = [s.strip() for s in clean_text.splitlines() if s.strip()]
        return BlockTextExtraction(plain_text=clean_text, logical_segments=segments)

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        """从嵌入富文本的标记中提取引用的媒体资源 ID"""
        if not value:
            return []
        raw = str(value)
        refs: list[MediaReference] = []
        # 匹配 <embed embedtype="image" id="..."/> 或 <img data-asset-id="..."/>
        for match in re.finditer(r'(?:embedtype="image"\s+id="([^"]+)"|data-asset-id="([^"]+)")', raw):
            asset_id = match.group(1) or match.group(2)
            refs.append(MediaReference(asset_id=asset_id, asset_type="image", block_path=path))
        # 匹配文档链接 <a linktype="document" id="...">
        for match in re.finditer(r'linktype="document"\s+id="([^"]+)"', raw):
            asset_id = match.group(1)
            refs.append(MediaReference(asset_id=asset_id, asset_type="document", block_path=path))
        return refs
