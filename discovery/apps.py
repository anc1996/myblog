from django.apps import AppConfig


class DiscoveryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "discovery"
    verbose_name = "全站内容发现与搜索"

    def ready(self):
        import discovery.signals  # 注册页面生命周期信号监听器
