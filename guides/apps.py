from django.apps import AppConfig


class GuidesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "guides"
    verbose_name = "技术专栏与专题指南"

    def ready(self):
        # 注册指南与聚合页到 discovery 全站搜索适配器表
        try:
            from discovery.registry import register_type_adapter
            from .adapters import GuidePageAdapter, GuideIndexPageAdapter
            register_type_adapter(GuidePageAdapter())
            register_type_adapter(GuideIndexPageAdapter())
        except Exception:
            pass
