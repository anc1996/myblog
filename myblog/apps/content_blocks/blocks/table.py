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
from django.utils.safestring import mark_safe
from django.core.exceptions import ValidationError
from wagtail import blocks
from wagtail.blocks import StructBlockValidationError

from content_blocks.contracts import (
    BlockContract,
    BlockValidationResult,
    BlockRenderResult,
    BlockTextExtraction,
    MediaReference,
)
from content_blocks.rendering.media_refs import extract_media_references
from content_blocks.rendering.table_content import analyze_table_grid, render_table_cell_content
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

    def __init__(self, local_blocks=None, **kwargs):
        super().__init__(local_blocks=local_blocks, **kwargs)
        from content_blocks.widgets import TableJsonSplitWidget
        if "table_data" in self.child_blocks:
            self.child_blocks["table_data"].field_options["widget"] = TableJsonSplitWidget(
                attrs={"rows": 10}
            )

    def clean(self, value: Any) -> Any:
        """
        Wagtail 8.1 结构化块校验契约：
        当表格数据损坏或非法时，抛出 StructBlockValidationError 并将错误精准标红到 table_data 字段。
        """
        result = super().clean(value)
        val_res = self.validate_value(result)
        if not val_res.is_valid:
            err_msg = "; ".join(val_res.errors) if val_res.errors else "表格数据格式无效"
            raise StructBlockValidationError(
                block_errors={"table_data": ValidationError(err_msg)}
            )
        return result

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value:
            return BlockValidationResult(is_valid=True)
        raw_data = value.get("table_data")
        if raw_data:
            parsed = None
            if isinstance(raw_data, str):
                try:
                    parsed = json.loads(raw_data)
                except Exception as exc:
                    return BlockValidationResult(is_valid=False, errors=[f"表格数据 JSON 解析失败: {exc}"])
            elif isinstance(raw_data, (dict, list)):
                parsed = raw_data
            else:
                return BlockValidationResult(is_valid=False, errors=["表格数据必须为 JSON 字符串、对象或数组"])

            if not isinstance(parsed, (dict, list)):
                return BlockValidationResult(is_valid=False, errors=["表格数据必须为 JSON 对象或数组"])

            rows = parsed.get("rows") if isinstance(parsed, dict) else parsed
            if not isinstance(rows, list):
                return BlockValidationResult(is_valid=False, errors=["表格 rows 属性必须为数组列表"])

            has_header_row = bool(value.get("has_header_row", True))
            _, _, _, errors = analyze_table_grid(rows, has_header_row=has_header_row)
            if errors:
                return BlockValidationResult(is_valid=False, errors=errors)

        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)

        caption = value.get("caption") or ""
        has_header_row = value.get("has_header_row", True)
        has_header_col = value.get("has_header_col", False)
        alignment = value.get("alignment") if value.get("alignment") in ("left", "center", "right") else "center"
        payload = parse_table_payload(value.get("table_data"))
        rows = payload.get("rows") or []

        if not rows or not isinstance(rows, list):
            return BlockRenderResult(html="", is_safe=True)

        placed_rows, header_end_idx, use_thead, _ = analyze_table_grid(
            rows, has_header_row=bool(has_header_row)
        )

        # 渲染 HTML 表格结构（避免单元格跨 <thead>/<tbody> 截断错位）
        table_parts: list[str] = []
        table_parts.append('<div class="responsive-table-wrapper">')
        table_parts.append(f'<table class="custom-table-block table-align-{alignment}">')

        if caption:
            table_parts.append(f'<caption class="table-caption">{html.escape(caption)}</caption>')

        if use_thead and header_end_idx >= 0:
            table_parts.append("<thead>")
            for r_idx in range(0, header_end_idx + 1):
                table_parts.append("<tr>")
                for item in placed_rows[r_idx]:
                    table_parts.append(self._render_placed_cell(item, default_tag="th"))
                table_parts.append("</tr>")
            table_parts.append("</thead>")
            body_range = range(header_end_idx + 1, len(placed_rows))
        else:
            body_range = range(0, len(placed_rows))

        if body_range:
            table_parts.append("<tbody>")
            for r_idx in body_range:
                table_parts.append("<tr>")
                for item in placed_rows[r_idx]:
                    # 若无独立 thead 且为首行表头，或开启首列表头且处于逻辑第 0 列，使用 th
                    is_row_header = (not use_thead) and has_header_row and (r_idx <= header_end_idx)
                    is_col_header = has_header_col and (item["col_idx"] == 0)
                    tag = "th" if (is_row_header or is_col_header) else "td"
                    table_parts.append(self._render_placed_cell(item, default_tag=tag))
                table_parts.append("</tr>")
            table_parts.append("</tbody>")

        table_parts.append("</table></div>")
        table_html = "".join(table_parts)

        # 为源码对照面板构造安全的 JSON 展示副本，剔除单元格中的恶意脚本或危险 URL 协议
        safe_rows_for_json = []
        for row in rows:
            if isinstance(row, list):
                safe_row = []
                for cell in row:
                    if isinstance(cell, dict):
                        c_copy = dict(cell)
                        if "value" in c_copy:
                            c_copy["value"] = sanitize_html(str(c_copy["value"]))
                        safe_row.append(c_copy)
                    else:
                        safe_row.append(cell)
                safe_rows_for_json.append(safe_row)
            else:
                safe_rows_for_json.append(row)
        safe_payload = dict(payload, rows=safe_rows_for_json) if isinstance(payload, dict) else {"rows": safe_rows_for_json}

        pretty_json = json.dumps(safe_payload, ensure_ascii=False, indent=2)
        safe_json = html.escape(pretty_json)
        safe_toolbar_title = html.escape(caption) if caption else "复杂结构化表格"

        wrapped_html = f"""
<div class="cb-table-block" data-cb-table>
    <div class="cb-table-toolbar">
        <span class="cb-table-toolbar-title">{safe_toolbar_title}</span>
        <div class="cb-table-actions">
            <button type="button" class="cb-btn cb-table-split-btn" aria-pressed="false" title="切换左侧 JSON 源码与右侧表格分栏对照">⇄ 分栏对照</button>
            <button type="button" class="cb-btn cb-table-copy-btn" title="复制表格 JSON 源码">📋 复制 JSON</button>
        </div>
    </div>
    <div class="cb-table-split-body">
        <div class="cb-table-source-pane" hidden>
            <pre class="cb-table-source-pre"><code class="language-json" data-cb-table-source>{safe_json}</code></pre>
        </div>
        <div class="cb-table-render-pane">
            {table_html}
        </div>
    </div>
</div>
""".strip()
        return BlockRenderResult(html=wrapped_html, is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        """Wagtail 模板原生渲染集成入口"""
        res = self.render_clean(value, context)
        return mark_safe(res.html)

    def _render_placed_cell(self, placed: dict[str, Any], default_tag: str = "td") -> str:
        """渲染经逻辑网格定位的单元格"""
        cell = placed["cell"]
        rowspan = placed["rowspan"]
        colspan = placed["colspan"]

        if isinstance(cell, dict):
            raw_val = str(cell.get("value", ""))
            align = cell.get("align", "")
            is_th = cell.get("header", default_tag == "th")
            tag = "th" if is_th else "td"
            is_rich = True
        else:
            raw_val = str(cell)
            align = ""
            tag = default_tag
            is_rich = False

        attrs: list[str] = []
        if rowspan > 1:
            attrs.append(f'rowspan="{rowspan}"')
        if colspan > 1:
            attrs.append(f'colspan="{colspan}"')
        if align in ("left", "center", "right"):
            attrs.append(f'align="{align}"')
        if tag == "th" and (colspan > 1 or rowspan > 1):
            scope = "colgroup" if colspan > 1 else "rowgroup"
            attrs.append(f'scope="{scope}"')

        attr_str = f" {' '.join(attrs)}" if attrs else ""
        rendered_content = render_table_cell_content(raw_val, is_rich=is_rich)
        return f"<{tag}{attr_str}>{rendered_content}</{tag}>"

    def _render_cell(self, cell: Any, default_tag: str = "td") -> str:
        """渲染单个单元格属性与内容"""
        rowspan = max(1, int(cell.get("rowspan", 1))) if isinstance(cell, dict) else 1
        colspan = max(1, int(cell.get("colspan", 1))) if isinstance(cell, dict) else 1
        return self._render_placed_cell(
            {"cell": cell, "rowspan": rowspan, "colspan": colspan, "row_idx": 0, "col_idx": 0},
            default_tag=default_tag,
        )

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
                cell_refs = extract_media_references(cell_val, path=f"{path}.r{r_idx}c{c_idx}")
                refs.extend(cell_refs)
        return refs
