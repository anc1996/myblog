"""
HTML 安全净化器。

基于 Rust 实现的高性能安全库 nh3，提供白名单标签、属性及 URL 协议过滤，彻底阻断 XSS 跨站脚本攻击。
"""

from html import escape
from html.parser import HTMLParser
import re
import nh3

from content_blocks.embeds.providers import is_canonical_player_url, is_safe_embed_url

_SAFE_IMG_STYLE_DECL_RE = re.compile(
    r"^\s*((?:max-)?width)\s*:\s*(\d{1,4}(?:px|%)|auto)\s*$",
    re.IGNORECASE,
)
_GATEWAY_DOC_HREF_RE = re.compile(
    r"^/media-gateway/document/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/$"
)
_GATEWAY_IMG_SRC_RE = re.compile(
    r"^/media-gateway/image/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/$"
)
_SAFE_ALIGN_CLASSES = {"cb-align-left", "cb-align-center", "cb-align-right"}
_SAFE_P_CLASSES = _SAFE_ALIGN_CLASSES | {
    "step-caption",
    "doc-desc",
    "portfolio-tech-desc",
    "featured-posts-desc",
    "comment-inspect-risk",
}
_SAFE_HEADING_CLASSES = _SAFE_ALIGN_CLASSES | {
    "lead-title",
    "step-heading",
    "callout-title",
    "faq-question",
    "portfolio-repo-title",
    "portfolio-gallery-heading",
    "featured-posts-heading",
    "cb-heading-anchor",
}

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
    "a": {"href", "title", "target", "class", "download", "data-analytics-conv"},
    "img": {"src", "alt", "title", "width", "height", "loading", "data-asset-id", "data-full-src", "class", "style"},
    "table": {"class", "align", "border"},
    "caption": {"class"},
    "thead": {"class"},
    "tbody": {"class"},
    "tr": {"class"},
    "th": {"colspan", "rowspan", "align", "valign", "scope"},
    "td": {"colspan", "rowspan", "align", "valign"},
    "code": {"class"},
    "pre": {"class"},
    "p": {"class"},
    "blockquote": {"class"},
    "h1": {"id", "class"},
    "h2": {"id", "class"},
    "h3": {"id", "class"},
    "h4": {"id", "class"},
    "h5": {"id", "class"},
    "h6": {"id", "class"},
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
        "data-full-src",
        "data-caption",
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
    if tag == "div" and attr == "data-full-src":
        return value if _GATEWAY_IMG_SRC_RE.match(value or "") else None
    if tag == "img" and attr == "data-full-src":
        return value if _GATEWAY_IMG_SRC_RE.match(value or "") else None
    if tag == "p" and attr == "class":
        kept = [c for c in (value or "").split() if c in _SAFE_P_CLASSES]
        return " ".join(kept) if kept else None
    if tag == "blockquote" and attr == "class":
        kept = [c for c in (value or "").split() if c in _SAFE_ALIGN_CLASSES]
        return " ".join(kept) if kept else None
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and attr == "class":
        kept = [c for c in (value or "").split() if c in _SAFE_HEADING_CLASSES]
        return " ".join(kept) if kept else None
    if tag == "a" and attr == "data-analytics-conv":
        return value if value in {"repo_click", "demo_click"} else None
    if tag == "a" and attr == "class":
        allowed_classes = {"doc-download-link"}
        kept = [c for c in (value or "").split() if c in allowed_classes]
        return " ".join(kept) if kept else None
    if tag == "a" and attr == "download":
        # 站内受控下载提示默认使用无值属性；仅防换行与路径分隔符，具体是否属于受控网关由后置结构化过滤把关
        cleaned = (value or "").strip()
        if any(ch in cleaned for ch in ("\r", "\n", "/", "\\")):
            return None
        return ""
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


class _AnchorDownloadGateParser(HTMLParser):
    """
    结构化二次过滤：确保仅当 <a> 的 href 指向站内受控文档网关
    /media-gateway/document/<UUID>/ 时，才保留 download 属性与 doc-download-link 类。
    """

    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit_tag(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit_tag(tag, attrs, self_closing=True)

    def _emit_tag(self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool) -> None:
        filtered_attrs = attrs
        if tag == "a":
            href_val = ""
            for k, v in attrs:
                if k == "href" and v:
                    href_val = v
                    break
            is_doc_gateway = bool(_GATEWAY_DOC_HREF_RE.match(href_val))
            new_attrs: list[tuple[str, str | None]] = []
            for k, v in attrs:
                if k == "download":
                    if is_doc_gateway:
                        new_attrs.append(("download", None))
                    continue
                if k == "class":
                    classes = [c for c in (v or "").split() if c != "doc-download-link" or is_doc_gateway]
                    if classes:
                        new_attrs.append(("class", " ".join(classes)))
                    continue
                new_attrs.append((k, v))
            filtered_attrs = new_attrs

        attr_chunks: list[str] = []
        for k, v in filtered_attrs:
            if v is None:
                attr_chunks.append(f" {k}")
            else:
                attr_chunks.append(f' {k}="{escape(v, quote=True)}"')
        suffix = " />" if (self_closing and tag not in self._VOID_TAGS) else ">"
        self.parts.append(f"<{tag}{''.join(attr_chunks)}{suffix}")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        pass


def sanitize_html(raw_html: str) -> str:
    """对富文本或 Markdown 渲染后的 HTML 执行白名单安全净化"""
    if not raw_html:
        return ""
    cleaned = nh3.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_filter_attributes,
        filter_style_properties={"width", "max-width"},
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
    )
    if "download" in cleaned or "doc-download-link" in cleaned:
        parser = _AnchorDownloadGateParser()
        parser.feed(cleaned)
        parser.close()
        return "".join(parser.parts)
    return cleaned
