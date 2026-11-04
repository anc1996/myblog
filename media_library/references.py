"""
MEDIA-REF-01 媒体引用索引追踪与删除保护服务。

落实删除保护原则：
1. 保存 Revision 时，自动登记所有 Block 提取出的 MediaReference；
2. 物理删除前执行 check_asset_references()，只要存在任一有效引用（包括草稿、历史、已发布、回收站），
   坚决返回 can_delete=False (fail closed)！
"""

from uuid import UUID
from typing import Any
from django.db import transaction
from wagtail.models import Page

from media_library.models import MediaReferenceRecord
from content_blocks.contracts import MediaReference


class MediaReferenceTracker:
    """全站媒体引用管理器"""

    @classmethod
    def register_revision_references(
        cls,
        page_id: int,
        revision_id: int,
        references: list[MediaReference],
    ):
        """
        在 Revision 生成时，批量登记该版本提取出的全部媒体引用。
        幂等清空原版本残留记录，重新注册。
        """
        with transaction.atomic():
            MediaReferenceRecord.objects.filter(page_id=page_id, revision_id=revision_id).delete()
            records = []
            for ref in references:
                if not ref.asset_id:
                    continue
                try:
                    asset_uuid = UUID(str(ref.asset_id))
                except Exception:
                    continue
                records.append(
                    MediaReferenceRecord(
                        asset_id=asset_uuid,
                        asset_type=ref.asset_type,
                        page_id=page_id,
                        revision_id=revision_id,
                        field_name=ref.extra.get("field_name", "body") if hasattr(ref, "extra") else "body",
                        block_path=ref.block_path,
                    )
                )
            if records:
                MediaReferenceRecord.objects.bulk_create(records)

    @classmethod
    def check_asset_references(cls, asset_id: UUID | str) -> tuple[bool, str, list[dict[str, Any]]]:
        """
        核心防御检查：判定某项媒体资源是否允许物理删除。
        返回: (can_delete: bool, reason: str, details: list[dict])
        只要仍被任何页面版本引用，一票否决阻断物理删除！
        """
        try:
            asset_uuid = UUID(str(asset_id))
        except Exception:
            return False, "非法资产 UUID 格式", []

        refs = MediaReferenceRecord.objects.filter(asset_id=asset_uuid)
        if not refs.exists():
            return True, "全站零引用，允许执行受控物理删除", []

        details = []
        for r in refs:
            try:
                page = Page.objects.get(id=r.page_id)
                is_live = bool(page.live and page.live_revision_id == r.revision_id)
                is_recycled = getattr(page, "is_recycled", False)
                details.append({
                    "page_id": r.page_id,
                    "page_title": page.title,
                    "revision_id": r.revision_id,
                    "is_live_ref": is_live,
                    "is_recycled": is_recycled,
                    "block_path": r.block_path,
                })
            except Page.DoesNotExist:
                details.append({
                    "page_id": r.page_id,
                    "page_title": "已物理删除的历史遗留页面",
                    "revision_id": r.revision_id,
                    "is_live_ref": False,
                    "is_recycled": False,
                    "block_path": r.block_path,
                })

        return False, f"资源正被 {len(details)} 处页面版本引用，禁止物理删除！", details
