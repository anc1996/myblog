"""
HTML 安全净化器。

基于 Rust 实现的高性能安全库 nh3，提供白名单标签、属性及 URL 协议过滤，彻底阻断 XSS 跨站脚本攻击。
"""

import re
import nh3

from content_blocks.embeds.providers import is_canonical_player_url, is_safe_embed_url

_SAFE_IMG_STYLE_DECL_RE = re.compile(
    r"^\s*((?:max-)?width)\s*:\s*(\d{1,4}(?:px|%)|auto)\s*$",
    re.IGNORECASE,
)

# 允许的 HTML 标签白名单
ALLOWED_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s", "strike", "del",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "code",
    "ul", "ol", "li",
    "a", "img",
    "table", "caption", "thead", "tbody", "tfoot", "tr", "th", "td",
    "div", "span", "hr", "details", "summary", "sup", "sub",
    "figure", "figcaption", "button",
    "audio", "video", "source", "track",
}

# 允许的属性白名单
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "title", "width", "height", "loading", "data-asset-id", "class", "style"},
    "table": {"class", "align", "border"},
    "caption": {"class"},
    "thead": {"class"},
    "tbody": {"class"},
    "tr": {"class"},
    "th": {"colspan", "rowspan", "align", "valign", "scope"},
    "td": {"colspan", "rowspan", "align", "valign"},
    "code": {"class"},
    "pre": {"class"},
    "h3": {"id", "class"},
    "h4": {"id", "class"},
    "div": {
        "id",
        "class",
        "data-language",
        "data-mermaid",
        "data-mode",
        "data-theme",
        "data-asset-id",
        "data-cb-media-kind",
        "data-embed-url",
        "data-embed-src",
        "data-embed-provider",
        "data-embed-provider-name",
        "data-embed-kind",
        "data-embed-title",
        "data-step",
    },
    "span": {"class"},
    "summary": {"class"},
    "details": {"open"},
    "figure": {"class", "data-asset-id"},
    "figcaption": {"class"},
    "button": {"type", "class", "disabled", "data-clipboard-text"},
    "audio": {"controls", "preload", "src", "class"},
    "video": {"controls", "playsinline", "preload", "src", "poster", "width", "height", "class"},
    "source": {"src", "type"},
    "track": {"src", "kind", "srclang", "label"},
}

# 允许的 URL 协议（严格排除 javascript: / data: / vbscript:）
ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


def _filter_attributes(tag: str, attr: str, value: str) -> str | None:
    if tag == "div" and attr == "data-cb-media-kind":
        return value if value in {"audio", "video"} else None
    if tag == "img" and attr == "style":
        safe_decls: list[str] = []
        for part in (value or "").split(";"):
            m = _SAFE_IMG_STYLE_DECL_RE.match(part)
            if m:
                safe_decls.append(f"{m.group(1).lower()}:{m.group(2).lower()}")
        return ";".join(safe_decls) if safe_decls else None
    if tag == "div" and attr == "data-embed-src" and not is_canonical_player_url(value):
        return None
    if tag == "div" and attr == "data-embed-url" and not is_safe_embed_url(value)[0]:
        return None
    return value


def sanitize_html(raw_html: str) -> str:
    """对富文本或 Markdown 渲染后的 HTML 执行白名单安全净化"""
    if not raw_html:
        return ""
    return nh3.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_filter_attributes,
        filter_style_properties={"width", "max-width"},
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
    )
