"""
content_lifecycle.public_access
全站公共访问资格与权限穿透防御服务 (Public Access Guard)。

落实刚性生命周期安全规范：
1. 别名页 (Alias) 追溯与剔除：凡属于别名页或指向受限/已回收源页面的页面严格阻断；
2. PageViewRestriction 穿透防御：候选页面自身或任一树祖先有访问密码/登录/组限制时，严禁进入公开订阅与归档；
3. 回收站穿透防御：候选页面自身或任一树祖先处于 RecycleEntry(status in ["recycled", "restoring"]) 或 is_recycled=True 时严格阻断；
4. 零 N+1 批量物化路径祖先解析：利用 Treebeard path 步长 (steplen=4) 一次性批量计算所有祖先路径与限制。
"""

import logging
from typing import Iterable
from wagtail.models import Page, PageViewRestriction
from content_lifecycle.models import RecycleEntry

logger = logging.getLogger(__name__)


def get_public_blocked_page_ids(candidate_records: Iterable[tuple[int, str]]) -> set[int]:
    """
    接收候选页面的 (page_id, root_path) 迭代器。
    返回所有不具备公开访问资格的 page_id 集合 (Fail-Closed)。
    """
    candidate_records = list(candidate_records)
    if not candidate_records:
        return set()

    candidate_map: dict[str, int] = {}
    candidate_paths: list[str] = []
    for pid, path in candidate_records:
        if path:
            candidate_map[path] = pid
            candidate_paths.append(path)

    if not candidate_paths:
        return set()

    # 1. 批量切分所有树祖先路径 (Treebeard 每层固定 4 字符)
    ancestor_paths: set[str] = set()
    for p in candidate_paths:
        for i in range(4, len(p) + 1, 4):
            ancestor_paths.add(p[:i])

    # 2. 一次查询 wagtailcore_page 取出所有节点元数据
    node_rows = list(
        Page.objects.filter(path__in=ancestor_paths).values_list("id", "path", "alias_of_id", "live")
    )
    node_id_to_path = {row[0]: row[1] for row in node_rows}
    all_node_ids = set(node_id_to_path.keys())

    # 2.1 检查节点自身未上线 (live=False)
    unlive_node_ids = {row[0] for row in node_rows if not row[3]}

    # 3. 追查别名源页面及其祖先
    alias_of_ids = {row[2] for row in node_rows if row[2] is not None}
    source_ancestor_paths: set[str] = set()
    if alias_of_ids:
        source_rows = list(Page.objects.filter(id__in=alias_of_ids).values_list("id", "path"))
        for sid, sp in source_rows:
            if sp:
                for i in range(4, len(sp) + 1, 4):
                    source_ancestor_paths.add(sp[:i])
        if source_ancestor_paths:
            source_node_rows = list(Page.objects.filter(path__in=source_ancestor_paths).values_list("id", "path"))
            for s_nid, s_np in source_node_rows:
                all_node_ids.add(s_nid)

    # 4. 检查 PageViewRestriction（自身或祖先受限）
    restricted_node_ids = set(
        PageViewRestriction.objects.filter(page_id__in=all_node_ids).values_list("page_id", flat=True)
    )

    # 5. 检查 RecycleEntry（自身或祖先处于回收站或恢复中）
    recycled_node_ids = set(
        RecycleEntry.objects.filter(
            page_id__in=all_node_ids,
            status__in=["recycled", "restoring"],
        ).values_list("page_id", flat=True)
    )

    # 6. 检查具体页面表的 is_recycled=True
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

    bad_node_ids = restricted_node_ids | recycled_node_ids | unlive_node_ids

    # 7. 将受限或回收节点的 path 映射回受影响的 candidate page_id
    bad_paths: set[str] = set()
    for nid in bad_node_ids:
        if nid in node_id_to_path:
            bad_paths.add(node_id_to_path[nid])

    blocked_page_ids: set[int] = set()
    found_node_paths = set(node_id_to_path.values())
    for cand_path, cand_id in candidate_map.items():
        # 若页面已从主表物理删除
        if cand_path not in found_node_paths:
            blocked_page_ids.add(cand_id)
            continue
        # 若候选页面本身就是 Alias，或者其祖先处于受限/回收状态
        for bad_p in bad_paths:
            if cand_path.startswith(bad_p):
                blocked_page_ids.add(cand_id)
                break

    # 另外：如果有候选页本身是别名页，也标记阻断（订阅与归档只收录源页面）
    for row in node_rows:
        if row[2] is not None and row[0] in candidate_map.values():
            blocked_page_ids.add(row[0])

    return blocked_page_ids
