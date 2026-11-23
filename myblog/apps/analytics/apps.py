import logging
from django.apps import AppConfig
from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save

logger = logging.getLogger(__name__)


def _schedule_display_cache_invalidation(*, page_ids=None, site_id=None) -> None:
    from analytics.settlement import invalidate_analytics_display_cache

    pids = [int(pid) for pid in (page_ids or []) if pid is not None]
    sid = int(site_id) if site_id is not None else None
    transaction.on_commit(
        lambda: invalidate_analytics_display_cache(page_ids=pids or None, site_id=sid)
    )


def _on_page_published_or_unpublished(sender, instance, **kwargs) -> None:
    try:
        page_id = getattr(instance, "id", None)
        _schedule_display_cache_invalidation(page_ids=[page_id] if page_id else None)
    except Exception as exc:
        logger.debug("Analytics cache invalidation skipped on page lifecycle signal: %s", exc)


def _on_restriction_changed(sender, instance, **kwargs) -> None:
    try:
        page_id = getattr(instance, "page_id", None)
        _schedule_display_cache_invalidation(page_ids=[page_id] if page_id else None)
    except Exception as exc:
        logger.debug("Analytics cache invalidation skipped on restriction signal: %s", exc)


def _on_restriction_groups_changed(sender, instance, action, **kwargs) -> None:
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    try:
        page_id = getattr(instance, "page_id", None)
        _schedule_display_cache_invalidation(page_ids=[page_id] if page_id else None)
    except Exception as exc:
        logger.debug("Analytics cache invalidation skipped on restriction m2m signal: %s", exc)


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "analytics"
    verbose_name = "全站数据分析"

    def ready(self) -> None:
        from wagtail.models import PageViewRestriction
        from wagtail.signals import page_published, page_unpublished

        page_published.connect(
            _on_page_published_or_unpublished,
            dispatch_uid="analytics_invalidate_on_page_published",
        )
        page_unpublished.connect(
            _on_page_published_or_unpublished,
            dispatch_uid="analytics_invalidate_on_page_unpublished",
        )
        post_save.connect(
            _on_restriction_changed,
            sender=PageViewRestriction,
            dispatch_uid="analytics_invalidate_on_restriction_save",
        )
        post_delete.connect(
            _on_restriction_changed,
            sender=PageViewRestriction,
            dispatch_uid="analytics_invalidate_on_restriction_delete",
        )
        m2m_changed.connect(
            _on_restriction_groups_changed,
            sender=PageViewRestriction.groups.through,
            dispatch_uid="analytics_invalidate_on_restriction_groups",
        )
