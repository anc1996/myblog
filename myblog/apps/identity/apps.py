from django.apps import AppConfig


class IdentityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "identity"
    verbose_name = "后台身份"

    def ready(self):
        from identity.deletion_guard import register_user_deletion_guard
        from identity.signals import register_identity_signals

        register_user_deletion_guard()
        register_identity_signals()
