"""
site_settings.blocks
全局导航菜单与社交链接 StreamField 积木定义。
"""

from urllib.parse import urlparse
from django.core.exceptions import ValidationError
from wagtail import blocks
from wagtail.blocks import PageChooserBlock
from wagtail.blocks.struct_block import StructBlockValidationError


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

ALLOWED_SOCIAL_PLATFORM_KEYS = {key for key, _ in SOCIAL_PLATFORMS}
SOCIAL_PLATFORM_LABELS = dict(SOCIAL_PLATFORMS)


def sanitize_social_url(raw_url: str) -> tuple[str, bool]:
    """
    仅放行 https://、http://、mailto: 与站内单斜杠相对路径 /...；
    返回 (安全 URL 或空串, 是否站外新窗打开)。
    """
    url = str(raw_url or "").strip()
    if not url or any(ord(ch) < 32 for ch in url):
        return "", False
    if url.startswith("/"):
        if url.startswith("//"):
            return "", False
        return url, False

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in ("http", "https") and parsed.netloc:
        return url, True
    if scheme == "mailto":
        addr = (parsed.path or "").strip()
        if addr and "@" in addr and not any(ch.isspace() for ch in addr):
            return url, False
    return "", False


class SocialLinkBlock(blocks.StructBlock):
    """全站社交媒体链接"""
    platform = blocks.ChoiceBlock(choices=SOCIAL_PLATFORMS, default="github", label="社交平台类型")
    label = blocks.CharBlock(max_length=64, label="链接悬停标题")
    url = blocks.CharBlock(max_length=255, label="链接地址 (URL 或 mailto:...)")

    def clean(self, value):
        cleaned = super().clean(value)
        safe_url, _ = sanitize_social_url(cleaned.get("url", ""))
        if not safe_url:
            raise StructBlockValidationError(
                block_errors={
                    "url": ValidationError("请输入有效的 https://、http://、mailto: 邮箱或站内 / 开头链接。")
                }
            )
        cleaned["url"] = safe_url
        return cleaned

    class Meta:
        icon = "site"
        label = "社交媒体链接"
