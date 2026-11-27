"""
content_lifecycle.public_access
全站公共访问资格与权限穿透防御服务 (Public Access Guard)。

【教学架构导读】：
在内容系统中，很多初学者会以为“只要 page.live == True 就可以公开展示”。
这是极度危险的！在树形页面层级中存在三大穿透漏洞：
1. 祖先受限穿透：父栏目甚至爷爷栏目设置了访问密码或内部登录限制（PageViewRestriction），子页面即便 live 也不准在 RSS/搜索中公开曝光！
2. 祖先回收穿透：父栏目被删入回收站，子页面即便孤立留在库中，也绝不可在全站订阅中出现！
3. 别名页污染：Wagtail 的 Alias 别名页不是独立内容源，订阅与归档只收录源页面；且别名的源页面若被封锁，别名也必须封锁。

本服务是全站公共资格的“安检机”（Fail-Closed 默认拒绝）：
- 采用精妙的 Materialized Path 字符切片技术，零 N+1 循环，一次 SQL 批量算出所有祖先的合法性！
"""

import logging
from typing import Any, Iterable
from django.db import DatabaseError
from django.db.models import Q, QuerySet
from django.http.request import split_domain_port
from wagtail.models import Page, PageViewRestriction, Site
from content_lifecycle.models import RecycleEntry

logger = logging.getLogger(__name__)


def _minimize_prefixes(paths: Iterable[str]) -> tuple[str, ...]:
    """按字典序对 Treebeard 物化路径做前缀去重最小化，减少多余 NOT LIKE 条件。"""
    ordered = sorted({str(p) for p in paths if p})
    kept: list[str] = []
    for path in ordered:
        if not any(path.startswith(prefix) for prefix in kept):
            kept.append(path)
    return tuple(kept)


def _get_nested_site_root_paths_strict(site: Site) -> list[str]:
    """严格查找挂载在当前站点根路径下的嵌套子站点根路径（失败时上抛，绝不静默降级）。"""
    root_page = getattr(site, "root_page", None)
    root_path = getattr(root_page, "path", None) if root_page is not None else None
    if not root_path:
        return []
    nested: list[str] = []
    for other in Site.objects.exclude(id=site.id).select_related("root_page"):
        other_root = getattr(other, "root_page", None)
        other_path = getattr(other_root, "path", None) if other_root is not None else None
        if other_path and other_path.startswith(root_path) and len(other_path) > len(root_path):
            nested.append(other_path)
    return nested


def _match_site_strict_for_request(request: Any) -> Site | None:
    """严格按请求 Host/Port 匹配已配置 Site，绝不向默认站点静默回退。"""
    raw_host = request._get_raw_host() if hasattr(request, "_get_raw_host") else (
        request.get_host() if hasattr(request, "get_host") else ""
    )
    hostname, port_str = split_domain_port(str(raw_host or ""))
    hostname = (hostname or "").strip().lower()
    if not hostname:
        return None

    try:
        port = int(port_str) if port_str and port_str.isdigit() else int(request.get_port())
    except Exception:
        port = 80

    matched = list(Site.objects.filter(hostname__iexact=hostname).select_related("root_page"))
    if not matched:
        return None
    for s in matched:
        if int(getattr(s, "port", 80)) == port:
            return s
    if len(matched) == 1:
        return matched[0]
    for s in matched:
        if getattr(s, "is_default_site", False):
            return s
    return None


def resolve_public_site_for_request(
    request: Any = None,
    *,
    page: Any = None,
    site: Site | None = None,
) -> Site | None:
    """
    Fail-Closed 站点解析：
    - 若显式传入 site，核验 root_page 与可选 page 的归属一致；
    - 若传入 request，严格按 Host 匹配已配置站点（禁止未知域名回退默认站点，同时与 page.get_site() 交叉校验）；
    - 未知/冲突/数据库异常统一返回 None，绝不跨站放行。
    """
    try:
        req_site = site
        if req_site is None and request is not None:
            req_site = _match_site_strict_for_request(request)
            if req_site is None:
                return None

        page_site = None
        if page is not None and hasattr(page, "get_site"):
            page_site = page.get_site()

        if req_site is not None and page_site is not None and req_site.id != page_site.id:
            return None

        resolved = req_site or page_site
        if resolved is None:
            return None

        root_page = getattr(resolved, "root_page", None)
        root_path = getattr(root_page, "path", None) if root_page is not None else None
        if not root_path:
            return None

        page_path = getattr(page, "path", None) if page is not None else None
        if page_path:
            if not str(page_path).startswith(root_path):
                return None
            for nested_path in _get_nested_site_root_paths_strict(resolved):
                if str(page_path).startswith(nested_path):
                    return None
        return resolved
    except DatabaseError:
        return None


def _scope_paths_for_site(raw_paths: Iterable[str], site_root_path: str | None) -> list[str]:
    if not site_root_path:
        return [p for p in raw_paths if p]
    scoped: list[str] = []
    for p in raw_paths:
        if not p:
            continue
        if p.startswith(site_root_path) or site_root_path.startswith(p):
            scoped.append(p)
    return scoped


def get_public_blocked_path_prefixes(*, site: Site | None = None) -> tuple[str, ...]:
    """
    返回当前应从公开集合中剔除的全部 Treebeard 路径前缀（已做前缀去重）：
    1. PageViewRestriction（覆盖站点自身/祖先与站点子树）；
    2. RecycleEntry(status__in=["recycled", "restoring"])；
    3. live=False 且存在子节点（numchild__gt=0, depth>=2）的非叶子未发布祖先；
    4. 源页面或其祖先处于上述阻断前缀/未发布状态的别名节点（阻断其公开分支下挂载的子孙）。
    """
    site_root_path = None
    if site is not None:
        root_page = getattr(site, "root_page", None)
        site_root_path = getattr(root_page, "path", None) if root_page is not None else None

    restricted_paths = [
        p for p in PageViewRestriction.objects.values_list("page__path", flat=True) if p
    ]
    recycled_page_ids = RecycleEntry.objects.filter(
        status__in=["recycled", "restoring"]
    ).values("page_id")
    recycled_paths = [
        p for p in Page.objects.filter(pk__in=recycled_page_ids).values_list("path", flat=True) if p
    ]
    unlive_paths = [
        p for p in Page.objects.filter(depth__gte=2, live=False, numchild__gt=0).values_list("path", flat=True) if p
    ]
    global_direct_blocked = _minimize_prefixes([*restricted_paths, *recycled_paths, *unlive_paths])

    # 多跳不动点展开别名链（例如 S 受限 -> 别名 A -> 子页 C -> 别名 B -> 子页 D），并天然防环
    blocked_set: list[str] = list(global_direct_blocked)
    all_aliases = [
        (apath, spath, slive, slrev)
        for apath, spath, slive, slrev in Page.objects.filter(alias_of_id__isnull=False).values_list(
            "path", "alias_of__path", "alias_of__live", "alias_of__live_revision_id"
        )
        if apath
    ]
    changed = True
    while changed:
        changed = False
        for apath, src_path, src_live, src_live_rev in all_aliases:
            if any(apath.startswith(bp) for bp in blocked_set):
                continue
            if (
                (not src_path)
                or (not src_live)
                or (src_live_rev is None)
                or any(src_path.startswith(bp) for bp in blocked_set)
            ):
                blocked_set.append(apath)
                changed = True

    scoped = _scope_paths_for_site(
        blocked_set,
        site_root_path,
    )
    return _minimize_prefixes(scoped)


def get_public_blocked_page_ids(
    candidate_records: Iterable[tuple[int, str]],
    *,
    site: Site | None = None,
) -> set[int]:
    """
    批量核验候选页面的公开访问资格，筛选出所有不合格的页面 ID（阻断黑名单）。

    【参数与关键变量解析】：
    - candidate_records (Iterable[tuple[int, str]]): 候选页面的 (page_id, root_path) 二元组迭代器。
    - candidate_map (dict[str, int]): 路径到页面 ID 的映射字典，便于后续通过路径前缀快速反查目标页面。
    - candidate_paths (list[str]): 所有候选页面的树路径列表（形如 "000100020003"）。
    - ancestor_paths (set[str]): 核心变量！利用 Treebeard 每层 4 字符步长，纯字符串内存切分出的所有祖先路径全集。
    - node_rows (list[tuple]): 单次 SQL 批量查出的所有祖先节点元数据 (id, path, alias_of_id, live)。
    - node_id_to_path (dict[int, str]): 节点 ID 到路径的快速反查表。
    - all_node_ids (set[int]): 涉及到的全部节点 ID（包含候选页面本身与它们的历代祖先）。
    - unlive_node_ids (set[int]): 链路中处于未发布状态（live=False）的节点 ID。
    - alias_of_ids (set[int]): 属于别名页的源页面 ID，用于进一步递归拉取别名源节点的祖先链。
    - restricted_node_ids (set[int]): 挂载了 PageViewRestriction（密码/用户组限制）的不合格节点。
    - recycled_node_ids (set[int]): 记录在回收站快照或具体页面表标记了 is_recycled=True 的已回收节点。
    - bad_node_ids (set[int]): 所有问题节点的并集（未上线 | 受限 | 回收）。
    - bad_paths (set[str]): 问题节点对应的路径前缀集合。
    - blocked_page_ids (set[int]): 返回值集合，所有自身或祖先命中 bad_paths 的候选页面 ID。
    """
    candidate_records = list(candidate_records)
    if not candidate_records:
        return set()

    raw_ids = [int(pid) for pid, _ in candidate_records if pid is not None]
    db_paths = dict(Page.objects.filter(id__in=raw_ids).values_list("id", "path")) if raw_ids else {}

    candidate_map: dict[int, str] = {}
    candidate_paths: list[str] = []
    blocked_page_ids: set[int] = set()

    site_root_path = None
    nested_paths: list[str] = []
    if site is not None:
        root_page = getattr(site, "root_page", None)
        site_root_path = getattr(root_page, "path", None) if root_page is not None else None
        if not site_root_path:
            return {int(pid) for pid, _ in candidate_records if pid is not None}
        nested_paths = _get_nested_site_root_paths_strict(site)

    for pid, path in candidate_records:
        if pid is None:
            continue
        pid_int = int(pid)
        # 强制使用主库当前真实路径，防止未刷新的内存实例借用其他页面旧路径绕过检查
        auth_path = db_paths.get(pid_int, "")
        if auth_path:
            if site_root_path and (
                not auth_path.startswith(site_root_path)
                or any(auth_path.startswith(np) for np in nested_paths)
            ):
                blocked_page_ids.add(pid_int)
                continue
            candidate_map[pid_int] = auth_path
            candidate_paths.append(auth_path)
        else:
            blocked_page_ids.add(pid_int)

    if not candidate_paths:
        return blocked_page_ids

    # 1. 算法教学：批量切分所有树祖先路径 (Treebeard 每层固定 4 字符)
    # 例如 "000100020003" -> 切出 "0001", "00010002", "000100020003"
    ancestor_paths: set[str] = set()
    for p in candidate_paths:
        for i in range(4, len(p) + 1, 4):
            ancestor_paths.add(p[:i])

    # 2. 性能保障：单次 SQL 查询 wagtailcore_page 取出所有祖先节点元数据，杜绝 N+1
    node_rows = list(
        Page.objects.filter(path__in=ancestor_paths).values_list(
            "id", "path", "alias_of_id", "live", "live_revision_id", "depth"
        )
    )
    node_id_to_path = {row[0]: row[1] for row in node_rows}
    all_node_ids = set(node_id_to_path.keys())

    # 2.1 检查节点自身未上线 (live=False)
    unlive_node_ids = {row[0] for row in node_rows if not row[3]}
    candidate_ids = set(candidate_map.keys())
    for row in node_rows:
        nid, _npath, alias_of_id, _live, live_rev_id, depth = row
        if nid in candidate_ids and depth >= 2 and live_rev_id is None:
            blocked_page_ids.add(nid)
        if alias_of_id is not None and nid in candidate_ids:
            blocked_page_ids.add(nid)

    # 3. 深度防御：多跳不动点追查别名源页面及其祖先链路（防 S -> A -> C -> B -> D 穿透）
    alias_of_ids = {row[2] for row in node_rows if row[2] is not None}
    alias_to_source_paths: dict[str, str] = {}
    source_path_by_id: dict[int, str] = {}
    seen_source_ids: set[int] = set()
    pending_alias_pairs: list[tuple[str, int]] = [
        (row[1], int(row[2])) for row in node_rows if row[2] is not None and row[1]
    ]
    frontier_alias_ids = alias_of_ids - seen_source_ids
    while pending_alias_pairs or frontier_alias_ids:
        source_ancestor_paths: set[str] = set()
        if frontier_alias_ids:
            seen_source_ids.update(frontier_alias_ids)
            source_rows = list(
                Page.objects.filter(id__in=frontier_alias_ids).values_list("id", "path", "live", "live_revision_id")
            )
            for sid, sp, slive, slive_rev in source_rows:
                if (not slive) or (slive_rev is None):
                    unlive_node_ids.add(sid)
                if sp:
                    source_path_by_id[sid] = sp
                    for i in range(4, len(sp) + 1, 4):
                        source_ancestor_paths.add(sp[:i])
        for apath, sid in pending_alias_pairs:
            if sid in source_path_by_id:
                alias_to_source_paths[apath] = source_path_by_id[sid]
        next_frontier: set[int] = set()
        new_pending: list[tuple[str, int]] = []
        if source_ancestor_paths:
            source_node_rows = list(
                Page.objects.filter(path__in=source_ancestor_paths).values_list(
                    "id", "path", "alias_of_id", "live", "live_revision_id"
                )
            )
            for s_nid, s_np, s_alias_of_id, s_live, s_live_rev in source_node_rows:
                all_node_ids.add(s_nid)
                node_id_to_path[s_nid] = s_np
                if (not s_live) or (s_nid in source_path_by_id and s_live_rev is None):
                    unlive_node_ids.add(s_nid)
                if s_alias_of_id is not None and s_np:
                    new_pending.append((s_np, int(s_alias_of_id)))
                    if int(s_alias_of_id) not in seen_source_ids:
                        next_frontier.add(int(s_alias_of_id))
        pending_alias_pairs = new_pending
        frontier_alias_ids = next_frontier - seen_source_ids

    # 4. 检查 PageViewRestriction（页面密码/登录限制）
    restricted_node_ids = set(
        PageViewRestriction.objects.filter(page_id__in=all_node_ids).values_list("page_id", flat=True)
    )

    # 5. 检查 RecycleEntry（回收站表记录状态处于回收或正在恢复）
    recycled_node_ids = set(
        RecycleEntry.objects.filter(
            page_id__in=all_node_ids,
            status__in=["recycled", "restoring"],
        ).values_list("page_id", flat=True)
    )

    # 6. 检查具体子类模型表的 is_recycled=True 字段
    from content_lifecycle.models import CustomBasePage

    def _get_all_subclasses(cls):
        subs = set(cls.__subclasses__())
        for s in list(subs):
            subs.update(_get_all_subclasses(s))
        return subs

    for subcls in _get_all_subclasses(CustomBasePage):
        try:
            flagged = set(
                subcls.objects.filter(page_ptr_id__in=all_node_ids, is_recycled=True).values_list("page_ptr_id", flat=True)
            )
            recycled_node_ids.update(flagged)
        except Exception:
            pass

    # 汇总所有不合规节点的集合
    bad_node_ids = restricted_node_ids | recycled_node_ids | unlive_node_ids

    # 7. 路径前缀穿透匹配：受限祖先的路径会连锁阻断其下属所有子孙页面
    bad_paths: set[str] = set()
    for nid in bad_node_ids:
        if nid in node_id_to_path:
            bad_paths.add(node_id_to_path[nid])
    # 多跳不动点向别名祖先路径传播阻断前缀
    if alias_to_source_paths:
        prop_changed = True
        while prop_changed:
            prop_changed = False
            for alias_prefix, src_path in alias_to_source_paths.items():
                if alias_prefix not in bad_paths and any(src_path.startswith(bp) for bp in bad_paths):
                    bad_paths.add(alias_prefix)
                    prop_changed = True

    found_node_paths = set(node_id_to_path.values())
    for cand_id, cand_path in candidate_map.items():
        if cand_id in blocked_page_ids:
            continue
        # 若页面已从主库树中物理删除或 ID 与当前节点路径不匹配
        if cand_path not in found_node_paths or node_id_to_path.get(cand_id) != cand_path:
            blocked_page_ids.add(cand_id)
            continue
        # 若候选页面其祖先处于受限或回收路径前缀下 (startswith)
        for bad_p in bad_paths:
            if cand_path.startswith(bad_p):
                blocked_page_ids.add(cand_id)
                break

    return blocked_page_ids


def filter_public_page_queryset(
    qs: QuerySet,
    *,
    site: Site | None = None,
    request: Any = None,
    require_live_revision: bool = True,
    exclude_aliases: bool = True,
) -> QuerySet:
    """
    QuerySet 级统一公开资格过滤（供 API / 列表复用，零具体业务模型导入）：
    强制站点范围、排除嵌套子站点、live=True、live_revision 非空、非别名、未回收及无祖先限制/草稿/回收。
    """
    resolved_site = resolve_public_site_for_request(request, site=site)
    if resolved_site is None:
        return qs.none()

    root_path = resolved_site.root_page.path
    qs = qs.filter(live=True, path__startswith=root_path)
    if require_live_revision:
        qs = qs.filter(live_revision_id__isnull=False)
    if exclude_aliases:
        qs = qs.filter(alias_of_id__isnull=True)

    model_field_names = {f.name for f in qs.model._meta.get_fields()}
    if "is_recycled" in model_field_names:
        qs = qs.filter(is_recycled=False)

    for nested_path in _get_nested_site_root_paths_strict(resolved_site):
        qs = qs.exclude(path__startswith=nested_path)

    for bad_prefix in get_public_blocked_path_prefixes(site=resolved_site):
        qs = qs.exclude(path__startswith=bad_prefix)

    return qs


def filter_public_pages(
    pages: Iterable[Any],
    *,
    site: Site | None = None,
    request: Any = None,
    require_live_revision: bool = True,
    exclude_aliases: bool = True,
) -> list[Any]:
    """对象集合级公开资格过滤（固定 O(1) 次 SQL，禁止循环内调用 .specific / .get_ancestors()）。"""
    raw_list = [p for p in (pages or ()) if p is not None and getattr(p, "id", None) is not None]
    if not raw_list:
        return []
    resolved_site = resolve_public_site_for_request(request, site=site)
    if resolved_site is None:
        return []

    candidate_records: list[tuple[int, str]] = []
    for p in raw_list:
        if getattr(p, "is_recycled", False):
            continue
        # live / live_revision_id / alias_of_id 由 get_public_blocked_page_ids 统一查主库裁决，避免未 refresh_from_db 的内存对象误判
        candidate_records.append((int(p.id), str(getattr(p, "path", "") or "")))

    if not candidate_records:
        return []
    blocked_ids = get_public_blocked_page_ids(candidate_records, site=resolved_site)
    allowed_ids = {pid for pid, _ in candidate_records if pid not in blocked_ids}
    return [p for p in raw_list if int(p.id) in allowed_ids]
