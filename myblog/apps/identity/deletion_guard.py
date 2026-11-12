"""
账号物理删除守卫 (User Deletion Guard)。

防止绕过 AccountLifecycleService.hard_delete 直接调用 user.delete() 或 QuerySet.delete()，
避免留下 owner=NULL 无主页面、失真的 CommentReaction 赞踩计数或未入队的 RustFS 头像残留。
"""

from contextlib import contextmanager
import contextvars
from django.core.exceptions import PermissionDenied
from django.db.models.signals import pre_delete

_AUTHORIZED_USER_DELETE: contextvars.ContextVar[tuple[str, int] | None] = contextvars.ContextVar(
    "authorized_user_hard_delete",
    default=None,
)


@contextmanager
def authorize_user_hard_delete(target_user_id: int, using: str = "default"):
    """
    仅限 AccountLifecycleService.hard_delete 内部在校验过户、赞踩重算与头像入队后使用的授权上下文。
    严格限定数据库别名与目标 User 主键。
    """
    token = _AUTHORIZED_USER_DELETE.set((using, int(target_user_id)))
    try:
        yield
    finally:
        _AUTHORIZED_USER_DELETE.reset(token)


def is_user_hard_delete_authorized(target_user_id: int, using: str = "default") -> bool:
    current = _AUTHORIZED_USER_DELETE.get()
    return current == (using, int(target_user_id))


def guard_user_pre_delete(sender, instance, using="default", **kwargs):
    """拦截未经受控服务授权的 User 物理删除。"""
    if not is_user_hard_delete_authorized(instance.pk, using=using):
        raise PermissionDenied(
            f"禁止直接物理删除账号 '{instance.username}' (id={instance.pk})！"
            "请使用 AccountLifecycleService.hard_delete 执行受控删除、资产过户与头像清理。"
        )


def register_user_deletion_guard():
    from identity.models import User

    pre_delete.connect(
        guard_user_pre_delete,
        sender=User,
        dispatch_uid="identity_guard_user_pre_delete",
    )
