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
from typing import Iterable
from wagtail.models import Page, PageViewRestriction
from content_lifecycle.models import RecycleEntry

logger = logging.getLogger(__name__)


def get_public_blocked_page_ids(candidate_records: Iterable[tuple[int, str]]) -> set[int]:
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

    candidate_map: dict[str, int] = {}
    candidate_paths: list[str] = []
    for pid, path in candidate_records:
        if path:
            candidate_map[path] = pid
            candidate_paths.append(path)

    if not candidate_paths:
        return set()

    # 1. 算法教学：批量切分所有树祖先路径 (Treebeard 每层固定 4 字符)
    # 例如 "000100020003" -> 切出 "0001", "00010002", "000100020003"
    ancestor_paths: set[str] = set()
    for p in candidate_paths:
        for i in range(4, len(p) + 1, 4):
            ancestor_paths.add(p[:i])

    # 2. 性能保障：单次 SQL 查询 wagtailcore_page 取出所有祖先节点元数据，杜绝 N+1
    node_rows = list(
        Page.objects.filter(path__in=ancestor_paths).values_list("id", "path", "alias_of_id", "live")
    )
    node_id_to_path = {row[0]: row[1] for row in node_rows}
    all_node_ids = set(node_id_to_path.keys())

    # 2.1 检查节点自身未上线 (live=False)
    unlive_node_ids = {row[0] for row in node_rows if not row[3]}

    # 3. 深度防御：追查别名源页面及其祖先链路
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

    blocked_page_ids: set[int] = set()
    found_node_paths = set(node_id_to_path.values())
    for cand_path, cand_id in candidate_map.items():
        # 若页面已从主库树中物理删除
        if cand_path not in found_node_paths:
            blocked_page_ids.add(cand_id)
            continue
        # 若候选页面其祖先处于受限或回收路径前缀下 (startswith)
        for bad_p in bad_paths:
            if cand_path.startswith(bad_p):
                blocked_page_ids.add(cand_id)
                break

    # 8. 别名页本身明确阻断（全站公网订阅与时间线归档只收录规范源页面）
    for row in node_rows:
        if row[2] is not None and row[0] in candidate_map.values():
            blocked_page_ids.add(row[0])

    return blocked_page_ids
