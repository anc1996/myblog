"""
content_blocks.blocks.structured
全站公共结构化教程与卡片积木 (CustomLeadBlock, CustomStepBlock, CustomTipBlock, CustomFaqBlock, CustomReferenceBlock)。

落实全站复用契约：
1. 沉淀至 content_blocks 基础模块，供 BlogPage、GuidePage 及未来所有业务 Page 统一开箱调用；
2. 严格实现 BlockContract 协议（validate_value, render_clean, render_basic, extract_text, extract_media_refs）；
3. CustomStepBlock 包含可选步骤配图与代码片段：
   - 步骤配图严格通过 extract_media_refs() 向 MEDIA-REF-01 提供稳定资源标识 public_id；
   - 代码片段复用语法高亮与一键复制；
4. 渲染结果通过 nh3 安全净化，保证 XSS 免疫与受控网关交付。
"""

import html
from typing import Any
from django.utils.safestring import mark_safe
from wagtail import blocks
from wagtail.images.blocks import ImageChooserBlock

from content_blocks.contracts import (
    BlockContract,
    BlockValidationResult,
    BlockRenderResult,
    BlockTextExtraction,
    MediaReference,
)
from content_blocks.sanitizer import sanitize_html
from .code import LANGUAGE_CHOICES, highlight_code
from .media import _resolve_asset_id

TIP_CHOICES = [
    ("tip", "💡 实用技巧 (Tip)"),
    ("info", "ℹ️ 补充说明 (Info)"),
    ("warning", "⚠️ 踩坑预警 (Warning)"),
    ("danger", "🛑 高危操作 (Danger)"),
]


class CustomLeadBlock(blocks.StructBlock):
    """全站公共教程导读/目标卡片"""
    type_name = "lead"
    schema_version = 1

    title = blocks.CharBlock(required=True, label="导读标题", default="学习目标与前置要求")
    prerequisites = blocks.RichTextBlock(required=False, label="先决条件与环境准备")
    overview = blocks.RichTextBlock(required=True, label="核心知识点概述")

    class Meta:
        icon = "info-circle"
        label = "教程导读卡片"

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("overview"):
            return BlockValidationResult(is_valid=False, errors=["导言概述内容不能为空"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)

        title = html.escape(str(value.get("title") or "学习目标与前置要求"))
        prereq = str(value.get("prerequisites") or "")
        overview = str(value.get("overview") or "")

        html_output = f"""
        <div class="tutorial-lead-card block-lead">
            <h3 class="lead-title">{title}</h3>
            {f'<div class="lead-prerequisites"><strong>先决条件与环境准备：</strong>{prereq}</div>' if prereq else ''}
            <div class="lead-overview">{overview}</div>
        </div>
        """
        return BlockRenderResult(html=sanitize_html(html_output), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        return mark_safe(self.render_clean(value, context).html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        title = str(value.get("title") or "")
        prereq = str(value.get("prerequisites") or "")
        overview = str(value.get("overview") or "")
        combined = f"{title}\n{prereq}\n{overview}".strip()
        return BlockTextExtraction(plain_text=combined, logical_segments=[title, prereq, overview])

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        return []


class CustomStepBlock(blocks.StructBlock):
    """全站公共操作步骤卡片 (Step)"""
    type_name = "step"
    schema_version = 1

    step_number = blocks.CharBlock(required=True, label="步骤编号", default="1")
    title = blocks.CharBlock(required=True, label="步骤名称")
    content = blocks.RichTextBlock(required=True, label="详细操作说明")
    code_snippet = blocks.TextBlock(required=False, label="示例代码 / 终端命令")
    code_language = blocks.ChoiceBlock(choices=LANGUAGE_CHOICES, required=False, default="bash", label="代码语言")
    image = ImageChooserBlock(required=False, label="步骤配图 / 结果截图")
    image_caption = blocks.CharBlock(required=False, label="图注说明")

    class Meta:
        icon = "tasks"
        label = "操作步骤卡片 (Step)"

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("title") or not value.get("content"):
            return BlockValidationResult(is_valid=False, errors=["步骤标题与操作说明不能为空"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)

        step_num = html.escape(str(value.get("step_number") or "1"))
        title = html.escape(str(value.get("title") or ""))
        content = str(value.get("content") or "")
        code = str(value.get("code_snippet") or "").strip()
        lang = str(value.get("code_language") or "bash")
        caption = html.escape(str(value.get("image_caption") or ""))

        # 处理代码片段高亮
        code_html = ""
        if code:
            highlighted = highlight_code(code, lang)
            code_html = f"""
            <div class="step-code-container" data-language="{lang}">
                <div class="code-header">
                    <span class="code-lang-label">{lang.upper()}</span>
                    <button class="copy-code-btn" type="button" data-clipboard-text="{html.escape(code)}">复制</button>
                </div>
                <div class="code-body">{highlighted}</div>
            </div>
            """

        # 处理步骤配图受控交付网关
        image_html = ""
        img_obj = value.get("image")
        if img_obj:
            pub_id = _resolve_asset_id(img_obj)
            img_url = f"/media-gateway/image/{pub_id}/" if pub_id else (img_obj.file.url if hasattr(img_obj, "file") else "")
            image_html = f"""
            <div class="step-image-container" data-asset-id="{pub_id}">
                <img src="{img_url}" alt="{title}" class="step-img" loading="lazy" />
                {f'<p class="step-caption">{caption}</p>' if caption else ''}
            </div>
            """

        html_output = f"""
        <div class="tutorial-step-card block-step" id="step-{step_num}" data-step="{step_num}">
            <div class="step-header">
                <span class="step-badge">{step_num}</span>
                <h3 class="step-heading" id="heading-step-{step_num}">{title}</h3>
            </div>
            <div class="step-body-content">{content}</div>
            {code_html}
            {image_html}
        </div>
        """
        return BlockRenderResult(html=sanitize_html(html_output), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        return mark_safe(self.render_clean(value, context).html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        title = str(value.get("title") or "")
        content = str(value.get("content") or "")
        code = str(value.get("code_snippet") or "")
        caption = str(value.get("image_caption") or "")
        segments = [title, content]
        if code:
            segments.append(code)
        if caption:
            segments.append(caption)
        return BlockTextExtraction(plain_text="\n".join(segments), logical_segments=segments)

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        """向 MEDIA-REF-01 提供步骤配图的精准引用"""
        refs = []
        if not value:
            return refs
        img_obj = value.get("image")
        if img_obj:
            pub_id = _resolve_asset_id(img_obj)
            if pub_id:
                refs.append(MediaReference(
                    asset_id=pub_id,
                    asset_type="image",
                    block_path=f"{path}.image" if path else "image",
                ))
        return refs


class CustomTipBlock(blocks.StructBlock):
    """全站公共技巧与预警卡片 (Callout)"""
    type_name = "tip"
    schema_version = 1

    tip_type = blocks.ChoiceBlock(choices=TIP_CHOICES, default="tip", label="卡片类型")
    title = blocks.CharBlock(required=False, label="提示标题")
    content = blocks.RichTextBlock(required=True, label="提示内容")

    class Meta:
        icon = "help"
        label = "技巧预警卡片 (Callout)"

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("content"):
            return BlockValidationResult(is_valid=False, errors=["提示内容不能为空"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)

        tip_type = value.get("tip_type") or "tip"
        title = html.escape(str(value.get("title") or ""))
        content = str(value.get("content") or "")

        html_output = f"""
        <div class="callout-card callout-{tip_type} block-tip">
            {f'<h4 class="callout-title">{title}</h4>' if title else ''}
            <div class="callout-content">{content}</div>
        </div>
        """
        return BlockRenderResult(html=sanitize_html(html_output), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        return mark_safe(self.render_clean(value, context).html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        title = str(value.get("title") or "")
        content = str(value.get("content") or "")
        combined = f"{title}\n{content}".strip()
        return BlockTextExtraction(plain_text=combined, logical_segments=[title, content])

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        return []


class CustomFaqBlock(blocks.StructBlock):
    """全站公共 FAQ 问答卡片"""
    type_name = "faq"
    schema_version = 1

    question = blocks.CharBlock(required=True, label="常见疑问 / 报错信息 (Q)")
    answer = blocks.RichTextBlock(required=True, label="排错思路与解决方案 (A)")

    class Meta:
        icon = "comment-dots"
        label = "FAQ 问答卡片"

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("question") or not value.get("answer"):
            return BlockValidationResult(is_valid=False, errors=["问答的提问与解答均不能为空"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)

        q = html.escape(str(value.get("question") or ""))
        a = str(value.get("answer") or "")

        html_output = f"""
        <div class="faq-card block-faq">
            <h4 class="faq-question"><span class="faq-badge">Q</span>{q}</h4>
            <div class="faq-answer"><span class="faq-badge-a">A</span>{a}</div>
        </div>
        """
        return BlockRenderResult(html=sanitize_html(html_output), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        return mark_safe(self.render_clean(value, context).html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        q = str(value.get("question") or "")
        a = str(value.get("answer") or "")
        return BlockTextExtraction(plain_text=f"{q}\n{a}", logical_segments=[q, a])

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        return []


class CustomReferenceBlock(blocks.StructBlock):
    """全站公共权威参考外链卡片"""
    type_name = "reference"
    schema_version = 1

    title = blocks.CharBlock(required=True, label="参考资源标题")
    url = blocks.URLBlock(required=True, label="官方权威链接")
    description = blocks.CharBlock(required=False, label="简要说明")

    class Meta:
        icon = "link-external"
        label = "参考外链卡片"

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("title") or not value.get("url"):
            return BlockValidationResult(is_valid=False, errors=["参考标题与 URL 不能为空"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)

        title = html.escape(str(value.get("title") or ""))
        url = html.escape(str(value.get("url") or ""))
        desc = html.escape(str(value.get("description") or ""))

        html_output = f"""
        <div class="reference-link-card block-reference">
            <a href="{url}" target="_blank" rel="noopener noreferrer" class="ref-link">
                🔗 {title}
            </a>
            {f'<span class="ref-desc">— {desc}</span>' if desc else ''}
        </div>
        """
        return BlockRenderResult(html=sanitize_html(html_output), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        return mark_safe(self.render_clean(value, context).html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        title = str(value.get("title") or "")
        desc = str(value.get("description") or "")
        return BlockTextExtraction(plain_text=f"{title} {desc}".strip(), logical_segments=[title, desc] if desc else [title])

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        return []
