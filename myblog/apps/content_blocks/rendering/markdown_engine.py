"""
全站共享 CommonMark / Typora 兼容 Markdown 渲染引擎。

同时服务正文 CustomMarkdownBlock 与评论区 comments.sanitizer，避免双份解析实现：
1. 基于已锁定的 markdown-it-py 3.0.0 (CommonMark + table + strikethrough + breaks=True)；
2. 原生支持 Typora 常见的 2 空格 / 3 空格 / 4 空格嵌套列表、段落紧接列表、
   有序与无序混合嵌套、以及引用块 (>) 内嵌多级有序/无序列表；
3. 兼容中文标点紧邻的 **加粗** / *斜体* 语法，同时绝不误伤代码行内/围栏或蛇形命名下划线；
4. 将表格列的 style="text-align:..." 规范转换为 align="..." 属性，配合 nh3 严格白名单保留对齐；
5. 提供栈式围栏扫描器 extract_fenced_mermaid_blocks，防止代码块内的 ```mermaid 示例被误提取。
"""

from __future__ import annotations

import html
import re
import uuid
from typing import Callable

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML

_STRONG_CJK_RE = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")
_EM_CJK_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_TASK_PREFIX_RE = re.compile(r"^\[([ xX])\]\s+")
_ALIGN_OPEN_RE = re.compile(r"^[ ]{0,3}:::[ \t]*(align-(?:left|center|right))[ \t]*$")
_ALIGN_CLOSE_RE = re.compile(r"^[ ]{0,3}:::[ \t]*$")
_FENCE_LINE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")


def _protect_align_containers(md_text: str, nonce: str) -> tuple[str, list[tuple[str, str, str]]]:
    """
    在 CommonMark 解析前扫描成对的 `::: align-left|center|right` ... `:::` 块指令，
    跳过围栏代码块内部，仅把合法闭合的块边界替换为独立段落令牌。
    """
    if ":::" not in md_text:
        return md_text, []

    lines = md_text.splitlines(keepends=True)
    out: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    open_align: str | None = None
    block_buf: list[str] = []
    blocks_meta: list[tuple[str, str, str]] = []

    for line in lines:
        stripped_eol = line.rstrip("\r\n")
        if not in_fence:
            mf = _FENCE_LINE_RE.match(stripped_eol)
            if mf:
                marker = mf.group(1)
                info = mf.group(2).strip()
                if not (marker[0] == "`" and "`" in info):
                    in_fence = True
                    fence_char = marker[0]
                    fence_len = len(marker)
                if open_align is not None:
                    block_buf.append(line)
                else:
                    out.append(line)
                continue

            if open_align is None:
                m_open = _ALIGN_OPEN_RE.match(stripped_eol)
                if m_open:
                    open_align = m_open.group(1)
                    block_buf = []
                    continue
                out.append(line)
            else:
                if _ALIGN_CLOSE_RE.match(stripped_eol):
                    idx = len(blocks_meta)
                    open_tok = f"CBALIGNOPEN_{nonce}_{idx}_X"
                    close_tok = f"CBALIGNCLOSE_{nonce}_{idx}_X"
                    blocks_meta.append((open_tok, close_tok, open_align))
                    out.append(f"\n\n{open_tok}\n\n")
                    out.extend(block_buf)
                    out.append(f"\n\n{close_tok}\n\n")
                    open_align = None
                    block_buf = []
                else:
                    block_buf.append(line)
        else:
            close_stripped = stripped_eol.strip()
            if (
                len(close_stripped) >= fence_len
                and set(close_stripped) == {fence_char}
                and len(stripped_eol) - len(stripped_eol.lstrip(" \t")) <= 3
            ):
                in_fence = False
            if open_align is not None:
                block_buf.append(line)
            else:
                out.append(line)

    if open_align is not None:
        # 未闭合的指令原样回退为普通文本，不吞掉后续正文
        out.append(f"::: {open_align}\n")
        out.extend(block_buf)

    return "".join(out), blocks_meta


def _restore_align_containers(html_str: str, blocks_meta: list[tuple[str, str, str]]) -> str:
    if not blocks_meta:
        return html_str
    out = html_str
    for open_tok, close_tok, align_key in blocks_meta:
        div_open = f'<div class="cb-align-block cb-{align_key}">'
        out = re.sub(r"<p>\s*" + re.escape(open_tok) + r"\s*</p>", lambda _: div_open, out)
        out = re.sub(r"<p>\s*" + re.escape(close_tok) + r"\s*</p>", "</div>", out)
        out = out.replace(open_tok, div_open).replace(close_tok, "</div>")
    return out


def extract_fenced_mermaid_blocks(
    text: str,
    *,
    on_mermaid: Callable[[str, int], str],
) -> str:
    """
    按 CommonMark 围栏规则扫描文本，仅提取真正的顶层 ```mermaid 围栏块，
    避免 ````markdown 或 ```python 代码示例内嵌的 ```mermaid 被误解析。
    """
    if not text or "mermaid" not in text.lower():
        return text

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    is_mermaid_fence = False
    mermaid_lines: list[str] = []
    mermaid_idx = 0

    fence_open_re = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")

    for line in lines:
        stripped_eol = line.rstrip("\r\n")
        if not in_fence:
            m = fence_open_re.match(stripped_eol)
            if m:
                marker = m.group(1)
                info = m.group(2).strip().lower()
                # CommonMark: 反引号围栏的 info string 不能再包含反引号
                if marker[0] == "`" and "`" in info:
                    out.append(line)
                    continue
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
                is_mermaid_fence = info == "mermaid"
                if is_mermaid_fence:
                    mermaid_lines = []
                else:
                    out.append(line)
                continue
            out.append(line)
        else:
            close_stripped = stripped_eol.strip()
            is_closing = (
                len(close_stripped) >= fence_len
                and set(close_stripped) == {fence_char}
                and len(stripped_eol) - len(stripped_eol.lstrip(" \t")) <= 3
            )
            if is_closing:
                in_fence = False
                if is_mermaid_fence:
                    code = "".join(mermaid_lines)
                    out.append(on_mermaid(code, mermaid_idx))
                    mermaid_idx += 1
                    is_mermaid_fence = False
                    mermaid_lines = []
                else:
                    out.append(line)
                continue

            if is_mermaid_fence:
                mermaid_lines.append(line)
            else:
                out.append(line)

    if in_fence and is_mermaid_fence:
        # 未闭合围栏视为普通代码文本保留，不吞掉后续正文
        out.append(f"{fence_char * fence_len}mermaid\n")
        out.extend(mermaid_lines)

    return "".join(out)


def _render_cjk_friendly_text(tokens, index, options, env) -> str:
    """
    在普通文本节点中补充支持紧邻中文标点的 **加粗** / *斜体*；
    先 html.escape 转义原文再替换受控标签，代码与 HTML 节点不经过此函数。
    """
    raw = tokens[index].content
    escaped = html.escape(raw, quote=False)
    if "*" not in escaped:
        return escaped
    escaped = _STRONG_CJK_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _EM_CJK_RE.sub(r"<em>\1</em>", escaped)
    return escaped


def _convert_cell_align_attr(token) -> None:
    style_val = token.attrGet("style")
    if not style_val:
        return
    m = re.search(r"text-align\s*:\s*(left|center|right)", str(style_val), re.IGNORECASE)
    if m:
        token.attrSet("align", m.group(1).lower())
    # 剥离内联 style，交给 align 属性 + CSS 类统一控制
    if token.attrs:
        if isinstance(token.attrs, dict):
            token.attrs.pop("style", None)
        else:
            token.attrs = [(k, v) for k, v in token.attrs if k != "style"]


def _render_th_open(tokens, index, options, env) -> str:
    _convert_cell_align_attr(tokens[index])
    return "<th" + RendererHTML.renderAttrs(tokens[index]) + ">"


def _render_td_open(tokens, index, options, env) -> str:
    _convert_cell_align_attr(tokens[index])
    return "<td" + RendererHTML.renderAttrs(tokens[index]) + ">"


def _apply_task_list_boxes(html_str: str) -> str:
    """将标准 GFM/Typora 任务列表项 [ ] / [x] 转为无交互安全展示框。"""
    if "[" not in html_str:
        return html_str

    def _replace_li_task(m: re.Match) -> str:
        lead = m.group(1)
        mark = m.group(2).lower()
        if mark == "x":
            return f'{lead}<span class="md-task-box is-checked" aria-hidden="true">☑</span>'
        return f'{lead}<span class="md-task-box" aria-hidden="true">☐</span>'

    return re.sub(
        r"(<li>\s*(?:<p>)?)\[([ xX])\]\s+",
        _replace_li_task,
        html_str,
    )


def _build_md_renderer(*, allow_raw_html: bool) -> MarkdownIt:
    md = MarkdownIt(
        "commonmark",
        {
            "html": bool(allow_raw_html),
            "linkify": False,
            "typographer": False,
            "breaks": True,
        },
    ).enable(["table", "strikethrough"])
    # 允许解析阶段识别 [text](scheme:...) 链接并交由后置 nh3.clean() 按协议白名单剥离 href，
    # 避免危险协议原样残留在正文中（且绝对安全无 href 可点击）。
    md.validateLink = lambda _url: True
    md.renderer.rules["text"] = _render_cjk_friendly_text
    md.renderer.rules["th_open"] = _render_th_open
    md.renderer.rules["td_open"] = _render_td_open
    return md


_MD_WITH_HTML = _build_md_renderer(allow_raw_html=True)
_MD_NO_HTML = _build_md_renderer(allow_raw_html=False)


def render_markdown_to_html(md_text: str, *, allow_raw_html: bool = True) -> str:
    """使用统一 CommonMark + GFM 渲染器转换 Markdown 文本（调用方负责后续 nh3 净化）。"""
    if not md_text:
        return ""
    nonce = uuid.uuid4().hex[:10]
    protected_md, align_blocks = _protect_align_containers(md_text, nonce)
    renderer = _MD_WITH_HTML if allow_raw_html else _MD_NO_HTML
    rendered = renderer.render(protected_md)
    rendered = _restore_align_containers(rendered, align_blocks)
    return _apply_task_list_boxes(rendered)
