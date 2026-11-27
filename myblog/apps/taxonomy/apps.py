import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)


def _sync_tag_directory_metadata(sender, instance, **kwargs):
    """写侧自动补齐 TagDirectoryMetadata，消除前台 GET 期间的隐式写库。"""
    try:
        from taxonomy.models import TagDirectoryMetadata

        TagDirectoryMetadata.get_or_create_for_tag(instance)
    except Exception:
        logger.warning(
            "Failed to auto-sync TagDirectoryMetadata for tag_id=%s",
            getattr(instance, "pk", None),
            exc_info=True,
        )


class TaxonomyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "taxonomy"
    verbose_name = "分类与标签词库"

    def ready(self):
        from django.db.models.signals import post_save
        from taggit.models import Tag
        from discovery.registry import register_type_adapter
        from .adapters import TagIndexPageAdapter

        register_type_adapter(TagIndexPageAdapter())
        post_save.connect(
            _sync_tag_directory_metadata,
            sender=Tag,
            weak=False,
            dispatch_uid="taxonomy.sync_tag_directory_metadata",
        )
