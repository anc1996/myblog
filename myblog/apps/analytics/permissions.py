"""
analytics.permissions
全站数据分析与单篇报表权限守卫。

安全设计原则（严格遵循 RFC §8.1 与 Ownership 隔离策略）：
1. 严禁仅凭 `is_staff` 或 `people.Person` 署名关联放行全站/单篇分析；
2. 全站大盘仅限超级用户或明确持有 `analytics.view_site_analytics` 权限的用户访问；
3. 受限作者或非全局权限用户访问单篇分析时，必须同时满足 `OwnershipPagePermissionTester`
   与 `Page.owner_id == user.id` 校验，严禁越权查看他人 `page_id` 统计；
4. 若页面处于受控回收站（`is_recycled=True` 或存在回收快照），额外核验既有回收站查看权限。
"""

from __future__ import annotations

from django.db.models import Q
from wagtail.models import Page

from content_lifecycle.models import CustomBasePage, RecycleEntry
from content_lifecycle.permissions import (
    OwnershipPagePermissionPolicy,
    OwnershipPagePermissionTester,
    can_restore_recycled_page,
    get_author_creatable_content_type_ids,
    user_effective_page_actions,
)

VIEW_SITE_ANALYTICS_PERM = "analytics.view_site_analytics"
VIEW_PAGE_ANALYTICS_PERM = "analytics.view_page_analytics"
APPROVE_CURATION_PERM = "analytics.approve_curatedrecommendation"
RESOLVE_ANALYTICS_ALERT_PERM = "analytics.resolve_analyticsalert"


def _is_active_authenticated(user) -> bool:
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
    )


def _get_all_recycled_page_ids() -> set[int]:
    """获取当前处于受控回收或恢复中的全部页面 ID（含子类表标记）。"""
    recycled_ids = set(
        RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list(
            "page_id", flat=True
        )
    )

    def _collect_subclasses(cls):
        subs = set(cls.__subclasses__())
        for sub in list(subs):
            subs.update(_collect_subclasses(sub))
        return subs

    for subcls in _collect_subclasses(CustomBasePage):
        try:
            flagged = subcls.objects.filter(is_recycled=True).values_list(
                "page_ptr_id", flat=True
            )
            recycled_ids.update(flagged)
        except Exception:
            pass
    return recycled_ids


def _is_page_recycled(page: Page) -> bool:
    """判定页面自身或其任一祖先是否处于受控回收站中。"""
    if page is None or getattr(page, "pk", None) is None:
        return False
    spec = getattr(page, "specific_deferred", page)
    if getattr(spec, "is_recycled", False):
        return True
    ancestor_ids = list(page.get_ancestors(inclusive=True).values_list("id", flat=True))
    if ancestor_ids and RecycleEntry.objects.filter(
        page_id__in=ancestor_ids,
        status__in=["recycled", "restoring"],
    ).exists():
        return True
    return any(
        getattr(anc.specific_deferred, "is_recycled", False)
        for anc in page.get_ancestors()
    )


def _has_recycled_analytics_access(user, page: Page) -> bool:
    """
    已回收页面的分析查看权限校验：
    - 超级管理员直接放行；
    - 具备子树 `change` 权限且具有后台访问权的主编/管理员可看；
    - 受限作者仅当本人是页面所有者且为原回收批次操作人（具备回收恢复/查看权）时可看。
    """
    if not _is_active_authenticated(user):
        return False
    if getattr(user, "is_superuser", False):
        return True

    from content_lifecycle.wagtail_hooks import can_user_view_recycled_notice

    specific_page = getattr(page, "specific_deferred", page)
    actions = user_effective_page_actions(user, specific_page)
    if "change" in actions:
        return bool(
            getattr(user, "is_staff", False)
            or user.has_perm("wagtailadmin.access_admin")
            or user.has_perm(VIEW_SITE_ANALYTICS_PERM)
        )

    if getattr(specific_page, "owner_id", None) != user.pk:
        return False

    ancestor_ids = list(
        specific_page.get_ancestors(inclusive=True).values_list("id", flat=True)
    )
    entry = (
        RecycleEntry.objects.filter(
            page_id__in=ancestor_ids,
            status__in=["recycled", "restoring"],
        )
        .select_related("batch")
        .order_by("-batch__recycled_at")
        .first()
    )
    if entry is not None and entry.batch is not None:
        root_page = Page.objects.filter(id=entry.batch.root_page_id).first()
        if root_page is not None and can_restore_recycled_page(
            user, root_page.specific_deferred, entry.batch
        ):
            return True
        return bool(
            entry.batch.actor_id == user.pk
            and can_user_view_recycled_notice(user, specific_page)
        )
    return bool(can_user_view_recycled_notice(user, specific_page))


def can_view_site_analytics(user) -> bool:
    """
    全站大盘权限校验：
    仅超级管理员或明确拥有 `analytics.view_site_analytics` 权限的用户可看。
    绝不因 `is_staff` 或 `people.Person` 署名放行。
    """
    if not _is_active_authenticated(user):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return bool(user.has_perm(VIEW_SITE_ANALYTICS_PERM))


def can_approve_curation(user) -> bool:
    """
    精选工作台审批与手工提名权限校验：
    仅超级管理员或明确拥有 `analytics.approve_curatedrecommendation` 权限的用户可执行。
    绝不凭 `is_staff` 或 `people.Person` 署名放行。
    """
    if not _is_active_authenticated(user):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return bool(user.has_perm(APPROVE_CURATION_PERM))


def can_resolve_analytics_alerts(user) -> bool:
    """
    降噪告警治理（限时豁免 / 确认下榜）权限校验：
    仅超级管理员或明确拥有 `analytics.resolve_analyticsalert` 权限的用户可执行。
    绝不凭 `is_staff` 放行。
    """
    if not _is_active_authenticated(user):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return bool(user.has_perm(RESOLVE_ANALYTICS_ALERT_PERM))


def can_view_page_analytics(user, page) -> bool:
    """
    单篇页面数据分析权限校验：
    1. 超级管理员 / `analytics.view_site_analytics` 全局可看（若页面已回收，需同时满足回收查看权）；
    2. 受限作者与栏目编辑复用 `OwnershipPagePermissionTester` 与 `Page.owner_id == user.id` 校验：
       - 子树编辑（拥有 `change` 且持有 `analytics.view_page_analytics`）可看授权子树内页面；
       - 受限作者仅可查看 `owner_id == user.id` 且 `OwnershipPagePermissionTester` 校验通过的本人页面，
         严禁查看他人 `page_id` 统计。
    """
    if not _is_active_authenticated(user):
        return False
    if page is None or getattr(page, "pk", None) is None or getattr(page, "depth", 0) <= 1:
        return False
    if getattr(user, "is_superuser", False):
        return True

    specific_page = page.specific if hasattr(page, "specific") else page
    is_recycled = _is_page_recycled(specific_page)
    if is_recycled and not _has_recycled_analytics_access(user, specific_page):
        return False

    if user.has_perm(VIEW_SITE_ANALYTICS_PERM):
        return True

    tester = OwnershipPagePermissionTester(user, specific_page)
    permissions = getattr(tester, "permissions", set()) or set()

    # 具备子树 change 权限的栏目主编：仅当显式具备页面分析权限时可看该子树内页面
    if "change" in permissions:
        if not user.has_perm(VIEW_PAGE_ANALYTICS_PERM):
            return False
        return True if is_recycled else bool(tester.can_edit())

    # 受限作者模式：严格要求本人拥有页面所有权，且页面级权限策略有效（未撤权）
    if getattr(specific_page, "owner_id", None) != user.pk:
        return False

    if is_recycled:
        specific_cls = getattr(specific_page, "specific_class", None) or type(specific_page)
        return bool(
            getattr(specific_cls, "author_creatable", False)
            and ("add" in permissions or "publish" in permissions)
        )

    return bool(tester.can_edit() or tester.can_publish())


def filter_accessible_pages_for_analytics(user, queryset):
    """
    按当前用户权限过滤 Page QuerySet，确保列表、搜索或聚合绝不泄露无权页面统计。
    """
    if not _is_active_authenticated(user):
        return queryset.none()

    base_qs = queryset.filter(depth__gt=1)
    if getattr(user, "is_superuser", False):
        return base_qs

    recycled_ids = _get_all_recycled_page_ids()

    if user.has_perm(VIEW_SITE_ANALYTICS_PERM):
        if not recycled_ids:
            return base_qs
        allowed_recycled = [
            p.pk
            for p in base_qs.filter(id__in=recycled_ids)
            if _has_recycled_analytics_access(user, p)
        ]
        return base_qs.filter(~Q(id__in=recycled_ids) | Q(id__in=allowed_recycled))

    page_policy = OwnershipPagePermissionPolicy(Page)
    user_perms = list(page_policy.get_cached_permissions_for_user(user))
    if not user_perms:
        return queryset.none()

    change_codename = page_policy._get_permission_codename("change")
    add_codename = page_policy._get_permission_codename("add")
    publish_codename = page_policy._get_permission_codename("publish")

    editor_roots = page_policy._deduplicate_roots(
        [p.page for p in user_perms if p.permission.codename == change_codename]
    )
    restricted_add_roots = page_policy._deduplicate_roots(
        [p.page for p in user_perms if p.permission.codename == add_codename]
    )
    restricted_pub_roots = page_policy._deduplicate_roots(
        [p.page for p in user_perms if p.permission.codename == publish_codename]
    )

    q = Q()
    if editor_roots and user.has_perm(VIEW_PAGE_ANALYTICS_PERM):
        q |= page_policy._descendants_q(editor_roots)

    creatable_ct_ids = get_author_creatable_content_type_ids()
    not_editor_q = ~page_policy._descendants_q(editor_roots) if editor_roots else Q()
    author_roots = page_policy._deduplicate_roots(restricted_add_roots + restricted_pub_roots)
    if author_roots and creatable_ct_ids:
        q |= (
            page_policy._descendants_q(author_roots)
            & not_editor_q
            & Q(owner_id=user.pk, content_type_id__in=creatable_ct_ids)
        )

    if not q:
        return queryset.none()

    candidate_qs = base_qs.filter(q)
    if not recycled_ids:
        return candidate_qs

    allowed_recycled = [
        p.pk
        for p in candidate_qs.filter(id__in=recycled_ids)
        if _has_recycled_analytics_access(user, p)
    ]
    return candidate_qs.filter(~Q(id__in=recycled_ids) | Q(id__in=allowed_recycled))


def can_view_analytics_menu(user) -> bool:
    """控制后台左侧「数据分析」菜单项的可见性。"""
    if not _is_active_authenticated(user):
        return False
    if (
        can_view_site_analytics(user)
        or can_approve_curation(user)
        or can_resolve_analytics_alerts(user)
    ):
        return True
    if user.has_perm(VIEW_PAGE_ANALYTICS_PERM):
        return True
    return filter_accessible_pages_for_analytics(
        user, Page.objects.filter(owner_id=user.pk)
    ).exists()
