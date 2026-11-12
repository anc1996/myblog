"""
comments.access
全站通用页面评论访问与鉴权守卫 (C-01 & C-07 契约)。

落实核心安全隔离：
1. 通用 Page 解析：支持所有 Wagtail Page（depth > 1 且类属性 allow_comments=True）；
2. 别名归一：若访问 alias 页面，自动解引用到原始源页面，避免评论分裂；
3. Site 边界校验：严格校验目标页面属于当前访问 Site 树；
4. 全链防回收与发布校验：强校验目标页面与祖先节点均未回收且已上线；
5. PageViewRestriction 100% 等价校验：受限/密码页面未经验证前严格抛出 PermissionDenied，且禁止使用公共缓存；
6. 细粒度行级共享锁：写操作时对目标 wagtailcore_page 加 FOR SHARE (lock_timeout=3s)，兼顾高并发与页面下架互斥。
"""

import logging
from django.db import connection
from django.core.exceptions import PermissionDenied
from wagtail.models import Page, Site
from content_lifecycle.models import RecycleEntry

logger = logging.getLogger(__name__)


def get_accessible_comment_page(request, page_id: int, *, for_write: bool = False) -> tuple[Page, bool]:
    """
    通用解析并校验目标评论页面。
    返回: (target_page: Page, is_restricted: bool)
    若校验不通过，直接抛出 PermissionDenied。
    """
    # 1. 存在性与树深度校验（排除 depth=1 的根节点）
    page = Page.objects.filter(id=page_id, depth__gt=1).first()
    if not page:
        raise PermissionDenied("目标页面不存在或无权访问。")

    specific_page = page.specific

    # 2. 模型级评论开启开关检查
    supports = getattr(specific_page, "supports_comments", None)
    if supports is False or (supports is None and not getattr(specific_page, "allow_comments", False)):
        raise PermissionDenied("该类型页面未开放评论互动功能。")

    # 3. 别名页面（Alias）归一化至源页面
    target_page = specific_page
    if getattr(specific_page, "alias_of_id", None):
        source = specific_page.alias_of.specific
        if not source.live or getattr(source, "is_recycled", False):
            raise PermissionDenied("别名源页面未发布或已移入回收站。")
        target_page = source

    # 4. 当前 Site 边界校验
    try:
        current_site = Site.find_for_request(request)
    except Exception:
        current_site = None
    if current_site and not specific_page.is_descendant_of(current_site.root_page):
        raise PermissionDenied("目标页面不属于当前访问站点。")

    # 5. 正式发布与全链防回收状态校验
    if not target_page.live or not target_page.live_revision_id or getattr(target_page, "is_recycled", False):
        raise PermissionDenied("页面处于未发布或回收状态，无法访问评论。")

    recycled_pks = set(
        RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
    )
    if target_page.id in recycled_pks:
        raise PermissionDenied("页面当前处于回收站中。")

    for ancestor in target_page.get_ancestors():
        if getattr(ancestor, "is_recycled", False) or ancestor.id in recycled_pks:
            raise PermissionDenied("页面的父级栏目已移入回收站。")

    # 6. PageViewRestriction 权限校验 (密码、登录、用户组限制)
    restrictions = list(specific_page.get_view_restrictions())
    is_restricted = bool(restrictions)
    if is_restricted:
        for r in restrictions:
            if not r.accept_request(request):
                raise PermissionDenied("该页面受密码或用户组权限保护，未通过前无法访问评论。")

    # 7. 写操作细粒度页面行级共享锁 (C-07)
    if for_write and connection.vendor == "postgresql":
        try:
            with connection.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '3s';")
                cur.execute(
                    "SELECT id, live FROM wagtailcore_page WHERE id = %s FOR SHARE;",
                    [target_page.id],
                )
        except Exception as lock_exc:
            logger.warning("Failed to acquire page row lock FOR SHARE: %s", lock_exc)
            raise PermissionDenied("页面正在执行发布或回收操作，请稍后重试。")

    return target_page, is_restricted
