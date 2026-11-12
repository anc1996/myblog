import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.utils import timezone


class User(AbstractUser):
    """
    全站账号主表（AUTH_USER_MODEL）。
    负责登录凭证、启停状态（is_active）、用户组（Group）与权限（Permission）管理。
    普通会员前台注册默认 is_staff=False，绝不授予后台权限。
    对外公开的作者等人物资料由独立的 people.Person 管理，两者严格解耦。
    """

    class Meta:
        verbose_name = "后台用户"
        verbose_name_plural = "后台用户"

    @property
    def display_name(self) -> str:
        try:
            profile = self.member_profile
        except Exception:
            profile = None
        if profile is not None:
            return profile.display_name
        return self.username


class MemberProfile(models.Model):
    """
    全站会员前台资料、独立头像元数据与会话代次表（一对一挂载 identity.User）。
    """

    user = models.OneToOneField(
        User,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="member_profile",
        verbose_name="所属账号",
    )
    nickname = models.CharField(max_length=50, blank=True, verbose_name="展示昵称")
    bio = models.CharField(max_length=300, blank=True, verbose_name="个人简介")
    recovery_hint = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="人工核密线索",
        help_text="仅超级管理员在后台核实忘记密码身份时可见，绝不对外公开输出",
    )
    must_change_password = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="强制改密标志",
    )
    session_epoch = models.PositiveBigIntegerField(
        default=1,
        verbose_name="会话有效代次",
        help_text="改密、撤权、停用或角色变更时原子递增，使全站旧会话在下次请求立即失效",
    )
    role_updated_at = models.DateTimeField(null=True, blank=True, verbose_name="角色更新时间")
    avatar_token = models.UUIDField(
        null=True,
        blank=True,
        unique=True,
        verbose_name="头像网关访问令牌",
    )
    avatar_version = models.PositiveBigIntegerField(default=0, verbose_name="头像版本号")
    avatar_storage_alias = models.CharField(
        max_length=64,
        default="default",
        blank=True,
        verbose_name="头像存储别名",
    )
    avatar_storage_namespace = models.CharField(
        max_length=64,
        blank=True,
        verbose_name="头像存储命名空间",
    )
    avatar_key = models.CharField(
        max_length=1024,
        blank=True,
        verbose_name="头像对象存储键",
    )
    avatar_updated_at = models.DateTimeField(null=True, blank=True, verbose_name="头像更新时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "会员资料"
        verbose_name_plural = "会员资料"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(avatar_key="")
                    | (
                        Q(avatar_token__isnull=False)
                        & ~Q(avatar_storage_namespace="")
                        & Q(avatar_version__gt=0)
                    )
                ),
                name="identity_memberprofile_avatar_consistent",
            ),
            models.UniqueConstraint(
                fields=("avatar_storage_namespace", "avatar_key"),
                condition=~Q(avatar_key=""),
                name="uniq_memberprofile_avatar_key",
            ),
        ]

    @classmethod
    def get_or_create_for_user(cls, user: User) -> "MemberProfile":
        profile, _ = cls.objects.get_or_create(user=user)
        return profile

    @property
    def display_name(self) -> str:
        clean_nick = (self.nickname or "").strip()
        return clean_nick or self.user.username

    @property
    def has_active_avatar(self) -> bool:
        return bool(self.avatar_token and self.avatar_key and self.user.is_active)

    @property
    def avatar_url(self) -> str:
        if not self.has_active_avatar:
            return ""
        return f"/avatar-gateway/{self.avatar_token}/?v={self.avatar_version}"

    def __str__(self) -> str:
        return f"MemberProfile({self.user.username}, epoch={self.session_epoch})"


class AvatarPurgeTask(models.Model):
    """
    独立头像物理清理任务表（不依赖 User 级联外键）。
    记录需要异步擦除的旧头像或注销用户头像，经核查无任何在用引用后调用受控物理删除接口。
    """

    SOURCE_CHOICES = [
        ("avatar_replace", "更换头像"),
        ("account_delete", "注销账号"),
        ("legacy_migration", "旧头像迁移"),
        ("orphan_reconcile", "孤儿对账"),
    ]
    STATUS_CHOICES = [
        ("pending", "待处理"),
        ("processing", "处理中"),
        ("blocked", "引用阻断"),
        ("done", "已完成"),
        ("failed", "失败"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    storage_alias = models.CharField(max_length=64, default="default", verbose_name="存储别名")
    storage_namespace = models.CharField(max_length=64, verbose_name="存储命名空间")
    storage_key = models.CharField(max_length=1024, verbose_name="对象存储键")
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, verbose_name="来源")
    operation_id = models.UUIDField(db_index=True, verbose_name="操作唯一ID")
    target_user_id_snapshot = models.BigIntegerField(null=True, blank=True, verbose_name="目标账号ID快照")
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default="pending",
        db_index=True,
        verbose_name="任务状态",
    )
    attempts = models.PositiveIntegerField(default=0, verbose_name="尝试次数")
    next_attempt_at = models.DateTimeField(default=timezone.now, verbose_name="下次重试时间")
    lease_token = models.UUIDField(null=True, blank=True, verbose_name="租约令牌")
    lease_until = models.DateTimeField(null=True, blank=True, verbose_name="租约截止时间")
    last_error = models.TextField(blank=True, verbose_name="最近错误")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="完成时间")

    class Meta:
        verbose_name = "头像物理清理任务"
        verbose_name_plural = "头像物理清理任务"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=("storage_namespace", "storage_key"),
                condition=Q(status__in=["pending", "processing", "blocked", "failed"]),
                name="uniq_active_avatar_purge_task",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="idx_avatar_purge_queue"),
            models.Index(fields=["status", "lease_until"], name="idx_avatar_purge_lease"),
        ]

    def __str__(self) -> str:
        return f"AvatarPurgeTask({self.source}, {self.storage_key}, {self.status})"


class AccountAuditLog(models.Model):
    """
    账号生命周期审计日志（绝不记录明文密码、令牌或会话密钥）。
    """

    ACTION_CHOICES = [
        ("deactivate", "停用账号"),
        ("reactivate", "恢复账号"),
        ("admin_reset_password", "管理员重置密码"),
        ("self_change_password", "用户自助改密"),
        ("hard_delete", "受控彻底删除"),
        ("role_change", "权限或角色变更"),
    ]

    action = models.CharField(max_length=32, choices=ACTION_CHOICES, db_index=True, verbose_name="操作动作")
    actor_id_snapshot = models.BigIntegerField(null=True, blank=True, verbose_name="操作人ID快照")
    actor_username_snapshot = models.CharField(max_length=150, blank=True, verbose_name="操作人账号快照")
    target_user_id_snapshot = models.BigIntegerField(db_index=True, verbose_name="目标用户ID快照")
    target_username_snapshot = models.CharField(max_length=150, verbose_name="目标账号快照")
    operation_id = models.UUIDField(default=uuid.uuid4, db_index=True, verbose_name="操作唯一ID")
    details = models.JSONField(default=dict, blank=True, verbose_name="非敏感审计摘要")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="记录时间")

    class Meta:
        verbose_name = "账号治理审计日志"
        verbose_name_plural = "账号治理审计日志"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action}: {self.target_username_snapshot} (op={self.operation_id})"
