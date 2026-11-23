"""
通用多页面 Markdown 导入持久化模型：
1. MarkdownImportToken: 专用于本地导入客户端的短期 Bearer Token（仅存 SHA-256 摘要，绝不存明文）；
2. MarkdownImportPairCode: 后台浏览器签发的一次性短期配对码，用于安全兑换客户端 Token；
3. ImportJob: 分阶段导入作业与 (actor, site, idempotency_key) 整单幂等锁台账；
4. ImportAsset: 逐项资源台账，标记 is_staging=True 直到 commit 成功才解除暂存对账隔离。
"""

import uuid
from django.conf import settings
from django.db import models


class ImportJobStatus(models.TextChoices):
    UPLOADING = "uploading", "上传资源中"
    READY = "ready", "资源就绪"
    PREPARING = "preparing", "准备外部存储中"
    DRAFT_CREATED = "draft_created", "已创建草稿"
    RETRYABLE_FAILED = "retryable_failed", "可重试失败"
    EXPIRED = "expired", "已过期"
    ABORTED = "aborted", "已中止"


class MarkdownImportToken(models.Model):
    """受控导入专用令牌（仅记录 SHA-256 摘要，绑定用户、站点与过期时间）。"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="markdown_import_tokens",
        verbose_name="所属账号",
    )
    site = models.ForeignKey(
        "wagtailcore.Site",
        on_delete=models.CASCADE,
        related_name="markdown_import_tokens",
        verbose_name="关联站点",
    )
    name = models.CharField(max_length=100, default="本地导入客户端", verbose_name="令牌备注")
    prefix = models.CharField(max_length=16, db_index=True, verbose_name="前缀标识")
    token_hash = models.CharField(max_length=64, unique=True, verbose_name="令牌SHA256摘要")
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="是否启用")
    session_epoch = models.PositiveBigIntegerField(default=1, verbose_name="签发时账号会话代次")
    expires_at = models.DateTimeField(db_index=True, verbose_name="过期时间")
    revoked_at = models.DateTimeField(null=True, blank=True, verbose_name="吊销时间")
    last_used_at = models.DateTimeField(null=True, blank=True, verbose_name="最近使用时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "Markdown 导入令牌"
        verbose_name_plural = "Markdown 导入令牌"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} - {self.name} ({self.prefix}...)"


class MarkdownImportPairCode(models.Model):
    """后台页面签发的 10 分钟一次性配对码，用于桌面客户端免手填长 Token 登录。"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="markdown_import_pair_codes",
    )
    site = models.ForeignKey(
        "wagtailcore.Site",
        on_delete=models.CASCADE,
        related_name="markdown_import_pair_codes",
    )
    code_hash = models.CharField(max_length=64, unique=True)
    session_epoch = models.PositiveBigIntegerField(default=1)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "导入配对码"
        verbose_name_plural = "导入配对码"
        ordering = ["-created_at"]


class ImportJob(models.Model):
    """分阶段 Markdown 导入作业台账与整单幂等控制表。"""
    job_id = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True, verbose_name="作业UUID")
    document_uuid = models.UUIDField(default=uuid.uuid4, db_index=True, verbose_name="预分配文档UUID")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="markdown_import_jobs",
        verbose_name="操作人",
    )
    site = models.ForeignKey(
        "wagtailcore.Site",
        on_delete=models.CASCADE,
        related_name="markdown_import_jobs",
        verbose_name="目标站点",
    )
    idempotency_key = models.CharField(max_length=128, verbose_name="客户端幂等键")
    input_digest = models.CharField(max_length=64, verbose_name="请求内容SHA256摘要")
    type_key = models.CharField(max_length=64, verbose_name="页面适配器类型键")
    parent_page_id = models.IntegerField(verbose_name="目标父栏目ID")
    source_name = models.CharField(max_length=255, default="untitled.md", verbose_name="源文件名")
    payload_json = models.JSONField(default=dict, verbose_name="经服务端冻结的规范化草稿载荷")
    status = models.CharField(
        max_length=32,
        choices=ImportJobStatus.choices,
        default=ImportJobStatus.UPLOADING,
        db_index=True,
        verbose_name="作业状态",
    )
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    result_page_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="已创建草稿页ID")
    result_revision_id = models.IntegerField(null=True, blank=True, verbose_name="已创建版本ID")
    error_code = models.CharField(max_length=64, blank=True, default="")
    expires_at = models.DateTimeField(db_index=True, verbose_name="暂存过期时间")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Markdown 导入作业"
        verbose_name_plural = "Markdown 导入作业"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=("actor", "site", "idempotency_key"),
                name="uniq_markdown_import_job_idem",
            ),
        ]

    def __str__(self):
        return f"ImportJob({self.job_id}, {self.type_key}, {self.status})"


class ImportAsset(models.Model):
    """导入作业下的单个媒体暂存台账；commit 成功前保持 is_staging=True 以协调对账隔离。"""
    job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="assets",
        verbose_name="所属作业",
    )
    asset_key = models.CharField(max_length=255, verbose_name="相对路径键")
    media_type = models.CharField(max_length=16, verbose_name="媒体类型")  # "image" | "document"
    filename = models.CharField(max_length=255, verbose_name="文件名")
    expected_size = models.BigIntegerField(verbose_name="声明大小")
    expected_sha256 = models.CharField(max_length=64, verbose_name="声明SHA256")
    actual_size = models.BigIntegerField(null=True, blank=True, verbose_name="实测大小")
    actual_sha256 = models.CharField(max_length=64, blank=True, default="", verbose_name="实测SHA256")
    object_key = models.CharField(max_length=512, blank=True, default="", verbose_name="存储对象Key")
    target_public_id = models.UUIDField(null=True, blank=True, db_index=True, verbose_name="已创建媒体public_id")
    is_staging = models.BooleanField(default=True, db_index=True, verbose_name="是否仍为未提交暂存态")
    uploaded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "导入作业媒体项"
        verbose_name_plural = "导入作业媒体项"
        constraints = [
            models.UniqueConstraint(
                fields=("job", "asset_key"),
                name="uniq_markdown_import_job_asset",
            ),
        ]
