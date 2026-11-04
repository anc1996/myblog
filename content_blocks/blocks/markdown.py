"""
自定义 Markdown 块 (CustomMarkdownBlock)。

支持表格、数学公式 (LaTeX)、代码围栏、流程图围栏 (fenced mermaid)；
转换后经 nh3 白名单严格净化，杜绝 XSS 注入；
提供文本提取与媒体资源引用提取。
"""

import html
import re
from typing import Any
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


class CustomMarkdownBlock(blocks.TextBlock):
    type_name = "markdown"
    schema_version = 1

    def __init__(self, **kwargs):
        kwargs.setdefault("help_text", "支持标准 Markdown、表格、LaTeX 数学公式与 Mermaid 图表语法")
        super().__init__(**kwargs)

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)

        raw_md = str(value)

        # 1. 预处理：将 ```mermaid ... ``` 保护性替换为特有标记，避免被普通代码围栏吞并
        mermaid_blocks: list[str] = []
        def replace_mermaid(match):
            idx = len(mermaid_blocks)
            code = match.group(1).strip()
            mermaid_blocks.append(code)
            return f"<!--MERMAID_PLACEHOLDER_{idx}-->"

        processed_md = re.sub(r"```mermaid\s*\n(.*?)\n```", replace_mermaid, raw_md, flags=re.DOTALL)

        # 2. 转换 Markdown 为 HTML
        rendered_html = markdown.markdown(
            processed_md,
            extensions=[
                "tables",
                "fenced_code",
                "nl2br",
                "sane_lists",
            ],
        )

        # 3. 还原 Mermaid 块为安全容器
        for idx, code in enumerate(mermaid_blocks):
            safe_code = html.escape(code)
            container = f'<div class="mermaid-diagram" data-mermaid="{safe_code}">{safe_code}</div>'
            rendered_html = rendered_html.replace(f"<!--MERMAID_PLACEHOLDER_{idx}-->", container)

        # 4. 执行白名单安全净化
        cleaned_html = sanitize_html(rendered_html)
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
        raw = str(value)
        refs: list[MediaReference] = []
        # 匹配 Markdown 图片引用中的资源标识：![alt](asset://image/<uuid>) 或 asset-id=<uuid>
        for match in re.finditer(r'(?:asset://image/|asset-id="?)([0-9a-fA-F-]{36})', raw):
            refs.append(MediaReference(asset_id=match.group(1), asset_type="image", block_path=path))
        # 匹配文档引用：[doc](asset://document/<uuid>)
        for match in re.finditer(r'asset://document/([0-9a-fA-F-]{36})', raw):
            refs.append(MediaReference(asset_id=match.group(1), asset_type="document", block_path=path))
        return refs
