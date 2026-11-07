"""
site_settings.models
全站通用导航菜单、社交链接与页脚设置模型 (Wagtail Generic Settings)。

落实架构契约与并发安全：
1. 单例约束：singleton_key=1 数据库唯一约束与 CHECK(singleton_key=1)，杜绝多行污染；
2. 只读安全：重写 _get_or_create 返回单例实例；公开 GET API 严禁调用 .load()；
3. 锁顺序无环死锁防护 (H-04)：保存时先获取 ContentMutationGuard("global") 锁，再保存并原子自增 ContentDiscoveryEpoch；
4. 菜单下架优雅降级 (H-07)：API 序列化时批量核验公开资格，自动过滤已回收或受密码保护的页面。
"""

import logging
from django.db import models
from wagtail.contrib.settings.models import BaseGenericSetting, register_setting
from wagtail.fields import StreamField
from wagtail.admin.panels import FieldPanel

from site_settings.blocks import MenuItemBlock, SocialLinkBlock

logger = logging.getLogger(__name__)


class BaseSiteSetting(BaseGenericSetting):
    """全站单例通用设置抽象基类"""
    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    version = models.PositiveIntegerField(default=1, verbose_name="配置版本号")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="最后更新时间")

    class Meta:
        abstract = True
        constraints = [
            models.CheckConstraint(
                condition=models.Q(singleton_key=1),
                name="%(app_label)s_%(class)s_singleton_key_one",
            ),
        ]

    @classmethod
    def _get_or_create(cls):
        obj, _ = cls.objects.get_or_create(singleton_key=1)
        return obj

    def save(self, *args, **kwargs):
        from content_lifecycle.guard import content_mutation_guard
        from discovery.models import ContentDiscoveryEpoch

        with content_mutation_guard(timeout_seconds=3):
            if self.pk:
                self.version += 1
            res = super().save(*args, **kwargs)
            ContentDiscoveryEpoch.increment_epoch()
            return res


@register_setting
class NavigationSettings(BaseSiteSetting):
    """顶部主导航菜单配置"""
    nav_items = StreamField(
        [("menu_item", MenuItemBlock())],
        blank=True,
        use_json_field=True,
        verbose_name="主导航项目",
    )

    panels = [
        FieldPanel("nav_items"),
    ]

    class Meta(BaseSiteSetting.Meta):
        verbose_name = "主导航设置"
        verbose_name_plural = "主导航设置"


@register_setting
class SocialMediaSettings(BaseSiteSetting):
    """全站社交媒体链接配置"""
    social_links = StreamField(
        [("social_link", SocialLinkBlock())],
        blank=True,
        use_json_field=True,
        verbose_name="社交媒体链接",
    )

    panels = [
        FieldPanel("social_links"),
    ]

    class Meta(BaseSiteSetting.Meta):
        verbose_name = "社交媒体设置"
        verbose_name_plural = "社交媒体设置"


@register_setting
class FooterSettings(BaseSiteSetting):
    """全站页脚版权与备案号配置"""
    copyright_text = models.CharField(
        max_length=255,
        default="© 2026 myblog. All Rights Reserved.",
        verbose_name="版权说明文本",
    )
    icp_number = models.CharField(max_length=64, blank=True, verbose_name="ICP 备案号")
    police_record_number = models.CharField(max_length=64, blank=True, verbose_name="公安联网备案号")
    about_text = models.TextField(blank=True, verbose_name="页脚关于导语")

    panels = [
        FieldPanel("copyright_text"),
        FieldPanel("icp_number"),
        FieldPanel("police_record_number"),
        FieldPanel("about_text"),
    ]

    class Meta(BaseSiteSetting.Meta):
        verbose_name = "页脚版权设置"
        verbose_name_plural = "页脚版权设置"
