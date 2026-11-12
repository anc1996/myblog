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
from datetime import timedelta
from django.db import connection
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from wagtail.models import Page, PageViewRestriction, Site
from content_lifecycle.models import RecycleEntry

logger = logging.getLogger(__name__)


def get_comment_user_tier(user, community_settings=None) -> str:
    """
    解析评论发起人的分级身份档位：
    - 'guest': 未登录访客
    - 'superuser': 超级管理员
    - 'new_member': 注册时间短于 new_member_days_threshold（默认 7 天）的会员
    - 'trusted_member': 超过观察期的普通可信会员
    """
    if not user or not getattr(user, "is_authenticated", False):
        return "guest"
    if getattr(user, "is_superuser", False):
        return "superuser"
    if community_settings is None:
        from comments.models import CommunitySettings
        community_settings = CommunitySettings.get_solo()
    days_threshold = int(getattr(community_settings, "new_member_days_threshold", 7) or 7)
    joined_at = getattr(user, "date_joined", None)
    if joined_at is None:
        return "new_member"
    if timezone.now() - joined_at < timedelta(days=days_threshold):
        return "new_member"
    return "trusted_member"


def _get_page_permissions_set(user, page) -> set[str]:
    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return set()
    spec_page = page.specific if hasattr(page, "specific") else page
    perms = spec_page.permissions_for_user(user)
    return set(getattr(perms, "permissions", ()) or ())


def can_user_manage_guest_comments(user, page) -> bool:
    """
    判断操作人是否有权查看或修改单篇文章的受控访客留言开关 (allow_guest_comments)。
    仅限超级管理员、具备 comments.govern_comment_globally 者、或对该页面树具备 change 权限的主编 (Editors)。
    普通作者 (仅具备 add 且为 owner) 严禁修改该开关。
    """
    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    if user.is_superuser or user.has_perm("comments.govern_comment_globally"):
        return True
    if page is None:
        return False
    return "change" in _get_page_permissions_set(user, page)


def can_user_access_page_comment_settings(user, page) -> bool:
    """
    判断操作人是否有权访问或修改单篇文章的基础评论配置 (allow_comments / close_reason / auto_moderate_new)。
    严格按 RFC 7.1 隔离：仅限超管、全站评论治理员 (govern_comment_globally)、该页主编 (change)，
    以及仍具备编辑权的页面所有者 (owner == user)。纯评论审核员 (moderate_comment) 无权修改他人页面开关。
    """
    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    if (
        user.is_superuser
        or user.has_perm("comments.govern_comment_globally")
    ):
        return True
    if page is None:
        return False
    spec_page = page.specific if hasattr(page, "specific") else page
    perms = spec_page.permissions_for_user(user)
    perm_set = set(getattr(perms, "permissions", ()) or ())
    if "change" in perm_set and perms.can_edit():
        return True
    if getattr(spec_page, "owner_id", None) == user.pk and perms.can_edit():
        return True
    return False


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
    # 若通过别名页面访问，必须同时核对别名页自身所在树分支与源页各自祖先链上的全部访问限制，防止无密码别名绕过受限源页。
    restrictions = list(specific_page.get_view_restrictions())
    if target_page.id != specific_page.id:
        restrictions.extend(list(target_page.get_view_restrictions()))
        alias_anc_ids = list(specific_page.get_ancestors(inclusive=True).values_list("id", flat=True))
        if alias_anc_ids:
            restrictions.extend(list(PageViewRestriction.objects.filter(page_id__in=alias_anc_ids)))
    seen_rids = set()
    unique_restrictions = []
    for r in restrictions:
        if r.id not in seen_rids:
            seen_rids.add(r.id)
            unique_restrictions.append(r)
    restrictions = unique_restrictions
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
