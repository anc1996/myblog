"""
content_lifecycle.workflows
Wagtail 工作流终审版本防篡改保护实现 (WAGTAIL_FINISH_WORKFLOW_ACTION / H-06)。

【教学点与重大安全漏洞背景】：
在 Wagtail 默认的工作流审批机制中，存在一个极其危险的“偷天换日”时序漏洞：
1. 作者写好了版本 V1 并提交审批；
2. 主编打开后台正在仔细审查 V1；
3. 此时作者偷偷再次点击保存，产生了包含违规内容的新草稿 V2；
4. 主编阅读完 V1 觉得没问题，点击“审核通过”；
5. Wagtail 默认行为是调用 get_latest_revision().publish()，结果直接把未经审核的 V2 发布到了线上！

【解决方案】：
我们在 settings 中配置 WAGTAIL_FINISH_WORKFLOW_ACTION 指向本模块的 publish_approved_revision 函数。
强校验并【仅发布被送审并核准的目标 Revision（task_state.revision）】，坚决阻断未审草稿搭便车串版！
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def publish_approved_revision(workflow_state: Any, user: Any = None) -> None:
    """
    自定义工作流终审发布动作函数 (WAGTAIL_FINISH_WORKFLOW_ACTION)。

    【接口契约与变量说明】：
    - workflow_state (WorkflowState): Wagtail 原生工作流状态实例，包含当前流转任务、关联页面与历史任务状态。
    - user (User | None): 执行最终审批通过操作的审核人实体对象。
    - task_state (TaskState): 局部变量，当前审批任务状态。
      * task_state.revision_id 记录了当年提交送审时的确切 Revision ID（防盲批与防串版凭据）。
    - target_revision (Revision): 最终判定准许上线的权威版本对象。
      * 严格优先绑定 task_state.revision_id；
      * 只有在非页面或老旧数据缺失 revision_id 时，才安全兜底回退。
    """
    from django.db import transaction

    with transaction.atomic():
        task_state = workflow_state.current_task_state
        target_revision = None
        # 核心防篡改：核验任务绑定的送审版本号
        if task_state and getattr(task_state, "revision_id", None):
            from wagtail.models import Revision
            target_revision = Revision.objects.filter(id=task_state.revision_id).first()

        # 兜底回退：若未记录 revision_id（如老数据），取最新版本
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
        # 正式发布被核准的目标 Revision，更新 live=True 与 live_revision_id
        target_revision.publish(user=user)
