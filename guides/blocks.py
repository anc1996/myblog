"""
guides.blocks
专题指南专属积木流集合。

实现全站无重复造轮子：
所有教程与结构化积木均直接沉淀并复用自 content_blocks 基础模块。
"""

from wagtail.blocks import StreamBlock
from content_blocks.blocks import (
    CustomLeadBlock,
    CustomStepBlock,
    CustomTipBlock,
    CustomFaqBlock,
    CustomReferenceBlock,
    get_common_stream_blocks,
)

# 向后兼容与别名导出（确保迁移历史与序列化 100% 稳定）
GuideLeadBlock = CustomLeadBlock
GuideStepBlock = CustomStepBlock
GuideTipBlock = CustomTipBlock
GuideFaqBlock = CustomFaqBlock
GuideReferenceBlock = CustomReferenceBlock


class GuideStreamBlock(StreamBlock):
    """专题技术指南专属积木流：装配全套 15 个全站公共积木"""
    def __init__(self, **kwargs):
        super().__init__(get_common_stream_blocks(include_structured=True), **kwargs)

    class Meta:
        label = "指南积木内容流"
