from django.apps import AppConfig


class PeopleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "people"
    verbose_name = "公开人物与作者管理"

    def ready(self):
        # 确保 pre_delete 信号与受控 Snippet 注册在任何调用路径下就绪
        import people.wagtail_hooks  # noqa: F401
        from discovery.contracts import AnalyticsCapabilities, EntryAnalyticsSpec
        from discovery.registry import register_entry_analytics

        register_entry_analytics(
            EntryAnalyticsSpec(
                entry_kind="author_detail",
                model_label="people.Person",
                analytics_capabilities=AnalyticsCapabilities(
                    kind="entry",
                    entry_kind="author_detail",
                    metric_keys=("pv", "uv", "card_exposure", "entry_click"),
                ),
            )
        )
