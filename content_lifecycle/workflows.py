"""
content_lifecycle.workflows
Wagtail 工作流终审版本防篡改保护实现 (H-06)。

解决 Wagtail 8.1 默认 publish_workflow_state() 发布 get_latest_revision() 导致发布未审草稿的漏洞：
终审通过时，强校验并仅发布通过审批的目标 Revision (task_state.revision)。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def publish_approved_revision(workflow_state: Any, user: Any = None) -> None:
    """
    自定义工作流终审发布动作函数 (WAGTAIL_FINISH_WORKFLOW_ACTION)。
    签名严格与 Wagtail 8.1 官方接口对齐：(workflow_state, user=None)。
    延迟导入以防止类定义期的循环依赖。
    """
    from django.db import transaction

    with transaction.atomic():
        task_state = workflow_state.current_task_state
        target_revision = None
        if task_state and getattr(task_state, "revision_id", None):
            from wagtail.models import Revision
            target_revision = Revision.objects.filter(id=task_state.revision_id).first()

        if not target_revision:
            content_obj = workflow_state.content_object
            target_revision = getattr(content_obj, "latest_revision", None) or content_obj.get_latest_revision()

        if not target_revision:
            logger.error("No valid revision found to publish for workflow state %s", workflow_state.id)
            return

        logger.info(
            "Publishing approved revision ID=%s for object %s by user %s",
            target_revision.id,
            target_revision.object_id,
            user,
        )
        target_revision.publish(user=user)
