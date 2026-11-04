"""
Wagtail 页面生命周期与全站内容发现信号监听器。

在页面发布与下架时自动触发搜索副本的同步与状态流转：
1. page_published: 触发 project_page_to_discovery(instance)；
2. page_unpublished: 触发 unpublish_page_discovery(instance.id)。
"""

import logging
from django.dispatch import receiver
from wagtail.signals import page_published, page_unpublished
from discovery.projection import project_page_to_discovery, unpublish_page_discovery

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
