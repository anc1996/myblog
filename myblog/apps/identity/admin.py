"""
Django Admin (/django-admin/) 账号治理安全加固：
禁止通过原生 UserAdmin 单条删除或批量 delete_selected 绕过 AccountLifecycleService.hard_delete。
"""

from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from identity.models import AccountAuditLog, AvatarPurgeTask, MemberProfile, User


@admin.register(User)
class ControlledUserAdmin(BaseUserAdmin):
    list_display = ("username", "is_active", "is_staff", "is_superuser", "date_joined")

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def has_add_permission(self, request):
        return bool(request.user and request.user.is_superuser)

    def has_change_permission(self, request, obj=None):
        return bool(request.user and request.user.is_superuser)

    def has_delete_permission(self, request, obj=None):
        return False

    def user_change_password(self, request, id, form_url=""):
        if not request.user or not request.user.is_superuser:
            raise PermissionDenied("仅超级管理员可重置用户密码，且须经账号治理工作台执行。")
        if str(request.user.pk) != str(id):
            messages.warning(
                request,
                "请使用 Wagtail【账号治理】工作台重置他人临时密码，以确保设置强制改密并踢出旧会话。",
            )
            return redirect("identity_admin_user_manage", user_id=id)
        return super().user_change_password(request, id, form_url=form_url)

    def delete_model(self, request, obj):
        raise PermissionDenied("禁止在 Django Admin 直接删除账号，请使用 Wagtail 账号治理工作台或 AccountLifecycleService.hard_delete。")

    def delete_queryset(self, request, queryset):
        raise PermissionDenied("禁止批量直接物理删除账号。")


@admin.register(MemberProfile)
class MemberProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "nickname", "must_change_password", "session_epoch", "avatar_version", "updated_at")
    search_fields = ("user__username", "nickname")
    readonly_fields = ("user", "session_epoch", "avatar_token", "avatar_version", "avatar_storage_alias", "avatar_storage_namespace", "avatar_key", "avatar_updated_at")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AvatarPurgeTask)
class AvatarPurgeTaskAdmin(admin.ModelAdmin):
    list_display = ("storage_key", "source", "status", "attempts", "created_at", "finished_at")
    list_filter = ("status", "source")
    readonly_fields = [f.name for f in AvatarPurgeTask._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AccountAuditLog)
class AccountAuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "target_username_snapshot", "actor_username_snapshot", "operation_id", "created_at")
    list_filter = ("action",)
    readonly_fields = [f.name for f in AccountAuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
