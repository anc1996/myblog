"""
comments.models
全站通用评论核心数据模型。

落实契约：
1. Comment: 外键通用关联 wagtailcore.Page（SET_NULL + 快照冗余，彻底杜绝复制污染与销毁崩溃 C-02）；
2. 扁平二级反嵌套 (C-05)：parent 恒指向根评论，reply_to_comment 记录目标，reply_count 原子维护；
3. CommentVersion (C-03)：独立不可变版本表，唯一约束 (comment, version_number)，严格版本绑定审核；
4. is_publicly_visible 状态跃迁计数 (C-04)：仅在可见性翻转时变更根评论 reply_count，杜绝 V1->V2 编辑审批重复累加；
5. CommentReaction (C-05)：点赞/点踩唯一约束与非负 CheckConstraint；
6. 封堵物理硬删 (C-06)：QuerySet.delete 与 Model.delete 默认抛出 PermissionDenied。
"""

import hashlib
import uuid
from datetime import timedelta
from typing import Any
from django.conf import settings
from django.db import connection, models, transaction
from django.db.models import Q, F
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from wagtail.admin.panels import FieldPanel, MultiFieldPanel
from wagtail.contrib.settings.models import BaseGenericSetting, register_setting


class CommentQuerySet(models.QuerySet):
    def delete(self):
        raise PermissionDenied("禁止通过 QuerySet 批量物理删除评论！必须调用业务服务执行软删除。")


class CommentManager(models.Manager):
    def get_queryset(self):
        return CommentQuerySet(self.model, using=self._db)


class Comment(models.Model):
    """全站通用评论主表"""
    objects = CommentManager()

    # 1. 通用 Page 外键挂载（支持所有继承 Page 的具体页面模型，禁止使用 ParentalKey C-02）
    page = models.ForeignKey(
        "wagtailcore.Page",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="site_comments",
        verbose_name="所属页面",
    )
    page_document_uuid = models.UUIDField(db_index=True, verbose_name="页面文档UUID快照")
    page_title_snapshot = models.CharField(max_length=255, verbose_name="页面标题快照")

    # 2. 作者与用户信息
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="site_comments",
        verbose_name="评论作者",
    )
    author_subject_key = models.CharField(max_length=64, blank=True, db_index=True, verbose_name="稳定主体标识")
    guest_name = models.CharField(max_length=50, blank=True, verbose_name="访客显示昵称")

    # 3. 扁平两级反嵌套结构 (C-05)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="replies",
        verbose_name="顶级根评论",
        help_text="回复时恒指向顶级根评论，确保数据库与 UI 严格保持二级扁平结构",
    )
    reply_to_comment = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="直接回复的目标评论",
    )
    replied_to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="被回复的用户",
    )

    # 4. 生命周期与可见性
    STATUS_CHOICES = [
        ("active", "正常"),
        ("pending", "待审核"),
        ("rejected", "已驳回/违规"),
        ("deleted", "已软删除"),
    ]
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending", db_index=True, verbose_name="生命周期状态")
    is_publicly_visible = models.BooleanField(default=False, db_index=True, verbose_name="当前前台是否公开可见")

    DELETION_REASON_CHOICES = [
        ("author", "作者自删"),
        ("moderator", "管理下架"),
        ("risk_control", "风控拦截"),
    ]
    deletion_reason = models.CharField(max_length=16, blank=True, choices=DELETION_REASON_CHOICES, db_index=True, verbose_name="删除原因")
    deleted_at = models.DateTimeField(null=True, blank=True, verbose_name="软删除时间")
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="执行删除人",
    )
    is_pinned = models.BooleanField(default=False, db_index=True, verbose_name="是否官方置顶")
    is_official = models.BooleanField(default=False, db_index=True, verbose_name="是否站方/作者回复")
    state_generation = models.PositiveIntegerField(default=1, verbose_name="状态代次序号")
    pending_submitted_at = models.DateTimeField(null=True, blank=True, verbose_name="待审版本提交时间")

    # 5. 版本指针 (B-01: on_delete=DO_NOTHING 避免 Collector 置空导致 CheckConstraint 报错)
    latest_version = models.ForeignKey(
        "CommentVersion",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="+",
        verbose_name="最新提交版本",
    )
    approved_version = models.ForeignKey(
        "CommentVersion",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="+",
        verbose_name="当前生效批准版本",
    )
    pending_version = models.ForeignKey(
        "CommentVersion",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="+",
        verbose_name="当前待审版本",
    )

    @property
    def latest_version_id(self):
        return self.latest_version.id if self.latest_version else None

    @property
    def approved_version_id(self):
        return self.approved_version.id if self.approved_version else None

    @property
    def pending_version_id(self):
        return self.pending_version.id if self.pending_version else None

    # 6. 原子计数 (CheckConstraint 保护非负)
    like_count = models.PositiveIntegerField(default=0, verbose_name="点赞数")
    dislike_count = models.PositiveIntegerField(default=0, verbose_name="点踩数")
    reply_count = models.PositiveIntegerField(default=0, verbose_name="公开回复数")

    # 7. 安全编辑窗口
    can_edit_until = models.DateTimeField(null=True, blank=True, verbose_name="编辑截止时间(5分钟)")

    # 8. 审计与风控元数据
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name="客户端IP")
    user_agent = models.TextField(blank=True, verbose_name="User-Agent")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "评论"
        verbose_name_plural = "评论管理"
        ordering = ["-created_at"]
        permissions = [
            ("moderate_comment", "可以审核、批准、驳回或隐藏全站评论"),
            ("govern_comment_globally", "可以全站批量治理与封禁发评人"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(like_count__gte=0) & Q(dislike_count__gte=0) & Q(reply_count__gte=0),
                name="comments_non_negative_counters",
            ),
            models.CheckConstraint(
                condition=~Q(id=F("parent_id")),
                name="comments_no_self_parent",
            ),
        ]
        indexes = [
            models.Index(fields=["page", "is_publicly_visible", "created_at"], name="comm_page_vis_idx"),
            models.Index(fields=["parent", "is_publicly_visible", "created_at"], name="comm_parent_vis_idx"),
            # B-04 / Argon 17.3 优化：单一复合部分索引，0 内存排序合并
            models.Index(
                fields=["page", "is_pinned", "created_at", "id"],
                condition=Q(parent__isnull=True) & Q(approved_version__isnull=False) & (
                    (Q(status="active") & Q(is_publicly_visible=True))
                    | (Q(status="deleted") & Q(deletion_reason="author"))
                ),
                name="comm_root_thread_idx",
            ),
            # 待审队列快速索引
            models.Index(
                fields=["created_at", "id"],
                condition=Q(pending_version__isnull=False),
                name="comm_pending_idx",
            ),
            # 单篇后台全状态管理索引
            models.Index(
                fields=["page", "status", "created_at", "id"],
                name="comm_page_admin_idx",
            ),
        ]

    def delete(self, *args, allow_physical_purge=False, **kwargs):
        """禁止普通物理删除，强制通过业务服务软删除 (C-06 / D5-B)"""
        from content_lifecycle.models import is_purge_authorized
        if not allow_physical_purge and not is_purge_authorized():
            raise PermissionDenied("禁止物理硬删评论记录！必须调用 CommentService.soft_delete_comment 执行软删除。")
        return super().delete(*args, **kwargs)

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def is_editable(self) -> bool:
        return bool(self.can_edit_until and timezone.now() <= self.can_edit_until)

    @property
    def page_display(self) -> str:
        if self.page:
            return self.page.title
        return f"{self.page_title_snapshot} [页面已永久销毁]"

    def get_author_presentation(self) -> dict[str, Any]:
        """
        统一计算评论作者的前后台展示结构，精确区分会员、已注销用户、访客与未知主体。

        教学说明：
        1. 注册会员（author_id 存在且 author 不为空）：
           优先读取关联 MemberProfile 的 display_name 与 avatar_url，无资料行时回退 username；
        2. 已注销用户（author_id 为 None 且 author_subject_key 以 "user:" 开头）：
           账号彻底删除后外键置空（SET_NULL），但稳定主体标识保留为 "user:<id>"，
           据此识别为 deleted_member（"已注销用户"），避免与普通访客混淆；
        3. 访客（author_subject_key 以 "ip:" 或 "guest:" 开头）：
           返回 kind="guest"，展示 guest_name 或兜底 "访客"；
        4. 空主体兜底（author_id 为 None 且 author_subject_key 为空）：
           仅当 guest_name 非空时视为访客，否则必须返回 kind="unknown"（"未知用户"），
           绝不把空主体误判成“已注销用户”或“访客”。
        """
        if self.author_id and self.author is not None:
            profile = getattr(self.author, "member_profile", None)
            return {
                "id": self.author_id,
                "username": self.author.username,
                "display_name": (profile.display_name if profile else self.author.username),
                "kind": "member",
                "avatar_url": (profile.avatar_url if profile else ""),
            }

        subject_key = self.author_subject_key or ""
        if self.author_id is None and subject_key.startswith("user:"):
            return {
                "id": None,
                "username": "已注销用户",
                "display_name": "已注销用户",
                "kind": "deleted_member",
                "avatar_url": "",
            }

        if subject_key.startswith(("ip:", "guest:")):
            guest_label = self.guest_name or "访客"
            return {
                "id": None,
                "username": guest_label,
                "display_name": guest_label,
                "kind": "guest",
                "avatar_url": "",
            }

        if self.author_id is None and not subject_key:
            if self.guest_name:
                return {
                    "id": None,
                    "username": self.guest_name,
                    "display_name": self.guest_name,
                    "kind": "guest",
                    "avatar_url": "",
                }
            return {
                "id": None,
                "username": "未知用户",
                "display_name": "未知用户",
                "kind": "unknown",
                "avatar_url": "",
            }

        return {
            "id": None,
            "username": "未知用户",
            "display_name": "未知用户",
            "kind": "unknown",
            "avatar_url": "",
        }

    @property
    def author_display(self) -> str:
        return self.get_author_presentation()["display_name"]

    @property
    def content_snippet(self) -> str:
        ver = self.pending_version or self.approved_version or self.latest_version
        if ver:
            status_tag = f"[{ver.get_status_display()}] " if self.pending_version else ""
            return f"{status_tag}{ver.raw_content[:60]}"
        return "（无内容）"

    def actions_column(self):
        from django.urls import reverse
        from django.utils.safestring import mark_safe
        actions_html = []
        if self.pending_version_id:
            approve_url = reverse("comments:approve", args=[self.id])
            reject_url = reverse("comments:reject", args=[self.id])
            actions_html.append(f"""
            <form method="POST" action="{approve_url}" style="display:inline-block;margin-right:4px;">
                <input type="hidden" name="csrfmiddlewaretoken" value="" class="js-csrf-token">
                <input type="hidden" name="expected_version_id" value="{self.pending_version_id}">
                <button type="submit" class="button button-small" style="background:#16a34a;color:#fff;">批准</button>
            </form>
            <form method="POST" action="{reject_url}" style="display:inline-block;margin-right:4px;">
                <input type="hidden" name="csrfmiddlewaretoken" value="" class="js-csrf-token">
                <input type="hidden" name="expected_version_id" value="{self.pending_version_id}">
                <button type="submit" class="button button-small" style="background:#ea580c;color:#fff;">驳回</button>
            </form>
            """)
        elif self.status == "active":
            hide_url = reverse("comments:hide", args=[self.id])
            pin_url = reverse("comments:pin", args=[self.id])
            pin_text = "取消置顶" if self.is_pinned else "置顶"
            actions_html.append(f"""
            <form method="POST" action="{hide_url}" style="display:inline-block;margin-right:4px;">
                <input type="hidden" name="csrfmiddlewaretoken" value="" class="js-csrf-token">
                <button type="submit" class="button button-small button-secondary">隐藏</button>
            </form>
            <form method="POST" action="{pin_url}" style="display:inline-block;">
                <input type="hidden" name="csrfmiddlewaretoken" value="" class="js-csrf-token">
                <button type="submit" class="button button-small button-secondary">{pin_text}</button>
            </form>
            """)
        return mark_safe("".join(actions_html)) if actions_html else "-"
    actions_column.short_description = "审核操作"

    def __str__(self):
        page_str = self.page.title if self.page else self.page_title_snapshot
        return f"Comment #{self.id} on '{page_str}' by {self.author_display} ({self.status})"


class CommentVersion(models.Model):
    """
    评论内容不可变版本表 (C-03)。
    每次作者在 5 分钟内修改评论，均生成全新不可变版本记录，实现严格版本级审核。
    """
    comment = models.ForeignKey(
        Comment,
        on_delete=models.CASCADE,
        related_name="versions",
        verbose_name="所属评论",
    )
    version_number = models.PositiveIntegerField(default=1, verbose_name="版本号")
    raw_content = models.TextField(verbose_name="原始内容(Markdown)")
    safe_html = models.TextField(verbose_name="安全渲染HTML(nh3净化)")
    content_hash = models.CharField(max_length=64, db_index=True, verbose_name="内容SHA-256哈希")
    renderer_version = models.CharField(max_length=32, blank=True, default="nh3-0.3.5", verbose_name="渲染器规则版本")
    risk_reasons = models.JSONField(default=list, blank=True, verbose_name="风控/待审触发原因")
    risk_policy_version = models.CharField(max_length=32, blank=True, default="v1.0", verbose_name="风控规则版本")

    STATUS_CHOICES = [
        ("pending", "待审核"),
        ("approved", "已批准"),
        ("superseded", "已被新版本替代"),
        ("rejected", "已驳回"),
    ]
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending", db_index=True, verbose_name="版本审核状态")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="提交时间")
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="审核时间")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="审核人",
    )

    class Meta:
        verbose_name = "评论版本"
        verbose_name_plural = "评论版本"
        ordering = ["comment", "-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["comment", "version_number"],
                name="uniq_comment_version_num",
            ),
            models.UniqueConstraint(
                fields=["comment"],
                condition=models.Q(status="pending"),
                name="uniq_comment_one_pending_version",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.raw_content.encode("utf-8")).hexdigest()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Version #{self.version_number} of Comment #{self.comment_id} ({self.status})"


class CommentReaction(models.Model):
    """
    评论点赞/点踩互动记录 (C-05)。
    同一用户对单条评论仅有一条互斥记录。
    """
    comment = models.ForeignKey(
        Comment,
        on_delete=models.CASCADE,
        related_name="reactions",
        verbose_name="目标评论",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="comment_reactions",
        verbose_name="用户",
    )
    REACTION_CHOICES = [
        (1, "👍 点赞"),
        (-1, "👎 点踩"),
    ]
    reaction_type = models.SmallIntegerField(choices=REACTION_CHOICES, verbose_name="互动类型")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="互动时间")

    class Meta:
        verbose_name = "评论点赞互动"
        verbose_name_plural = "评论点赞互动"
        constraints = [
            models.UniqueConstraint(
                fields=["comment", "user"],
                name="uniq_comment_user_reaction",
            ),
        ]

    def __str__(self):
        action = "赞" if self.reaction_type == 1 else "踩"
        return f"{self.user.username} {action} Comment #{self.comment_id}"


class CommentReviewAction(models.Model):
    """评论审核与治理追加式审计日志模型"""
    comment = models.ForeignKey(
        Comment,
        on_delete=models.CASCADE,
        related_name="audit_logs",
        verbose_name="目标评论",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        verbose_name="操作人",
    )
    action = models.CharField(max_length=32, verbose_name="操作动作")
    version_number = models.PositiveIntegerField(null=True, blank=True, verbose_name="目标版本号")
    previous_status = models.CharField(max_length=16, verbose_name="操作前状态")
    new_status = models.CharField(max_length=16, verbose_name="操作后状态")
    reason = models.TextField(blank=True, verbose_name="审核原因/批注")
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name="操作人IP")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="操作时间")

    class Meta:
        verbose_name = "评论审核审计日志"
        verbose_name_plural = "评论审核审计日志"
        ordering = ["-created_at"]

    def __str__(self):
        actor_name = self.actor.username if self.actor else "系统"
        return f"Audit: {actor_name} {self.action} on Comment #{self.comment_id} at {self.created_at}"


class PageCommentSettings(models.Model):
    """单篇页面评论运行控制模型 (就地开关、单篇状态代次)"""
    page = models.OneToOneField(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="comment_settings",
        verbose_name="所属页面",
    )
    allow_comments = models.BooleanField(default=True, verbose_name="允许发表评论")
    allow_guest_comments = models.BooleanField(default=False, verbose_name="允许受控访客留言")
    close_reason = models.CharField(max_length=255, blank=True, verbose_name="关评公告说明")
    auto_moderate_new = models.BooleanField(default=False, verbose_name="新评论强制人工复核")
    state_generation = models.PositiveIntegerField(default=1, verbose_name="状态代次")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="最后配置时间")

    class Meta:
        verbose_name = "单篇页面评论配置"
        verbose_name_plural = "单篇页面评论配置"

    def __str__(self):
        return f"CommentSettings for Page #{self.page_id} (allowed={self.allow_comments})"


@register_setting
class CommunitySettings(BaseGenericSetting):
    """
    全站社区评论准入与双轨配额设置 (Wagtail Generic Settings 单例)。
    不混入导航/页脚设置，仅管理员或被授予 comments.change_communitysettings 权限者可修改。
    """

    COMMENT_MODE_CHOICES = [
        ("closed", "全站暂停新增评论"),
        ("members_only", "仅限登录会员发表评论（默认）"),
        ("controlled_guests", "允许指定文章开启受控访客留言"),
    ]

    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    comment_mode = models.CharField(
        max_length=24,
        choices=COMMENT_MODE_CHOICES,
        default="members_only",
        verbose_name="全站评论准入模式",
        help_text="收紧规则：仅当全站设为 controlled_guests 且单篇文章勾选允许访客留言时，访客入口才生效。",
    )
    guest_daily_ip_cap = models.PositiveIntegerField(default=5, verbose_name="访客单 IP 每日留言上限")
    guest_daily_browser_cap = models.PositiveIntegerField(default=2, verbose_name="访客单浏览器每日留言上限")
    member_daily_cap = models.PositiveIntegerField(default=30, verbose_name="可信会员每日评论上限")
    new_member_daily_cap = models.PositiveIntegerField(default=10, verbose_name="新注册会员每日评论上限")
    new_member_days_threshold = models.PositiveIntegerField(default=7, verbose_name="新注册会员观察期（天）")
    new_member_default_pending = models.BooleanField(default=True, verbose_name="新注册会员发表评论默认人工待审")
    max_pending_queue_budget = models.PositiveIntegerField(
        default=200,
        verbose_name="全站待审队列熔断预算",
        help_text="当全站待审评论总数达到该阈值时，自动阻断新的匿名访客留言投稿。",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="最后更新时间")

    panels = [
        MultiFieldPanel(
            [
                FieldPanel("comment_mode"),
                FieldPanel("max_pending_queue_budget"),
            ],
            heading="全站准入模式与队列熔断",
        ),
        MultiFieldPanel(
            [
                FieldPanel("guest_daily_browser_cap"),
                FieldPanel("guest_daily_ip_cap"),
            ],
            heading="受控访客每日配额",
        ),
        MultiFieldPanel(
            [
                FieldPanel("new_member_days_threshold"),
                FieldPanel("new_member_daily_cap"),
                FieldPanel("new_member_default_pending"),
                FieldPanel("member_daily_cap"),
            ],
            heading="注册会员分级配额与审核",
        ),
    ]

    class Meta:
        verbose_name = "社区评论与防刷设置"
        verbose_name_plural = "社区评论与防刷设置"
        constraints = [
            models.CheckConstraint(
                condition=Q(singleton_key=1),
                name="comments_community_settings_singleton_one",
            ),
        ]

    @classmethod
    def _get_or_create(cls):
        obj, _ = cls.objects.get_or_create(singleton_key=1)
        return obj

    @classmethod
    def get_solo(cls) -> "CommunitySettings":
        """只读安全读取当前全站社区设置，未初始化时返回内存默认实例而不触发写库。"""
        obj = cls.objects.filter(singleton_key=1).first()
        return obj if obj is not None else cls()

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext('myblog:comments:community_settings'));"
                    )
            return super().save(*args, **kwargs)


class CommentDailyQuota(models.Model):
    """
    PostgreSQL 持久评论日配额计数表。
    按站点自然日统计，杜绝 Redis 重启或清理后配额被清零绕过。
    """

    SUBJECT_KIND_CHOICES = [
        ("user", "注册会员"),
        ("ip", "客户端 IP"),
        ("guest_browser", "访客浏览器标识"),
    ]

    site = models.ForeignKey(
        "wagtailcore.Site",
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name="所属站点",
    )
    subject_kind = models.CharField(max_length=16, choices=SUBJECT_KIND_CHOICES, verbose_name="主体类别")
    subject_key = models.CharField(max_length=64, verbose_name="主体标识")
    quota_date = models.DateField(verbose_name="统计自然日")
    action = models.CharField(max_length=32, default="post_comment", verbose_name="限额动作")
    used_count = models.PositiveIntegerField(default=0, verbose_name="已用次数")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="最后更新时间")

    class Meta:
        verbose_name = "评论每日配额计数"
        verbose_name_plural = "评论每日配额计数"
        constraints = [
            models.UniqueConstraint(
                fields=("site", "subject_kind", "subject_key", "quota_date", "action"),
                name="uniq_comment_daily_quota",
            ),
            models.CheckConstraint(
                condition=Q(used_count__gte=0),
                name="chk_comment_daily_quota_non_neg",
            ),
        ]
        indexes = [
            models.Index(
                fields=("quota_date", "subject_kind", "subject_key"),
                name="idx_comment_daily_quota_lookup",
            ),
        ]

    def __str__(self):
        return f"Quota({self.site_id}, {self.subject_kind}:{self.subject_key}, {self.quota_date})={self.used_count}"


class CommentGovernanceAuditLog(models.Model):
    """评论主体限制/解封审计日志。"""

    ACTION_CHOICES = [
        ("restrict", "实施 24 小时限制"),
        ("lift_restriction", "提前解除限制"),
    ]

    kind = models.CharField(max_length=8, verbose_name="主体类别")
    subject_key = models.CharField(max_length=64, db_index=True, verbose_name="规范化主体标识")
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, db_index=True, verbose_name="治理动作")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="操作人",
    )
    actor_username_snapshot = models.CharField(max_length=150, blank=True, verbose_name="操作人账号快照")
    reason = models.CharField(max_length=255, blank=True, verbose_name="操作原因")
    restricted_until = models.DateTimeField(null=True, blank=True, verbose_name="限制截止时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="记录时间")

    class Meta:
        verbose_name = "评论主体治理审计日志"
        verbose_name_plural = "评论主体治理审计日志"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.kind}:{self.subject_key} by {self.actor_username_snapshot or 'system'}"


class CommentGovernanceSubject(models.Model):
    """治理主体限制模型 (24小时禁言/风控限制)"""
    KIND_CHOICES = [
        ("user", "用户"),
        ("ip", "IP地址"),
    ]
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, verbose_name="主体类别")
    subject_key = models.CharField(max_length=64, db_index=True, verbose_name="规范化主体标识")  # user:<id> 或 ip:<ip>
    restricted_until = models.DateTimeField(db_index=True, verbose_name="限制截止时间")
    generation = models.PositiveIntegerField(default=1, verbose_name="主体代次")
    reason = models.CharField(max_length=255, blank=True, verbose_name="治理原因")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="操作人",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="最后更新时间")

    class Meta:
        verbose_name = "评论治理主体限制"
        verbose_name_plural = "评论治理主体限制"
        constraints = [
            models.UniqueConstraint(fields=["kind", "subject_key"], name="uniq_comment_gov_subject"),
        ]

    def __str__(self):
        return f"GovSubject {self.kind}:{self.subject_key} until {self.restricted_until}"


class CommentGovernanceBatch(models.Model):
    """全站跨页面批量治理批次模型"""
    batch_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, verbose_name="批次UUID")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="执行人",
    )
    action = models.CharField(max_length=32, verbose_name="批量治理动作")  # e.g., 'batch_hide'
    filter_summary = models.JSONField(default=dict, blank=True, verbose_name="筛选条件摘要")
    snapshot_time = models.DateTimeField(auto_now_add=True, verbose_name="快照截止时间")
    progress = models.CharField(max_length=16, default="running", verbose_name="批次进度")  # running, completed, partial_failed
    success_count = models.PositiveIntegerField(default=0, verbose_name="成功条数")
    conflict_count = models.PositiveIntegerField(default=0, verbose_name="冲突跳过数")
    failure_count = models.PositiveIntegerField(default=0, verbose_name="失败条数")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "评论批量治理批次"
        verbose_name_plural = "评论批量治理批次"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Batch #{self.batch_id} ({self.action}): {self.success_count} success, {self.conflict_count} conflict"


class CommentGovernanceItem(models.Model):
    """批量治理具体条目明细模型"""
    batch = models.ForeignKey(
        CommentGovernanceBatch,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="所属批次",
    )
    comment = models.ForeignKey(
        Comment,
        on_delete=models.CASCADE,
        related_name="governance_items",
        verbose_name="目标评论",
    )
    expected_version = models.ForeignKey(
        CommentVersion,
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,  # B-01 闭环
        related_name="+",
        verbose_name="期望版本",
    )
    expected_generation = models.PositiveIntegerField(default=1, verbose_name="期望代次")
    status = models.CharField(max_length=16, default="pending", verbose_name="处理状态")  # success, conflict, failed, skipped
    result_message = models.CharField(max_length=255, blank=True, verbose_name="处理结果说明")

    class Meta:
        verbose_name = "批量治理明细项"
        verbose_name_plural = "批量治理明细项"
        constraints = [
            models.UniqueConstraint(fields=["batch", "comment"], name="uniq_gov_batch_comment"),
        ]

    def __str__(self):
        return f"GovItem {self.batch_id} -> Comment #{self.comment_id}: {self.status}"


class CommentMutationReceipt(models.Model):
    """评论写入请求幂等凭据表 (防止重复发评/重放攻击)"""
    subject_key = models.CharField(max_length=64, db_index=True, verbose_name="主体标识")
    action = models.CharField(max_length=32, verbose_name="动作")
    request_key = models.CharField(max_length=64, db_index=True, verbose_name="请求幂等键")
    request_hash = models.CharField(max_length=64, verbose_name="请求参数SHA256哈希")
    result_comment_id = models.IntegerField(null=True, blank=True, verbose_name="结果评论ID")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "评论写入幂等凭据"
        verbose_name_plural = "评论写入幂等凭据"
        constraints = [
            models.UniqueConstraint(fields=["subject_key", "action", "request_key"], name="uniq_comm_mutation_receipt"),
        ]

    def __str__(self):
        return f"Receipt {self.subject_key}:{self.action}:{self.request_key}"
