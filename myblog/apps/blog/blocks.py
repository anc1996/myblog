"""
博客专属 StreamField 组合积木。

1. BlogIntroBlocks: 简介区块，仅允许 RichText 与 Markdown 组合；
2. BlogBodyBlocks: 正文区块，装配全套 10 个公共积木。
"""

from wagtail import blocks
from content_blocks.blocks import (
    CustomRichTextBlock,
    CustomMarkdownBlock,
    get_common_stream_blocks,
)


class BlogIntroBlocks(blocks.StreamBlock):
    """文章简介专属积木流：允许富文本或 Markdown"""
    rich_text = CustomRichTextBlock(label="富文本简介")
    markdown = CustomMarkdownBlock(label="Markdown 简介")

    class Meta:
        label = "文章简介区块"


class BlogBodyBlocks(blocks.StreamBlock):
    """正文专属积木流：装配全部 15 个全站公共积木（含基础积木与结构化教程卡片）"""
    def __init__(self, **kwargs):
        super().__init__(get_common_stream_blocks(include_structured=True), **kwargs)

    class Meta:
        label = "文章正文区块"
