"""
复杂表格 (CustomTableBlock) 逻辑网格引擎与单元格全能排版渲染器。

解决核心问题：
1. 逻辑占位网格校验：精确处理 rowspan/colspan 跨行跨列占用、拦截重叠/越界/非正跨度，
   并按逻辑列判定 has_header_col，避免阵列下标错位；
2. 解决 <thead> / <tbody> 跨行截断：自动递归表头跨度边界，绝不允许单元格跨行组；
3. 单元格全能排版：先保护围栏代码块与行内反引号 `code`，再解析 LaTeX 公式与 asset:// 媒体，
   最后以分层净化安全回填，确保 Highlight.js 代码高亮、KaTeX 公式与表格几何 100% 共存。
"""

from __future__ import annotations

import html
import re
import textwrap
import uuid
from typing import Any

from content_blocks.rendering.math import protect_math_in_markdown, restore_math_in_html
from content_blocks.rendering.media_refs import UUID_PATTERN, resolve_media_urls
from content_blocks.sanitizer import sanitize_html

_FENCE_CODE_RE = re.compile(r"```([a-zA-Z0-9_+#.-]*)[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*```")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_SAFE_LANG_RE = re.compile(r"^[a-zA-Z0-9_+#.-]{1,32}$")
_MD_IMAGE_ASSET_RE = re.compile(r"!\[([^\]]*)\]\(asset://image/(" + UUID_PATTERN + r")\)")
_MD_DOC_ASSET_RE = re.compile(r"\[([^\]]+)\]\(asset://document/(" + UUID_PATTERN + r")\)")


def analyze_table_grid(
    rows: list[list[Any]],
    *,
    has_header_row: bool = True,
) -> tuple[list[list[dict[str, Any]]], int, bool, list[str]]:
    """
    铺设二维逻辑占位网格，返回：
      (placed_rows, header_end_idx, use_thead, errors)
    """
    if not rows:
        return [], -1, False, []

    occupied: set[tuple[int, int]] = set()
    placed_rows: list[list[dict[str, Any]]] = []
    errors: list[str] = []
    max_col_count = 0

    for r_idx, row in enumerate(rows):
        if not isinstance(row, list):
            errors.append(f"表格第 {r_idx + 1} 行数据必须为单元格列表")
            placed_rows.append([])
            continue

        placed_row: list[dict[str, Any]] = []
        c_cursor = 0
        for raw_idx, cell in enumerate(row):
            while (r_idx, c_cursor) in occupied:
                c_cursor += 1

            if isinstance(cell, dict):
                raw_r = cell.get("rowspan", 1)
                raw_c = cell.get("colspan", 1)
                try:
                    rowspan = int(1 if raw_r is None else raw_r)
                except (TypeError, ValueError):
                    errors.append(f"第 {r_idx + 1} 行第 {raw_idx + 1} 格 rowspan 必须为有效整数")
                    rowspan = 1
                try:
                    colspan = int(1 if raw_c is None else raw_c)
                except (TypeError, ValueError):
                    errors.append(f"第 {r_idx + 1} 行第 {raw_idx + 1} 格 colspan 必须为有效整数")
                    colspan = 1

                if rowspan < 1:
                    errors.append(f"第 {r_idx + 1} 行第 {raw_idx + 1} 格 rowspan 必须大于等于 1")
                    rowspan = 1
                if colspan < 1:
                    errors.append(f"第 {r_idx + 1} 行第 {raw_idx + 1} 格 colspan 必须大于等于 1")
                    colspan = 1
            else:
                rowspan = 1
                colspan = 1

            # 检查跨度是否与已有跨行单元格冲突
            for dr in range(rowspan):
                for dc in range(colspan):
                    pos = (r_idx + dr, c_cursor + dc)
                    if pos in occupied:
                        errors.append(
                            f"第 {r_idx + 1} 行第 {c_cursor + dc + 1} 逻辑列与上方合并单元格重叠"
                        )
                    else:
                        occupied.add(pos)

            placed_row.append(
                {
                    "cell": cell,
                    "row_idx": r_idx,
                    "col_idx": c_cursor,
                    "rowspan": rowspan,
                    "colspan": colspan,
                }
            )
            c_cursor += colspan
            if c_cursor > max_col_count:
                max_col_count = c_cursor

        placed_rows.append(placed_row)

    # 检查纵向跨度是否超出总行数
    total_rows = len(rows)
    for r_idx, prow in enumerate(placed_rows):
        for item in prow:
            if r_idx + item["rowspan"] > total_rows:
                errors.append(
                    f"第 {r_idx + 1} 行第 {item['col_idx'] + 1} 列 rowspan={item['rowspan']} 超出表格总行数 {total_rows}"
                )
                item["rowspan"] = max(1, total_rows - r_idx)

    # 检查各行逻辑总列宽是否一致，拦截因 rowspan/colspan 导致的越界溢出或残缺行
    if total_rows > 1 and not errors:
        row_widths: list[int] = []
        for r_idx in range(total_rows):
            max_c_in_row = 0
            for rr, cc in occupied:
                if rr == r_idx and (cc + 1) > max_c_in_row:
                    max_c_in_row = cc + 1
            row_widths.append(max_c_in_row)
        expected_width = row_widths[0]
        for r_idx, rw in enumerate(row_widths[1:], start=1):
            if rw != expected_width:
                errors.append(
                    f"第 {r_idx + 1} 行逻辑列宽 ({rw}) 与首行列宽 ({expected_width}) 不一致，请检查合并跨度是否越界"
                )

    # 递归计算无跨行组边界冲突的 <thead> 截止行
    if not has_header_row or total_rows == 0:
        return placed_rows, -1, False, errors

    header_end_idx = 0
    changed = True
    while changed:
        changed = False
        for r_idx in range(0, header_end_idx + 1):
            for item in placed_rows[r_idx]:
                reach = r_idx + item["rowspan"] - 1
                if reach > header_end_idx:
                    header_end_idx = min(reach, total_rows - 1)
                    changed = True

    # 若表头直接跨到了最后一行（不存在独立表体行），统一使用 <tbody> 避免空表体或截断
    use_thead = header_end_idx < total_rows - 1
    return placed_rows, header_end_idx, use_thead, errors


def render_table_cell_content(raw_val: str, *, is_rich: bool) -> str:
    """
    渲染单个表格单元格的内容：
    支持：围栏代码块 (Highlight.js)、行内反引号 `code`、LaTeX 公式、受控媒体网关 URL 与基础 HTML。
    执行严格分层净化：代码/公式先占位 -> 剩余部分净化 -> 安全回填。
    """
    if raw_val == "":
        return ""

    nonce = uuid.uuid4().hex[:10]
    code_items: list[dict[str, str]] = []

    # 1. 先提取围栏代码块（```lang ... ```），防止代码内的 $ 或 asset:// 被误解析
    def stash_fence(m: re.Match) -> str:
        lang_raw = (m.group(1) or "").strip().lower()
        lang = lang_raw if _SAFE_LANG_RE.match(lang_raw) else "plaintext"
        code_body = textwrap.dedent(m.group(2).replace("\r\n", "\n")).strip("\n")
        idx = len(code_items)
        token = f"CBTBLCODE_{nonce}_{idx}_X"
        code_items.append({"type": "fence", "lang": lang, "code": code_body, "token": token})
        return token

    text = _FENCE_CODE_RE.sub(stash_fence, raw_val)

    # 2. 提取行内反引号 `code`
    def stash_inline_code(m: re.Match) -> str:
        code_body = m.group(1)
        idx = len(code_items)
        token = f"CBTBLCODE_{nonce}_{idx}_X"
        code_items.append({"type": "inline", "lang": "", "code": code_body, "token": token})
        return token

    text = _INLINE_CODE_RE.sub(stash_inline_code, text)

    # 3. 提取 LaTeX 公式（此时代码块已占位保护，不会误抓代码里的 $）
    text, math_items = protect_math_in_markdown(text, nonce)

    # 4. 转换受控媒体引用并执行分层净化
    # 注意：标量单元格不解释作者输入的原始 HTML；富单元格先做 asset:// 转换再过 nh3
    media_tokens: list[tuple[str, str]] = []
    if is_rich:
        resolved = resolve_media_urls(text)
        cleaned = sanitize_html(resolved)
    else:
        # 标量支持受控 ![alt](asset://image/<uuid>) / [text](asset://document/<uuid>) 或纯文本
        def stash_md_img(m: re.Match) -> str:
            idx = len(media_tokens)
            tok = f"CBMEDIA_{nonce}_{idx}_X"
            alt_safe = html.escape(m.group(1) or "")
            aid = m.group(2).lower()
            media_tokens.append((tok, f'<img src="/media-gateway/image/{aid}/" alt="{alt_safe}" loading="lazy" data-asset-id="{aid}" />'))
            return tok

        def stash_md_doc(m: re.Match) -> str:
            idx = len(media_tokens)
            tok = f"CBMEDIA_{nonce}_{idx}_X"
            lbl_safe = html.escape(m.group(1))
            aid = m.group(2).lower()
            media_tokens.append((tok, f'<a href="/media-gateway/document/{aid}/" download>{lbl_safe}</a>'))
            return tok

        text = _MD_IMAGE_ASSET_RE.sub(stash_md_img, text)
        text = _MD_DOC_ASSET_RE.sub(stash_md_doc, text)
        cleaned = html.escape(text)
        for tok, rep in media_tokens:
            cleaned = cleaned.replace(tok, rep)

    # 5. 回填 LaTeX 公式为合法行内/单元格展示 span（绝不插 <div> 破坏表格）
    if math_items:
        for item in math_items:
            token = item["token"]
            safe_tex = html.escape(item["tex"])
            if item["is_display"]:
                rep = (
                    f'<span class="cb-math cb-math-display" data-cb-math="display" '
                    f'data-tex="{safe_tex}">$${safe_tex}$$</span>'
                )
            else:
                rep = (
                    f'<span class="cb-math cb-math-inline" data-cb-math="inline" '
                    f'data-tex="{safe_tex}">${safe_tex}$</span>'
                )
            cleaned = cleaned.replace(token, rep)

    # 6. 回填代码块与行内反引号（全部经 html.escape 转义）
    for item in code_items:
        safe_code = html.escape(item["code"])
        if item["type"] == "fence":
            safe_lang = html.escape(item["lang"])
            rep = f'<div class="cb-cell-code"><pre><code class="language-{safe_lang}">{safe_code}</code></pre></div>'
        else:
            rep = f'<code class="cb-inline-code">{safe_code}</code>'
        cleaned = cleaned.replace(item["token"], rep)

    return cleaned
