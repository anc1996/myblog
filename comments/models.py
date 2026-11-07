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
from datetime import timedelta
from django.conf import settings
from django.db import models
from django.db.models import Q, F
from django.core.exceptions import PermissionDenied
from django.utils import timezone


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
        on_delete=models.SET_NULL,
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

    # 5. 版本指针
    latest_version_id = models.IntegerField(null=True, blank=True, verbose_name="最新提交版本ID")
    approved_version_id = models.IntegerField(null=True, blank=True, verbose_name="当前生效批准版本ID")
    pending_version_id = models.IntegerField(null=True, blank=True, verbose_name="当前待审版本ID")

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
        ]

    def delete(self, *args, allow_physical_purge=False, **kwargs):
        """禁止普通物理删除，强制通过业务服务软删除 (C-06)"""
        if not allow_physical_purge:
            raise PermissionDenied("禁止物理硬删评论记录！必须调用 CommentService.soft_delete_comment 执行软删除。")
        return super().delete(*args, **kwargs)

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def is_editable(self) -> bool:
        return bool(self.can_edit_until and timezone.now() <= self.can_edit_until)

    def __str__(self):
        page_str = self.page.title if self.page else self.page_title_snapshot
        author_str = self.author.username if self.author else "匿名/已注销"
        return f"Comment #{self.id} on '{page_str}' by {author_str} ({self.status})"


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
