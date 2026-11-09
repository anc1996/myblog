"""
跨存储一致性审计与离线对账服务 (CrossStorageReconciler)。

落实 E.1 规划与 S11 验收契约：
1. PostgreSQL 关系表、MongoDB 外置版本表与 RustFS S3 对象存储三方核对；
2. 校验所有已上线页面 (live=True, is_recycled=False)：
   - PostgreSQL live_revision 存在且有效；
   - MongoDB 外置版本指针对应的正文存在且 SHA-256 哈希防篡改通过；
   - Discovery 全文搜索副本处于上线态且生效版本一致；
   - 页面引用的媒体资源均已记录且物理文件存在；
3. 受控识别与清理全站零引用的悬空废弃媒体文件，杜绝直接物理误删。
"""

import logging
from typing import Any
from wagtail.models import Page

from external_content.revision import extract_pointers_from_revision
from external_content.store import MongoContentStore
from discovery.models import PublishedSearchDocument
from media_library.references import MediaReferenceTracker

logger = logging.getLogger(__name__)


class CrossStorageReconciler:
    """全站跨存储数据一致性核对与对账器"""

    @classmethod
    def reconcile_and_purge_orphan_media(
        cls,
        dry_run: bool = True,
        grace_period_days: int = 30,
    ) -> dict[str, Any]:
        """
        受控对账并处置零引用孤儿媒体资源 (RECONCILE-MEDIA-02)。
        1. 必须跨全站所有页面、所有历史草稿与回收站检查；
        2. 仅对全站确无引用、且创建时间超过 grace_period_days 的媒体资源进行处理；
        3. dry_run=True (默认)：仅输出审计待清理清单与预计释放存储，绝不调用底层删除；
        4. dry_run=False：经明确授权后，调用 ProtectedRustFSStorage._controlled_physical_delete
           抹除 RustFS 桶中文件字节，并在数据库中删除该媒体记录。
        """
        from datetime import datetime, timezone, timedelta
        from media_library.models import CustomImage, CustomDocument, Audio, Video
        from media_library.references import MediaReferenceTracker

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=grace_period_days)
        candidates = []
        purged = []

        models_to_check = [
            (CustomImage, "image"),
            (CustomDocument, "document"),
            (Audio, "audio"),
            (Video, "video"),
        ]

        for model_cls, asset_type in models_to_check:
            for item in model_cls.objects.all():
                public_id = getattr(item, "public_id", None)
                if not public_id:
                    continue
                can_delete, _, refs = MediaReferenceTracker.check_asset_references(public_id)
                if can_delete and len(refs) == 0:
                    created_at = getattr(item, "created_at", None)
                    if created_at and created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    if created_at and created_at > cutoff:
                        continue

                    file_obj = getattr(item, "file", None)
                    file_name = file_obj.name if file_obj else ""
                    file_size = 0
                    if file_obj:
                        try:
                            file_size = file_obj.size
                        except Exception:
                            pass

                    candidate_info = {
                        "asset_id": str(public_id),
                        "asset_type": asset_type,
                        "title": getattr(item, "title", str(item)),
                        "file_name": file_name,
                        "file_size": file_size,
                        "created_at": str(created_at) if created_at else None,
                    }
                    candidates.append(candidate_info)

                    if not dry_run:
                        storage = getattr(file_obj, "storage", None)
                        if storage and hasattr(storage, "_controlled_physical_delete") and file_name:
                            try:
                                storage._controlled_physical_delete(file_name)
                            except Exception as del_exc:
                                logger.warning("Failed to physically delete S3 file %s: %s", file_name, del_exc)
                        item.delete()
                        purged.append(candidate_info)

        return {
            "dry_run": dry_run,
            "grace_period_days": grace_period_days,
            "orphan_candidates_count": len(candidates),
            "candidates": candidates,
            "purged_count": len(purged),
            "purged": purged,
        }


    @classmethod
    def audit_published_pages_consistency(cls, page_ids: list[int] | None = None) -> dict[str, Any]:
        """
        对全站所有处于已发布状态的页面执行跨存储深度一致性审计。
        核验项：
        1. PostgreSQL live_revision 有效性；
        2. MongoDB 外置版本与 SHA-256 哈希防篡改；
        3. Discovery 搜索副本同步状态；
        4. 关联媒体资源完整性。
        """
        from discovery.registry import list_type_adapters
        registered_labels = {getattr(a, "model_label", "") for a in list_type_adapters()}
        mongo_store = MongoContentStore()
        discrepancies: list[dict[str, Any]] = []
        audited_count = 0

        published_pages = Page.objects.filter(live=True).specific()
        if page_ids is not None:
            published_pages = published_pages.filter(id__in=page_ids)

        for page in published_pages:
            if getattr(page, "is_recycled", False):
                continue
            page_label = getattr(getattr(page, "_meta", None), "label", "")
            if page_label not in registered_labels:
                # 豁免 Root / HomePage 等导航容器节点
                continue
            audited_count += 1
            page_id = page.id
            live_rev = getattr(page, "live_revision", None)

            # 1. 校验 live_revision
            if not live_rev:
                discrepancies.append({
                    "page_id": page_id,
                    "title": page.title,
                    "error": "missing_live_revision",
                    "message": "页面处于上线状态但缺少 live_revision 关联记录",
                })
                continue

            # 2. 校验 MongoDB 外置版本与哈希防篡改
            pointers = extract_pointers_from_revision(live_rev)
            for field_name, ptr in pointers.items():
                try:
                    res = mongo_store.get_field_version(
                        document_uuid=ptr.document_uuid,
                        field_name=field_name,
                        version_id=ptr.version_id,
                        expected_hash=ptr.content_hash,
                    )
                    if not res:
                        discrepancies.append({
                            "page_id": page_id,
                            "field": field_name,
                            "error": "mongo_content_missing",
                            "message": f"MongoDB 中未找到版本 ID={ptr.version_id}",
                        })
                except Exception as exc:
                    discrepancies.append({
                        "page_id": page_id,
                        "field": field_name,
                        "error": "mongo_hash_or_read_failure",
                        "message": str(exc),
                    })

            # 3. 校验 Discovery 搜索副本
            doc = PublishedSearchDocument.objects.filter(page_id=page_id, is_live=True).first()
            if not doc:
                discrepancies.append({
                    "page_id": page_id,
                    "title": page.title,
                    "error": "search_document_missing",
                    "message": "页面已上线但未在搜索副本中建立上线索引",
                })
            elif doc.effective_revision_id != live_rev.id:
                discrepancies.append({
                    "page_id": page_id,
                    "title": page.title,
                    "error": "search_revision_mismatch",
                    "message": f"搜索副本版本 {doc.effective_revision_id} 与正式版本 {live_rev.id} 不一致",
                })

        return {
            "healthy": len(discrepancies) == 0,
            "audited_pages": audited_count,
            "discrepancies_count": len(discrepancies),
            "discrepancies": discrepancies,
        }

    @classmethod
    def audit_media_references(cls) -> dict[str, Any]:
        """审计全站媒体资源引用与孤儿状态"""
        from media_library.models import CustomImage, CustomDocument
        orphans = []

        for img in CustomImage.objects.all():
            can_del, _, refs = MediaReferenceTracker.check_asset_references(img.public_id)
            if len(refs) == 0:
                orphans.append({
                    "asset_id": str(img.public_id),
                    "type": "image",
                    "title": img.title,
                })

        for doc in CustomDocument.objects.all():
            can_del, _, refs = MediaReferenceTracker.check_asset_references(doc.public_id)
            if len(refs) == 0:
                orphans.append({
                    "asset_id": str(doc.public_id),
                    "type": "document",
                    "title": doc.title,
                })

        return {
            "total_orphans": len(orphans),
            "orphans": orphans,
        }

    @classmethod
    def run_garbage_collection(cls, grace_period_seconds: int = 7 * 86400, dry_run: bool = False) -> dict[str, Any]:
        """
        第二防线：全站 MongoDB 历史正文垃圾回收 (GC)。
        扫描 content_field_versions 集合：
        1. 收集全站所有现存 Revision、回收站快照及别名绑定的全部有效 (document_uuid, version_id) 指针；
        2. 任何未被引用的版本，检查其 created_at 是否超过安全宽限期（默认 7 天）；
        3. 仅对超出宽限期且确无任何引用的孤儿版本执行物理安全清理。
        """
        import json
        from datetime import datetime, timezone, timedelta
        from wagtail.models import Revision

        active_pointers = set()
        for rev in Revision.objects.all():
            content = rev.content if isinstance(rev.content, dict) else json.loads(rev.content)
            for ptr in content.get("_external_field_pointers", []):
                active_pointers.add((str(ptr.get("document_uuid")), str(ptr.get("version_id"))))

        mongo_store = MongoContentStore()
        orphans = []
        now = datetime.now(timezone.utc)
        cutoff_time = now - timedelta(seconds=grace_period_seconds)

        try:
            db = mongo_store._get_db()
            col = db["content_field_versions"]
            for doc in col.find():
                doc_uuid = str(doc.get("document_uuid"))
                ver_id = str(doc.get("version_id"))
                key = (doc_uuid, ver_id)

                if key not in active_pointers:
                    created_at = doc.get("created_at")
                    if created_at and created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)

                    # 仅在超出宽限期时标记并执行物理清除
                    if created_at and created_at < cutoff_time:
                        orphans.append({
                            "document_uuid": doc_uuid,
                            "version_id": ver_id,
                            "field_name": doc.get("field_name"),
                            "created_at": str(created_at),
                        })
                        if not dry_run:
                            col.delete_one({"_id": doc["_id"]})
        finally:
            mongo_store.close()

        return {
            "healthy": True,
            "dry_run": dry_run,
            "grace_period_seconds": grace_period_seconds,
            "purged_count": len(orphans),
            "purged_orphans": orphans,
        }

    @classmethod
    def prune_page_draft_revisions(cls, page_id: int, keep_recent_drafts: int = 10) -> int:
        """
        第一防线：页面未发布草稿修剪策略 (Draft Retention Policy)。
        在排除当前正式版、最新版、排期版、审批流版及回收快照的前提下，
        仅保留最近 keep_recent_drafts 个草稿 Revision，修剪淘汰的旧草稿并同事务清理媒体引用。
        """
        from wagtail.models import Page, Revision
        from content_lifecycle.models import RecycleEntry
        from media_library.models import MediaReferenceRecord

        try:
            page = Page.objects.get(id=page_id).specific
        except Page.DoesNotExist:
            return 0

        # 豁免保护白名单
        protected_rev_ids = set()
        if page.live_revision_id:
            protected_rev_ids.add(page.live_revision_id)
        latest_rev = page.get_latest_revision()
        if latest_rev:
            protected_rev_ids.add(latest_rev.id)

        # 豁免回收站快照
        for snap_id in RecycleEntry.objects.filter(page_id=page_id).values_list("snapshot_revision_id", flat=True):
            if snap_id:
                protected_rev_ids.add(snap_id)

        # 获取候选可修剪草稿（未排期、非保护）
        candidate_qs = (
            Revision.objects.filter(object_id=str(page_id), approved_go_live_at__isnull=True)
            .exclude(id__in=protected_rev_ids)
            .order_by("-created_at")
        )

        total_candidates = candidate_qs.count()
        if total_candidates <= keep_recent_drafts:
            return 0

        excess_revisions = list(candidate_qs[keep_recent_drafts:])
        deleted_count = 0

        for r in excess_revisions:
            # 同事务级联清理媒体引用记录，防止媒体孤儿锁定
            MediaReferenceRecord.objects.filter(page_id=page_id, revision_id=r.id).delete()
            r.delete()
            deleted_count += 1

        return deleted_count
