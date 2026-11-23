"""
Markdown 导入父栏目权限、树约束与工作区媒体集合校验。

严格对齐 content_lifecycle.permissions 与 content_lifecycle.views.OwnershipChooseParentView：
绝不绕过受限作者的 author_creatable / author_allow_child_creation / 回收隔离规则。
"""

from django.apps import apps
from django.core.exceptions import PermissionDenied
from wagtail.models import Collection, Page, Site
from wagtail.permissions import page_permission_policy, policy_registry

from content_lifecycle.models import RecycleEntry
from content_lifecycle.permissions import (
    get_author_allow_child_content_type_ids,
    user_is_restricted_author_for_page,
)


def resolve_model_from_label(model_label: str):
    app_label, model_name = model_label.split(".", 1)
    return apps.get_model(app_label, model_name)


def _page_or_ancestors_recycled(page: Page) -> bool:
    ancestor_ids = list(page.get_ancestors(inclusive=True).values_list("id", flat=True))
    if not ancestor_ids:
        return False
    if RecycleEntry.objects.filter(page_id__in=ancestor_ids, status__in=["recycled", "restoring"]).exists():
        return True
    return page.get_ancestors(inclusive=True).filter(
        id__in=RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values("page_id")
    ).exists()


def validate_import_target_parent(actor, parent_id: int, target_model, site: Site) -> Page:
    """
    在 catalog / preflight / commit 三阶段共用的父栏目与树规则校验：
    1. 父页必须存在、归属目标站点、非别名页、自身与祖先均未移入回收站；
    2. 当前账号对父页具有 add_subpage 权限；
    3. target_model 满足 Wagtail parent_page_types / subpage_types 树规则；
    4. 若为受限作者，强制检验 parent.author_allow_child_creation 与 target_model.author_creatable。
    """
    parent_raw = Page.objects.filter(id=parent_id).first()
    if not parent_raw:
        raise PermissionDenied("目标父栏目不存在。")
    parent_page = parent_raw.specific
    if getattr(parent_page, "alias_of_id", None):
        raise PermissionDenied("不允许向别名页面下导入文章。")
    if getattr(parent_page, "is_recycled", False) or _page_or_ancestors_recycled(parent_page):
        raise PermissionDenied("目标父栏目或其祖先已移入回收站，禁止导入。")

    if site is not None:
        root_path = site.root_page.path
        if not parent_page.path.startswith(root_path):
            raise PermissionDenied("目标父栏目不属于当前站点。")

    perms = parent_page.permissions_for_user(actor)
    if not perms.can_add_subpage():
        raise PermissionDenied("当前账号没有在此栏目下创建子页面的权限。")

    if not target_model.can_create_at(parent_page) or target_model not in parent_page.specific_class.creatable_subpage_models():
        raise PermissionDenied(f"栏目“{parent_page.title}”不允许创建该类型的页面。")

    if user_is_restricted_author_for_page(actor, parent_page, policy=page_permission_policy):
        if not getattr(target_model, "author_creatable", False):
            raise PermissionDenied("受限作者无权导入创建此页面类型。")
        if not getattr(parent_page, "author_allow_child_creation", False):
            raise PermissionDenied("受限作者无权在该父页面下直接创建子文章，请选择栏目页。")

    return parent_page


def _is_container_page(page: Page) -> bool:
    """判断节点是否属于栏目容器页（IndexPage / HomePage），与具体文章页区分展示。"""
    cls_name = page.specific_class.__name__
    return cls_name.endswith("IndexPage") or cls_name == "HomePage"


def _allowed_child_types_for_parent(actor, parent_page: Page, all_adapters: list) -> list[dict[str, str]]:
    """遵循 Wagtail subpage_types 与受限作者规则，计算该父节点下允许导入哪些已注册页面类型。"""
    creatable = set(parent_page.specific_class.creatable_subpage_models())
    is_restricted = user_is_restricted_author_for_page(actor, parent_page, policy=page_permission_policy)
    allow_child = bool(getattr(parent_page, "author_allow_child_creation", False))
    allowed: list[dict[str, str]] = []
    for adp in all_adapters:
        model_cls = resolve_model_from_label(adp.model_label)
        if not model_cls.can_create_at(parent_page) or model_cls not in creatable:
            continue
        if is_restricted and (not allow_child or not getattr(model_cls, "author_creatable", False)):
            continue
        schema = adp.describe(actor=actor)
        allowed.append({"type_key": adp.type_key, "label": schema.label})
    return allowed


def list_allowed_parents(
    actor,
    target_model,
    site: Site,
    *,
    limit: int = 50,
    offset: int = 0,
    query: str = "",
    containers_only: bool = True,
    all_adapters: list | None = None,
) -> tuple[list[dict], bool, int]:
    """
    列出当前用户有权限向其中导入指定页面类型的父栏目树节点：
    - 栏目容器页（IndexPage/HomePage）优先排在最前，防止 500 篇普通文章把后续栏目挤出分页；
    - 支持按标题、完整中文路径或 ID 关键词过滤；
    - containers_only=True 默认仅返回栏目容器页，传 False 时一并返回可挂子文章的文章页。
    """
    recycled_ids = set(
        RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
    )
    qs = Page.objects.filter(path__startswith=site.root_page.path, alias_of__isnull=True).order_by("path")
    if recycled_ids:
        qs = qs.exclude(id__in=recycled_ids)

    allow_child_ct_ids = set(get_author_allow_child_content_type_ids())
    target_creatable = bool(getattr(target_model, "author_creatable", False))
    q_norm = (query or "").strip().casefold()
    adapters_list = list(all_adapters or [])
    allowed: list[dict] = []

    for page in qs.specific().iterator(chunk_size=100):
        if getattr(page, "is_recycled", False):
            continue
        is_container = _is_container_page(page)
        if containers_only and not is_container:
            continue
        if not target_model.can_create_at(page):
            continue
        if target_model not in page.specific_class.creatable_subpage_models():
            continue
        if not page.permissions_for_user(actor).can_add_subpage():
            continue
        if user_is_restricted_author_for_page(actor, page, policy=page_permission_policy):
            if not target_creatable or page.content_type_id not in allow_child_ct_ids:
                continue
        if _page_or_ancestors_recycled(page):
            continue

        ancestor_titles = list(
            page.get_ancestors(inclusive=True)
            .filter(depth__gte=site.root_page.depth)
            .order_by("path")
            .values_list("title", flat=True)
        )
        breadcrumb_path = " / ".join(ancestor_titles) if ancestor_titles else page.title
        if q_norm:
            haystack = f"{page.id} {page.title} {breadcrumb_path} {page.url_path}".casefold()
            if q_norm not in haystack:
                continue

        verbose = str(getattr(page._meta, "verbose_name", page.specific_class.__name__))
        allowed.append(
            {
                "id": page.id,
                "title": page.title,
                "url_path": page.url_path,
                "breadcrumb_path": breadcrumb_path,
                "content_type": f"{page._meta.app_label}.{page._meta.model_name}",
                "content_type_label": verbose,
                "is_container": is_container,
                "allowed_child_types": _allowed_child_types_for_parent(actor, page, adapters_list) if adapters_list else [],
            }
        )

    allowed.sort(key=lambda item: (0 if item["is_container"] else 1, item["breadcrumb_path"], item["id"]))
    total = len(allowed)
    sliced = allowed[offset:offset + limit]
    has_more = (offset + limit) < total
    return sliced, has_more, total


def resolve_upload_collection_for_actor(actor, model_cls) -> Collection:
    """
    解析当前作者有 add 权限的媒体集合：
    - 超管优先 Root Collection；
    - 受限作者强制放入非 Root 的“作者个人工作区”集合（否则作者自己无权查看或发布！）。
    """
    policy = policy_registry.get_by_type(model_cls)
    if not policy.user_has_permission(actor, "add"):
        raise PermissionDenied(f"当前账号没有上传 {model_cls._meta.verbose_name} 的权限。")

    collections = list(policy.collections_user_has_permission_for(actor, "add").order_by("id"))
    if not collections:
        raise PermissionDenied("当前账号没有可写入的媒体素材集合。")

    if getattr(actor, "is_superuser", False):
        root_col = Collection.get_first_root_node()
        if root_col and any(c.pk == root_col.pk for c in collections):
            return root_col
        return collections[0]

    non_root = [c for c in collections if c.depth > 1]
    return non_root[0] if non_root else collections[0]
