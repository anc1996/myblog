"""
独立头像物理清理任务队列与租约处理器 (AvatarPurgeProcessor)。

核心契约：
1. 任务记录独立存活，不依赖已删除用户的级联外键；
2. 处理器先用短事务领取租约并释放任务行锁，再执行外部 RustFS/存储核查与删除；
3. 删除前再次核实该对象键未在 MemberProfile / UserProfile 中被重新绑定或引用；
4. 仅通过受控接口 controlled_delete_storage_object 删除对象；对象已不存在按成功结束。
"""

from datetime import timedelta
import logging
from typing import Any
import uuid

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from identity.avatar_storage import controlled_delete_storage_object, get_storage_and_namespace
from identity.models import AvatarPurgeTask, MemberProfile

logger = logging.getLogger(__name__)


def enqueue_avatar_purge_task(
    *,
    storage_alias: str,
    storage_namespace: str,
    storage_key: str,
    source: str,
    operation_id: uuid.UUID,
    target_user_id_snapshot: int | None = None,
) -> AvatarPurgeTask | None:
    """幂等入队头像清理任务，防止同一命名空间与对象键重复排队。"""
    clean_key = (storage_key or "").strip()
    if not clean_key:
        return None
    active_statuses = ["pending", "processing", "blocked", "failed"]
    existing = AvatarPurgeTask.objects.filter(
        storage_namespace=storage_namespace,
        storage_key=clean_key,
        status__in=active_statuses,
    ).first()
    if existing:
        return existing
    return AvatarPurgeTask.objects.create(
        storage_alias=storage_alias or "default",
        storage_namespace=storage_namespace,
        storage_key=clean_key,
        source=source,
        operation_id=operation_id,
        target_user_id_snapshot=target_user_id_snapshot,
        status="pending",
    )


class AvatarPurgeProcessor:
    """异步/后台头像物理擦除消费器。"""

    @classmethod
    def _is_key_still_referenced(cls, storage_namespace: str, storage_key: str) -> bool:
        if MemberProfile.objects.filter(
            avatar_storage_namespace=storage_namespace,
            avatar_key=storage_key,
        ).exists():
            return True
        from django.apps import apps

        if apps.is_installed("wagtail.users"):
            from wagtail.users.models import UserProfile

            if UserProfile.objects.filter(avatar=storage_key).exists():
                return True
        return False

    @classmethod
    def process_pending_tasks(cls, batch_size: int = 50, lease_seconds: int = 60) -> dict[str, int]:
        """领取一批待处理或到期重试的清理任务并受控擦除存储文件。"""
        now = timezone.now()
        lease_token = uuid.uuid4()
        lease_until = now + timedelta(seconds=lease_seconds)

        with transaction.atomic():
            due_q = (
                Q(status="pending", next_attempt_at__lte=now)
                | Q(status="failed", next_attempt_at__lte=now)
                | Q(status="processing", lease_until__lt=now)
            )
            tasks = list(
                AvatarPurgeTask.objects.select_for_update(skip_locked=True)
                .filter(due_q)
                .order_by("created_at")[:batch_size]
            )
            task_ids = [t.id for t in tasks]
            if task_ids:
                AvatarPurgeTask.objects.filter(id__in=task_ids).update(
                    status="processing",
                    lease_token=lease_token,
                    lease_until=lease_until,
                )

        stats = {"claimed": len(task_ids), "done": 0, "blocked": 0, "failed": 0}
        for tid in task_ids:
            task = AvatarPurgeTask.objects.filter(id=tid).first()
            if not task or task.lease_token != lease_token:
                continue

            try:
                storage, current_ns = get_storage_and_namespace(task.storage_alias or "default")
                if current_ns != task.storage_namespace:
                    AvatarPurgeTask.objects.filter(id=tid, lease_token=lease_token).update(
                        status="blocked",
                        lease_token=None,
                        lease_until=None,
                        last_error=f"存储命名空间不匹配: 任务={task.storage_namespace}, 当前={current_ns}",
                    )
                    stats["blocked"] += 1
                    continue

                if cls._is_key_still_referenced(task.storage_namespace, task.storage_key):
                    AvatarPurgeTask.objects.filter(id=tid, lease_token=lease_token).update(
                        status="blocked",
                        lease_token=None,
                        lease_until=None,
                        last_error="对象键仍被会员资料或后台头像引用，拒绝物理删除",
                    )
                    stats["blocked"] += 1
                    continue

                if storage.exists(task.storage_key):
                    controlled_delete_storage_object(storage, task.storage_key)

                AvatarPurgeTask.objects.filter(id=tid, lease_token=lease_token).update(
                    status="done",
                    finished_at=timezone.now(),
                    lease_token=None,
                    lease_until=None,
                    last_error="",
                )
                stats["done"] += 1

            except Exception as exc:
                logger.exception("AvatarPurgeTask %s failed for key=%s: %s", tid, task.storage_key, exc)
                new_attempts = task.attempts + 1
                backoff_secs = min(3600, 30 * (2 ** min(new_attempts, 6)))
                AvatarPurgeTask.objects.filter(id=tid, lease_token=lease_token).update(
                    status="failed",
                    attempts=new_attempts,
                    next_attempt_at=timezone.now() + timedelta(seconds=backoff_secs),
                    lease_token=None,
                    lease_until=None,
                    last_error=str(exc)[:1000],
                )
                stats["failed"] += 1

        return stats
