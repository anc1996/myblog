"""
HTML 安全净化器。

基于 Rust 实现的高性能安全库 nh3，提供白名单标签、属性及 URL 协议过滤，彻底阻断 XSS 跨站脚本攻击。
"""

import nh3

# 允许的 HTML 标签白名单
ALLOWED_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s", "strike", "del",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "code",
    "ul", "ol", "li",
    "a", "img",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "div", "span", "hr", "details", "summary", "sup", "sub",
    "figure", "figcaption", "button",
    "audio", "video", "source", "track",
}

# 允许的属性白名单
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "title", "width", "height", "loading", "data-asset-id"},
    "th": {"colspan", "rowspan", "align", "valign", "scope"},
    "td": {"colspan", "rowspan", "align", "valign"},
    "code": {"class"},
    "pre": {"class"},
    "div": {"class", "data-language", "data-mermaid", "data-mode", "data-theme", "data-asset-id", "data-embed-url"},
    "span": {"class"},
    "details": {"open"},
    "figure": {"class", "data-asset-id"},
    "figcaption": {"class"},
    "button": {"type", "class", "disabled", "data-clipboard-text"},
    "audio": {"controls", "preload", "src", "class"},
    "video": {"controls", "preload", "src", "poster", "width", "height", "class"},
    "source": {"src", "type"},
    "track": {"src", "kind", "srclang", "label"},
}

# 允许的 URL 协议（严格排除 javascript: / data: / vbscript:）
ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


def sanitize_html(raw_html: str) -> str:
    """对富文本或 Markdown 渲染后的 HTML 执行白名单安全净化"""
    if not raw_html:
        return ""
    return nh3.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
    )
