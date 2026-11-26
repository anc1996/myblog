"""
Markdown 与复杂表格通用图片尺寸/对齐解析与安全标签构建 (content_blocks.rendering.images)。

支持作者在 Markdown 与 TableJSON 单元格中通过简洁语法调整图片尺寸与对齐：
- ![图注|320](asset://image/<uuid>)            -> 宽 320px，高度自适应
- ![图注|320x200](asset://image/<uuid>)        -> 宽 320px，高 200px
- ![图注|50%](asset://image/<uuid>)            -> 映射受控语义宽度类 cb-img-w-50（无内联 style）
- ![图注|sm|center](asset://image/<uuid>)      -> 预设尺寸 (sm=240, md=480, lg=720, full=100%) + 居中
- ![图注|md,left,card](asset://image/<uuid> "可选图注") -> 左图右文环绕 + 轻强调修饰 + 图注
- ![图注|w=360,align=center](...)              -> 显式键值语法
"""

from __future__ import annotations

import html
import re
import uuid
from html.parser import HTMLParser
from typing import Any

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


def _map_pct_to_bucket_class(pct_str: str) -> str:
    """按 Option A 将合法整数百分比就近映射到有限 6 档语义宽度类，消除内联 style。"""
    val = int(pct_str.rstrip("%"))
    if val <= 29:
        return "cb-img-w-25"
    if val <= 41:
        return "cb-img-w-33"
    if val <= 58:
        return "cb-img-w-50"
    if val <= 70:
        return "cb-img-w-67"
    if val <= 87:
        return "cb-img-w-75"
    return "cb-img-w-100"


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
            classes.extend(["cb-img-w-100", "cb-image--full"])
            continue
        if low in ("left", "center", "right"):
            classes.extend([f"cb-img-align-{low}", f"cb-image--{low}"])
            continue
        if low in ("card", "framed", "accent"):
            classes.append("cb-image--card")
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
            classes.append(_map_pct_to_bucket_class(pct))
            continue

        m_align = re.fullmatch(r"align=(left|center|right)", low)
        if m_align:
            align_val = m_align.group(1)
            classes.extend([f"cb-img-align-{align_val}", f"cb-image--{align_val}"])
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
        if m_gw:
            aid = m_gw.group(1).lower()
            if "data-asset-id" not in attr_map:
                attr_map["data-asset-id"] = aid
                order.append("data-asset-id")
            if "data-full-src" not in attr_map:
                attr_map["data-full-src"] = f"/media-gateway/image/{aid}/"
                order.append("data-full-src")

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


_IMG_ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"')
_WIDTH_BUCKET_CLS_RE = re.compile(r"^cb-img-w-(?:25|33|50|67|75|100)$")


class _StandaloneMarkdownFigureUpgrader(HTMLParser):
    """
    用严格 HTMLParser 状态机将正文最外层独立段落 <p><img ...></p> 升级为 <figure>，
    自动跳过任意层级的 <table> / <figure> / <pre> / <code> 内部，无正则截断或固定占位符碰撞风险。
    """

    _SKIP_CONTAINERS = {"table", "figure", "pre", "code"}
    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._skip_depth = 0
        self._in_plain_p = False
        self._p_buf: list[tuple[str, Any]] = []

    def _flush_p_as_raw(self) -> None:
        for kind, val in self._p_buf:
            self._out.append(val[0] if kind == "img" else val)
        self._p_buf.clear()
        self._in_plain_p = False

    def _build_upgraded_figure(self, img_raw: str, img_attrs: list[tuple[str, str | None]]) -> str:
        attrs_dict = {k.lower(): ("" if v is None else html.unescape(v)) for k, v in img_attrs}
        img_classes = [c for c in attrs_dict.get("class", "").split() if c]

        align = "center"
        if "cb-image--left" in img_classes or "cb-img-align-left" in img_classes:
            align = "left"
        elif "cb-image--right" in img_classes or "cb-img-align-right" in img_classes:
            align = "right"
        elif "cb-image--full" in img_classes or "cb-img-w-100" in img_classes:
            align = "full"

        has_card = "cb-image--card" in img_classes
        moved_width_classes = [c for c in img_classes if _WIDTH_BUCKET_CLS_RE.match(c)]
        kept_img_classes = [
            c
            for c in img_classes
            if c not in {
                "cb-img-align-left",
                "cb-img-align-center",
                "cb-img-align-right",
                "cb-image--left",
                "cb-image--center",
                "cb-image--right",
                "cb-image--full",
                "cb-image--card",
            } and not _WIDTH_BUCKET_CLS_RE.match(c)
        ]
        if "cb-image-img" not in kept_img_classes:
            kept_img_classes.append("cb-image-img")

        # 重建 img 属性（将对齐类与百分比宽度类统一上移至 <figure>，避免与外层 max-width 双重缩放）
        new_img_attrs: list[str] = []
        has_cls = False
        for k, v in img_attrs:
            lk = k.lower()
            if lk == "class":
                has_cls = True
                new_img_attrs.append(f'class="{html.escape(" ".join(kept_img_classes), quote=True)}"')
            elif v is None:
                new_img_attrs.append(k)
            else:
                new_img_attrs.append(f'{k}="{html.escape(html.unescape(v), quote=True)}"')
        if not has_cls:
            new_img_attrs.append(f'class="{html.escape(" ".join(kept_img_classes), quote=True)}"')
        new_img_tag = f"<img {' '.join(new_img_attrs)} />"

        compat_fmt = "centered" if align == "center" else align
        fig_classes = ["media-image-wrapper", "cb-image", f"cb-image--{align}", f"format-{compat_fmt}"]
        fig_classes.extend(moved_width_classes)
        if has_card:
            fig_classes.append("cb-image--card")

        asset_id = attrs_dict.get("data-asset-id", "")
        asset_attr = f' data-asset-id="{html.escape(asset_id, quote=True)}"' if asset_id else ""
        title_caption = (attrs_dict.get("title") or "").strip()
        cap_html = f'<figcaption class="media-caption">{html.escape(title_caption)}</figcaption>' if title_caption else ""

        return f'<figure class="{" ".join(dict.fromkeys(fig_classes))}"{asset_attr}>{new_img_tag}{cap_html}</figure>'

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ltag = tag.lower()
        raw = self.get_starttag_text()
        if self._skip_depth > 0:
            if ltag in self._SKIP_CONTAINERS:
                self._skip_depth += 1
            self._out.append(raw)
            return
        if ltag in self._SKIP_CONTAINERS:
            if self._in_plain_p:
                self._flush_p_as_raw()
            self._skip_depth += 1
            self._out.append(raw)
            return
        if ltag == "p" and not self._in_plain_p and not attrs:
            self._in_plain_p = True
            self._p_buf = [("p_open", raw)]
            return
        if self._in_plain_p:
            if ltag == "img":
                self._p_buf.append(("img", (raw, attrs)))
            else:
                self._flush_p_as_raw()
                self._out.append(raw)
            return
        self._out.append(raw)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ltag = tag.lower()
        raw = self.get_starttag_text()
        if self._in_plain_p:
            if ltag == "img":
                self._p_buf.append(("img", (raw, attrs)))
            else:
                self._flush_p_as_raw()
                self._out.append(raw)
            return
        self._out.append(raw)

    def handle_endtag(self, tag: str) -> None:
        ltag = tag.lower()
        raw = f"</{tag}>"
        if self._skip_depth > 0:
            self._out.append(raw)
            if ltag in self._SKIP_CONTAINERS:
                self._skip_depth -= 1
            return
        if self._in_plain_p:
            if ltag == "p":
                non_ws = [item for item in self._p_buf[1:] if not (item[0] == "text" and not str(item[1]).strip())]
                if len(non_ws) == 1 and non_ws[0][0] == "img":
                    img_raw, img_attrs = non_ws[0][1]
                    self._out.append(self._build_upgraded_figure(img_raw, img_attrs))
                    self._p_buf.clear()
                    self._in_plain_p = False
                    return
                self._flush_p_as_raw()
                self._out.append(raw)
                return
            self._flush_p_as_raw()
        self._out.append(raw)

    def handle_data(self, data: str) -> None:
        if self._in_plain_p:
            if data.strip():
                self._flush_p_as_raw()
                self._out.append(data)
            else:
                self._p_buf.append(("text", data))
            return
        self._out.append(data)

    def handle_entityref(self, name: str) -> None:
        txt = f"&{name};"
        if self._in_plain_p:
            self._flush_p_as_raw()
        self._out.append(txt)

    def handle_charref(self, name: str) -> None:
        txt = f"&#{name};"
        if self._in_plain_p:
            self._flush_p_as_raw()
        self._out.append(txt)

    def handle_comment(self, data: str) -> None:
        txt = f"<!--{data}-->"
        if self._in_plain_p:
            self._flush_p_as_raw()
        self._out.append(txt)

    def handle_decl(self, decl: str) -> None:
        txt = f"<!{decl}>"
        if self._in_plain_p:
            self._flush_p_as_raw()
        self._out.append(txt)

    def render(self) -> str:
        if self._in_plain_p:
            for kind, val in self._p_buf:
                self._out.append(val[0] if kind == "img" else val)
            self._p_buf.clear()
            self._in_plain_p = False
        return "".join(self._out)


def upgrade_standalone_markdown_figures(html_str: str) -> str:
    """
    将 Markdown 独立段落里的单张图片（<p><img ...></p>，非表格/figure 内）升级为
    与 CustomImageBlock 同构的 <figure class="media-image-wrapper cb-image ...">：
    - 浮动/居中对齐类及百分比宽度类迁移至外层 <figure>，避免双重缩放与图注错位；
    - 仅当存在非空 title 属性时输出 <figcaption class="media-caption">，不拿 alt 冒充图注。
    """
    if not html_str or "<img" not in html_str.lower():
        return html_str
    parser = _StandaloneMarkdownFigureUpgrader()
    parser.feed(html_str)
    parser.close()
    return parser.render()
