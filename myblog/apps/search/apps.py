from django.apps import AppConfig


class SearchConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "search"
    verbose_name = "全站搜索与内容发现"

    def ready(self):
        from discovery.contracts import AnalyticsCapabilities, EntryAnalyticsSpec
        from discovery.registry import register_entry_analytics

        register_entry_analytics(
            EntryAnalyticsSpec(
                entry_kind="search",
                model_label="",
                analytics_capabilities=AnalyticsCapabilities(
                    kind="entry",
                    entry_kind="search",
                    metric_keys=("pv", "uv", "card_exposure", "entry_click"),
                ),
            )
        )
