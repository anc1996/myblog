"""
全站共享栏目目录树与标签路由服务 (discovery.navigation)。

设计原则：
1. 共享层零具体页面模型导入：通过 discovery.registry.list_directory_index_models()
   动态发现全部公开层级列表模型，新增第四类 XxxIndexPage 时本模块零改动；
2. 单一数据源：全站左侧栏目目录抽屉、页脚核心大厅、首页栏目目录卡共享同一份树构建结果；
3. 单请求级缓存 + BFS 有界拉取 + Fail-Closed 公开资格过滤 + 异常路径安全跳过。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from django.apps import apps as django_apps
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError
from django.urls import NoReverseMatch, reverse
from wagtail.models import Page, Site

from content_lifecycle.public_access import get_public_blocked_page_ids
from discovery.detail_navigation import _get_nested_site_root_paths, resolve_site_readonly
from discovery.registry import get_model_to_type_key_map, list_directory_index_models

logger = logging.getLogger(__name__)


def resolve_site_for_navigation(
    request: Any = None,
    *,
    page: Any = None,
    site: Site | None = None,
) -> Site | None:
    """纯只读解析当前请求的 Wagtail Site（带 request._wagtail_site 复用与 DB 异常保护）。"""
    if site is not None and getattr(site, "root_page_id", None):
        return site

    if request is not None:
        cached_site = getattr(request, "_wagtail_site", None)
        if cached_site is not None and getattr(cached_site, "root_page_id", None):
            return cached_site
        try:
            found = Site.find_for_request(request)
            if found is not None and getattr(found, "root_page_id", None):
                request._wagtail_site = found
                return found
        except Exception:
            pass

    if page is not None and hasattr(page, "get_site"):
        try:
            page_site = page.get_site()
            if page_site is not None and getattr(page_site, "root_page_id", None):
                return page_site
        except Exception:
            pass

    try:
        default_site = Site.objects.select_related("root_page").filter(is_default_site=True).first()
        if default_site is not None and getattr(default_site, "root_page_id", None):
            return default_site
        return Site.objects.select_related("root_page").order_by("id").first()
    except DatabaseError:
        return None


def resolve_tag_index_page(request: Any = None, page: Any = None, site: Site | None = None):
    """
    仅在当前站点下查找已发布、未回收且无访问限制的 TagIndexPage（支持单请求级缓存）。
    严禁跨站或越界到嵌套子站点。
    """
    try:
        resolved_site = site if (site is not None and getattr(site, "root_page", None)) else resolve_site_readonly(request, page)
        if resolved_site is None or not getattr(resolved_site, "root_page", None):
            return None

        cache_bucket = None
        if request is not None:
            cache_bucket = getattr(request, "_tag_index_page_cache", None)
            if isinstance(cache_bucket, dict) and resolved_site.id in cache_bucket:
                return cache_bucket[resolved_site.id]

        tag_index_model = django_apps.get_model("taxonomy", "TagIndexPage")
        qs = (
            tag_index_model.objects.live()
            .filter(
                is_recycled=False,
                alias_of_id__isnull=True,
                live_revision_id__isnull=False,
            )
            .descendant_of(resolved_site.root_page, inclusive=True)
            .order_by("path")
        )
        for np in _get_nested_site_root_paths(resolved_site):
            qs = qs.exclude(path__startswith=np)

        candidates = list(qs.values_list("id", "path"))
        found = None
        if candidates:
            blocked_ids = get_public_blocked_page_ids(candidates)
            for candidate in qs:
                if candidate.id in blocked_ids:
                    continue
                cand_site = candidate.get_site() if hasattr(candidate, "get_site") else None
                if cand_site is not None and cand_site.id != resolved_site.id:
                    continue
                found = candidate
                break

        if request is not None:
            if not isinstance(cache_bucket, dict):
                cache_bucket = {}
                request._tag_index_page_cache = cache_bucket
            cache_bucket[resolved_site.id] = found
        return found
    except Exception:
        return None


def build_tag_detail_url(
    tag_index: Any,
    slug: str | None,
    *,
    base_url: str = "",
    request: Any = None,
) -> str | None:
    """安全拼装 TagIndexPage 标签详情路径；无大厅、空 slug 或 NoReverseMatch 时返回 None。"""
    slug_str = str(slug or "").strip()
    if tag_index is None or not slug_str:
        return None
    resolved_base = base_url
    if not resolved_base:
        try:
            resolved_base = (tag_index.get_url(request) if request is not None else tag_index.url) or tag_index.url or ""
        except Exception:
            resolved_base = getattr(tag_index, "url", "") or ""
    if not resolved_base:
        return None
    try:
        sub = tag_index.reverse_subpage("detail", args=[slug_str])
    except Exception:
        return None
    if sub is None:
        return None
    return f"{resolved_base.rstrip('/')}/{str(sub).lstrip('/')}"


def get_site_directory_tree(
    request: Any = None,
    *,
    site: Site | None = None,
    page: Any = None,
    max_nodes: int = 1000,
) -> list[dict[str, Any]]:
    """
    只读构建当前站点的全量公开栏目目录树（N-02 / P0 / P1 收口）：
    - 不依赖 show_in_menus=True，自动纳入全部继承 HierarchicalListingMixin 的已发布公开栏目；
    - 按广度优先 (depth, path) 有界拉取，防止第 1 个大栏目截断后面的所有一级栏目；
    - 排除嵌套子站点根路径、回收站页面、未发布/受限祖先子树；
    - URL 从 Page.url_path 校验前缀后调 reverse('wagtail_serve')，异常路径安全跳过不 500；
    - 同一请求内通过 request._site_directory_tree_cache 复用，零冗余 Redis/DB 复核开销。
    """
    resolved_site = resolve_site_for_navigation(request, page=page, site=site)
    if resolved_site is None or not getattr(resolved_site, "root_page_id", None):
        return []

    if request is not None:
        cache_map = getattr(request, "_site_directory_tree_cache", None)
        if isinstance(cache_map, dict) and resolved_site.id in cache_map:
            return copy.deepcopy(cache_map[resolved_site.id])

    try:
        root_page = resolved_site.root_page
        root_path = str(getattr(root_page, "path", "") or "")
        root_depth = int(getattr(root_page, "depth", 2) or 2)
        root_url_path = str(getattr(root_page, "url_path", "") or "")
        if not root_path or not root_url_path:
            return []
        if not root_url_path.endswith("/"):
            root_url_path = root_url_path + "/"

        dir_models = list_directory_index_models()
        if not dir_models:
            return []

        model_to_tk = get_model_to_type_key_map()
        ct_map = ContentType.objects.get_for_models(*dir_models, for_concrete_models=False)
        ct_to_type_key: dict[int, str] = {}
        for m_cls, ct_obj in ct_map.items():
            ct_to_type_key[ct_obj.id] = model_to_tk.get(m_cls, "")
        if not ct_to_type_key:
            return []

        qs = Page.objects.filter(
            path__startswith=root_path,
            depth__gt=root_depth,
            live=True,
            alias_of_id__isnull=True,
            live_revision_id__isnull=False,
            content_type_id__in=list(ct_to_type_key.keys()),
        )
        for nested_path in _get_nested_site_root_paths(resolved_site):
            qs = qs.exclude(path__startswith=nested_path)

        # 广度优先截断，确保所有一级、二级主栏目优先保留
        rows = list(
            qs.order_by("depth", "path").values(
                "id",
                "title",
                "path",
                "depth",
                "url_path",
                "content_type_id",
            )[:max_nodes]
        )
        if not rows:
            tree: list[dict[str, Any]] = []
        else:
            blocked_ids = get_public_blocked_page_ids([(r["id"], r["path"]) for r in rows])
            # 重排为 Treebeard 物化路径顺序以便自顶向下挂树
            rows.sort(key=lambda r: r["path"])

            nodes_by_path: dict[str, dict[str, Any]] = {}
            tree = []
            min_child_path_len = len(root_path) + 4

            for r in rows:
                if r["id"] in blocked_ids:
                    continue
                raw_url_path = str(r["url_path"] or "")
                if not raw_url_path.startswith(root_url_path):
                    continue
                suffix = raw_url_path[len(root_url_path):]
                try:
                    url = reverse("wagtail_serve", args=(suffix,))
                except NoReverseMatch:
                    continue

                node: dict[str, Any] = {
                    "page_id": int(r["id"]),
                    "title": str(r["title"] or ""),
                    "url": url,
                    "type_key": ct_to_type_key.get(r["content_type_id"], ""),
                    "depth": int(r["depth"]) - root_depth,
                    "raw_depth": int(r["depth"]),
                    "path": r["path"],
                    "is_active": False,
                    "is_ancestor": False,
                    "children": [],
                }
                nodes_by_path[r["path"]] = node

                parent_node = None
                # 向上寻找最近的目录祖先节点（兼容中间存在非目录容器页的场景）
                probe_len = len(r["path"]) - 4
                while probe_len >= min_child_path_len:
                    cand_parent = nodes_by_path.get(r["path"][:probe_len])
                    if cand_parent is not None:
                        parent_node = cand_parent
                        break
                    probe_len -= 4

                if parent_node is not None:
                    parent_node["children"].append(node)
                else:
                    tree.append(node)

        if request is not None:
            cache_map = getattr(request, "_site_directory_tree_cache", None)
            if not isinstance(cache_map, dict):
                cache_map = {}
                request._site_directory_tree_cache = cache_map
            cache_map[resolved_site.id] = copy.deepcopy(tree)

        return tree
    except DatabaseError:
        logger.debug("DatabaseError while building site directory tree", exc_info=True)
        return []
    except Exception:
        logger.warning("Unexpected error while building site directory tree", exc_info=True)
        return []


def annotate_active_directory_tree(
    tree: list[dict[str, Any]],
    current_page: Any = None,
) -> list[dict[str, Any]]:
    """
    为目录树节点标记 is_active 与 is_ancestor，便于抽屉高亮当前所在栏目并自动展开路径。
    安全容忍 current_page=None（搜索/时间线/错误页）及未落库预览页（pk=None, path=''）。
    """
    annotated = copy.deepcopy(tree or [])
    if not annotated or current_page is None:
        return annotated

    current_pk = getattr(current_page, "pk", None)
    current_path = str(getattr(current_page, "path", "") or "")
    if not current_pk and not current_path:
        return annotated

    def _walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            node_id = node.get("page_id")
            node_path = str(node.get("path") or "")
            is_active = bool(current_pk and node_id == current_pk)
            is_ancestor = bool(
                not is_active
                and current_path
                and node_path
                and current_path.startswith(node_path)
            )
            node["is_active"] = is_active
            node["is_ancestor"] = is_ancestor
            if node.get("children"):
                _walk(node["children"])

    _walk(annotated)
    return annotated


def find_primary_entry_urls(tree: list[dict[str, Any]]) -> dict[str, str]:
    """从共享目录树中按广度优先顺序为各 type_key 提取首个公开入口 URL（零页面硬编码）。"""
    urls: dict[str, str] = {}
    queue = list(tree or [])
    while queue:
        node = queue.pop(0)
        tk = node.get("type_key")
        url = node.get("url")
        if tk and url and tk not in urls:
            urls[tk] = url
        for child in node.get("children") or []:
            queue.append(child)
    return urls
