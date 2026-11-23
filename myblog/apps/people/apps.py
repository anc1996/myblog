from django.apps import AppConfig


class PeopleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "people"
    verbose_name = "公开人物与作者管理"

    def ready(self):
        # 确保 pre_delete 信号与受控 Snippet 注册在任何调用路径下就绪
        import people.wagtail_hooks  # noqa: F401
