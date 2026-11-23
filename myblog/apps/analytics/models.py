"""
analytics.models
第 1 期全站数据分析核心 PostgreSQL 数据模型（5 张表）：
1. AnalyticsSettings: 站点级功能开关与冷启动阈值配置（Wagtail BaseSiteSetting）；
2. PageAnalyticsDaily: 单页按日按正式版本（live_revision_id）流量与阅读深度明细；
3. PageAnalyticsTotal: 单页累计总表（总 PV、每日访客日之和、有效阅读与完读数）；
4. ScopeAnalyticsDaily: 站点、类型、入口、栏目及脱敏安全搜索词日聚合表（不含 page_id）；
5. AnalyticsBatchReceipt: 结算批次幂等收据表（防止重复入库）。
"""

from django.conf import settings
from django.db import DatabaseError, models
from django.db.models import Q
from wagtail.admin.panels import FieldPanel, MultiFieldPanel
from wagtail.contrib.settings.models import BaseSiteSetting, register_setting
from wagtail.models import Page, Site


HEALTH_STATUS_CHOICES = [
    ("normal", "正常"),
    ("partial", "部分缺测"),
    ("missing", "缺测"),
]

SCOPE_KIND_CHOICES = [
    ("site", "全站"),
    ("type", "内容类型"),
    ("entry", "聚合入口"),
    ("column", "栏目"),
    ("safe_search_term", "安全搜索词"),
]

BATCH_KIND_CHOICES = [
    ("pv_reading", "PV与阅读"),
    ("daily_uv", "日UV封账"),
]


@register_setting
class AnalyticsSettings(BaseSiteSetting):
    """站点级数据分析开关与冷启动参数设置。"""

    collection_enabled = models.BooleanField(default=True, verbose_name="开启流量采集")
    public_pv_display_enabled = models.BooleanField(
        default=True,
        verbose_name="前台公开展示阅读量",
    )
    rating_enabled = models.BooleanField(default=False, verbose_name="开启四档文章评价")
    ranking_enabled = models.BooleanField(default=False, verbose_name="开启全站榜单")
    min_rating_votes = models.PositiveIntegerField(
        default=20,
        verbose_name="有效评价最低可信样本数",
    )
    observe_rating_votes = models.PositiveIntegerField(
        default=5,
        verbose_name="早期低口碑观察样本阈值",
    )
    high_pv_min = models.PositiveIntegerField(
        default=300,
        verbose_name="高浏览判定下限阈值",
    )
    min_healthy_days_in_30d = models.PositiveIntegerField(
        default=25,
        verbose_name="30日窗口最低健康天数",
    )

    panels = [
        MultiFieldPanel(
            [
                FieldPanel("collection_enabled"),
                FieldPanel("public_pv_display_enabled"),
                FieldPanel("rating_enabled"),
                FieldPanel("ranking_enabled"),
            ],
            heading="功能开关",
        ),
        MultiFieldPanel(
            [
                FieldPanel("min_rating_votes"),
                FieldPanel("observe_rating_votes"),
                FieldPanel("high_pv_min"),
                FieldPanel("min_healthy_days_in_30d"),
            ],
            heading="冷启动与降噪阈值",
        ),
    ]

    class Meta:
        verbose_name = "站点数据分析设置"
        verbose_name_plural = "站点数据分析设置"
        permissions = [
            ("view_site_analytics", "Can view site-wide analytics"),
            ("view_page_analytics", "Can view page-level analytics"),
            ("approve_curatedrecommendation", "Can approve curated recommendations"),
            ("resolve_analyticsalert", "Can resolve analytics alerts"),
        ]

    @classmethod
    def get_for_site_readonly(cls, site: Site | int | None) -> "AnalyticsSettings":
        """
        只读获取站点分析设置：绝不调用 Wagtail 原生 for_site()（其内部会 get_or_create 写库）。
        当行记录尚未创建或表尚未迁移时，返回带默认值的未保存内存实例，保障前台渲染零阻断。
        """
        site_id = site.id if isinstance(site, Site) else site
        if not site_id:
            return cls()
        try:
            obj = cls.objects.filter(site_id=site_id).first()
            if obj is not None:
                return obj
        except DatabaseError:
            return cls(site_id=site_id)
        return cls(site_id=site_id)


class PageAnalyticsDaily(models.Model):
    """单页按自然日与正式发布版本 (live_revision_id) 的流量与阅读深度明细（保留 13 个月）。"""

    site = models.ForeignKey(
        Site,
        on_delete=models.CASCADE,
        related_name="page_analytics_daily",
        verbose_name="所属站点",
    )
    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="analytics_daily",
        verbose_name="关联页面",
    )
    stat_date = models.DateField(db_index=True, verbose_name="统计日期")
    live_revision_id = models.PositiveIntegerField(verbose_name="正式版本 Revision ID")
    type_key = models.CharField(max_length=64, db_index=True, verbose_name="页面类型标识")
    column_page_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="所属栏目页面 ID",
    )
    pv = models.BigIntegerField(default=0, verbose_name="页面浏览量")
    daily_uv = models.BigIntegerField(default=0, verbose_name="当日独立访客数")
    measurable_views = models.BigIntegerField(default=0, verbose_name="可测阅读样本数")
    valid_views = models.BigIntegerField(default=0, verbose_name="有效阅读数")
    completed_views = models.BigIntegerField(default=0, verbose_name="完读数")
    short_bounce_views = models.BigIntegerField(default=0, verbose_name="短读离开数")
    total_active_seconds = models.BigIntegerField(default=0, verbose_name="累计活跃阅读秒数")
    organic_pv = models.BigIntegerField(default=0, verbose_name="自然流量 PV")
    conversions_json = models.JSONField(default=dict, blank=True, verbose_name="转化计数明细")
    health_status = models.CharField(
        max_length=16,
        choices=HEALTH_STATUS_CHOICES,
        default="normal",
        db_index=True,
        verbose_name="健康状态",
    )

    class Meta:
        verbose_name = "页面日统计"
        verbose_name_plural = "页面日统计"
        constraints = [
            models.UniqueConstraint(
                fields=["site", "page", "stat_date", "live_revision_id"],
                name="uniq_page_analytics_daily_site_page_date_rev",
            ),
            models.CheckConstraint(
                condition=(
                    Q(pv__gte=0)
                    & Q(daily_uv__gte=0)
                    & Q(measurable_views__gte=0)
                    & Q(valid_views__gte=0)
                    & Q(completed_views__gte=0)
                    & Q(short_bounce_views__gte=0)
                    & Q(total_active_seconds__gte=0)
                    & Q(organic_pv__gte=0)
                ),
                name="chk_page_analytics_daily_non_neg",
            ),
        ]
        indexes = [
            models.Index(fields=["site", "stat_date"], name="idx_page_ana_site_date"),
            models.Index(fields=["site", "type_key", "stat_date"], name="idx_page_ana_site_type_date"),
            models.Index(fields=["page", "stat_date"], name="idx_page_ana_page_date"),
        ]

    def __str__(self) -> str:
        return f"PageDaily(site={self.site_id}, page={self.page_id}, date={self.stat_date}, rev={self.live_revision_id})"


class PageAnalyticsTotal(models.Model):
    """单页累计统计总表（随 Page Purge 级联删除）。"""

    site = models.ForeignKey(
        Site,
        on_delete=models.CASCADE,
        related_name="page_analytics_totals",
        verbose_name="所属站点",
    )
    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="analytics_totals",
        verbose_name="关联页面",
    )
    total_pv = models.BigIntegerField(default=0, verbose_name="累计 PV")
    total_visitor_days = models.BigIntegerField(
        default=0,
        verbose_name="累计访客日（各日 UV 之和）",
    )
    total_valid_views = models.BigIntegerField(default=0, verbose_name="累计有效阅读数")
    total_completed_views = models.BigIntegerField(default=0, verbose_name="累计完读数")
    last_settled_date = models.DateField(null=True, blank=True, verbose_name="最近结算日期")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "页面累计统计"
        verbose_name_plural = "页面累计统计"
        constraints = [
            models.UniqueConstraint(
                fields=["site", "page"],
                name="uniq_page_analytics_total_site_page",
            ),
            models.CheckConstraint(
                condition=(
                    Q(total_pv__gte=0)
                    & Q(total_visitor_days__gte=0)
                    & Q(total_valid_views__gte=0)
                    & Q(total_completed_views__gte=0)
                ),
                name="chk_page_analytics_total_non_neg",
            ),
        ]
        indexes = [
            models.Index(fields=["site", "-total_pv"], name="idx_page_ana_tot_site_pv"),
        ]

    def __str__(self) -> str:
        return f"PageTotal(site={self.site_id}, page={self.page_id}, pv={self.total_pv})"


class ScopeAnalyticsDaily(models.Model):
    """
    全站、类型、入口、栏目与命中白名单的安全搜索词日聚合表。
    严格不包含 page_id，确保历史站点统计不会泄露已销毁页面。
    """

    site = models.ForeignKey(
        Site,
        on_delete=models.CASCADE,
        related_name="scope_analytics_daily",
        verbose_name="所属站点",
    )
    stat_date = models.DateField(db_index=True, verbose_name="统计日期")
    scope_kind = models.CharField(
        max_length=32,
        choices=SCOPE_KIND_CHOICES,
        db_index=True,
        verbose_name="统计维度",
    )
    scope_key = models.CharField(max_length=128, db_index=True, verbose_name="维度键值")
    pv = models.BigIntegerField(default=0, verbose_name="维度浏览量")
    daily_uv = models.BigIntegerField(default=0, verbose_name="维度日独立访客数")
    entry_clicks = models.BigIntegerField(default=0, verbose_name="入口导流点击数")
    health_status = models.CharField(
        max_length=16,
        choices=HEALTH_STATUS_CHOICES,
        default="normal",
        db_index=True,
        verbose_name="健康状态",
    )

    class Meta:
        verbose_name = "维度日统计"
        verbose_name_plural = "维度日统计"
        constraints = [
            models.UniqueConstraint(
                fields=["site", "stat_date", "scope_kind", "scope_key"],
                name="uniq_scope_analytics_daily_key",
            ),
            models.CheckConstraint(
                condition=Q(pv__gte=0) & Q(daily_uv__gte=0) & Q(entry_clicks__gte=0),
                name="chk_scope_analytics_daily_non_neg",
            ),
        ]
        indexes = [
            models.Index(fields=["site", "scope_kind", "stat_date"], name="idx_scope_ana_site_kind_date"),
        ]

    def __str__(self) -> str:
        return f"ScopeDaily(site={self.site_id}, date={self.stat_date}, {self.scope_kind}={self.scope_key})"


class AnalyticsBatchReceipt(models.Model):
    """结算批次幂等收据表（保留 7 天，防止并发重试或 Redis 删键失败导致重复记账）。"""

    batch_id = models.CharField(max_length=128, unique=True, verbose_name="批次 ID")
    payload_hash = models.CharField(max_length=64, verbose_name="载荷 SHA256 摘要")
    batch_kind = models.CharField(
        max_length=32,
        choices=BATCH_KIND_CHOICES,
        verbose_name="批次类型",
    )
    settled_at = models.DateTimeField(auto_now_add=True, verbose_name="结算时间")
    expires_at = models.DateTimeField(db_index=True, verbose_name="过期时间")

    class Meta:
        verbose_name = "分析结算收据"
        verbose_name_plural = "分析结算收据"
        indexes = [
            models.Index(fields=["batch_kind", "settled_at"], name="idx_ana_receipt_kind_settled"),
        ]

    def __str__(self) -> str:
        return f"BatchReceipt({self.batch_id}, {self.batch_kind})"


# ---------------------------------------------------------------------------
# 第 2 期：四档文章评价、精选推荐审批与降噪告警模型
# ---------------------------------------------------------------------------

RATING_GRADE_CHOICES = [
    ("excellent", "优秀"),
    ("good", "良好"),
    ("pass", "及格"),
    ("poor", "差"),
]

RATING_TRUST_TIER_CHOICES = [
    ("member", "正常会员"),
    ("new_member", "新注册会员"),
]

CURATION_POSITION_CHOICES = [
    ("home_carousel", "首页轮播"),
    ("home_guides", "首页精选指南"),
    ("home_projects", "首页精选项目"),
    ("column_featured", "栏目精选"),
]

CURATION_STATUS_CHOICES = [
    ("pending", "待审批"),
    ("approved", "已批准"),
    ("rejected", "暂不入选"),
    ("withdrawn", "已撤回"),
]

ALERT_KIND_CHOICES = [
    ("LOW_REPUTATION", "低口碑预警"),
    ("HIGH_TRAFFIC_LOW_REPUTATION", "高流量低口碑阻断"),
    ("HIGH_TRAFFIC_LOW_ENGAGEMENT", "高浏览低完读预警"),
    ("TRAFFIC_ANOMALY", "异常流量预警"),
]

ALERT_STATUS_CHOICES = [
    ("observing", "观察中"),
    ("active", "已生效"),
    ("exempted", "限时豁免"),
    ("confirmed_demoted", "确认下榜"),
    ("resolved", "已解除"),
]

RANKING_BOARD_KIND_CHOICES = [
    ("surging", "实时飙升"),
    ("popular_7d", "7日人气"),
    ("popular_30d", "30日人气"),
    ("reputation", "口碑榜"),
]


class ArticleRating(models.Model):
    """四档文章评价真值表（每用户每页最多 1 行；user 采用 PROTECT 防删号漏扣票）。"""

    site = models.ForeignKey(
        Site,
        on_delete=models.CASCADE,
        related_name="article_ratings",
        verbose_name="所属站点",
    )
    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="article_ratings",
        verbose_name="关联页面",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="article_ratings",
        verbose_name="评价用户",
    )
    live_revision_id = models.PositiveIntegerField(verbose_name="评价指向正式版本 ID")
    grade = models.CharField(
        max_length=16,
        null=True,
        blank=True,
        choices=RATING_GRADE_CHOICES,
        verbose_name="评价档位",
    )
    trust_tier = models.CharField(
        max_length=16,
        choices=RATING_TRUST_TIER_CHOICES,
        default="member",
        verbose_name="会员可信层级",
    )
    first_eligible_day = models.DateField(verbose_name="首次有效评价日期")
    evidence_cohort = models.CharField(max_length=16, default="organic", verbose_name="流量来源分组")
    version = models.PositiveBigIntegerField(default=1, verbose_name="单调版本号")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "文章四档评价"
        verbose_name_plural = "文章四档评价"
        constraints = [
            models.UniqueConstraint(
                fields=["page", "user"],
                name="uniq_article_rating_page_user",
            ),
            models.CheckConstraint(
                condition=Q(live_revision_id__gte=0) & Q(version__gte=1),
                name="chk_article_rating_non_neg",
            ),
        ]
        indexes = [
            models.Index(fields=["site", "page", "live_revision_id"], name="idx_art_rating_site_page_rev"),
            models.Index(fields=["user", "page"], name="idx_art_rating_user_page"),
        ]

    def __str__(self) -> str:
        return f"ArticleRating(page={self.page_id}, user={self.user_id}, grade={self.grade}, v={self.version})"


class ArticleRatingAggregate(models.Model):
    """按站点、页面与正式版本汇总的四档现存有效票分布。"""

    site = models.ForeignKey(
        Site,
        on_delete=models.CASCADE,
        related_name="article_rating_aggregates",
        verbose_name="所属站点",
    )
    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="article_rating_aggregates",
        verbose_name="关联页面",
    )
    live_revision_id = models.PositiveIntegerField(verbose_name="正式版本 Revision ID")
    excellent_count = models.BigIntegerField(default=0, verbose_name="优秀票数")
    good_count = models.BigIntegerField(default=0, verbose_name="良好票数")
    pass_count = models.BigIntegerField(default=0, verbose_name="及格票数")
    poor_count = models.BigIntegerField(default=0, verbose_name="差票数")
    rating_evidence_version = models.PositiveBigIntegerField(default=1, verbose_name="评价证据版本号")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "文章评价聚合统计"
        verbose_name_plural = "文章评价聚合统计"
        constraints = [
            models.UniqueConstraint(
                fields=["site", "page", "live_revision_id"],
                name="uniq_art_rating_agg_site_page_rev",
            ),
            models.CheckConstraint(
                condition=(
                    Q(live_revision_id__gte=0)
                    & Q(excellent_count__gte=0)
                    & Q(good_count__gte=0)
                    & Q(pass_count__gte=0)
                    & Q(poor_count__gte=0)
                    & Q(rating_evidence_version__gte=1)
                ),
                name="chk_art_rating_agg_non_neg",
            ),
        ]
        indexes = [
            models.Index(fields=["site", "page"], name="idx_art_rating_agg_site_page"),
            models.Index(fields=["page", "live_revision_id"], name="idx_art_rating_agg_page_rev"),
        ]

    def __str__(self) -> str:
        return f"RatingAggregate(site={self.site_id}, page={self.page_id}, rev={self.live_revision_id})"


class ArticleRatingReceipt(models.Model):
    """四档评价写请求幂等收据表（防止前端双击或网络重试重复扣加票）。"""

    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="article_rating_receipts",
        verbose_name="关联页面",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="article_rating_receipts",
        verbose_name="评价用户",
    )
    request_id = models.CharField(max_length=64, verbose_name="幂等请求 ID")
    request_hash = models.CharField(max_length=64, verbose_name="请求参数 SHA256 摘要")
    result_version = models.PositiveBigIntegerField(verbose_name="结果评价版本号")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    expires_at = models.DateTimeField(db_index=True, verbose_name="过期时间")

    class Meta:
        verbose_name = "文章评价幂等收据"
        verbose_name_plural = "文章评价幂等收据"
        constraints = [
            models.UniqueConstraint(
                fields=["page", "user", "request_id"],
                name="uniq_art_rating_receipt_req",
            ),
            models.CheckConstraint(
                condition=Q(result_version__gte=0),
                name="chk_art_rating_receipt_non_neg",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "expires_at"], name="idx_art_rating_rcpt_user_exp"),
        ]

    def __str__(self) -> str:
        return f"RatingReceipt(page={self.page_id}, user={self.user_id}, req={self.request_id})"


class CuratedRecommendation(models.Model):
    """编辑精选候选与投放池表。"""

    site = models.ForeignKey(
        Site,
        on_delete=models.CASCADE,
        related_name="curated_recommendations",
        verbose_name="所属站点",
    )
    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="curated_recommendations",
        verbose_name="关联页面",
    )
    live_revision_id = models.PositiveIntegerField(verbose_name="正式版本 Revision ID")
    position = models.CharField(max_length=32, choices=CURATION_POSITION_CHOICES, verbose_name="投放位置")
    column_page_id = models.PositiveIntegerField(null=True, blank=True, verbose_name="所属栏目页 ID")
    status = models.CharField(
        max_length=16,
        choices=CURATION_STATUS_CHOICES,
        default="pending",
        db_index=True,
        verbose_name="审批状态",
    )
    priority = models.IntegerField(default=0, verbose_name="展示优先级")
    is_pinned = models.BooleanField(default=False, verbose_name="置顶标记")
    starts_at = models.DateTimeField(null=True, blank=True, verbose_name="生效开始时间")
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name="过期时间")
    rejected_cooldown_until = models.DateTimeField(null=True, blank=True, verbose_name="拒绝冷却截止时间")
    quality_score = models.FloatField(default=0.0, verbose_name="综合质量分")
    evidence_json = models.JSONField(default=dict, blank=True, verbose_name="入围证据快照")
    version = models.PositiveBigIntegerField(default=1, verbose_name="乐观锁版本号")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "精选推荐位条目"
        verbose_name_plural = "精选推荐位条目"
        constraints = [
            models.UniqueConstraint(
                fields=["site", "page", "live_revision_id", "position"],
                name="uniq_curated_rec_site_page_rev_pos",
            ),
            models.CheckConstraint(
                condition=Q(live_revision_id__gte=0) & Q(quality_score__gte=0.0) & Q(version__gte=1),
                name="chk_curated_rec_non_neg",
            ),
        ]
        indexes = [
            models.Index(
                fields=["site", "position", "status", "-is_pinned", "-priority", "-quality_score"],
                name="idx_curated_rec_placement",
            ),
            models.Index(fields=["site", "column_page_id", "status"], name="idx_curated_rec_column"),
            models.Index(fields=["page", "status"], name="idx_curated_rec_page_status"),
        ]

    def __str__(self) -> str:
        return f"CuratedRec(site={self.site_id}, page={self.page_id}, pos={self.position}, status={self.status})"


class CuratedRecommendationAction(models.Model):
    """精选推荐审批与运营操作审计流水。"""

    recommendation = models.ForeignKey(
        CuratedRecommendation,
        on_delete=models.CASCADE,
        related_name="actions",
        verbose_name="关联精选记录",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="curated_recommendation_actions",
        verbose_name="操作人",
    )
    action = models.CharField(max_length=32, verbose_name="操作动作")
    reason = models.TextField(blank=True, verbose_name="审批或撤回原因")
    evidence_version = models.PositiveBigIntegerField(default=1, verbose_name="证据版本号")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="操作时间")

    class Meta:
        verbose_name = "精选审批操作记录"
        verbose_name_plural = "精选审批操作记录"
        constraints = [
            models.CheckConstraint(
                condition=Q(evidence_version__gte=1),
                name="chk_curated_rec_act_non_neg",
            ),
        ]
        indexes = [
            models.Index(fields=["recommendation", "-created_at"], name="idx_curated_act_rec_created"),
        ]

    def __str__(self) -> str:
        return f"CuratedAction(rec={self.recommendation_id}, action={self.action})"


class AnalyticsAlert(models.Model):
    """低口碑、标题党高浏览低完读与异常流量降噪告警表。"""

    site = models.ForeignKey(
        Site,
        on_delete=models.CASCADE,
        related_name="analytics_alerts",
        verbose_name="所属站点",
    )
    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="analytics_alerts",
        verbose_name="关联页面",
    )
    live_revision_id = models.PositiveIntegerField(verbose_name="正式版本 Revision ID")
    alert_kind = models.CharField(max_length=48, choices=ALERT_KIND_CHOICES, verbose_name="告警类型")
    status = models.CharField(
        max_length=24,
        choices=ALERT_STATUS_CHOICES,
        default="observing",
        db_index=True,
        verbose_name="告警状态",
    )
    consecutive_hit_days = models.PositiveIntegerField(default=1, verbose_name="连续命中天数")
    consecutive_clear_days = models.PositiveIntegerField(default=0, verbose_name="连续恢复天数")
    last_evaluated_date = models.DateField(null=True, blank=True, verbose_name="最近评估日期")
    exempt_until = models.DateTimeField(null=True, blank=True, verbose_name="限时豁免截止时间")
    evidence_json = models.JSONField(default=dict, blank=True, verbose_name="评估证据快照")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "分析降噪告警"
        verbose_name_plural = "分析降噪告警"
        constraints = [
            models.UniqueConstraint(
                fields=["site", "page", "live_revision_id", "alert_kind"],
                name="uniq_ana_alert_site_page_rev_kind",
            ),
            models.CheckConstraint(
                condition=Q(live_revision_id__gte=0) & Q(consecutive_hit_days__gte=0) & Q(consecutive_clear_days__gte=0),
                name="chk_ana_alert_non_neg",
            ),
        ]
        indexes = [
            models.Index(fields=["site", "status", "alert_kind"], name="idx_ana_alert_site_stat_kind"),
            models.Index(fields=["page", "status"], name="idx_ana_alert_page_stat"),
        ]

    def __str__(self) -> str:
        return f"AnalyticsAlert(site={self.site_id}, page={self.page_id}, kind={self.alert_kind}, status={self.status})"


class RankingSnapshot(models.Model):
    """全站与分类型榜单可追溯快照表。"""

    site = models.ForeignKey(
        Site,
        on_delete=models.CASCADE,
        related_name="ranking_snapshots",
        verbose_name="所属站点",
    )
    board_kind = models.CharField(max_length=24, choices=RANKING_BOARD_KIND_CHOICES, verbose_name="榜单类型")
    type_key = models.CharField(max_length=64, default="all", verbose_name="内容类型分组")
    stat_date = models.DateField(db_index=True, verbose_name="榜单日期")
    rule_version = models.CharField(max_length=32, default="v1", verbose_name="规则版本")
    items_json = models.JSONField(default=list, blank=True, verbose_name="上榜条目快照")
    generated_at = models.DateTimeField(auto_now=True, verbose_name="生成时间")

    class Meta:
        verbose_name = "全站榜单快照"
        verbose_name_plural = "全站榜单快照"
        constraints = [
            models.UniqueConstraint(
                fields=["site", "board_kind", "type_key", "stat_date"],
                name="uniq_ranking_snap_key",
            ),
        ]
        indexes = [
            models.Index(fields=["site", "board_kind", "type_key", "-stat_date"], name="idx_ranking_snap_lookup"),
        ]

    def __str__(self) -> str:
        return f"RankingSnapshot(site={self.site_id}, board={self.board_kind}, type={self.type_key}, date={self.stat_date})"
