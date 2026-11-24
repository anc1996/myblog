"""
Wagtail 页面生命周期与全站内容发现信号监听器。

在页面发布、下架、权限变更及维度修改时自动触发搜索副本的同步与版本流转：
1. page_published: 触发 project_page_to_discovery(instance)；
2. page_unpublished: 触发 unpublish_page_discovery(instance.id)；
3. post_page_move / PageViewRestriction / Site / Person / Topic / Tag 变更:
   原子递增全局 ContentDiscoveryEpoch，驱动前台缓存、游标与 ETag 瞬间失效 (BLK-08)。
"""

import logging
from django.db import transaction
from django.dispatch import receiver
from django.db.models.signals import post_save, post_delete, m2m_changed
from wagtail.signals import page_published, page_unpublished, post_page_move, page_slug_changed
from wagtail.models import PageViewRestriction, Site
from taggit.models import Tag
from discovery.projection import project_page_to_discovery, unpublish_page_discovery, sync_subtree_tree_metadata
from discovery.models import ContentDiscoveryEpoch, PublishedSearchDocument


def _sync_restriction_affected_archives(restriction_instance) -> None:
    """PageViewRestriction 增删改时同步受影响页面的归档日历计数；大子树自动转入 Outbox。"""
    from discovery.archive_counts import (
        enqueue_subtree_archive_reconcile,
        sync_page_archive_contribution,
    )
    page = getattr(restriction_instance, "page", None)
    if not page or not getattr(page, "path", None):
        enqueue_subtree_archive_reconcile()
        return
    affected_ids = list(
        PublishedSearchDocument.objects.filter(
            root_path__startswith=page.path,
            is_live=True,
            is_recycled=False,
        ).values_list("page_id", flat=True)[:201]
    )
    if len(affected_ids) > 200:
        enqueue_subtree_archive_reconcile(root_path=page.path, page_id=page.id)
    else:
        for pid in affected_ids:
            sync_page_archive_contribution(pid)

logger = logging.getLogger(__name__)


@receiver(page_published)
def on_page_published(sender, instance, **kwargs):
    """页面正式上线：强制原子登记 Outbox 任务并尝试同步投影，投影异常时回滚内部 savepoint 并保留 pending 待办。"""
    from external_content.models import OutboxEvent

    page_id = getattr(instance, "id", None)
    rev_id = getattr(instance, "live_revision_id", None)
    event = OutboxEvent.objects.create(
        event_type="page.published",
        page_id=page_id,
        revision_id=rev_id,
        payload={"revision_id": rev_id},
        status="pending",
    )

    try:
        with transaction.atomic():
            project_page_to_discovery(instance)
        from django.utils import timezone
        OutboxEvent.objects.filter(pk=event.pk, status="pending").update(
            status="processed",
            processed_at=timezone.now(),
        )
    except Exception as exc:
        logger.exception("Failed to project published page %s to discovery: %s", page_id, exc)
        OutboxEvent.objects.filter(pk=event.pk).update(
            retry_count=event.retry_count + 1,
            payload={"revision_id": rev_id, "last_error": str(exc)[:500]},
        )


@receiver(page_unpublished)
def on_page_unpublished(sender, instance, **kwargs):
    """页面撤销发布：登记 Outbox 并同步下架"""
    from external_content.models import OutboxEvent

    page_id = getattr(instance, "id", None)
    try:
        unpublish_page_discovery(instance.id)
    except Exception as exc:
        logger.exception("Failed to mark unpublished page %s in discovery: %s", page_id, exc)
        try:
            OutboxEvent.objects.create(
                event_type="page.unpublished",
                page_id=page_id,
                payload={"last_error": str(exc)[:500]},
                status="pending",
            )
        except Exception:
            pass


@receiver(post_page_move)
def on_page_moved(sender, instance, **kwargs):
    """页面在树中移动：同步子树元数据并原子自增 epoch (B-02)"""
    try:
        sync_subtree_tree_metadata(instance)
    except Exception as exc:
        logger.exception("Failed to handle page_move for page %s: %s", getattr(instance, "id", None), exc)


@receiver(page_slug_changed)
def on_page_slug_changed(sender, instance, **kwargs):
    """页面 slug 发生变更：同步子树 URL 与元数据 (B-02)"""
    try:
        sync_subtree_tree_metadata(instance)
    except Exception as exc:
        logger.exception("Failed to handle page_slug_changed for page %s: %s", getattr(instance, "id", None), exc)


@receiver([post_save, post_delete], sender=PageViewRestriction)
def on_view_restriction_changed(sender, instance, **kwargs):
    """页面访问权限增删：递增 epoch 触发前台缓存失效"""
    try:
        ContentDiscoveryEpoch.increment_epoch()
        _sync_restriction_affected_archives(instance)
    except Exception as exc:
        logger.exception("Failed to increment epoch on PageViewRestriction mutation: %s", exc)


@receiver(m2m_changed, sender=PageViewRestriction.groups.through)
def on_view_restriction_groups_changed(sender, instance, action, **kwargs):
    """页面权限组关联变动：递增 epoch"""
    if action in ("post_add", "post_remove", "post_clear"):
        try:
            ContentDiscoveryEpoch.increment_epoch()
            _sync_restriction_affected_archives(instance)
        except Exception as exc:
            logger.exception("Failed to increment epoch on PageViewRestriction groups change: %s", exc)


@receiver(post_delete, sender=PublishedSearchDocument)
def on_search_doc_deleted(sender, instance, **kwargs):
    """搜索副本被物理清理时同步回退归档日桶计数"""
    try:
        from discovery.archive_counts import sync_page_archive_contribution
        sync_page_archive_contribution(instance.page_id)
    except Exception as exc:
        logger.exception("Failed to sync archive contribution on doc delete %s: %s", getattr(instance, "page_id", None), exc)


@receiver([post_save, post_delete], sender=Site)
def on_site_changed(sender, instance, **kwargs):
    """站点域名与根页面变更：递增 epoch"""
    try:
        ContentDiscoveryEpoch.increment_epoch()
    except Exception as exc:
        logger.exception("Failed to increment epoch on Site mutation: %s", exc)


@receiver([post_save, post_delete], sender=Tag)
def on_tag_changed(sender, instance, **kwargs):
    """标签变更：递增 epoch"""
    try:
        ContentDiscoveryEpoch.increment_epoch()
    except Exception as exc:
        logger.exception("Failed to increment epoch on Tag mutation: %s", exc)


# 监听 Person 与 GuideTopic 维度变更
try:
    from people.models import LegacyAuthorAttribution, Person
    @receiver([post_save, post_delete], sender=Person)
    def on_person_changed(sender, instance, **kwargs):
        try:
            ContentDiscoveryEpoch.increment_epoch()
        except Exception as exc:
            logger.exception("Failed to increment epoch on Person mutation: %s", exc)

    @receiver([post_save, post_delete], sender=LegacyAuthorAttribution)
    def on_legacy_attribution_changed(sender, instance, **kwargs):
        try:
            ContentDiscoveryEpoch.increment_epoch()
        except Exception as exc:
            logger.exception("Failed to increment epoch on LegacyAuthorAttribution mutation: %s", exc)
except Exception:
    pass

_REGISTERED_EPOCH_DIM_MODELS: set[type] = set()


def _on_dimension_model_changed(sender, instance, **kwargs):
    try:
        ContentDiscoveryEpoch.increment_epoch()
    except Exception as exc:
        logger.exception("Failed to increment epoch on dimension mutation (%s): %s", sender, exc)


def register_epoch_dimension_model(model_cls: type) -> None:
    """由业务 App 在 ready() 中主动注册会触发 ContentDiscoveryEpoch 递增的维度模型（如 GuideTopic）。"""
    if model_cls in _REGISTERED_EPOCH_DIM_MODELS:
        return
    _REGISTERED_EPOCH_DIM_MODELS.add(model_cls)
    post_save.connect(_on_dimension_model_changed, sender=model_cls, weak=False)
    post_delete.connect(_on_dimension_model_changed, sender=model_cls, weak=False)
