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
from django.core.cache import cache
from django.db import DatabaseError, models, transaction
from wagtail.contrib.settings.models import BaseGenericSetting, register_setting
from wagtail.fields import StreamField
from wagtail.admin.panels import FieldPanel

from site_settings.blocks import MenuItemBlock, SocialLinkBlock

logger = logging.getLogger(__name__)

THEME_SETTINGS_CACHE_KEY = "site_theme_settings:v1"
THEME_SETTINGS_CACHE_TTL = 300
THEME_PALETTE_CHOICES = [
    ("mist-blue", "雾蓝工程手册（默认）"),
    ("moss-lab", "苔青实验室"),
]
THEME_APPEARANCE_CHOICES = [
    ("system", "跟随系统"),
    ("light", "白天（浅色）"),
    ("dark", "黑夜（深色）"),
]
ALLOWED_PALETTES = {k for k, _ in THEME_PALETTE_CHOICES}
ALLOWED_APPEARANCE_MODES = {k for k, _ in THEME_APPEARANCE_CHOICES}


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


@register_setting
class SiteThemeSettings(BaseGenericSetting):
    """
    全站前台外观与主题默认配置（独立单例，不继承 BaseSiteSetting）：
    1. 绝不递增 ContentDiscoveryEpoch，避免改一次配色使全站列表游标与归档缓存失效；
    2. 使用独立缓存 site_theme_settings:v1 (TTL 300s) 并在 transaction.on_commit 刷新；
    3. 公开 GET 仅通过 get_readonly_dict() / {% get_theme_settings %} 只读访问，空表绝不隐式写库。
    """

    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    default_palette = models.CharField(
        max_length=32,
        choices=THEME_PALETTE_CHOICES,
        default="mist-blue",
        verbose_name="默认主题色板",
        help_text="控制全站前台的主题色板风格；每套色板均内置白天与黑夜两套高对比度配色。",
    )
    default_appearance_mode = models.CharField(
        max_length=16,
        choices=THEME_APPEARANCE_CHOICES,
        default="system",
        verbose_name="默认昼夜模式",
        help_text="首次访问或访客未手动覆盖时的默认明暗策略（推荐跟随系统）。",
    )
    version = models.PositiveIntegerField(default=1, verbose_name="外观配置版本号")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="最后更新时间")

    panels = [
        FieldPanel("default_palette"),
        FieldPanel("default_appearance_mode"),
    ]

    class Meta:
        verbose_name = "外观与主题设置"
        verbose_name_plural = "外观与主题设置"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(singleton_key=1),
                name="site_settings_sitethemesettings_singleton_key_one",
            ),
        ]

    @classmethod
    def default_dict(cls) -> dict:
        return {
            "default_palette": "mist-blue",
            "default_appearance_mode": "system",
            "version": 1,
        }

    def to_cache_dict(self) -> dict:
        palette = self.default_palette if self.default_palette in ALLOWED_PALETTES else "mist-blue"
        mode = self.default_appearance_mode if self.default_appearance_mode in ALLOWED_APPEARANCE_MODES else "system"
        return {
            "default_palette": palette,
            "default_appearance_mode": mode,
            "version": int(self.version or 1),
        }

    @classmethod
    def get_readonly_dict(cls) -> dict:
        """公开模板与 API 唯一允许的读取入口：纯只读，绝不因表空触发 INSERT 写库。"""
        try:
            cached = cache.get(THEME_SETTINGS_CACHE_KEY)
            if isinstance(cached, dict) and cached.get("default_palette") in ALLOWED_PALETTES:
                return cached
        except Exception:
            pass

        payload = cls.default_dict()
        try:
            obj = cls.objects.filter(singleton_key=1).first()
            if obj is not None:
                payload = obj.to_cache_dict()
        except DatabaseError:
            return payload

        try:
            cache.set(THEME_SETTINGS_CACHE_KEY, payload, THEME_SETTINGS_CACHE_TTL)
        except Exception:
            pass
        return payload

    def save(self, *args, **kwargs):
        self.singleton_key = 1
        if self.default_palette not in ALLOWED_PALETTES:
            self.default_palette = "mist-blue"
        if self.default_appearance_mode not in ALLOWED_APPEARANCE_MODES:
            self.default_appearance_mode = "system"
        if self.pk:
            self.version = int(self.version or 1) + 1
        res = super().save(*args, **kwargs)
        payload = self.to_cache_dict()

        def _refresh_cache():
            try:
                cache.set(THEME_SETTINGS_CACHE_KEY, payload, THEME_SETTINGS_CACHE_TTL)
            except Exception:
                pass

        transaction.on_commit(_refresh_cache)
        return res

    def delete(self, *args, **kwargs):
        res = super().delete(*args, **kwargs)

        def _clear_cache():
            try:
                cache.delete(THEME_SETTINGS_CACHE_KEY)
            except Exception:
                pass

        transaction.on_commit(_clear_cache)
        return res
