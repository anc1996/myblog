"""
全站内容发现与列表公开资格核验服务 (Eligibility)。

落实 RFC 规范：
1. 单次 SQL 关联核验：通过 Exists(OuterRef) 校验 wagtailcore_page 是否为 live=True、非 alias、非回收；
2. 祖先路径穿透排除：若当前栏目子树下存在已回收 (RecycleEntry) 或受限 (PageViewRestriction) 的中间祖先，
   统一在数据库端以 ~Q(root_path__startswith=bad_path) 批量剔除，防止权限穿透或回收节点泄漏；
3. 纯只读 SQL 集合操作，绝不触发外部网络 IO 或实例化业务 Page 模型。
"""

import logging
from django.db.models import Exists, OuterRef, Q, QuerySet
from wagtail.models import Page, PageViewRestriction
from content_lifecycle.models import RecycleEntry

logger = logging.getLogger(__name__)


def apply_public_eligibility_filter(candidates: QuerySet) -> QuerySet:
    """
    对 PublishedSearchDocument 候选集施加严格的公开资格过滤。
    仅保留当前在 PostgreSQL 中 live=True、非别名、路径与深度匹配的正式发布页面，
    并剔除被已回收或私有受限祖先阻断的内容。
    """
    # 1. 联合校验当前 Wagtail Page 实体状态与生效版本 (BLK-02)
    valid_page_exists = Page.objects.filter(
        pk=OuterRef("page_id"),
        live=True,
        alias_of_id__isnull=True,
        live_revision_id=OuterRef("effective_revision_id"),
        path=OuterRef("root_path"),
        depth=OuterRef("depth"),
    )
    qs = candidates.filter(index_generation=1).filter(Exists(valid_page_exists))

    # 2. 检查处于受控回收状态的页面路径 (BLK-02)
    recycled_page_ids = RecycleEntry.objects.filter(
        batch__status__in=["recycled", "restoring"]
    ).values("page_id")
    recycled_paths = list(
        Page.objects.filter(pk__in=recycled_page_ids).values_list("path", flat=True)
    )
    if recycled_paths:
        for rp in recycled_paths:
            if rp:
                qs = qs.exclude(root_path__startswith=rp)

    # 3. 检查私有权限限制 (PageViewRestriction) 祖先节点路径
    restricted_paths = list(
        PageViewRestriction.objects.values_list("page__path", flat=True)
    )
    if restricted_paths:
        for rp in restricted_paths:
            if rp:
                qs = qs.exclude(root_path__startswith=rp)

    return qs
