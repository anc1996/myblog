"""
身份认证与权限变更信号监听。

1. user_logged_in: 自动按需建立 MemberProfile，并将当前 session_epoch 写入会话；
2. 组/权限变更与组删除：同步递增受影响账号的 session_epoch，确保撤权/降权后旧会话立即失效；
3. 直接保存 User 修改 password / is_active / is_staff / is_superuser 时兜底递增 session_epoch。
"""

from contextlib import contextmanager
import contextvars
import threading
from django.contrib.auth.models import Group
from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import m2m_changed, post_save, pre_delete, pre_save

from identity.models import MemberProfile, User

_SUPPRESS_USER_SAVE_EPOCH_BUMP: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "suppress_user_save_epoch_bump",
    default=False,
)


@contextmanager
def suppress_user_save_epoch_bump():
    """供 AccountLifecycleService 内部显式递增 session_epoch 时避免 post_save 重复加 1。"""
    token = _SUPPRESS_USER_SAVE_EPOCH_BUMP.set(True)
    try:
        yield
    finally:
        _SUPPRESS_USER_SAVE_EPOCH_BUMP.reset(token)


def sync_session_epoch_on_login(sender, request, user, **kwargs):
    """登录（含测试 client.force_login）成功时同步写入 session_epoch。"""
    if request is None or user is None:
        return
    profile = MemberProfile.get_or_create_for_user(user)
    if hasattr(request, "session") and request.session is not None:
        request.session["session_epoch"] = profile.session_epoch


_PENDING_CLEAR_USER_IDS = threading.local()


def _stash_clear_ids(tag: str, instance_pk, user_ids: list[int]) -> None:
    store = getattr(_PENDING_CLEAR_USER_IDS, "map", None)
    if store is None:
        store = {}
        _PENDING_CLEAR_USER_IDS.map = store
    store[(tag, instance_pk)] = user_ids


def _pop_clear_ids(tag: str, instance_pk) -> list[int]:
    store = getattr(_PENDING_CLEAR_USER_IDS, "map", None)
    if not store:
        return []
    return store.pop((tag, instance_pk), [])


def _bump_epochs(user_ids):
    from identity.account_lifecycle import AccountLifecycleService

    AccountLifecycleService.bump_session_epoch(user_ids)


def on_user_groups_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if action == "pre_clear":
        if reverse:
            _stash_clear_ids("user_groups_rev", instance.pk, list(instance.user_set.values_list("id", flat=True)))
        return
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    if not reverse:
        _bump_epochs([instance.pk])
    else:
        if action == "post_clear":
            _bump_epochs(_pop_clear_ids("user_groups_rev", instance.pk))
        elif pk_set:
            _bump_epochs(pk_set)


def on_user_permissions_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if action == "pre_clear":
        if reverse:
            _stash_clear_ids("user_perms_rev", instance.pk, list(instance.user_set.values_list("id", flat=True)))
        return
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    if not reverse:
        _bump_epochs([instance.pk])
    else:
        if action == "post_clear":
            _bump_epochs(_pop_clear_ids("user_perms_rev", instance.pk))
        elif pk_set:
            _bump_epochs(pk_set)


def on_group_permissions_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if action == "pre_clear":
        if reverse:
            _stash_clear_ids("grp_perms_rev", instance.pk, list(instance.group_set.values_list("id", flat=True)))
        return
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    if not reverse:
        user_ids = list(instance.user_set.values_list("id", flat=True))
        _bump_epochs(user_ids)
    else:
        if action == "post_clear":
            group_ids = _pop_clear_ids("grp_perms_rev", instance.pk)
        elif pk_set:
            group_ids = list(pk_set)
        else:
            group_ids = []
        if group_ids:
            user_ids = list(User.objects.filter(groups__id__in=group_ids).values_list("id", flat=True))
            _bump_epochs(user_ids)


def on_group_pre_delete(sender, instance, **kwargs):
    user_ids = list(instance.user_set.values_list("id", flat=True))
    _bump_epochs(user_ids)


def capture_user_security_state(sender, instance, raw=False, **kwargs):
    if raw or not instance.pk:
        instance._prev_security_state = None
        return
    row = User.objects.filter(pk=instance.pk).values("is_active", "is_staff", "is_superuser", "password").first()
    instance._prev_security_state = row


def bump_epoch_on_security_field_change(sender, instance, created, raw=False, **kwargs):
    if raw or created:
        return
    if _SUPPRESS_USER_SAVE_EPOCH_BUMP.get():
        return
    prev = getattr(instance, "_prev_security_state", None)
    if not prev:
        return
    changed = (
        prev["is_active"] != instance.is_active
        or prev["is_staff"] != instance.is_staff
        or prev["is_superuser"] != instance.is_superuser
        or prev["password"] != instance.password
    )
    if changed:
        _bump_epochs([instance.pk])


def register_identity_signals():
    user_logged_in.connect(
        sync_session_epoch_on_login,
        dispatch_uid="identity_sync_session_epoch_on_login",
    )
    m2m_changed.connect(
        on_user_groups_changed,
        sender=User.groups.through,
        dispatch_uid="identity_user_groups_epoch",
    )
    m2m_changed.connect(
        on_user_permissions_changed,
        sender=User.user_permissions.through,
        dispatch_uid="identity_user_perms_epoch",
    )
    m2m_changed.connect(
        on_group_permissions_changed,
        sender=Group.permissions.through,
        dispatch_uid="identity_group_perms_epoch",
    )
    pre_delete.connect(
        on_group_pre_delete,
        sender=Group,
        dispatch_uid="identity_group_delete_epoch",
    )
    pre_save.connect(
        capture_user_security_state,
        sender=User,
        dispatch_uid="identity_capture_user_sec_state",
    )
    post_save.connect(
        bump_epoch_on_security_field_change,
        sender=User,
        dispatch_uid="identity_bump_epoch_user_change",
    )
