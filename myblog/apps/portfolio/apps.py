from django.apps import AppConfig


class PortfolioConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "portfolio"
    verbose_name = "作品集与开源项目"

    def ready(self):
        from discovery.registry import register_type_adapter
        from .adapters import ProjectPageAdapter, PortfolioIndexPageAdapter
        from markdown_import.registry import register_import_adapter
        from .import_adapter import ProjectPageImportAdapter

        register_type_adapter(ProjectPageAdapter())
        register_type_adapter(PortfolioIndexPageAdapter())
        register_import_adapter(ProjectPageImportAdapter())
