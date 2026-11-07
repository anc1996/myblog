"""
portfolio.blocks
作品集与开源项目专属 StreamField 积木定义。

包含：
1. TechStackBlock: 技术栈徽标（分类、语言/框架、版本）；
2. RepositoryBlock: 开源仓库元数据（GitHub/Gitee 地址、Star 数、License、Demo 演示）；
3. ScreenshotGalleryBlock: 界面截图画廊（引用 CustomImage 并提取媒体引用）；
4. PortfolioStreamBlock: 聚合通用积木与作品集专属积木。
"""

import html
from typing import Any
from wagtail import blocks
from wagtail.images.blocks import ImageChooserBlock

from content_blocks.blocks import (
    CustomRichTextBlock,
    CustomMarkdownBlock,
    CustomCodeBlock,
    CustomTableBlock,
    CustomMermaidBlock,
    CustomImageBlock,
    CustomVideoBlock,
    CustomLeadBlock,
    CustomStepBlock,
    CustomTipBlock,
    CustomFaqBlock,
    CustomReferenceBlock,
)
from content_blocks.blocks.media import _resolve_asset_id
from content_blocks.contracts import MediaReference

TECH_CATEGORIES = [
    ("frontend", "前端技术 (Frontend)"),
    ("backend", "后端架构 (Backend)"),
    ("database", "数据库与存储 (Database)"),
    ("devops", "容器与运维 (DevOps)"),
    ("ai", "人工智能与算法 (AI/ML)"),
    ("tools", "开发工具与库 (Tools/Libs)"),
]

REPO_PLATFORMS = [
    ("github", "GitHub"),
    ("gitee", "Gitee"),
    ("gitlab", "GitLab"),
    ("custom", "自建 Git / 私有仓库"),
]


class TechStackBlock(blocks.StructBlock):
    """单个技术栈徽标组件"""
    category = blocks.ChoiceBlock(choices=TECH_CATEGORIES, default="backend", label="技术分类")
    name = blocks.CharBlock(max_length=64, label="技术/框架名称 (如 Django 6.1)")
    version = blocks.CharBlock(max_length=32, required=False, label="核心版本号")
    description = blocks.CharBlock(max_length=255, required=False, label="在项目中的角色与应用场景")

    class Meta:
        icon = "cogs"
        label = "技术栈组件"


class RepositoryBlock(blocks.StructBlock):
    """开源仓库与演示链接组件"""
    platform = blocks.ChoiceBlock(choices=REPO_PLATFORMS, default="github", label="代码托管平台")
    repo_url = blocks.URLBlock(label="代码仓库地址 (如 https://github.com/...)")
    demo_url = blocks.URLBlock(required=False, label="在线演示体验地址")
    license_type = blocks.CharBlock(required=False, max_length=64, default="MIT", label="开源协议 (License)")
    star_count = blocks.IntegerBlock(required=False, default=0, min_value=0, label="Star 数量（展示用）")

    class Meta:
        icon = "site"
        label = "代码仓库与演示"


class ScreenshotItemBlock(blocks.StructBlock):
    """画廊单张截图项"""
    image = ImageChooserBlock(label="选择截图")
    caption = blocks.CharBlock(required=False, max_length=255, label="截图说明/功能描述")
    is_cover = blocks.BooleanBlock(required=False, default=False, label="设为本画廊主推荐图")

    class Meta:
        icon = "image"
        label = "截图项"


class ScreenshotGalleryBlock(blocks.StructBlock):
    """界面截图与功能演示多图画廊"""
    heading = blocks.CharBlock(required=False, max_length=120, default="系统界面与功能演示", label="画廊标题")
    screenshots = blocks.ListBlock(ScreenshotItemBlock(), label="截图列表")

    class Meta:
        icon = "image"
        label = "界面截图画廊"

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        if not value or not value.get("screenshots"):
            return []
        refs = []
        for idx, item in enumerate(value["screenshots"]):
            img = item.get("image")
            asset_id = _resolve_asset_id(img)
            if asset_id:
                refs.append(
                    MediaReference(
                        asset_id=asset_id,
                        asset_type="image",
                        block_path=f"{path}.screenshots.{idx}.image",
                    )
                )
        return refs


class PortfolioStreamBlock(blocks.StreamBlock):
    """作品集与开源项目主积木流"""
    # 专属积木
    tech_stack = blocks.ListBlock(TechStackBlock(), label="核心技术栈列表", group="项目专属")
    repository = RepositoryBlock(label="代码仓库与在线演示", group="项目专属")
    gallery = ScreenshotGalleryBlock(label="界面截图画廊", group="项目专属")

    # 通用内容积木 (从 content_blocks 引入)
    rich_text = CustomRichTextBlock(label="富文本段落", group="排版内容")
    markdown = CustomMarkdownBlock(label="Markdown 章节", group="排版内容")
    code = CustomCodeBlock(label="代码块高亮", group="排版内容")
    table = CustomTableBlock(label="结构化对比表格", group="排版内容")
    mermaid = CustomMermaidBlock(label="架构流程图 (Mermaid)", group="排版内容")
    image = CustomImageBlock(label="单张配图", group="多媒体")
    video = CustomVideoBlock(label="演示视频", group="多媒体")

    # 教程积木
    lead = CustomLeadBlock(label="项目导读", group="结构化模块")
    step = CustomStepBlock(label="实施步骤", group="结构化模块")
    tip = CustomTipBlock(label="技术要点提示", group="结构化模块")
    faq = CustomFaqBlock(label="常见答疑 (FAQ)", group="结构化模块")
    reference = CustomReferenceBlock(label="参考资料", group="结构化模块")

    class Meta:
        label = "作品集内容积木流"
