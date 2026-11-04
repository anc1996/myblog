"""
全站公共 StreamField 内容积木集合。
"""

from .rich_text import CustomRichTextBlock
from .markdown import CustomMarkdownBlock
from .code import CustomCodeBlock
from .mermaid import CustomMermaidBlock
from .media import (
    CustomImageBlock,
    CustomDocumentBlock,
    CustomAudioBlock,
    CustomVideoBlock,
)
from .embed import CustomEmbedBlock

__all__ = [
    "CustomRichTextBlock",
    "CustomMarkdownBlock",
    "CustomCodeBlock",
    "CustomMermaidBlock",
    "CustomImageBlock",
    "CustomDocumentBlock",
    "CustomAudioBlock",
    "CustomVideoBlock",
    "CustomEmbedBlock",
    "get_common_stream_blocks",
]


def get_common_stream_blocks():
    """返回全站公共 StreamField 积木定义列表，供各类 Page 模型复用"""
    return [
        ("rich_text", CustomRichTextBlock()),
        ("markdown", CustomMarkdownBlock()),
        ("code", CustomCodeBlock()),
        ("mermaid", CustomMermaidBlock()),
        ("image", CustomImageBlock()),
        ("document", CustomDocumentBlock()),
        ("audio", CustomAudioBlock()),
        ("video", CustomVideoBlock()),
        ("embed", CustomEmbedBlock()),
    ]
