"""
全站公共 StreamField 内容积木集合。
"""

from .rich_text import CustomRichTextBlock
from .markdown import CustomMarkdownBlock
from .code import CustomCodeBlock
from .mermaid import CustomMermaidBlock
from .table import CustomTableBlock
from .media import (
    CustomImageBlock,
    CustomDocumentBlock,
    CustomAudioBlock,
    CustomVideoBlock,
)
from .embed import CustomEmbedBlock
from .structured import (
    CustomLeadBlock,
    CustomStepBlock,
    CustomTipBlock,
    CustomFaqBlock,
    CustomReferenceBlock,
)

__all__ = [
    "CustomRichTextBlock",
    "CustomMarkdownBlock",
    "CustomCodeBlock",
    "CustomMermaidBlock",
    "CustomTableBlock",
    "CustomImageBlock",
    "CustomDocumentBlock",
    "CustomAudioBlock",
    "CustomVideoBlock",
    "CustomEmbedBlock",
    "CustomLeadBlock",
    "CustomStepBlock",
    "CustomTipBlock",
    "CustomFaqBlock",
    "CustomReferenceBlock",
    "get_common_stream_blocks",
    "get_structured_stream_blocks",
]


def get_structured_stream_blocks():
    """返回教程与结构化卡片积木定义列表"""
    return [
        ("lead", CustomLeadBlock()),
        ("step", CustomStepBlock()),
        ("tip", CustomTipBlock()),
        ("faq", CustomFaqBlock()),
        ("reference", CustomReferenceBlock()),
    ]


def get_common_stream_blocks(include_structured: bool = False):
    """返回全站公共 StreamField 积木定义列表，供各类 Page 模型复用"""
    base_blocks = [
        ("rich_text", CustomRichTextBlock()),
        ("markdown", CustomMarkdownBlock()),
        ("code", CustomCodeBlock()),
        ("mermaid", CustomMermaidBlock()),
        ("table", CustomTableBlock()),
        ("image", CustomImageBlock()),
        ("document", CustomDocumentBlock()),
        ("audio", CustomAudioBlock()),
        ("video", CustomVideoBlock()),
        ("embed", CustomEmbedBlock()),
    ]
    if include_structured:
        base_blocks.extend(get_structured_stream_blocks())
    return base_blocks
