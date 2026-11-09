"""
PostgreSQL 内部操作回执表 (OperationResult) 与待处理任务表 (OutboxEvent)。

1. OperationResult: 记录每次写操作的幂等执行回执，绑定操作者、动作、页面及输入摘要；
2. OutboxEvent: 与业务数据处于同一事务中的事务性外发信箱 (Transactional Outbox)，
   负责在发布、下架、回收时异步触发搜索索引与缓存更新，保证跨存储最终一致性。
"""

import uuid
from django.db import models


class OperationResult(models.Model):
    """操作结果回执表：用于超时重试的幂等判定与状态查询"""
    operation_id = models.CharField(max_length=64, unique=True, db_index=True, verbose_name="操作ID")
    actor_id = models.IntegerField(null=True, blank=True, verbose_name="操作人ID")
    action = models.CharField(max_length=64, verbose_name="动作类型")
    page_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="关联页面ID")
    input_hash = models.CharField(max_length=64, verbose_name="输入摘要SHA256")
    result_json = models.JSONField(default=dict, verbose_name="执行结果快照")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="记录时间")

    class Meta:
        verbose_name = "操作回执"
        verbose_name_plural = "操作回执"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Operation {self.operation_id} ({self.action})"


class OutboxEvent(models.Model):
    """事务性 Outbox 任务事件：与页面状态在同一个 SQL 事务中提交"""
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True, verbose_name="事件唯一ID")
    event_type = models.CharField(max_length=64, db_index=True, verbose_name="事件类型")
    page_id = models.IntegerField(db_index=True, verbose_name="页面ID")
    revision_id = models.IntegerField(null=True, blank=True, verbose_name="版本ID")
    publication_generation = models.BigIntegerField(default=1, verbose_name="发布代次序号")
    payload = models.JSONField(default=dict, verbose_name="事件载荷")
    status = models.CharField(
        max_length=16,
        choices=[("pending", "待处理"), ("processed", "已处理"), ("failed", "失败待重试")],
        default="pending",
        db_index=True,
        verbose_name="状态",
    )
    retry_count = models.IntegerField(default=0, verbose_name="重试次数")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="入队时间")
    processed_at = models.DateTimeField(null=True, blank=True, verbose_name="处理时间")

    class Meta:
        verbose_name = "Outbox事件"
        verbose_name_plural = "Outbox事件"
        ordering = ["created_at"]

    def __str__(self):
        return f"OutboxEvent {self.event_type} on page={self.page_id} status={self.status}"
