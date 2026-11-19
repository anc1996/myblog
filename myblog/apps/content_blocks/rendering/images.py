"""
Markdown 与复杂表格通用图片尺寸/对齐解析与安全标签构建 (content_blocks.rendering.images)。

支持作者在 Markdown 与 TableJSON 单元格中通过简洁语法调整图片尺寸与对齐：
- ![图注|320](asset://image/<uuid>)            -> 宽 320px，高度自适应
- ![图注|320x200](asset://image/<uuid>)        -> 宽 320px，高 200px
- ![图注|50%](asset://image/<uuid>)            -> 相对容器宽度 50%
- ![图注|sm|center](asset://image/<uuid>)      -> 预设尺寸 (sm=240, md=480, lg=720, full=100%) + 居中
- ![图注|w=360,align=center](...)              -> 显式键值语法
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from content_blocks.rendering.media_refs import UUID_PATTERN

_PRESET_PX = {
    "sm": "240",
    "md": "480",
    "lg": "720",
}

_GATEWAY_IMG_RE = re.compile(r"^/media-gateway/image/(" + UUID_PATTERN + r")/?$", re.IGNORECASE)
_ASSET_IMG_RE = re.compile(r"^asset://image/(" + UUID_PATTERN + r")$", re.IGNORECASE)
_SAFE_IMG_URL_RE = re.compile(r"^(?:https?://|/media-gateway/image/)", re.IGNORECASE)


def _clamp_px(val_str: str) -> str | None:
    try:
        val = int(val_str)
    except (TypeError, ValueError):
        return None
    if 16 <= val <= 4000:
        return str(val)
    return None


def _clamp_pct(val_str: str) -> str | None:
    try:
        val = int(val_str)
    except (TypeError, ValueError):
        return None
    if 5 <= val <= 100:
        return f"{val}%"
    return None


def parse_image_alt_and_attrs(raw_alt: str) -> tuple[str, dict[str, str]]:
    """
    解析 alt 中的 `|尺寸/对齐` 后缀；若后缀不是合法尺寸/对齐指令，完整保留原始 alt。
    """
    alt_text = str(raw_alt or "")
    if "|" not in alt_text:
        return alt_text, {}

    base_alt, raw_spec = alt_text.rsplit("|", 1)
    if base_alt.endswith("\\"):
        base_alt = base_alt[:-1]
    spec = raw_spec.strip()
    if not spec:
        return alt_text, {}

    tokens = [tok.strip() for tok in re.split(r"[,;\s]+", spec) if tok.strip()]
    if not tokens:
        return alt_text, {}

    parsed: dict[str, str] = {}
    classes: list[str] = ["cb-img-sized"]

    for tok in tokens:
        low = tok.lower()
        if low in _PRESET_PX:
            parsed["width"] = _PRESET_PX[low]
            continue
        if low == "full":
            parsed["style"] = "width:100%;max-width:100%"
            continue
        if low in ("left", "center", "right"):
            classes.append(f"cb-img-align-{low}")
            continue

        m_dim = re.fullmatch(r"(\d{2,4})x(\d{2,4})(?:px)?", low)
        if m_dim:
            w = _clamp_px(m_dim.group(1))
            h = _clamp_px(m_dim.group(2))
            if not w or not h:
                return alt_text, {}
            parsed["width"] = w
            parsed["height"] = h
            continue

        m_px = re.fullmatch(r"(?:(?:w|width)=)?(\d{2,4})(?:px)?", low)
        if m_px:
            w = _clamp_px(m_px.group(1))
            if not w:
                return alt_text, {}
            parsed["width"] = w
            continue

        m_h = re.fullmatch(r"(?:h|height)=(\d{2,4})(?:px)?", low)
        if m_h:
            h = _clamp_px(m_h.group(1))
            if not h:
                return alt_text, {}
            parsed["height"] = h
            continue

        m_pct = re.fullmatch(r"(?:(?:w|width)=)?(\d{1,3})%", low)
        if m_pct:
            pct = _clamp_pct(m_pct.group(1))
            if not pct:
                return alt_text, {}
            parsed["style"] = f"width:{pct};max-width:100%"
            continue

        m_align = re.fullmatch(r"align=(left|center|right)", low)
        if m_align:
            classes.append(f"cb-img-align-{m_align.group(1)}")
            continue

        # 只要出现任何非指令词汇（如 "CPU | 内存"），按普通 alt 文本原样保留
        return alt_text, {}

    parsed["class"] = " ".join(dict.fromkeys(classes))
    return base_alt.strip(), parsed


def build_safe_img_tag(
    src: str,
    raw_alt: str = "",
    *,
    title: str = "",
    asset_id: str = "",
) -> str:
    """构建经过转义与尺寸/网关补全的安全 <img> 标签"""
    clean_src = str(src or "").strip()
    m_asset = _ASSET_IMG_RE.match(clean_src)
    resolved_aid = asset_id.lower() if asset_id else ""
    if m_asset:
        resolved_aid = m_asset.group(1).lower()
        clean_src = f"/media-gateway/image/{resolved_aid}/"
    elif not resolved_aid:
        m_gw = _GATEWAY_IMG_RE.match(clean_src)
        if m_gw:
            resolved_aid = m_gw.group(1).lower()

    if not _SAFE_IMG_URL_RE.match(clean_src):
        return html.escape(raw_alt or "")

    clean_alt, extra_attrs = parse_image_alt_and_attrs(raw_alt)
    attrs: list[tuple[str, str]] = [
        ("src", clean_src),
        ("alt", clean_alt),
        ("loading", "lazy"),
    ]
    if title:
        attrs.append(("title", title))
    if resolved_aid:
        attrs.append(("data-asset-id", resolved_aid))
    for key in ("width", "height", "style", "class"):
        if key in extra_attrs:
            attrs.append((key, extra_attrs[key]))

    attr_str = " ".join(f'{k}="{html.escape(v, quote=True)}"' for k, v in attrs)
    return f"<img {attr_str} />"


class _MarkdownImgAttrUpgrader(HTMLParser):
    """为已完成净化的 HTML 中的 <img> 应用 alt 尺寸参数并补齐 data-asset-id。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._parts: list[str] = []

    def _render_img(self, attrs: list[tuple[str, str | None]], self_closing: bool) -> None:
        attr_map: dict[str, str] = {}
        order: list[str] = []
        for k, v in attrs:
            lk = k.lower()
            if lk not in attr_map:
                order.append(lk)
            attr_map[lk] = "" if v is None else html.unescape(v)

        src = attr_map.get("src", "").strip()
        m_gw = _GATEWAY_IMG_RE.match(src)
        if m_gw and "data-asset-id" not in attr_map:
            attr_map["data-asset-id"] = m_gw.group(1).lower()
            order.append("data-asset-id")

        if "loading" not in attr_map:
            attr_map["loading"] = "lazy"
            order.append("loading")

        raw_alt = attr_map.get("alt", "")
        clean_alt, parsed_attrs = parse_image_alt_and_attrs(raw_alt)
        if parsed_attrs:
            attr_map["alt"] = clean_alt
            if "alt" not in order:
                order.append("alt")
            for k, v in parsed_attrs.items():
                if k == "class" and attr_map.get("class"):
                    merged = " ".join(dict.fromkeys((attr_map["class"] + " " + v).split()))
                    attr_map["class"] = merged
                elif k not in attr_map or not attr_map[k]:
                    attr_map[k] = v
                    if k not in order:
                        order.append(k)

        rendered_attrs = " ".join(
            f'{k}="{html.escape(attr_map[k], quote=True)}"'
            for k in order
            if k in attr_map
        )
        suffix = " />" if self_closing else ">"
        self._parts.append(f"<img {rendered_attrs}{suffix}" if rendered_attrs else f"<img{suffix}")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            self._render_img(attrs, self_closing=False)
        else:
            self._parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            self._render_img(attrs, self_closing=True)
        else:
            self._parts.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self._parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self._parts.append(f"<!{decl}>")

    def render(self) -> str:
        return "".join(self._parts)


def apply_markdown_image_sizing_in_html(html_str: str) -> str:
    """升级 HTML 片段中带 `alt|尺寸` 的 <img> 标签并补齐受控媒体属性"""
    if not html_str or "<img" not in html_str.lower():
        return html_str
    parser = _MarkdownImgAttrUpgrader()
    parser.feed(html_str)
    parser.close()
    return parser.render()
