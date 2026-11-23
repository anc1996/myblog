from django.apps import AppConfig


class HomeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "home"

    def ready(self):
        from discovery.contracts import AnalyticsCapabilities, EntryAnalyticsSpec
        from discovery.registry import register_entry_analytics

        register_entry_analytics(
            EntryAnalyticsSpec(
                entry_kind="home",
                model_label="home.HomePage",
                analytics_capabilities=AnalyticsCapabilities(
                    kind="entry",
                    entry_kind="home",
                    metric_keys=("pv", "uv", "card_exposure", "entry_click"),
                ),
            )
        )
