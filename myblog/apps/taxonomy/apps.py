from django.apps import AppConfig


class TaxonomyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "taxonomy"
    verbose_name = "分类与标签词库"

    def ready(self):
        from discovery.registry import register_type_adapter
        from .adapters import TagIndexPageAdapter
        register_type_adapter(TagIndexPageAdapter())
