"""
Wagtail 页面生命周期与全站内容发现信号监听器。

在页面发布、下架、权限变更及维度修改时自动触发搜索副本的同步与版本流转：
1. page_published: 触发 project_page_to_discovery(instance)；
2. page_unpublished: 触发 unpublish_page_discovery(instance.id)；
3. post_page_move / PageViewRestriction / Site / Person / Topic / Tag 变更:
   原子递增全局 ContentDiscoveryEpoch，驱动前台缓存、游标与 ETag 瞬间失效 (BLK-08)。
"""

import logging
from django.dispatch import receiver
from django.db.models.signals import post_save, post_delete, m2m_changed
from wagtail.signals import page_published, page_unpublished, post_page_move
from wagtail.models import PageViewRestriction, Site
from taggit.models import Tag
from discovery.projection import project_page_to_discovery, unpublish_page_discovery
from discovery.models import ContentDiscoveryEpoch

logger = logging.getLogger(__name__)


@receiver(page_published)
def on_page_published(sender, instance, **kwargs):
    """页面正式上线：自动投影/更新到搜索副本"""
    try:
        project_page_to_discovery(instance)
    except Exception as exc:
        logger.exception("Failed to project published page %s to discovery: %s", getattr(instance, "id", None), exc)


@receiver(page_unpublished)
def on_page_unpublished(sender, instance, **kwargs):
    """页面撤销发布：自动标记下架并递增 epoch"""
    try:
        unpublish_page_discovery(instance.id)
    except Exception as exc:
        logger.exception("Failed to mark unpublished page %s in discovery: %s", getattr(instance, "id", None), exc)


@receiver(post_page_move)
def on_page_moved(sender, instance, **kwargs):
    """页面在树中移动：重新投影并原子自增 epoch"""
    try:
        project_page_to_discovery(instance)
        ContentDiscoveryEpoch.increment_epoch()
    except Exception as exc:
        logger.exception("Failed to handle page_move for page %s: %s", getattr(instance, "id", None), exc)


@receiver([post_save, post_delete], sender=PageViewRestriction)
def on_view_restriction_changed(sender, instance, **kwargs):
    """页面访问权限增删：递增 epoch 触发前台缓存失效"""
    try:
        ContentDiscoveryEpoch.increment_epoch()
    except Exception as exc:
        logger.exception("Failed to increment epoch on PageViewRestriction mutation: %s", exc)


@receiver(m2m_changed, sender=PageViewRestriction.groups.through)
def on_view_restriction_groups_changed(sender, instance, action, **kwargs):
    """页面权限组关联变动：递增 epoch"""
    if action in ("post_add", "post_remove", "post_clear"):
        try:
            ContentDiscoveryEpoch.increment_epoch()
        except Exception as exc:
            logger.exception("Failed to increment epoch on PageViewRestriction groups change: %s", exc)


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
    from people.models import Person
    @receiver([post_save, post_delete], sender=Person)
    def on_person_changed(sender, instance, **kwargs):
        try:
            ContentDiscoveryEpoch.increment_epoch()
        except Exception as exc:
            logger.exception("Failed to increment epoch on Person mutation: %s", exc)
except Exception:
    pass

try:
    from guides.models import GuideTopic
    @receiver([post_save, post_delete], sender=GuideTopic)
    def on_guide_topic_changed(sender, instance, **kwargs):
        try:
            ContentDiscoveryEpoch.increment_epoch()
        except Exception as exc:
            logger.exception("Failed to increment epoch on GuideTopic mutation: %s", exc)
except Exception:
    pass
