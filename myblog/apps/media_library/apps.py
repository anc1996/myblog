from django.apps import AppConfig


class MediaLibraryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "media_library"
    verbose_name = "全站媒体库"

    def ready(self):
        # 1. 解绑原生 post_delete 自动删除存储文件信号，
        # 落实“失败或历史引用对象保留待对账，不立即物理删除”的安全合同。
        from django.db.models.signals import post_delete
        try:
            from wagtail.images.signal_handlers import post_delete_file_cleanup as image_cleanup
            from wagtail.documents.signal_handlers import post_delete_file_cleanup as doc_cleanup
            from .models import CustomImage, CustomRendition, CustomDocument

            post_delete.disconnect(image_cleanup, sender=CustomImage)
            post_delete.disconnect(image_cleanup, sender=CustomRendition)
            post_delete.disconnect(doc_cleanup, sender=CustomDocument)
        except Exception:
            pass

        # 2. 注册 MEDIA-REF-01 pre_delete 引用保护信号拦截器
        try:
            from .signals import register_media_signals
            register_media_signals()
        except Exception:
            pass
