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
