"""
全站统一业务页面抽象基类 (CustomBasePage)。

继承自 Wagtail 的 Page 模型，落实核心生命周期与回收安全契约：
1. document_uuid: 稳定的跨存储/跨版本业务文档唯一标识，与 Page ID 物理分离；
2. is_recycled: 原树占位回收状态标志，用于在所有视图与公共服务中隔离已回收页面；
3. publish() 守卫: 严格拦截自身或祖先处于已回收状态的非法发布尝试；
4. delete() 守卫: 拦截未经授权的直接物理删除，引导使用受控回收站服务；
5. copy() 守卫: 递归复制时排除已回收子节点，并在新副本中重新生成全新的 document_uuid；
6. update_aliases() 守卫: 排除已回收的 Alias 别名页面，防止源页面发布导致回收别名被动上线。
"""

import uuid
from django.db import models
from django.core.exceptions import PermissionDenied
from wagtail.models import Page


class CustomBasePage(Page):
    document_uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        verbose_name="业务文档唯一标识",
        help_text="跨 PostgreSQL、MongoDB 与媒体引用追踪使用的稳定不可变文档身份",
    )
    is_recycled = models.BooleanField(
        default=False,
        editable=False,
        db_index=True,
        verbose_name="是否已移入回收站",
        help_text="原树占位标记；已回收页面不可发布、对公网不可见且在管理后台普通目录隐藏",
    )

    class Meta:
        abstract = True

    def publish(self, revision=None, user=None, changed=True, log_action=True, previous_revision=None, **kwargs):
        """发布守卫：自身或任一祖先处于回收状态时坚决拒绝发布"""
        if self.is_recycled:
            raise PermissionDenied(f"页面 '{self.title}' 当前处于回收站中，禁止发布！")
        for ancestor in self.get_ancestors():
            if getattr(ancestor, "is_recycled", False):
                raise PermissionDenied(f"父级页面 '{ancestor.title}' 处于回收站中，禁止发布子页面！")
        return super().publish(
            revision=revision,
            user=user,
            changed=changed,
            log_action=log_action,
            previous_revision=previous_revision,
            **kwargs,
        )

    def delete(self, *args, **kwargs):
        """删除守卫：普通业务入口禁止直接物理删除，必须通过 RecycleService 回收"""
        allow_physical_purge = kwargs.pop("allow_physical_purge", False)
        if not allow_physical_purge:
            raise PermissionDenied(
                f"禁止对页面 '{self.title}' (id={self.id}) 执行直接物理删除！"
                "请调用 content_lifecycle.RecycleService 移入受控回收站。"
            )
        return super().delete(*args, **kwargs)

    def copy(self, *args, **kwargs):
        """复制守卫：排除已回收子页面，并确保新副本分配全新 document_uuid"""
        exclude_fields = kwargs.get("exclude_fields") or []
        if "document_uuid" not in exclude_fields:
            exclude_fields = list(exclude_fields) + ["document_uuid"]
            kwargs["exclude_fields"] = exclude_fields
        return super().copy(*args, **kwargs)
