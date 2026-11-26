"""
自定义富文本块 (CustomRichTextBlock)。

支持标题、文字样式、列表、下划线与链接；输出前严格经 nh3 白名单净化，
并提供全文提取与媒体引用提取实现。
"""

import html
import re
import uuid
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
from content_blocks.embeds import render_embed_wrapper_html
from content_blocks.blocks.media import _resolve_asset_id
from content_blocks.rendering.media_refs import extract_media_references
from content_blocks.sanitizer import sanitize_html

DEFAULT_RICH_TEXT_FEATURES = [
    "bold",
    "italic",
    "code",
    "superscript",
    "subscript",
    "strikethrough",
    "underline",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ol",
    "ul",
    "blockquote",
    "hr",
    "align-left",
    "align-center",
    "align-right",
    "link",
    "document-link",
    "image",
    "embed",
]

_EMBED_TAG_RE = re.compile(r"<embed\b([^>]*?)/?>", re.IGNORECASE)
_A_TAG_RE = re.compile(r"<a\b([^>]*?)>", re.IGNORECASE)
_ATTR_PAIR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"')


def _parse_attrs(attr_str: str) -> dict[str, str]:
    return {k.lower(): html.unescape(v) for k, v in _ATTR_PAIR_RE.findall(attr_str or "")}


def _replace_media_embeds_safely(raw_source: str) -> tuple[str, list[tuple[str, str]]]:
    """
    在 expand_db_html 前先将 <embed embedtype="media" url="..."/> 转为白名单校验后的
    .media-embed-wrapper 占位（与 CustomEmbedBlock 一致），避免 Wagtail 生成的 <iframe> 被 nh3 整段剥离。
    """
    if not raw_source or "embedtype" not in raw_source:
        return raw_source, []

    stashed: list[tuple[str, str]] = []
    nonce = uuid.uuid4().hex[:10]

    def _sub(match: re.Match) -> str:
        attrs = _parse_attrs(match.group(1))
        if attrs.get("embedtype") != "media":
            return match.group(0)
        url = (attrs.get("url") or "").strip()
        title = (attrs.get("title") or "").strip()
        wrapper_html = render_embed_wrapper_html(url, title=title)
        if not wrapper_html:
            return ""
        token = f"CBRTEMBED_{nonce}_{len(stashed)}_X"
        stashed.append((token, wrapper_html))
        return token

    return _EMBED_TAG_RE.sub(_sub, raw_source), stashed


def render_richtext_field_html(value: Any, *, expand_db: bool = True) -> str:
    """
    通用安全富文本渲染入口（供 RichTextField 与 CustomRichTextBlock 共用）：
    1. 在 expand_db_html 前安全替换 <embed embedtype="media"/>；
    2. 展开 Wagtail 图片嵌入与站内页/文档链接；
    3. 经 nh3 白名单清洗后返回安全 HTML。
    """
    if not value:
        return ""
    if isinstance(value, RichText):
        pre_source, media_embeds = _replace_media_embeds_safely(value.source or "")
        raw_html = str(RichText(pre_source))
    else:
        pre_source, media_embeds = _replace_media_embeds_safely(str(value))
        raw_html = str(RichText(pre_source)) if expand_db else pre_source

    for token, embed_div in media_embeds:
        p_pat = re.compile(r"<p>\s*" + re.escape(token) + r"\s*</p>")
        if p_pat.search(raw_html):
            raw_html = p_pat.sub(lambda _: embed_div, raw_html)
        else:
            raw_html = raw_html.replace(token, embed_div)

    return sanitize_html(raw_html)


def extract_richtext_field_text(value: Any) -> str:
    """从 RichText / DB HTML 字符串中剥离标记，返回规范化纯文本（供索引与摘要共用）。"""
    if not value:
        return ""
    raw_html = value.source if isinstance(value, RichText) else str(value)
    clean_text = html.unescape(re.sub(r"<[^>]+>", " ", raw_html))
    return re.sub(r"\s+", " ", clean_text).strip()


def extract_richtext_field_media_refs(
    value: Any,
    *,
    path: str = "",
    field_name: str = "",
) -> list[MediaReference]:
    """从 RichText / DB HTML 中提取图片嵌入、文档链接与受控媒体网关引用。"""
    if not value:
        return []
    from media_library.models import CustomDocument, CustomImage

    raw = value.source if isinstance(value, RichText) else str(value)
    refs: list[MediaReference] = []
    seen: set[tuple[str, str]] = set()
    extra_meta = {"field_name": field_name} if field_name else None

    for match in _EMBED_TAG_RE.finditer(raw):
        attrs = _parse_attrs(match.group(1))
        if attrs.get("embedtype") == "image" and attrs.get("id"):
            aid = _resolve_asset_id(attrs["id"], expected_model=CustomImage)
            if aid and ("image", aid) not in seen:
                seen.add(("image", aid))
                refs.append(MediaReference(asset_id=aid, asset_type="image", block_path=path, extra=extra_meta))

    for match in re.finditer(r'data-asset-id="([^"]+)"', raw):
        aid = _resolve_asset_id(match.group(1), expected_model=CustomImage)
        if aid and ("image", aid) not in seen:
            seen.add(("image", aid))
            refs.append(MediaReference(asset_id=aid, asset_type="image", block_path=path, extra=extra_meta))

    for match in _A_TAG_RE.finditer(raw):
        attrs = _parse_attrs(match.group(1))
        if attrs.get("linktype") == "document" and attrs.get("id"):
            aid = _resolve_asset_id(attrs["id"], expected_model=CustomDocument)
            if aid and ("document", aid) not in seen:
                seen.add(("document", aid))
                refs.append(MediaReference(asset_id=aid, asset_type="document", block_path=path, extra=extra_meta))

    for ref in extract_media_references(raw, path=path):
        key = (ref.asset_type, str(ref.asset_id).lower())
        if ref.asset_id and key not in seen:
            seen.add(key)
            if field_name:
                merged = dict(ref.extra) if getattr(ref, "extra", None) else {}
                merged.setdefault("field_name", field_name)
                refs.append(MediaReference(asset_id=ref.asset_id, asset_type=ref.asset_type, block_path=ref.block_path, extra=merged))
            else:
                refs.append(ref)
    return refs


class CustomRichTextBlock(blocks.RichTextBlock):
    type_name = "rich_text"
    schema_version = 1

    def __init__(self, **kwargs):
        features = kwargs.pop("features", list(DEFAULT_RICH_TEXT_FEATURES))
        super().__init__(features=features, **kwargs)

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        """验证富文本输入合法性"""
        if not value:
            return BlockValidationResult(is_valid=True)
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        """渲染并净化富文本内容"""
        cleaned_html = render_richtext_field_html(value, expand_db=False)
        if not cleaned_html:
            return BlockRenderResult(html="", is_safe=True)
        return BlockRenderResult(html=f'<div class="rich-text">{cleaned_html}</div>', is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        """Wagtail 模板原生渲染集成入口"""
        res = self.render_clean(value, context)
        return mark_safe(res.html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        """提取供搜索引擎索引的纯文本内容"""
        clean_text = extract_richtext_field_text(value)
        if not clean_text:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        segments = [s.strip() for s in clean_text.splitlines() if s.strip()]
        return BlockTextExtraction(plain_text=clean_text, logical_segments=segments)

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        """从嵌入富文本的标记中提取引用的媒体资源 ID"""
        return extract_richtext_field_media_refs(value, path=path)
