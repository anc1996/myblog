"""
自定义 Markdown 块 (CustomMarkdownBlock)。

支持表格、数学公式 (LaTeX)、代码围栏、流程图围栏 (fenced mermaid)；
转换后经 nh3 白名单严格净化，杜绝 XSS 注入；
提供文本提取与媒体资源引用提取。
"""

import re
import uuid
from typing import Any
from django import forms
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe
import markdown
from wagtail import blocks

from content_blocks.contracts import (
    BlockContract,
    BlockValidationResult,
    BlockRenderResult,
    BlockTextExtraction,
    MediaReference,
)
from content_blocks.sanitizer import sanitize_html
from content_blocks.rendering.images import apply_markdown_image_sizing_in_html
from content_blocks.rendering.math import protect_math_in_markdown, restore_math_in_html
from content_blocks.rendering.mermaid import render_mermaid_dom
from content_blocks.rendering.media_refs import resolve_media_urls, extract_media_references
from content_blocks.rendering.table_content import enhance_markdown_tables_html


def _escape_pipes_in_md_images(md_text: str, nonce: str) -> str:
    """
    仅在 Markdown 管道表格行内的 ![alt|尺寸](url) 中保护未转义的 `|`，避免 Python-Markdown tables 扩展误拆单元格；
    同时保留代码块、HTML 表格与行内反引号中的原文不被修改。
    """
    if "|" not in md_text:
        return md_text
    stashed: list[str] = []

    def _stash(m: re.Match) -> str:
        idx = len(stashed)
        stashed.append(m.group(0))
        return f"CBIMGCODE_{nonce}_{idx}_X"

    tmp = re.sub(r"```[\s\S]*?```", _stash, md_text)
    tmp = re.sub(r"`[^`\n]+`", _stash, tmp)

    lines = tmp.splitlines(keepends=True)
    out_lines: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        # 只处理 Markdown 管道表格行（不碰 HTML <td> 内的原文）
        if "|" in stripped and not stripped.startswith("<"):
            line = re.sub(
                r"!\[([^\]]+)\]\(([^\s\)]+)\)",
                lambda m: "![" + re.sub(r"(?<!\\)\|", r"\\|", m.group(1)) + "](" + m.group(2) + ")",
                line,
            )
        out_lines.append(line)
    tmp = "".join(out_lines)
    for idx, raw in enumerate(stashed):
        tmp = tmp.replace(f"CBIMGCODE_{nonce}_{idx}_X", raw)
    return tmp


class CustomMarkdownBlock(blocks.TextBlock):
    type_name = "markdown"
    schema_version = 1

    def __init__(self, **kwargs):
        kwargs.setdefault("help_text", "支持标准 Markdown、表格、LaTeX 数学公式与 Mermaid 图表语法")
        super().__init__(**kwargs)

    @cached_property
    def field(self):
        from content_blocks.widgets import VditorMarkdownWidget
        field_kwargs = {
            "widget": VditorMarkdownWidget(attrs={"rows": self.rows}),
            "strip": False,
        }
        field_kwargs.update(self.field_options)
        return forms.CharField(**field_kwargs)

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)

        raw_md = str(value)
        nonce = uuid.uuid4().hex[:12]

        # 1. 预处理：将 ```mermaid ... ``` 保护性替换为独立安全令牌，避免被普通代码围栏吞并
        mermaid_blocks: list[str] = []
        def replace_mermaid(match):
            idx = len(mermaid_blocks)
            code = match.group(1).strip()
            mermaid_blocks.append(code)
            return f"\n\nCBMERMAID_{nonce}_{idx}_X\n\n"

        processed_md = re.sub(
            r"(?m)^[ \t]{0,3}```[ \t]*mermaid[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*```[ \t]*\r?$",
            replace_mermaid,
            raw_md,
        )

        # 2. 保护数学公式：在 tables 与 nl2br 前提取公式，避免公式内的 _ 或 \\ 被 Markdown 损坏
        processed_md, math_items = protect_math_in_markdown(processed_md, nonce)
        processed_md = _escape_pipes_in_md_images(processed_md, nonce)

        # 3. 转换 Markdown 为 HTML
        rendered_html = markdown.markdown(
            processed_md,
            extensions=[
                "tables",
                "fenced_code",
                "nl2br",
                "sane_lists",
            ],
            extension_configs={
                "tables": {"use_align_attribute": True},
            },
        )

        # 4. 转换内部受控媒体网关链接 (asset://image/<uuid> 等)
        rendered_html = resolve_media_urls(rendered_html)

        # 5. 分层安全净化：先对作者可控 HTML 执行白名单净化，彻底拦截作者伪造组件标记或 XSS
        cleaned_html = sanitize_html(rendered_html)

        # 5b. 增强 Markdown 表格（含 HTML 合并单元格表格）的代码/图片渲染与自适应滚动容器，并应用图片尺寸语法
        cleaned_html = enhance_markdown_tables_html(cleaned_html)
        cleaned_html = apply_markdown_image_sizing_in_html(cleaned_html)

        # 6. 安全回填 Mermaid 图表容器（源码经严格转义）
        for idx, code in enumerate(mermaid_blocks):
            token = f"CBMERMAID_{nonce}_{idx}_X"
            container = render_mermaid_dom(code, title="", theme="follow-site")
            # 若被 Markdown 段落包裹成 <p>...</p>，整段替换为 <div> 容器避免非法嵌套
            p_pattern = re.compile(r"<p>\s*" + re.escape(token) + r"\s*</p>")
            if p_pattern.search(cleaned_html):
                cleaned_html = p_pattern.sub(lambda _: container, cleaned_html)
            else:
                cleaned_html = cleaned_html.replace(token, container)

        # 7. 安全回填 LaTeX 数学公式容器
        cleaned_html = restore_math_in_html(cleaned_html, math_items)

        return BlockRenderResult(html=cleaned_html, is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        """Wagtail 模板原生渲染集成入口"""
        res = self.render_clean(value, context)
        return mark_safe(res.html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        raw = str(value)
        # 剔除代码块与 Markdown 标记
        text_only = re.sub(r"```.*?```", " ", raw, flags=re.DOTALL)
        text_only = re.sub(r"[#*_~`>\[\]]", " ", text_only)
        text_only = re.sub(r"\s+", " ", text_only).strip()
        segments = [s.strip() for s in text_only.splitlines() if s.strip()]
        return BlockTextExtraction(plain_text=text_only, logical_segments=segments)

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        if not value:
            return []
        return extract_media_references(str(value), path=path)
