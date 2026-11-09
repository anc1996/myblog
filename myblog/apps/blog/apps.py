from django.apps import AppConfig


class BlogConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "blog"
    verbose_name = "博客文章与栏目系统"

    def ready(self):
        from discovery.registry import register_type_adapter
        from blog.adapters import BlogPageAdapter, BlogIndexPageAdapter

        register_type_adapter(BlogPageAdapter())
        register_type_adapter(BlogIndexPageAdapter())
