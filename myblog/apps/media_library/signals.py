"""
media_library.signals
媒体资源删除保护信号监听器 (MEDIA-REF-01)。

在底层拦截所有 CustomImage, CustomDocument, Audio, Video 的物理删除。
只要存在任何有效引用（包括线上正式发布、历史草稿、受控回收站），坚决一票否决阻断！
"""

import logging
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.core.exceptions import PermissionDenied

from .models import CustomImage, CustomDocument, Audio, Video
from .references import MediaReferenceTracker

logger = logging.getLogger(__name__)


def check_and_protect_media_delete(sender, instance, **kwargs):
    """pre_delete 拦截：只要被任何页面（含回收站页面）引用，一票否决阻止物理删除"""
    public_id = getattr(instance, "public_id", None)
    if not public_id:
        return

    can_delete, reason, details = MediaReferenceTracker.check_asset_references(public_id)
    if not can_delete:
        pages_summary = []
        for d in details[:3]:
            title = d.get("page_title", "未知页面")
            status = "【回收站中】" if d.get("is_recycled") else "【活动页面】"
            pages_summary.append(f"{status}《{title}》(ID:{d.get('page_id')})")

        detail_msg = "、".join(pages_summary)
        if len(details) > 3:
            detail_msg += f" 等共 {len(details)} 处页面引用"

        logger.warning(
            "MEDIA-REF-01: Intercepted physical deletion of %s (public_id=%s). Reason: %s",
            sender.__name__, public_id, detail_msg
        )
        raise PermissionDenied(
            f"该媒体资源正被 {detail_msg} 引用，在解除引用或相关页面彻底销毁前严禁删除！"
        )


def register_media_signals():
    """在 App ready 时注册信号"""
    pre_delete.connect(check_and_protect_media_delete, sender=CustomImage, dispatch_uid="protect_custom_image_delete")
    pre_delete.connect(check_and_protect_media_delete, sender=CustomDocument, dispatch_uid="protect_custom_doc_delete")
    pre_delete.connect(check_and_protect_media_delete, sender=Audio, dispatch_uid="protect_audio_delete")
    pre_delete.connect(check_and_protect_media_delete, sender=Video, dispatch_uid="protect_video_delete")
