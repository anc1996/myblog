from django.apps import AppConfig


class ContentLifecycleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "content_lifecycle"
    verbose_name = "内容生命周期与权限控制"

    def ready(self):
        from wagtail.models import AbstractPage
        from wagtail.permissions import register_permission_policy

        from content_lifecycle.forms import CustomBasePageAdminForm
        from content_lifecycle.models import CustomBasePage, RecycleBatch
        from content_lifecycle.permissions import (
            OwnershipPagePermissionPolicy,
            RecycleBatchPermissionPolicy,
        )

        CustomBasePage.base_form_class = CustomBasePageAdminForm
        register_permission_policy(AbstractPage, OwnershipPagePermissionPolicy())
        register_permission_policy(RecycleBatch, RecycleBatchPermissionPolicy(RecycleBatch))

        from django.contrib.auth.signals import user_logged_in
        user_logged_in.connect(_on_staff_or_admin_login_purge, dispatch_uid="content_lifecycle_auto_purge_on_login")


def _on_staff_or_admin_login_purge(sender, request, user, **kwargs):
    """管理员/编辑人员登录时按冷却节流触发一次超期回收站清理，弥补非 24 小时开机机器的定时真空。"""
    if not user or not getattr(user, "is_active", False):
        return
    if not (
        getattr(user, "is_superuser", False)
        or getattr(user, "is_staff", False)
        or user.has_perm("wagtailadmin.access_admin")
    ):
        return
    try:
        from content_lifecycle.recycle import maybe_trigger_expired_recycle_purge
        maybe_trigger_expired_recycle_purge(force=False)
    except Exception:
        pass
