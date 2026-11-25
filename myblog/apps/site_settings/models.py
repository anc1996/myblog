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
from urllib.parse import urlparse
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import DatabaseError, models, transaction
from django.utils import timezone
from wagtail.contrib.settings.models import (
    BaseGenericSetting,
    BaseSiteSetting as WagtailBaseSiteSetting,
    register_setting,
)
from wagtail.fields import StreamField
from wagtail.admin.panels import FieldPanel, MultiFieldPanel
from wagtail.models import Page

from site_settings.blocks import (
    ALLOWED_SOCIAL_PLATFORM_KEYS,
    SOCIAL_PLATFORM_LABELS,
    MenuItemBlock,
    SocialLinkBlock,
    sanitize_social_url,
)

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

    @classmethod
    def default_public_links(cls) -> list[dict]:
        return [
            {
                "platform": "github",
                "label": "GitHub",
                "url": "https://github.com/anc1996/myblog",
                "is_external": True,
            },
            {
                "platform": "rss",
                "label": "RSS 订阅",
                "url": "/feeds/",
                "is_external": False,
            },
            {
                "platform": "rss",
                "label": "Atom 订阅",
                "url": "/feeds/atom/",
                "is_external": False,
            },
            {
                "platform": "email",
                "label": "电子邮箱",
                "url": "mailto:contact@example.com",
                "is_external": False,
            },
        ]

    @classmethod
    def get_public_links_readonly(
        cls,
        social_obj=None,
        *,
        fallback_to_defaults: bool = True,
    ) -> list[dict]:
        """只读解析并过滤社交链接协议，空表绝不隐式写库。"""
        obj = social_obj
        if obj is None:
            try:
                obj = cls.objects.filter(singleton_key=1).first()
            except DatabaseError:
                obj = None

        items: list[dict] = []
        if obj is not None and getattr(obj, "social_links", None):
            for block in obj.social_links:
                val = getattr(block, "value", None) or {}
                raw_platform = str(val.get("platform") or "custom").strip().lower()
                platform = raw_platform if raw_platform in ALLOWED_SOCIAL_PLATFORM_KEYS else "custom"
                safe_url, is_external = sanitize_social_url(val.get("url", ""))
                if not safe_url:
                    continue
                label = str(val.get("label") or "").strip() or SOCIAL_PLATFORM_LABELS.get(platform, "链接")
                items.append(
                    {
                        "platform": platform,
                        "label": label,
                        "url": safe_url,
                        "is_external": is_external,
                    }
                )

        if not items and fallback_to_defaults:
            return cls.default_public_links()
        return items


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


@register_setting
class SiteAnnouncementSettings(WagtailBaseSiteSetting):
    """
    逐站点详情页公告配置（直接继承 Wagtail 原生 BaseSiteSetting，按 Site 隔离）：
    1. 绝不继承会递增 ContentDiscoveryEpoch 的自定义基类；
    2. 严禁模板通过 {% get_settings %} 读取（空表会隐式 write），必须经 get_active_for_site_readonly() 只读访问；
    3. 仅支持纯文本标题/正文与校验通过的站内 Page 或 https:// 外链，不接受 HTML。
    """

    is_enabled = models.BooleanField(default=False, verbose_name="开启站点公告")
    title = models.CharField(max_length=80, blank=True, verbose_name="公告标题")
    body = models.TextField(max_length=500, blank=True, verbose_name="公告正文（纯文本）")
    link_label = models.CharField(max_length=40, blank=True, verbose_name="链接按钮文案")
    target_page = models.ForeignKey(
        "wagtailcore.Page",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="站内目标页面",
    )
    external_url = models.URLField(blank=True, verbose_name="站外 HTTPS 链接")
    starts_at = models.DateTimeField(null=True, blank=True, verbose_name="生效开始时间")
    ends_at = models.DateTimeField(null=True, blank=True, verbose_name="生效结束时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="最后更新时间")

    panels = [
        FieldPanel("is_enabled"),
        FieldPanel("title"),
        FieldPanel("body"),
        MultiFieldPanel(
            [
                FieldPanel("link_label"),
                FieldPanel("target_page"),
                FieldPanel("external_url"),
            ],
            heading="可选跳转链接（站内页面与站外 HTTPS 链接二选一）",
        ),
        MultiFieldPanel(
            [
                FieldPanel("starts_at"),
                FieldPanel("ends_at"),
            ],
            heading="展示时间窗口（左闭右开 [starts_at, ends_at)）",
        ),
    ]

    class Meta:
        verbose_name = "站点公告设置"
        verbose_name_plural = "站点公告设置"

    def clean(self):
        super().clean()
        errors = {}
        ext = (self.external_url or "").strip()
        self.external_url = ext

        if self.starts_at and self.ends_at and self.starts_at >= self.ends_at:
            errors["ends_at"] = "结束时间必须晚于开始时间。"

        if ext:
            parsed = urlparse(ext)
            if parsed.scheme.lower() != "https" or not parsed.netloc:
                errors["external_url"] = "站外链接仅支持有效的 https:// 地址。"

        if self.target_page_id and ext:
            errors["external_url"] = "站内目标页面与站外链接请二选一，不要同时填写。"

        if self.is_enabled and not (self.title or "").strip() and not (self.body or "").strip():
            errors["title"] = "开启站点公告时，标题或正文至少填写一项。"

        if errors:
            raise ValidationError(errors)

    @classmethod
    def get_active_for_site_readonly(
        cls,
        site,
        *,
        request=None,
        now=None,
    ) -> dict | None:
        """
        只读获取当前站点在当前时刻有效且安全的公告字典；
        空表、未开启、不在展示时间窗或数据库异常时返回 None，绝不写库。
        """
        if site is None:
            return None
        site_id = getattr(site, "id", None) or (site if isinstance(site, int) else None)
        if not site_id:
            return None

        try:
            obj = cls.objects.select_related("target_page").filter(site_id=site_id).first()
        except DatabaseError:
            return None

        if obj is None or not obj.is_enabled:
            return None

        check_now = now or timezone.now()
        if obj.starts_at and check_now < obj.starts_at:
            return None
        if obj.ends_at and check_now >= obj.ends_at:
            return None

        title = (obj.title or "").strip()
        body = (obj.body or "").strip()
        if not title and not body:
            return None

        link_url = ""
        is_external = False
        if obj.target_page_id:
            try:
                from content_lifecycle.public_access import get_public_blocked_page_ids

                tp = (
                    Page.objects.filter(
                        id=obj.target_page_id,
                        live=True,
                        live_revision_id__isnull=False,
                        alias_of_id__isnull=True,
                    )
                    .first()
                )
                root_page = getattr(site, "root_page", None)
                root_path = getattr(root_page, "path", "") if root_page else ""
                if tp is not None and (not root_path or tp.path.startswith(root_path)):
                    tp_site = tp.get_site() if hasattr(tp, "get_site") else None
                    same_site = (tp_site is None and not root_path) or (tp_site is not None and tp_site.id == site_id)
                    if same_site and tp.id not in get_public_blocked_page_ids([(tp.id, tp.path)]):
                        resolved = (tp.get_url(request) if request is not None else tp.url) or ""
                        if resolved:
                            link_url = resolved
            except DatabaseError:
                link_url = ""
        elif (obj.external_url or "").strip():
            ext = (obj.external_url or "").strip()
            parsed = urlparse(ext)
            if parsed.scheme.lower() == "https" and parsed.netloc:
                link_url = ext
                is_external = True

        link_label = ((obj.link_label or "").strip() or "查看详情") if link_url else ""
        return {
            "title": title,
            "body": body,
            "link_label": link_label,
            "link_url": link_url,
            "is_external": is_external,
        }
