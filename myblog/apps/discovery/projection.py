"""
全站内容发布副本投影与同步服务 (Projection Service)。

落实契约：
1. 仅从已发布的有效版本 (live_revision) 提取纯文本与标签数据；
2. 严格隔离未发布的草稿修改与回收站页面；
3. 采用 SEARCH-CHUNK-02 规则切分文本块，保存带 256 重叠的 normalized_text 与不重叠的 search_vector；
4. 任何投影更新、下架或回收操作均原子自增 ContentDiscoveryEpoch，驱动前台游标失效与自动刷新。
"""

import logging
import uuid
from typing import Any
from django.db import transaction
from django.db.models import Value
from django.contrib.postgres.search import SearchVector

from discovery.models import (
    ContentDiscoveryEpoch,
    PublishedSearchDocument,
    PublishedSearchChunk,
    PublishedSearchTag,
    PublishedSearchAuthor,
)
from discovery.analysis import chunk_text_for_search
from discovery.registry import is_author_discovery_enabled, list_type_adapters
from people.attribution import (
    AUTHOR_PROJECTION_VERSION,
    build_projection_author_payload,
    extract_raw_author_refs_from_page,
    resolve_author_attributions,
)

logger = logging.getLogger(__name__)


def _find_adapter_for_page(page: Any):
    """在已注册的适配器中查找匹配当前 Page 模型的适配器"""
    page_label = getattr(getattr(page, "_meta", None), "label", "")
    for adapter in list_type_adapters():
        if getattr(adapter, "model_label", "") == page_label:
            return adapter
    return None


def project_page_to_discovery(page: Any, index_generation: int = 1) -> PublishedSearchDocument | None:
    """
    将已发布的页面投影到全站搜索副本表：
    1. 严格从数据库重新读取主表状态并排除别名页与未上线页面；
    2. 锁外从 live_revision.as_object() 还原正式快照并提取正文（不持写锁等待 MongoDB I/O）；
    3. 锁内重读 Page 状态与 live_revision_id 核验未漂移后，同事务原子写入文档、分块、标签与作者关系；
    4. 仅在可见内容或关系发生实际变化时递增全局 Epoch，避免幂等重放误刷游标。
    """
    from wagtail.models import Page

    page_id = getattr(page, "id", None)
    if not page_id:
        return None

    for attempt in range(2):
        # 1. 严格重读数据库当前 Page 主表状态，拒绝直接信任内存脏实例与 Alias (BLK-04)
        db_page = Page.objects.filter(id=page_id).select_related("live_revision").first()
        if not db_page or db_page.alias_of_id is not None:
            unpublish_page_discovery(page_id)
            return None

        specific_page = db_page.specific
        is_live = getattr(specific_page, "live", False) and (getattr(specific_page, "live_revision_id", None) is not None)
        is_recycled = getattr(specific_page, "is_recycled", False)

        if (not is_live) or is_recycled:
            unpublish_page_discovery(page_id)
            return None

        adapter = _find_adapter_for_page(specific_page)
        if not adapter:
            logger.debug("No TypeAdapter registered for page %s (%s)", page_id, getattr(specific_page, "title", ""))
            return None

        # 2. 锁外：从 live_revision.as_object() 还原正式快照并触发外置正文提取
        live_rev = specific_page.live_revision
        if str(live_rev.object_id) != str(specific_page.id):
            raise ValueError(f"Revision object_id mismatch for page {specific_page.id}")
        published_snapshot = live_rev.as_object()
        payload = adapter.extract(published_snapshot)

        site_id = None
        try:
            if hasattr(specific_page, "get_site"):
                site = specific_page.get_site()
                if site is None:
                    from wagtail.models import Site
                    Site.clear_site_root_paths_cache()
                    site = specific_page.get_site()
                if site:
                    site_id = site.id
        except Exception:
            site_id = None

        actual_url = payload.url_path
        if not actual_url or actual_url.startswith("/home/"):
            resolved_url = specific_page.get_url() if hasattr(specific_page, "get_url") else None
            actual_url = resolved_url or getattr(specific_page, "url_path", None) or "/"
            if actual_url.startswith("/home/"):
                actual_url = actual_url[5:] or "/"

        merged_extra = dict(payload.extra_data or {})
        author_rows_spec: list[tuple[int, str, str, str, int]] = []
        if is_author_discovery_enabled(adapter):
            raw_refs = list(payload.author_refs or [])
            if not raw_refs:
                raw_refs = extract_raw_author_refs_from_page(published_snapshot)
            resolved_authors = resolve_author_attributions(
                raw_refs,
                revision_id=live_rev.id,
                allow_anonymous=True,
            )
            authors_json, author_slugs = build_projection_author_payload(resolved_authors)
            merged_extra["authors"] = authors_json
            merged_extra["author_slugs"] = author_slugs
            merged_extra["author_projection_version"] = AUTHOR_PROJECTION_VERSION
            author_rows_spec = [
                (a.person_id, a.name, a.slug, a.role, a.sort_order)
                for a in resolved_authors
            ]

        # 3. 锁内：重读 Page 核验 live_revision_id 与回收/树状态未在提取期间漂移
        with transaction.atomic():
            locked_page = Page.objects.select_for_update().filter(id=page_id).first()
            if not locked_page or locked_page.alias_of_id is not None:
                unpublish_page_discovery(page_id)
                return None
            locked_specific = locked_page.specific
            if (
                not getattr(locked_specific, "live", False)
                or getattr(locked_specific, "is_recycled", False)
                or locked_page.live_revision_id is None
            ):
                unpublish_page_discovery(page_id)
                return None
            if locked_page.live_revision_id != live_rev.id:
                if attempt == 0:
                    continue
                raise RuntimeError(f"Page {page_id} live_revision changed concurrently during projection.")

            existing_doc = (
                PublishedSearchDocument.objects.select_for_update()
                .filter(page_id=locked_specific.id, index_generation=index_generation)
                .first()
            )
            existing_author_rows = (
                list(
                    PublishedSearchAuthor.objects.filter(document=existing_doc)
                    .order_by("sort_order", "id")
                    .values_list("person_id", "author_name", "author_slug", "role", "sort_order")
                )
                if existing_doc is not None
                else []
            )
            existing_tag_ids = (
                sorted(existing_doc.search_tags.values_list("tag_id", flat=True))
                if existing_doc is not None
                else []
            )
            new_tag_ids = sorted(int(t["id"]) for t in (payload.tags_data or []))
            doc_unchanged = (
                existing_doc is not None
                and existing_doc.effective_revision_id == (locked_page.live_revision_id or 0)
                and existing_doc.type_key == payload.type_key
                and existing_doc.title == payload.title
                and existing_doc.url_path == actual_url
                and existing_doc.authored_on == payload.authored_on
                and existing_doc.summary == payload.summary
                and existing_doc.is_live is True
                and existing_doc.is_recycled is False
                and existing_doc.site_id == site_id
                and existing_doc.root_path == getattr(locked_page, "path", "")
                and existing_doc.depth == getattr(locked_page, "depth", 1)
                and existing_doc.extra_data == merged_extra
                and existing_author_rows == author_rows_spec
                and existing_tag_ids == new_tag_ids
                and existing_doc.chunks.exists()
            )
            if doc_unchanged:
                return existing_doc

            doc, _ = PublishedSearchDocument.objects.update_or_create(
                page_id=locked_specific.id,
                index_generation=index_generation,
                defaults={
                    "document_uuid": getattr(locked_specific, "document_uuid", uuid.uuid4()),
                    "effective_revision_id": locked_page.live_revision_id or 0,
                    "publication_generation": 1,
                    "type_key": payload.type_key,
                    "title": payload.title,
                    "url_path": actual_url,
                    "authored_on": payload.authored_on,
                    "summary": payload.summary,
                    "is_live": True,
                    "is_recycled": False,
                    "site_id": site_id,
                    "root_path": getattr(locked_page, "path", ""),
                    "depth": getattr(locked_page, "depth", 1),
                    "extra_data": merged_extra,
                },
            )

            # 清除旧的分块、标签与作者关系
            doc.chunks.all().delete()
            doc.search_tags.all().delete()
            doc.search_authors.all().delete()

            # 切块并存储
            for segment in payload.text_segments:
                if not segment.text:
                    continue
                chunk_dicts = chunk_text_for_search(segment.text)
                for cd in chunk_dicts:
                    chunk = PublishedSearchChunk(
                        document=doc,
                        field_name=segment.field_name,
                        chunk_index=cd["chunk_index"],
                        normalized_text=cd["normalized_text"],
                        char_start=cd["char_start"],
                        char_end=cd["char_end"],
                    )
                    terms_str = cd.get("search_terms_str", "").strip()
                    if terms_str:
                        chunk.search_vector = SearchVector(Value(terms_str), config="simple")
                    chunk.save()

            # 标签存储
            for tag in payload.tags_data:
                PublishedSearchTag.objects.create(
                    document=doc,
                    tag_id=tag["id"],
                    tag_name=tag["name"],
                    tag_slug=tag.get("slug", ""),
                )

            # 作者归一化索引存储（同事务一一对齐 extra_data）
            if author_rows_spec:
                PublishedSearchAuthor.objects.bulk_create([
                    PublishedSearchAuthor(
                        document=doc,
                        person_id=pid,
                        author_name=pname,
                        author_slug=pslug,
                        role=prole,
                        sort_order=psort,
                    )
                    for pid, pname, pslug, prole, psort in author_rows_spec
                ])

            from discovery.archive_counts import sync_page_archive_contribution
            sync_page_archive_contribution(locked_specific.id)
            ContentDiscoveryEpoch.increment_epoch()
            return doc
    return None


def unpublish_page_discovery(page_id: int) -> None:
    """页面下架时先锁表核验 Page 当前确实非公开上线，再更新副本状态并递增 epoch，防止旧事件覆盖新发布。"""
    from wagtail.models import Page

    with transaction.atomic():
        locked_page = Page.objects.select_for_update().filter(id=page_id).first()
        if locked_page is not None:
            specific = locked_page.specific
            if (
                getattr(specific, "live", False)
                and locked_page.alias_of_id is None
                and locked_page.live_revision_id is not None
                and not getattr(specific, "is_recycled", False)
            ):
                return
        updated = PublishedSearchDocument.objects.filter(page_id=page_id, is_live=True).update(is_live=False)
    from discovery.archive_counts import sync_page_archive_contribution
    sync_page_archive_contribution(page_id)
    if updated:
        ContentDiscoveryEpoch.increment_epoch()


def recycle_page_discovery(page_id: int) -> None:
    """页面移入回收站时先锁表核验 Page 仍处于回收态，再更新副本状态并递增 epoch，防止旧事件覆盖已恢复发布页。"""
    from wagtail.models import Page
    from content_lifecycle.models import RecycleEntry

    with transaction.atomic():
        locked_page = Page.objects.select_for_update().filter(id=page_id).first()
        if locked_page is not None:
            specific = locked_page.specific
            is_recycled = getattr(specific, "is_recycled", False) or RecycleEntry.objects.filter(
                page_id=page_id,
                status__in=["recycled", "restoring"],
            ).exists()
            if not is_recycled:
                return
        updated = PublishedSearchDocument.objects.filter(page_id=page_id).update(is_recycled=True, is_live=False)
    from discovery.archive_counts import sync_page_archive_contribution
    sync_page_archive_contribution(page_id)
    if updated:
        ContentDiscoveryEpoch.increment_epoch()


def sync_subtree_tree_metadata(root_page: Any) -> int:
    """
    当栏目节点移动或 slug 改名时，在 PostgreSQL 端批量同步其所有后代页面的树元数据 (B-02)。
    仅更新 root_path, depth, url_path，绝不重新读取 MongoDB 或重新切分 PublishedSearchChunk。
    """
    from wagtail.models import Page

    root_id = getattr(root_page, "id", None)
    if not root_id:
        return 0

    # 1. 重新投影根节点自身
    project_page_to_discovery(root_page)

    # 2. 获取当前数据库中 root_page 下属的全部活页后代
    db_root = Page.objects.filter(id=root_id).first()
    if not db_root:
        return 0

    descendants = Page.objects.filter(
        path__startswith=db_root.path,
        depth__gt=db_root.depth,
    ).values("id", "path", "depth", "url_path")

    from wagtail.models import Site
    site_roots: list[tuple[str, int]] = []
    for s in Site.objects.select_related("root_page").all():
        rp = getattr(s, "root_page", None)
        if rp is not None and getattr(rp, "path", None):
            site_roots.append((rp.path, s.id))
    # 最长路径前缀优先匹配，确保嵌套子站点拿到自己的 site_id
    site_roots.sort(key=lambda item: len(item[0]), reverse=True)

    def _resolve_site_id_for_path(page_path: str) -> int | None:
        for root_p, sid in site_roots:
            if page_path.startswith(root_p):
                return sid
        return None

    updated_count = 0
    with transaction.atomic():
        root_site_id = _resolve_site_id_for_path(db_root.path)
        PublishedSearchDocument.objects.filter(page_id=db_root.id).update(
            root_path=db_root.path,
            depth=db_root.depth,
            site_id=root_site_id,
        )

        for d in descendants:
            page_id = d["id"]
            # 获取规范化公开 URL
            url_path = d["url_path"]
            if url_path.startswith("/home/"):
                url_path = url_path[5:]
            if not url_path.startswith("/"):
                url_path = "/" + url_path

            cnt = PublishedSearchDocument.objects.filter(page_id=page_id).update(
                root_path=d["path"],
                depth=d["depth"],
                url_path=url_path,
                site_id=_resolve_site_id_for_path(d["path"]),
            )
            updated_count += cnt

        ContentDiscoveryEpoch.increment_epoch()

    from discovery.archive_counts import (
        enqueue_subtree_archive_reconcile,
        sync_page_archive_contribution,
    )
    desc_ids = [d["id"] for d in descendants]
    if len(desc_ids) > 50:
        enqueue_subtree_archive_reconcile(root_path=db_root.path, page_id=root_id)
    else:
        for did in desc_ids:
            sync_page_archive_contribution(did)

    logger.info("sync_subtree_tree_metadata completed for root=%s, updated %d descendants", root_id, updated_count)
    return updated_count


def rebuild_all_discovery_documents() -> int:
    """全量重建所有合法上线页面的搜索副本"""
    from wagtail.models import Page

    count = 0
    for page in Page.objects.filter(live=True).specific():
        if getattr(page, "is_recycled", False):
            continue
        doc = project_page_to_discovery(page)
        if doc:
            count += 1
    return count
