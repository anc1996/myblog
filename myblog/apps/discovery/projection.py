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
)
from discovery.analysis import chunk_text_for_search
from discovery.registry import list_type_adapters

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
    2. 仅从 live_revision.as_object() 还原正式快照，杜绝在途草稿污染；
    3. 提取文本、标签与维度元数据，落库 PublishedSearchDocument / PublishedSearchChunk / PublishedSearchTag；
    4. 原子递增全局 Epoch。
    """
    from wagtail.models import Page

    page_id = getattr(page, "id", None)
    if not page_id:
        return None

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

    # 2. 必须且仅从 live_revision.as_object() 还原正式快照（内部触发 Mongo 指针校验水合）
    live_rev = specific_page.live_revision
    if str(live_rev.object_id) != str(specific_page.id):
        raise ValueError(f"Revision object_id mismatch for page {specific_page.id}")
    published_snapshot = live_rev.as_object()
    payload = adapter.extract(published_snapshot)

    site_id = None
    try:
        if hasattr(specific_page, "get_site"):
            site = specific_page.get_site()
            if site:
                site_id = site.id
    except Exception:
        site_id = None

    actual_url = payload.url_path
    if not actual_url or actual_url.startswith("/home/"):
        actual_url = specific_page.get_url() if hasattr(specific_page, "get_url") else getattr(specific_page, "url_path", "")

    with transaction.atomic():
        doc, _ = PublishedSearchDocument.objects.update_or_create(
            page_id=specific_page.id,
            index_generation=index_generation,
            defaults={
                "document_uuid": getattr(specific_page, "document_uuid", uuid.uuid4()),
                "effective_revision_id": specific_page.live_revision_id or 0,
                "publication_generation": 1,
                "type_key": payload.type_key,
                "title": payload.title,
                "url_path": actual_url,
                "authored_on": payload.authored_on,
                "summary": payload.summary,
                "is_live": True,
                "is_recycled": False,
                "site_id": site_id,
                "root_path": getattr(specific_page, "path", ""),
                "depth": getattr(specific_page, "depth", 1),
                "extra_data": payload.extra_data,
            },
        )

        # 清除旧的分块与标签
        doc.chunks.all().delete()
        doc.search_tags.all().delete()

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

        ContentDiscoveryEpoch.increment_epoch()
        return doc


def unpublish_page_discovery(page_id: int) -> None:
    """页面下架时同步更新副本状态并递增 epoch"""
    updated = PublishedSearchDocument.objects.filter(page_id=page_id, is_live=True).update(is_live=False)
    if updated:
        ContentDiscoveryEpoch.increment_epoch()


def recycle_page_discovery(page_id: int) -> None:
    """页面移入回收站时同步更新副本状态并递增 epoch"""
    updated = PublishedSearchDocument.objects.filter(page_id=page_id).update(is_recycled=True, is_live=False)
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

    updated_count = 0
    with transaction.atomic():
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
            )
            updated_count += cnt

        ContentDiscoveryEpoch.increment_epoch()

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
