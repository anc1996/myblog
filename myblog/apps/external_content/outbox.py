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
        """
        派发并执行单个 Outbox 任务事件（跨存储最终一致性的执行中枢）。

        【原理流程详解（教学导读）】：
        1. 为什么不直接在业务代码里调用 Mongo/搜索索引更新/文件删除？
           因为“跨存储双写”是不具备原子性的。如果在 SQL 事务里直接发网络请求删文件或写 Mongo，
           一旦网络超时或者 SQL 回滚，外部存储就会出现“幽灵脏数据”或“数据不一致”。
        2. 事务性外发信箱（Transactional Outbox）的设计精髓：
           在业务操作发生时，先把要干的“待办任务”（OutboxEvent）和业务数据写进同一个 PostgreSQL 本地事务。
           事务提交成功后，该方法（消费者）才独立拉取事件逐个执行。即使执行外部系统时崩溃，也能安全重试。
        3. 处理状态闭环：
           - 执行成功 -> 标记 processed 并打上处理时间戳；
           - 遇到异常 -> 捕获记录日志，递增 retry_count（最多重试 5 次，超限转为 failed 死信待人工排查），
             严格避免单个坏事件卡死整条任务队列。
        """
        try:
            event_type = event.event_type
            page_id = event.page_id

            # ==============================================================
            # 事件分支 1：页面发布（page.published）
            # 教学点：文章发布后，需要将标题、摘要、正文分块投影到全站搜索表（PublishedSearchDocument）
            # ==============================================================
            if event_type == "page.published":
                from discovery.projection import project_page_to_discovery
                try:
                    # 使用 .specific 将 Wagtail 通用 Page 转换为具体的子类实例（如 BlogPage/GuidePage）
                    page = Page.objects.get(id=page_id).specific
                    # 执行投影：提取纯文本、分词切块、计算 GIN 倒排索引，并递增全站发现 Epoch
                    project_page_to_discovery(page)
                except Page.DoesNotExist:
                    # 容错：若极端情况下页面已被物理删除，记录告警但不抛出崩溃，保证事件可正常结束
                    logger.warning("Page %s does not exist for page.published outbox event", page_id)

            # ==============================================================
            # 事件分支 2：页面下架或软删除入回收站（page.unpublished / page.recycled）
            # 教学点：内容一旦对公众不可见，搜索索引与 RSS 订阅必须立刻同步下架，保护数据隐私
            # ==============================================================
            elif event_type in ("page.unpublished", "page.recycled"):
                from discovery.projection import unpublish_page_discovery, recycle_page_discovery
                if event_type == "page.recycled":
                    # 移入回收站：将搜索副本标记为已回收，并保留原路径元数据供未来原位恢复
                    recycle_page_discovery(page_id)
                else:
                    # 撤销发布：将搜索副本从公开索引中隐藏
                    unpublish_page_discovery(page_id)

            # ==============================================================
            # 事件分支 3：页面超过 30 天保留期后彻底销毁（page.purged）
            # 教学点：执行不可逆的“物理清除”，级联清理跨系统关联数据（MongoDB 与媒体引用）
            # ==============================================================
            elif event_type == "page.purged":
                from external_content.store import MongoContentStore
                from media_library.models import MediaReferenceRecord
                payload = event.payload or {}
                doc_uuid = payload.get("document_uuid")
                if doc_uuid:
                    store = MongoContentStore()
                    try:
                        # 物理清除 MongoDB 中该 document_uuid 对应的全部历史正文快照
                        store.purge_document_versions(doc_uuid)
                    finally:
                        store.close()
                # 级联清理该页面的全部历史媒体引用记录，防止媒体孤儿锁定
                MediaReferenceRecord.objects.filter(page_id=page_id).delete()

            # ==============================================================
            # 事件分支 4：媒体物理文件受控延迟销毁（media.deleting）
            # 教学点：全站零引用安全网！图片绝不能在删除页面时就贸然物理删除，必须核查历史引用
            # ==============================================================
            elif event_type == "media.deleting":
                from media_library.references import MediaReferenceTracker
                from django.core.files.storage import default_storage
                payload = event.payload or {}
                asset_id = payload.get("asset_id")
                storage_key = payload.get("storage_key")
                if asset_id and storage_key:
                    # 双重核查：再次全表扫描该媒体资源是否已被全站所有页面彻底解除引用
                    can_del, _, refs = MediaReferenceTracker.check_asset_references(asset_id)
                    # 只有确认在整个站点中引用计数严格为 0 时，才允许执行底层对象存储的真实物理删除
                    if can_del and len(refs) == 0:
                        if hasattr(default_storage, "_controlled_physical_delete"):
                            default_storage._controlled_physical_delete(storage_key)

            # ==============================================================
            # 状态更新：处理成功
            # 教学点：将事件标记为 processed，打上完成时间戳，防止被轮询 worker 重复消费
            # ==============================================================
            event.status = "processed"
            event.processed_at = timezone.now()
            event.save(update_fields=["status", "processed_at"])
            return True

        except Exception as exc:
            # ==============================================================
            # 异常与死信重试机制（Dead Letter Queue）
            # 教学点：外部依赖（如网络抖动、Mongo/S3 暂时不可用）可能失败。
            # 递增重试次数；达到 5 次上限后转为 failed，避免死循环阻塞整个队列，等待管理员对账
            # ==============================================================
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
