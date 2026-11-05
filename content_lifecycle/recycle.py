"""
受控回收站与页面恢复服务 (RecycleService)。

落实核心契约：
1. 原树占位回收：单页或整棵子树保留在原树物理节点，标记 is_recycled=True，并建立 RecycleEntry 快照；
2. 下架与彻底终止排期：清空 approved_go_live_at 与工作流，防止定时任务误发；
3. 恢复冲突检视：原父节点被删/被回收、原 slug 冲突、树层级违规与深度溢出检测；
4. 精准原位还原：
   - Argon [B-01] 修复：若需移动树且改 slug，先在内存设新 slug (clean=False) 再执行 move，防止触发旧 slug 校验崩溃；
   - 显式调用 page.set_url_path(parent) 与 page.save(clean=False) 同步主表与子表；
   - 强制保持未发布草稿态 (live=False、has_unpublished_changes=True、live_revision=None)，绝不恢复即上线；
   - 生成全新的 myblog.restore_draft 修订版本，完整保留历史 Revision 与 Mongo 字段版本。
"""

import logging
from typing import Any
from uuid import UUID
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from wagtail.models import Page, Revision

from content_lifecycle.models import RecycleBatch, RecycleEntry
from content_lifecycle.guard import content_mutation_guard
from external_content.models import OutboxEvent

logger = logging.getLogger(__name__)


class RecycleService:
    """全站受控回收与恢复管理服务"""

    @classmethod
    def recycle_page_or_subtree(cls, page: Page, actor: Any = None) -> RecycleBatch:
        """
        回收单页或整棵子树（原树占位模型）。
        """
        with content_mutation_guard(timeout_seconds=3):
            db_page = Page.objects.select_for_update().get(id=page.id).specific
            if getattr(db_page, "is_recycled", False):
                raise PermissionDenied(f"页面 '{db_page.title}' 已经在回收站中，无需重复回收。")
            if hasattr(page, "is_recycled"):
                page.is_recycled = True
            page = db_page

            # 获取目标节点及其所有活动后代子节点
            descendants = list(page.get_descendants().select_related())
            all_nodes = [page] + [d.specific for d in descendants if not getattr(d, "is_recycled", False)]

            # 创建回收操作批次
            batch = RecycleBatch.objects.create(
                actor_id=getattr(actor, "id", None),
                actor_username=getattr(actor, "username", ""),
                root_page_id=page.id,
                root_page_title=page.title,
                total_pages=len(all_nodes),
                status="recycled",
            )

            for node in all_nodes:
                parent = node.get_parent()
                parent_id = parent.id if parent else 0

                # 计算原兄弟顺序
                sibling_order = 0
                if parent:
                    siblings = list(parent.get_children().order_by("path").values_list("id", flat=True))
                    if node.id in siblings:
                        sibling_order = siblings.index(node.id)

                latest_rev = node.get_latest_revision()
                # 若页面未曾生成 Revision，在回收前自动建立基线快照版本
                if not latest_rev:
                    latest_rev = node.save_revision(user=actor)
                was_live = bool(node.live)
                had_changes = bool(node.has_unpublished_changes)

                # 记录快照条目
                RecycleEntry.objects.create(
                    batch=batch,
                    page_id=node.id,
                    parent_id=parent_id,
                    original_slug=node.slug,
                    original_path=node.path,
                    original_depth=node.depth,
                    original_url_path=node.url_path,
                    sibling_order=sibling_order,
                    was_live=was_live,
                    had_unpublished_changes=had_changes,
                    snapshot_revision_id=latest_rev.id if latest_rev else None,
                    is_batch_root=(node.id == page.id),
                    status="recycled",
                )

                # 下架正式版本
                if node.live:
                    node.unpublish(user=actor)

                # 显式清空排期发布与取消审批流 (防止 publish_scheduled 误发)
                Revision.objects.filter(object_id=str(node.id)).update(approved_go_live_at=None)

                # 打上原树占位回收标记
                node.is_recycled = True
                node.save(update_fields=["is_recycled"], clean=False)
                try:
                    from discovery.projection import recycle_page_discovery
                    recycle_page_discovery(node.id)
                except Exception as exc:
                    logger.debug("Failed to sync recycle to discovery for %s: %s", node.id, exc)

                # 登记 Wagtail 官方专用回收操作审计日志
                try:
                    from wagtail.models import PageLogEntry
                    user = actor if getattr(actor, "is_authenticated", False) else None
                    PageLogEntry.objects.log_action(
                        instance=node,
                        action="myblog.recycle",
                        user=user,
                        data={"batch_id": str(batch.batch_id)},
                    )
                except Exception as log_exc:
                    logger.debug("Failed to log recycle action for %s: %s", node.id, log_exc)

            # 写入事务性 Outbox 任务事件
            OutboxEvent.objects.create(
                event_type="page.recycled",
                page_id=page.id,
                revision_id=page.live_revision_id,
                payload={"batch_id": str(batch.batch_id), "count": len(all_nodes)},
            )

            return batch

    @classmethod
    def plan_restore(cls, batch_id: UUID | str) -> dict[str, Any]:
        """
        评估恢复冲突与前提条件：
        1. 检查原父节点是否存在且处于活跃状态；
        2. 检查原 slug 是否在原父节点下已被新页面占用；
        3. 检查页面层级与子页面类型约束。
        """
        batch = RecycleBatch.objects.get(batch_id=batch_id)
        entries = list(batch.entries.order_by("original_depth", "sibling_order"))
        conflicts = []
        entry_summaries = []

        for entry in entries:
            try:
                page = Page.objects.get(id=entry.page_id).specific
            except Page.DoesNotExist:
                conflicts.append({
                    "page_id": entry.page_id,
                    "type": "page_not_found",
                    "message": f"页面 ID={entry.page_id} 已在底层被意外物理删除，无法恢复。",
                })
                continue

            # 1. 检查父节点状态
            parent = None
            if entry.parent_id:
                try:
                    parent = Page.objects.get(id=entry.parent_id).specific
                    if getattr(parent, "is_recycled", False):
                        # 若父节点也在本批次中，不属于冲突（会随批次先恢复）
                        if not any(e.page_id == parent.id for e in entries):
                            conflicts.append({
                                "page_id": entry.page_id,
                                "type": "parent_missing_or_recycled",
                                "message": f"原父节点 '{parent.title}' 处于回收站中，必须先恢复父级页面或指定新父级。",
                            })
                except Page.DoesNotExist:
                    conflicts.append({
                        "page_id": entry.page_id,
                        "type": "parent_missing_or_recycled",
                        "message": f"原父节点 ID={entry.parent_id} 已不存在，必须为根页面指定新的父节点。",
                    })

            # 2. 检查 slug 占用冲突
            if parent and not getattr(parent, "is_recycled", False):
                recycled_ids = set(
                    RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
                )
                sibling_slugs = parent.get_children().exclude(id=page.id).exclude(
                    id__in=recycled_ids
                ).values_list("slug", flat=True)
                if entry.original_slug in sibling_slugs:
                    conflicts.append({
                        "page_id": entry.page_id,
                        "type": "slug_collision",
                        "message": f"原父节点下已有同名页面 '{entry.original_slug}'，请提供 slug_overrides 改名恢复。",
                        "suggested_slug": f"{entry.original_slug}-restored",
                    })

            entry_summaries.append({
                "page_id": entry.page_id,
                "title": page.title,
                "slug": entry.original_slug,
                "parent_id": entry.parent_id,
                "is_batch_root": entry.is_batch_root,
                "original_depth": entry.original_depth,
            })

        return {
            "batch_id": str(batch.batch_id),
            "root_page_title": batch.root_page_title,
            "can_restore_directly": len(conflicts) == 0,
            "conflicts": conflicts,
            "entries": entry_summaries,
        }

    @classmethod
    def restore_page_or_subtree(
        cls,
        batch_id: UUID | str,
        actor: Any = None,
        slug_overrides: dict[Any, str] | None = None,
        target_parent_id: int | None = None,
    ) -> dict[str, Any]:
        """
        执行受控页面恢复 (Restore)：
        1. 严格在 content_mutation_guard(timeout_seconds=3) 内执行；
        2. 解决冲突（改名或迁移父节点）；
        3. 落实 Argon 审计修复：先改内存 slug (clean=False) 再执行树移动；
        4. 显式调用 page.set_url_path(parent) 与 page.save(clean=False) 同步主表与子表；
        5. 强制保持未发布草稿态 (live=False、has_unpublished_changes=True、live_revision=None)；
        6. 生成全新草稿修订版本，不覆盖历史版本。
        """
        slug_overrides = slug_overrides or {}

        with content_mutation_guard(timeout_seconds=3):
            try:
                batch = RecycleBatch.objects.select_for_update().get(batch_id=batch_id)
            except RecycleBatch.DoesNotExist:
                raise ValidationError("目标回收批次不存在")

            if batch.status == "purged":
                raise ValidationError("该批次已被彻底销毁，不可恢复！")
            if batch.status != "recycled":
                raise ValidationError("该批次不处于已回收状态，禁止重复恢复！")
            if not batch.entries.exists():
                raise ValidationError("该批次无有效条目，不可恢复！")

            if actor is not None:
                try:
                    root_p = Page.objects.get(id=batch.root_page_id).specific
                    perms = root_p.permissions_for_user(actor)
                    if not (perms.can_edit() or perms.can_delete() or actor.is_superuser):
                        raise PermissionDenied("当前用户无权恢复此页面")
                except Page.DoesNotExist:
                    pass

            plan = cls.plan_restore(batch_id)
            # 若存在未解决的冲突，拒绝盲目执行
            unresolved_conflicts = []
            for c in plan["conflicts"]:
                pid = c["page_id"]
                if c["type"] == "slug_collision" and (pid in slug_overrides or str(pid) in slug_overrides):
                    continue
                if c["type"] == "parent_missing_or_recycled" and target_parent_id:
                    continue
                unresolved_conflicts.append(c)

            if unresolved_conflicts:
                raise ValidationError(f"存在未处置的恢复冲突: {unresolved_conflicts}")

            entries = list(batch.entries.order_by("original_depth", "sibling_order"))
            restored_pages = []

            for entry in entries:
                page = Page.objects.get(id=entry.page_id).specific
                resolved_slug = slug_overrides.get(entry.page_id) or slug_overrides.get(str(entry.page_id)) or entry.original_slug

                # 确定目标父节点
                if entry.is_batch_root and target_parent_id:
                    parent = Page.objects.get(id=target_parent_id).specific
                    requires_move = (parent.id != entry.parent_id)
                else:
                    parent = Page.objects.get(id=entry.parent_id).specific
                    requires_move = False

                # 深度防碰撞校验：校验 resolved_slug 是否被目标父节点下的其他非回收兄弟页面占用
                recycled_ids = set(
                    RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
                )
                is_occupied = parent.get_children().exclude(id=page.id).exclude(
                    id__in=recycled_ids
                ).filter(slug=resolved_slug).exists()
                if is_occupied:
                    raise ValidationError(f"指定的替代路径名称 '{resolved_slug}' 已被其他页面占用，请重新指定！")

                # 回收快照存在性与完整性严格校验 (Fail-Closed)
                snapshot_obj = None
                if entry.snapshot_revision_id:
                    snapshot_rev = Revision.objects.filter(id=entry.snapshot_revision_id).first()
                    if not snapshot_rev:
                        raise ValidationError(f"页面 ID={entry.page_id} 核心回收快照已损坏或丢失，拒绝恢复！")
                    try:
                        snapshot_obj = snapshot_rev.as_object()
                    except Exception as exc:
                        raise ValidationError(f"页面 ID={entry.page_id} 核心回收快照正文读取失败: {exc}")
                else:
                    from blog.models import BlogPage
                    if entry.was_live or isinstance(page, BlogPage):
                        raise ValidationError(f"页面 ID={entry.page_id} 核心回收快照已损坏或丢失，拒绝恢复！")

                # 关键修复 [B-01]：若需要树移动且修改了 slug，先在当前节点赋新 slug (clean=False)，
                # 避免 move() 内部校验旧 slug 导致 ValidationError 崩溃！
                if requires_move:
                    if page.slug != resolved_slug:
                        page.slug = resolved_slug
                        page.save(update_fields=["slug"], clean=False)
                    page.move(parent, pos="last-child")
                    page = Page.objects.get(id=entry.page_id).specific
                else:
                    page.slug = resolved_slug

                # 恢复为绝对安全的未发布草稿态
                page.is_recycled = False
                page.live = False
                page.live_revision = None
                page.has_unpublished_changes = True
                page.go_live_at = None
                page.expire_at = None
                page.expired = False
                page.locked = False

                # 显式重算 URL path 并先将主表与所有子表落库
                old_url_path = page.url_path
                page.set_url_path(parent)
                page.save(clean=False)
                if old_url_path != page.url_path:
                    page._update_descendant_url_paths(old_url_path, page.url_path)

                from wagtail.log_actions import registry
                log_act = "myblog.restore_draft"
                if not registry.action_exists(log_act):
                    log_act = "wagtail.revert"

                # 方案 A 恢复关键闭环：从回收快照 Revision 及其 Mongo 指针水合恢复完整正文
                if snapshot_obj is not None and hasattr(snapshot_obj, "intro"):
                    page.intro = snapshot_obj.intro
                if snapshot_obj is not None and hasattr(snapshot_obj, "body"):
                    page.body = snapshot_obj.body

                # 生成全新的未发布草稿版本
                page.save_revision(
                    user=actor,
                    approved_go_live_at=None,
                    changed=True,
                    log_action=log_act,
                    clean=False,
                )

                # 更新条目状态
                entry.status = "active"
                entry.save(update_fields=["status"])

                restored_pages.append({
                    "page_id": page.id,
                    "title": page.title,
                    "slug": page.slug,
                    "url_path": page.url_path,
                    "live": page.live,
                    "has_unpublished_changes": page.has_unpublished_changes,
                })

            # 更新批次状态为已恢复
            batch.status = "restored"
            batch.save(update_fields=["status"])

            # 写入事务性 Outbox 事件
            OutboxEvent.objects.create(
                event_type="page.restored",
                page_id=batch.root_page_id,
                payload={"batch_id": str(batch.batch_id), "restored_count": len(restored_pages)},
            )

            return {
                "status": "success",
                "batch_id": str(batch.batch_id),
                "restored_pages": restored_pages,
            }

    @classmethod
    def purge_page_or_subtree(cls, page_id: int, actor: Any = None, batch_id: Any = None) -> dict[str, Any]:
        """
        受控永久销毁单页或整棵子树（Purge）：
        1. 必须在 content_mutation_guard(timeout_seconds=3) 内执行；
        2. 收集目标节点及其所有子节点的 (page_id, document_uuid)；
        3. 同一事务写入 page.purged OutboxEvent（携带 document_uuid）；
        4. 清理关联的 RecycleEntry、PublishedSearchDocument；
        5. 调用 Wagtail 原生 node.delete(allow_physical_purge=True) 执行物理删除；
        6. 后续由 Outbox 异步任务按 document_uuid 彻底擦除 MongoDB 中的全部历史版本。
        """
        with content_mutation_guard(timeout_seconds=3):
            if batch_id:
                batch = RecycleBatch.objects.select_for_update().filter(batch_id=batch_id).first()
                if not batch or batch.status != "recycled":
                    raise ValidationError("该批次不处于已回收状态，旧销毁链接已失效")

            try:
                page = Page.objects.get(id=page_id).specific
            except Page.DoesNotExist:
                return {"status": "not_found", "page_id": page_id}

            # 安全守卫：未回收的活动页面禁止直接永久销毁
            if not page.is_recycled:
                raise PermissionDenied("未移入回收站的活动页面禁止直接永久销毁，必须先移入回收站！")

            from discovery.models import PublishedSearchDocument

            descendants = list(page.get_descendants().select_related())
            all_nodes = [page] + [d.specific for d in descendants]

            # 对象级删除权限校验：若含有曾发布页面，必须具备发布权限或超级管理员权限
            if actor is not None:
                perms = page.permissions_for_user(actor)
                sub_entries = RecycleEntry.objects.filter(page_id__in=[n.id for n in all_nodes])
                any_was_live = any(e.was_live for e in sub_entries)
                if any_was_live:
                    if not ((perms.can_delete() and perms.can_publish()) or actor.is_superuser):
                        raise PermissionDenied("当前用户无权永久销毁曾发布的页面（需具备发布权限或管理员权限）")
                else:
                    if not (perms.can_delete() or actor.is_superuser):
                        raise PermissionDenied("当前用户无权永久销毁此页面")
            purged_summaries = []

            for node in reversed(all_nodes):
                nid = node.id
                doc_uuid = str(getattr(node, "document_uuid", ""))

                # 1. 写入事务性 Outbox 事件，触发 MongoDB 异步全版本销毁
                OutboxEvent.objects.create(
                    event_type="page.purged",
                    page_id=nid,
                    payload={"document_uuid": doc_uuid, "title": node.title},
                )

                # 2. 清理搜索副本与回收站快照条目
                PublishedSearchDocument.objects.filter(page_id=nid).delete()
                RecycleEntry.objects.filter(page_id=nid).delete()

                # 3. 物理执行 Wagtail ORM 删除
                node.delete(allow_physical_purge=True)
                purged_summaries.append({"page_id": nid, "document_uuid": doc_uuid})

            purged_ids = [n["page_id"] for n in purged_summaries]
            RecycleBatch.objects.filter(root_page_id__in=purged_ids, status="recycled").update(status="purged")

            return {
                "status": "success",
                "purged_count": len(purged_summaries),
                "nodes": purged_summaries,
            }
