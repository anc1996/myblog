from django.apps import AppConfig


class ExternalContentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "external_content"
    verbose_name = "外置内容存储契约"
