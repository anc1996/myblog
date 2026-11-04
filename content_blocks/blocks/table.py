"""
复杂表格内容块 (CustomTableBlock)。

功能特性：
1. 结构化单元格模型：支持 rowspan（行跨度）、colspan（列跨度）单元格合并与拆分；
2. 富内容单元格：支持加粗、链接、LaTeX 数学排版与公共图片引用；
3. 表头与排版控制：支持表头行 (thead/th)、首列表头与表格 caption 标题，支持左/中/右对齐；
4. 移动端自适应：外层包裹 overflow-x: auto 响应式滚动容器，绝不撑破手机屏幕；
5. 安全防线：单元格内容严格经 nh3 白名单净化，安全保留 rowspan/colspan，彻底剥离 XSS 脚本；
6. 全文与媒体提取：提取全部单元格纯文本供搜索倒排索引，提取单元格内的媒体 ID 供 MEDIA-REF-01 保护。
"""

import html
import json
import re
from typing import Any
from wagtail import blocks

from content_blocks.contracts import (
    BlockContract,
    BlockValidationResult,
    BlockRenderResult,
    BlockTextExtraction,
    MediaReference,
)
from content_blocks.sanitizer import sanitize_html


ALIGN_CHOICES = [
    ("left", "靠左对齐"),
    ("center", "居中对齐"),
    ("right", "靠右对齐"),
]


def parse_table_payload(raw: Any) -> dict[str, Any]:
    """解析表格数据为结构化字典，支持 dict 或 JSON 字符串"""
    if not raw:
        return {"rows": []}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw.strip())
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"rows": parsed}
        except Exception:
            return {"rows": []}
    return {"rows": []}


class CustomTableBlock(blocks.StructBlock):
    type_name = "table"
    schema_version = 1

    caption = blocks.CharBlock(required=False, max_length=255, label="表格标题 / 说明")
    has_header_row = blocks.BooleanBlock(required=False, default=True, label="首行为表头 (thead)")
    has_header_col = blocks.BooleanBlock(required=False, default=False, label="首列为表头")
    alignment = blocks.ChoiceBlock(choices=ALIGN_CHOICES, default="center", label="表格对齐")
    table_data = blocks.TextBlock(
        required=False,
        label="表格结构化数据 (JSON)",
        help_text="包含 rows 数组，每个单元格支持 value, rowspan, colspan, align, header",
        default='{"rows": []}',
    )

    class Meta:
        icon = "table"
        label = "复杂表格"

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value:
            return BlockValidationResult(is_valid=True)
        raw_data = value.get("table_data")
        if raw_data:
            if isinstance(raw_data, str):
                try:
                    parsed = json.loads(raw_data)
                    if not isinstance(parsed, (dict, list)):
                        return BlockValidationResult(is_valid=False, errors=["表格数据必须为 JSON 对象或数组"])
                except Exception as exc:
                    return BlockValidationResult(is_valid=False, errors=[f"表格数据 JSON 解析失败: {exc}"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)

        caption = value.get("caption") or ""
        has_header_row = value.get("has_header_row", True)
        has_header_col = value.get("has_header_col", False)
        alignment = value.get("alignment") or "center"
        payload = parse_table_payload(value.get("table_data"))
        rows = payload.get("rows") or []

        if not rows:
            return BlockRenderResult(html="", is_safe=True)

        # 渲染 HTML 结构
        table_parts: list[str] = []
        table_parts.append(
            f'<div class="responsive-table-wrapper">'
        )
        table_parts.append(f'<table class="custom-table-block table-align-{alignment}">')

        if caption:
            table_parts.append(f'<caption class="table-caption">{html.escape(caption)}</caption>')

        # 处理表头与表体
        body_rows = rows
        if has_header_row and len(rows) > 0:
            header_row = rows[0]
            body_rows = rows[1:]
            table_parts.append("<thead><tr>")
            for cell in header_row:
                table_parts.append(self._render_cell(cell, default_tag="th"))
            table_parts.append("</tr></thead>")

        if body_rows:
            table_parts.append("<tbody>")
            for row_idx, row in enumerate(body_rows):
                table_parts.append("<tr>")
                for col_idx, cell in enumerate(row):
                    # 若开启首列表头，第一列使用 th
                    tag = "th" if (has_header_col and col_idx == 0) else "td"
                    table_parts.append(self._render_cell(cell, default_tag=tag))
                table_parts.append("</tr>")
            table_parts.append("</tbody>")

        table_parts.append("</table></div>")
        raw_html = "".join(table_parts)
        # 经 nh3 安全净化，保留合法 colspan/rowspan 与单元格标签，剥离恶意 XSS
        cleaned_html = sanitize_html(raw_html)
        return BlockRenderResult(html=cleaned_html, is_safe=True)

    def _render_cell(self, cell: Any, default_tag: str = "td") -> str:
        """渲染单个单元格属性与内容"""
        if isinstance(cell, dict):
            val = cell.get("value", "")
            rowspan = int(cell.get("rowspan", 1))
            colspan = int(cell.get("colspan", 1))
            align = cell.get("align", "")
            is_th = cell.get("header", default_tag == "th")
            tag = "th" if is_th else "td"

            attrs: list[str] = []
            if rowspan > 1:
                attrs.append(f'rowspan="{rowspan}"')
            if colspan > 1:
                attrs.append(f'colspan="{colspan}"')
            if align in ("left", "center", "right"):
                attrs.append(f'align="{align}"')

            attr_str = f" {' '.join(attrs)}" if attrs else ""
            # 单元格内容允许基础排版标签，经外层统一净化
            return f"<{tag}{attr_str}>{val}</{tag}>"
        else:
            # 普通标量值单元格
            safe_val = html.escape(str(cell))
            return f"<{default_tag}>{safe_val}</{default_tag}>"

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])

        caption = value.get("caption") or ""
        payload = parse_table_payload(value.get("table_data"))
        rows = payload.get("rows") or []

        segments: list[str] = []
        if caption:
            segments.append(caption)

        for row in rows:
            row_texts: list[str] = []
            for cell in row:
                if isinstance(cell, dict):
                    val = str(cell.get("value", ""))
                else:
                    val = str(cell)
                # 剥离可能包含的 HTML 标签
                clean_val = re.sub(r"<[^>]+>", " ", val)
                clean_val = re.sub(r"\s+", " ", clean_val).strip()
                if clean_val:
                    row_texts.append(clean_val)
            if row_texts:
                segments.append(" | ".join(row_texts))

        full_text = "\n".join(segments)
        return BlockTextExtraction(plain_text=full_text, logical_segments=segments)

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        if not value:
            return []
        payload = parse_table_payload(value.get("table_data"))
        rows = payload.get("rows") or []
        refs: list[MediaReference] = []

        for r_idx, row in enumerate(rows):
            for c_idx, cell in enumerate(row):
                cell_val = str(cell.get("value", "") if isinstance(cell, dict) else cell)
                # 扫描单元格中嵌入的图片或文档资源 ID
                for match in re.finditer(r'(?:asset://image/|data-asset-id="?)([0-9a-fA-F-]{36})', cell_val):
                    refs.append(
                        MediaReference(
                            asset_id=match.group(1),
                            asset_type="image",
                            block_path=f"{path}.r{r_idx}c{c_idx}",
                        )
                    )
                for match in re.finditer(r'asset://document/([0-9a-fA-F-]{36})', cell_val):
                    refs.append(
                        MediaReference(
                            asset_id=match.group(1),
                            asset_type="document",
                            block_path=f"{path}.r{r_idx}c{c_idx}",
                        )
                    )
        return refs
