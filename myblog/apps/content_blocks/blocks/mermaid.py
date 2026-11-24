"""
自定义 Mermaid 流程图块 (CustomMermaidBlock)。

支持 Mermaid 图表语法、主题切换、全屏/折叠展示以及 SVG 安全导出。
与 Markdown 围栏中的 mermaid 代码共享安全渲染机制。
"""

from typing import Any
from django.utils.safestring import mark_safe
from wagtail import blocks

from content_blocks.contracts import (
    BlockContract,
    BlockValidationResult,
    BlockRenderResult,
    BlockTextExtraction,
    MediaReference,
)
from content_blocks.rendering.mermaid import render_mermaid_dom, ALLOWED_THEMES


THEME_CHOICES = [
    ("follow-site", "跟随站点昼夜（默认）"),
    ("default", "默认浅色"),
    ("dark", "深色模式"),
    ("forest", "自然绿色"),
    ("neutral", "简约黑白"),
]


class CustomMermaidBlock(blocks.StructBlock):
    type_name = "mermaid"
    schema_version = 1

    title = blocks.CharBlock(required=False, max_length=120, label="图表标题")
    diagram_code = blocks.TextBlock(
        label="Mermaid 语法内容",
        help_text="例如 graph TD; A[开始]-->B[结束];",
        rows=6,
    )
    theme = blocks.ChoiceBlock(choices=THEME_CHOICES, default="follow-site", label="图表主题")

    class Meta:
        icon = "resubmit"
        label = "Mermaid 流程图"

    def __init__(self, local_blocks=None, **kwargs):
        super().__init__(local_blocks=local_blocks, **kwargs)
        from content_blocks.widgets import MermaidSplitWidget
        if "diagram_code" in self.child_blocks:
            self.child_blocks["diagram_code"].field_options["widget"] = MermaidSplitWidget(
                attrs={"rows": 8}
            )

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("diagram_code"):
            return BlockValidationResult(is_valid=False, errors=["图表内容不能为空"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value or not value.get("diagram_code"):
            return BlockRenderResult(html="", is_safe=True)

        title = value.get("title") or ""
        code = value.get("diagram_code") or ""
        theme = value.get("theme") or "default"

        html_output = render_mermaid_dom(code, title=title, theme=theme)
        return BlockRenderResult(html=html_output, is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        """Wagtail 模板原生渲染集成入口"""
        res = self.render_clean(value, context)
        return mark_safe(res.html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        title = value.get("title") or ""
        code = value.get("diagram_code") or ""
        full_text = f"{title}\n{code}".strip()
        return BlockTextExtraction(plain_text=full_text, logical_segments=[title, code] if title else [code])

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        return []
