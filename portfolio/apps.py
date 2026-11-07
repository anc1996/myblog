from django.apps import AppConfig


class PortfolioConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "portfolio"
    verbose_name = "作品集与开源项目"

    def ready(self):
        from discovery.registry import register_type_adapter
        from .adapters import ProjectPageAdapter, PortfolioIndexPageAdapter

        register_type_adapter(ProjectPageAdapter())
        register_type_adapter(PortfolioIndexPageAdapter())
