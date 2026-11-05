"""
事务性外发信箱 (Transactional Outbox) 异步事件处理器。

落实跨存储最终一致性契约：
1. 消费在 SQL 事务中与业务数据一同落库的 OutboxEvent；
2. 异步派发：
   - page.published: 触发全站搜索副本投影与缓存更新；
   - page.unpublished / page.recycled: 触发全站搜索副本下架与 Epoch 递增；
   - media.deleting: 在确认全站零引用后调用受控物理删除；
3. 幂等与容错：旧代次事件不覆盖新代次，失败自动重试并记录异常。
"""

import logging
from typing import Any
from django.db import transaction
from django.utils import timezone
from wagtail.models import Page

from external_content.models import OutboxEvent

logger = logging.getLogger(__name__)


class OutboxProcessor:
    """Outbox 事件消费与调度处理器"""

    @classmethod
    def process_pending_events(cls, batch_size: int = 50) -> int:
        """批量拉取并处理待消费的 Outbox 事件"""
        with transaction.atomic():
            events = list(
                OutboxEvent.objects.select_for_update(skip_locked=True)
                .filter(status="pending")
                .order_by("created_at")[:batch_size]
            )

        processed_count = 0
        for event in events:
            success = cls.dispatch_event(event)
            if success:
                processed_count += 1
        return processed_count

    @classmethod
    def dispatch_event(cls, event: OutboxEvent) -> bool:
        """派发并执行单个 Outbox 事件（幂等保护）"""
        try:
            event_type = event.event_type
            page_id = event.page_id

            if event_type == "page.published":
                from discovery.projection import project_page_to_discovery
                try:
                    page = Page.objects.get(id=page_id).specific
                    project_page_to_discovery(page)
                except Page.DoesNotExist:
                    logger.warning("Page %s does not exist for page.published outbox event", page_id)

            elif event_type in ("page.unpublished", "page.recycled"):
                from discovery.projection import unpublish_page_discovery, recycle_page_discovery
                if event_type == "page.recycled":
                    recycle_page_discovery(page_id)
                else:
                    unpublish_page_discovery(page_id)

            elif event_type == "page.purged":
                from external_content.store import MongoContentStore
                from media_library.models import MediaReferenceRecord
                payload = event.payload or {}
                doc_uuid = payload.get("document_uuid")
                if doc_uuid:
                    store = MongoContentStore()
                    try:
                        store.purge_document_versions(doc_uuid)
                    finally:
                        store.close()
                # 级联清理该页面的全部历史媒体引用记录，防止媒体孤儿锁定
                MediaReferenceRecord.objects.filter(page_id=page_id).delete()

            elif event_type == "media.deleting":
                from media_library.references import MediaReferenceTracker
                from django.core.files.storage import default_storage
                payload = event.payload or {}
                asset_id = payload.get("asset_id")
                storage_key = payload.get("storage_key")
                if asset_id and storage_key:
                    can_del, _, refs = MediaReferenceTracker.check_asset_references(asset_id)
                    if can_del and len(refs) == 0:
                        if hasattr(default_storage, "_controlled_physical_delete"):
                            default_storage._controlled_physical_delete(storage_key)

            # 成功处理：标记 processed
            event.status = "processed"
            event.processed_at = timezone.now()
            event.save(update_fields=["status", "processed_at"])
            return True

        except Exception as exc:
            logger.exception("Failed to process OutboxEvent %s: %s", event.event_id, exc)
            event.retry_count += 1
            if event.retry_count >= 5:
                event.status = "failed"
            event.save(update_fields=["status", "retry_count"])
            return False

    @classmethod
    def replay_event(cls, event_id: Any) -> bool:
        """重放指定 Outbox 事件（供对账与灾难恢复使用）"""
        event = OutboxEvent.objects.get(event_id=event_id)
        return cls.dispatch_event(event)
