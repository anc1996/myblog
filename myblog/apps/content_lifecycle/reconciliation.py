"""
跨存储一致性审计与离线对账服务 (CrossStorageReconciler)。

【教学架构导读】：
在典型的现代化多存储系统（PostgreSQL 关系库 + MongoDB 文档库 + RustFS S3 对象存储）中，
最大的技术挑战之一就是“跨存储一致性与垃圾回收”。
如果系统只管写数据，时间久了就会出现三大痛点：
1. “幽灵版本”：编辑草稿修剪后，Mongo 中遗留了大量不再被任何 Revision 引用的陈旧正文快照；
2. “孤儿文件”：文章删除了，但当时上传的配图或视频文件依然占着 S3 磁盘空间；
3. “版本串号或损坏”：数据库里记录着发布版本，但 Mongo 或 S3 的对应实体可能由于硬件故障丢失或损坏。

本模块是全站的“纪检监察与对账巡检器”，提供三道严密的审计与治理防线：
- 第一防线：单页面草稿修剪（Draft Retention Policy）；
- 第二防线：全站 MongoDB 孤儿版本垃圾回收（GC）；
- 零引用安全网：受控物理擦除媒体资源（Zero-Reference Controlled Purge）。
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
    """
    全站跨存储数据一致性核对与对账器（静态服务类）。
    负责离线巡检、差异告警、孤儿媒体受控擦除与历史版本 GC。
    """

    @classmethod
    def reconcile_and_purge_orphan_media(
        cls,
        dry_run: bool = True,
        grace_period_days: int = 30,
    ) -> dict[str, Any]:
        """
        受控对账并处置全站零引用的孤儿媒体资源（RECONCILE-MEDIA-02）。

        【教学点与安全规范】：
        为什么媒体资源绝对禁止直接物理删除？
        因为图片/文档可能在某篇历史草稿或回收站页面中依然被使用。
        本方法执行严苛的“双重安全核查”：
        1. 跨全站所有模型（页面正文、历史版本、回收站、头像）全盘核查，确认全站引用数 strictly == 0；
        2. 宽限期保护：必须是创建时间超过 grace_period_days（默认 30 天）的孤儿，防止作者刚上传图片还没点保存就被当成垃圾删掉；
        3. dry_run 演练机制：默认 True，仅演练统计待清理清单，绝不擅自动磁盘；只有传 False 时才真正物理擦除。

        【参数与变量解析】：
        - dry_run (bool): 是否仅作演练输出（默认 True，安全第一）；
        - grace_period_days (int): 安全宽限天数（默认 30 天）；
        - cutoff (datetime): 宽限期时间阈值，早于该时刻创建的孤儿才被允许清理；
        - candidates (list[dict]): 扫描出的符合清理条件的孤儿资产信息列表；
        - purged (list[dict]): 实际执行物理擦除的资产列表（dry_run=True 时保持为空）；
        - models_to_check: 涵盖图片 (CustomImage)、文档 (CustomDocument)、音频 (Audio)、视频 (Video) 全种类。
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
                # 1. 深度核查引用计数：跨全站历史版本核实
                can_delete, _, refs = MediaReferenceTracker.check_asset_references(public_id)
                if can_delete and len(refs) == 0:
                    created_at = getattr(item, "created_at", None)
                    if created_at and created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    # 2. 宽限期防护：刚上传未满宽限期的新文件绝对豁免清理
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

                    # 3. 经授权后的底层受控物理擦除
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
        对全站已发布页面执行“四位一体”跨存储深度一致性对账巡检。

        【对账四大指标】：
        1. PostgreSQL: live_revision 外键是否存在且有效；
        2. MongoDB: 指针对应的外置正文是否存在，且重新计算的 SHA-256 必须严格与指针哈希匹配（防篡改）；
        3. Discovery: 搜索副本表（PublishedSearchDocument）中是否存在该页面，且索引版本与正式发布版本是否完全吻合；
        4. 排除项：自动豁免非内容容器节点（如 Root 树根或 HomePage 聚合大厅）。

        【参数与变量解析】：
        - page_ids (list[int] | None): 可选指定的页面 ID 列表（传 None 时审计全站所有发布页面）；
        - discrepancies (list[dict]): 核心审计输出，收集到的所有数据不一致或损坏的异常项清单；
        - audited_count (int): 实际完成核验的有效内容页面总数。
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
                # 教学点：豁免纯拓扑容器节点（例如 Root 或 HomePage，无正文脱水）
                continue
            audited_count += 1
            page_id = page.id
            live_rev = getattr(page, "live_revision", None)

            # 指标 1：校验 live_revision 存在性
            if not live_rev:
                discrepancies.append({
                    "page_id": page_id,
                    "title": page.title,
                    "error": "missing_live_revision",
                    "message": "页面处于上线状态但缺少 live_revision 关联记录",
                })
                continue

            # 指标 2：校验 MongoDB 外置正文完整性与内容防篡改
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

            # 指标 3：校验 Discovery 搜索副本索引一致性
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
        """审计全站图片与文档的孤儿引用状态，快速定位闲置资源"""
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
        【第二防线】：全站 MongoDB 历史正文垃圾回收（GC 机制）。

        【原理详解】：
        在脱水架构中，作者编辑保存草稿会持续向 MongoDB 写入新版本快照。
        当旧草稿被第一防线修剪后，Mongo 中就会残留“无任何 Revision 指向的版本记录”。

        【GC 回收流程】：
        1. 搜集活体指纹：全表扫描 PostgreSQL 的 Revision 表，把所有在用的 (document_uuid, version_id) 装入集合 active_pointers；
        2. 识别孤儿版本：遍历 MongoDB 的 content_field_versions 集合，凡是不在 active_pointers 中的均判定为孤儿；
        3. 7 天安全缓冲（Grace Period）：如果该版本刚生成不到 7 天，即便无指针指向也先保留（防止并发写入过程中的竞态条件误判）；
        4. 物理清理：对超过 7 天缓冲期且确无引用的孤儿版本执行 col.delete_one()，彻底回收 Mongo 存储容量。
        """
        import json
        from datetime import datetime, timezone, timedelta
        from wagtail.models import Revision

        # 1. 搜集当前全站所有存活 Revision 里的合法外置指针
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

                # 2. 该 Mongo 记录未被任何 Revision 引用
                if key not in active_pointers:
                    created_at = doc.get("created_at")
                    if created_at and created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)

                    # 3. 满足 7 天安全缓冲期，确认为废弃版本
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
        【第一防线】：单页面未发布草稿历史修剪策略（Draft Retention Policy）。

        【教学点与安全白名单】：
        文章编辑过程中会产生大量的历史草稿。如果不加节制，单篇文章可能产生几千个 Revision。
        修剪策略必须拥有“绝对豁免白名单”（Protected Revisions）：
        1. 线上正式生效的版本（page.live_revision_id）—— 绝不能动！
        2. 最新的草稿版本（page.get_latest_revision()）—— 绝不能动！
        3. 带有定时排期发布或正在处于工作流审批中的版本 —— 绝不能动！
        4. 回收站记录快照引用的版本 —— 绝不能动！

        在排除上述受保护版本后，仅保留最近生成的 keep_recent_drafts（默认 10 个）历史版本，
        淘汰更早的冗余草稿，并在同一本地事务中级联清理对应的 MediaReferenceRecord 引用记录。
        """
        from wagtail.models import Page, Revision
        from content_lifecycle.models import RecycleEntry
        from media_library.models import MediaReferenceRecord

        try:
            page = Page.objects.get(id=page_id).specific
        except Page.DoesNotExist:
            return 0

        # 1. 建立核心白名单：正式发布版与最新草稿版
        protected_rev_ids = set()
        if page.live_revision_id:
            protected_rev_ids.add(page.live_revision_id)
        latest_rev = page.get_latest_revision()
        if latest_rev:
            protected_rev_ids.add(latest_rev.id)

        # 2. 豁免回收站还原快照所引用的版本
        for snap_id in RecycleEntry.objects.filter(page_id=page_id).values_list("snapshot_revision_id", flat=True):
            if snap_id:
                protected_rev_ids.add(snap_id)

        # 3. 过滤可安全修剪的普通无排期草稿
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

        # 4. 逐个淘汰超期草稿，同步清理媒体引用
        for r in excess_revisions:
            MediaReferenceRecord.objects.filter(page_id=page_id, revision_id=r.id).delete()
            r.delete()
            deleted_count += 1

        return deleted_count

    @classmethod
    def reconcile_orphan_avatars(
        cls,
        *,
        dry_run: bool = True,
        grace_period_seconds: int = 86400,
        storage_aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        定期扫描 member-avatars/ 与 avatar_images/ 前缀下的孤儿头像文件（默认 24 小时宽限期）。
        - 排除 PostgreSQL 中所有 MemberProfile（含停用账号）及 UserProfile.avatar 正在引用的对象；
        - 遇到无法确定时间或枚举出错的对象保留并告警；
        - dry_run=True 仅演练输出候选清单；dry_run=False 先入队再经受控清理接口擦除。
        """
        from datetime import datetime, timezone as dt_timezone, timedelta
        from uuid import uuid4
        from identity.avatar_purge import AvatarPurgeProcessor, enqueue_avatar_purge_task
        from identity.avatar_storage import get_storage_and_namespace, list_prefix_objects
        from identity.models import MemberProfile

        aliases = storage_aliases or ["default"]
        prefixes = ("member-avatars/", "avatar_images/")
        now = datetime.now(dt_timezone.utc)
        cutoff = now - timedelta(seconds=grace_period_seconds)

        candidates: list[dict[str, Any]] = []
        purged: list[dict[str, Any]] = []
        warnings: list[str] = []
        op_id = uuid4()

        from django.apps import apps

        wagtail_userprofile_cls = None
        wagtail_avatar_keys: set[str] = set()
        if apps.is_installed("wagtail.users"):
            try:
                from wagtail.users.models import UserProfile

                wagtail_userprofile_cls = UserProfile
                wagtail_avatar_keys = {
                    k for k in UserProfile.objects.exclude(avatar="").values_list("avatar", flat=True) if k
                }
            except Exception as exc:
                warnings.append(f"查询 UserProfile 头像引用失败，已中止孤儿清理以防误删: {exc}")
                return {
                    "healthy": False,
                    "aborted": True,
                    "dry_run": dry_run,
                    "grace_period_seconds": grace_period_seconds,
                    "candidates_count": 0,
                    "candidates": [],
                    "purged_count": 0,
                    "purged": [],
                    "purge_stats": {"claimed": 0, "done": 0, "blocked": 0, "failed": 0},
                    "warnings": warnings,
                }

        for alias in aliases:
            try:
                storage, current_ns = get_storage_and_namespace(alias)
            except Exception as exc:
                warnings.append(f"无法读取存储别名 {alias}: {exc}")
                continue

            active_member_keys = set(
                MemberProfile.objects.filter(avatar_storage_namespace=current_ns)
                .exclude(avatar_key="")
                .values_list("avatar_key", flat=True)
            )
            in_use_keys = active_member_keys | wagtail_avatar_keys

            for prefix in prefixes:
                objects, list_warnings = list_prefix_objects(storage, prefix)
                warnings.extend(list_warnings)

                for obj in objects:
                    key = obj["key"]
                    if key in in_use_keys:
                        continue
                    lm = obj.get("last_modified")
                    if lm is None:
                        warnings.append(f"对象 {key} 缺少可靠修改时间，保守跳过清理")
                        continue
                    if lm > cutoff:
                        continue

                    info = {
                        "storage_alias": alias,
                        "storage_namespace": current_ns,
                        "storage_key": key,
                        "size": obj.get("size", 0),
                        "last_modified": lm.isoformat(),
                    }
                    candidates.append(info)

                    if not dry_run:
                        # 入队前二次复核引用，防止对账扫描期间并发绑定头像
                        still_in_use = (
                            MemberProfile.objects.filter(
                                avatar_storage_namespace=current_ns,
                                avatar_key=key,
                            ).exists()
                            or (
                                wagtail_userprofile_cls is not None
                                and wagtail_userprofile_cls.objects.filter(avatar=key).exists()
                            )
                        )
                        if still_in_use:
                            continue
                        enqueue_avatar_purge_task(
                            storage_alias=alias,
                            storage_namespace=current_ns,
                            storage_key=key,
                            source="orphan_reconcile",
                            operation_id=op_id,
                        )
                        purged.append(info)

        purge_stats = {"claimed": 0, "done": 0, "blocked": 0, "failed": 0}
        if not dry_run and purged:
            purge_stats = AvatarPurgeProcessor.process_pending_tasks(batch_size=max(50, len(purged)))

        return {
            "dry_run": dry_run,
            "grace_period_seconds": grace_period_seconds,
            "candidates_count": len(candidates),
            "candidates": candidates,
            "purged_count": len(purged),
            "purged": purged,
            "purge_stats": purge_stats,
            "warnings": warnings,
        }
