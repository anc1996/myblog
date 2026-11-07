"""
guides.blocks
专题指南专属技术教程 StreamField 积木定义。

提供面向实操教程的 6 类专业结构化积木：
1. lead: 导言与先决条件卡片
2. step: 实操步骤卡片（步骤编号、标题、详细说明、可选代码片段、可选架构配图）
3. code: 增强源代码块
4. tip: 技巧/提醒/警示卡片（tip, info, warning, danger）
5. faq: 常见排错与问答卡片
6. reference: 权威官方参考外链卡片
并集成复用已有的 CustomTableBlock、MermaidBlock 与 MarkdownBlock。
"""

from wagtail.blocks import (
    CharBlock,
    ChoiceBlock,
    RichTextBlock,
    StreamBlock,
    StructBlock,
    TextBlock,
    URLBlock,
)
from wagtail.images.blocks import ImageChooserBlock
from content_blocks.blocks import (
    CustomTableBlock,
    CustomMermaidBlock,
    CustomMarkdownBlock,
    CustomCodeBlock,
)

LANGUAGE_CHOICES = [
    ("bash", "Bash / Shell"),
    ("python", "Python"),
    ("sql", "PostgreSQL / SQL"),
    ("javascript", "JavaScript"),
    ("typescript", "TypeScript"),
    ("rust", "Rust"),
    ("go", "Go"),
    ("yaml", "YAML / Compose"),
    ("json", "JSON"),
    ("dockerfile", "Dockerfile"),
    ("nginx", "Nginx Conf"),
]

TIP_CHOICES = [
    ("tip", "💡 实用技巧 (Tip)"),
    ("info", "ℹ️ 补充说明 (Info)"),
    ("warning", "⚠️ 踩坑预警 (Warning)"),
    ("danger", "🛑 高危操作 (Danger)"),
]


class GuideLeadBlock(StructBlock):
    """教程导读与前置要求"""
    title = CharBlock(required=True, label="导言标题", default="学习目标与前置要求")
    prerequisites = RichTextBlock(required=False, label="先决条件与环境准备")
    overview = RichTextBlock(required=True, label="核心知识点概述")

    class Meta:
        icon = "info-circle"
        label = "教程导读卡片"
        template = "guides/blocks/lead_block.html"


class GuideStepBlock(StructBlock):
    """实操操作步骤块（发布强校验必需至少包含 1 个）"""
    step_number = CharBlock(required=True, label="步骤编号", default="1")
    title = CharBlock(required=True, label="步骤名称")
    content = RichTextBlock(required=True, label="详细操作说明")
    code_snippet = TextBlock(required=False, label="示例代码 / 终端命令")
    code_language = ChoiceBlock(choices=LANGUAGE_CHOICES, required=False, default="bash", label="代码语言")
    image = ImageChooserBlock(required=False, label="步骤配图 / 结果截图")
    image_caption = CharBlock(required=False, label="图注说明")

    class Meta:
        icon = "tasks"
        label = "操作步骤卡片 (Step)"
        template = "guides/blocks/step_block.html"


class GuideTipBlock(StructBlock):
    """情境预警与技巧提示卡片"""
    tip_type = ChoiceBlock(choices=TIP_CHOICES, default="tip", label="卡片类型")
    title = CharBlock(required=False, label="提示标题")
    content = RichTextBlock(required=True, label="提示内容")

    class Meta:
        icon = "help"
        label = "技巧预警卡片"
        template = "guides/blocks/tip_block.html"


class GuideFaqBlock(StructBlock):
    """常见问题排坑解答对"""
    question = CharBlock(required=True, label="常见疑问 / 报错信息 (Q)")
    answer = RichTextBlock(required=True, label="排错思路与解决方案 (A)")

    class Meta:
        icon = "comment-dots"
        label = "FAQ 问答卡片"
        template = "guides/blocks/faq_block.html"


class GuideReferenceBlock(StructBlock):
    """权威官方文档与外部参考资料"""
    title = CharBlock(required=True, label="参考资源标题")
    url = URLBlock(required=True, label="官方权威链接")
    description = CharBlock(required=False, label="简要说明")

    class Meta:
        icon = "link-external"
        label = "参考外链卡片"
        template = "guides/blocks/reference_block.html"


class GuideStreamBlock(StreamBlock):
    """技术指南专属积木流集合"""
    lead = GuideLeadBlock()
    step = GuideStepBlock()
    code = CustomCodeBlock()
    tip = GuideTipBlock()
    faq = GuideFaqBlock()
    reference = GuideReferenceBlock()
    markdown = CustomMarkdownBlock()
    table = CustomTableBlock()
    mermaid = CustomMermaidBlock()

    class Meta:
        label = "指南积木内容流"
