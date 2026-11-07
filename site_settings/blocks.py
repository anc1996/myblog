"""
site_settings.blocks
全局导航菜单与社交链接 StreamField 积木定义。
"""

from wagtail import blocks
from wagtail.blocks import PageChooserBlock


class MenuItemBlock(blocks.StructBlock):
    """单个导航菜单项（支持页面链接与站外链接，支持徽标与子项）"""
    label = blocks.CharBlock(max_length=64, label="导航显示名称")
    badge = blocks.CharBlock(max_length=32, required=False, label="角标/提示 (如 NEW, HOT)")
    target_page = PageChooserBlock(required=False, label="选择站内页面")
    external_url = blocks.URLBlock(required=False, label="站外目标 URL (优先于站内页面)")
    is_external = blocks.BooleanBlock(required=False, default=False, label="在新窗口中打开")

    class Meta:
        icon = "link"
        label = "主导航项"


SOCIAL_PLATFORMS = [
    ("github", "GitHub"),
    ("twitter", "Twitter / X"),
    ("bilibili", "哔哩哔哩 (Bilibili)"),
    ("zhihu", "知乎 (Zhihu)"),
    ("wechat", "微信公众平台"),
    ("email", "电子邮箱 (Mailto)"),
    ("rss", "RSS / Atom 订阅"),
    ("custom", "自定义链接"),
]


class SocialLinkBlock(blocks.StructBlock):
    """全站社交媒体链接"""
    platform = blocks.ChoiceBlock(choices=SOCIAL_PLATFORMS, default="github", label="社交平台类型")
    label = blocks.CharBlock(max_length=64, label="链接悬停标题")
    url = blocks.CharBlock(max_length=255, label="链接地址 (URL 或 mailto:...)")

    class Meta:
        icon = "site"
        label = "社交媒体链接"
