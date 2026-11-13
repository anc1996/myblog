"""
基于 CustomBasePage 与 Wagtail 通用生命周期的对象级归属隔离策略 (Phase 4 / P0-1, P0-3)。

核心原则（零单页面硬编码）：
1. 动态识别所有声明了 author_creatable=True 与 author_allow_child_creation=True 的页面子类；
2. 按树节点授权判定模式：具备 subtree 'change' 权限（主编/管理员）保留 Wagtail 原生全量管理能力；
   仅具备 'add' / 'publish' 而无 'change' 的受限作者自动进入对象归属隔离模式；
3. 收口 Explorer、全站后台搜索、报表、审计日志、发布、下架、删除、复制、排序与访问限制；
4. 回收批次视图 (RecycleBatchViewSet) 同步实施只读列表与超管专属物理清库 (Purge)。
"""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from wagtail.models import (
    Collection,
    GroupCollectionPermission,
    GroupPagePermission,
    Page,
    PagePermissionTester,
)
from wagtail.permission_policies.base import BasePermissionPolicy
from wagtail.permission_policies.pages import PagePermissionPolicy
from wagtail.permissions import policy_registry


def get_author_creatable_page_models() -> tuple[type[Page], ...]:
    """动态收集全站声明了 author_creatable=True 的具体 Page 子类。"""
    models_list = []
    for model in apps.get_models():
        if issubclass(model, Page) and not model._meta.abstract and getattr(model, "author_creatable", False):
            models_list.append(model)
    return tuple(models_list)


def get_author_allow_child_page_models() -> tuple[type[Page], ...]:
    """动态收集全站声明了 author_allow_child_creation=True 的具体 Page 子类。"""
    models_list = []
    for model in apps.get_models():
        if issubclass(model, Page) and not model._meta.abstract and getattr(model, "author_allow_child_creation", False):
            models_list.append(model)
    return tuple(models_list)


def get_author_creatable_content_type_ids() -> list[int]:
    creatable_models = get_author_creatable_page_models()
    if not creatable_models:
        return []
    ct_map = ContentType.objects.get_for_models(*creatable_models)
    return [ct.id for ct in ct_map.values()]


def get_author_allow_child_content_type_ids() -> list[int]:
    allow_models = get_author_allow_child_page_models()
    if not allow_models:
        return []
    ct_map = ContentType.objects.get_for_models(*allow_models)
    return [ct.id for ct in ct_map.values()]


def _get_recycled_page_ids() -> set[int]:
    from content_lifecycle.models import RecycleEntry

    return set(
        RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
    )


def user_effective_page_actions(user, page, policy=None) -> set[str]:
    """计算用户在指定页面节点上拥有的继承动作集合（如 {'add', 'change', 'publish'}）。"""
    if not user or not getattr(user, "is_active", False) or not getattr(user, "is_authenticated", False):
        return set()
    if getattr(user, "is_superuser", False):
        return {"add", "change", "publish", "lock", "unlock", "bulk_delete"}
    if policy is None:
        policy = policy_registry.get_by_type(Page)
    page_path = getattr(page, "path", "") or ""
    page_pk = getattr(page, "pk", None)
    actions = set()
    for perm in policy.get_cached_permissions_for_user(user):
        if (page_pk is not None and page_pk == perm.page_id) or (page_path and page_path.startswith(perm.page.path)):
            actions.add(perm.permission.codename.rsplit("_", maxsplit=1)[0])
    return actions


def user_is_restricted_author_for_page(user, page, policy=None) -> bool:
    """判定用户在给定页面（或父页面）子树是否处于受限作者模式（无 change 权限）。"""
    if not user or not getattr(user, "is_active", False) or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return False
    actions = user_effective_page_actions(user, page, policy=policy)
    return "change" not in actions


def can_restore_recycled_page(user, root_page, batch) -> bool:
    """
    专用于受控回收站恢复动作的实时权限核验：
    不依赖已禁用的 can_edit()（已回收页不可编辑），并确保被撤权的作者无法恢复旧批次。
    """
    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    if getattr(user, "is_superuser", False):
        return True

    actions = user_effective_page_actions(user, root_page)
    if "change" in actions:
        return True
    if "add" in actions:
        specific_cls = getattr(root_page, "specific_class", None) or type(root_page)
        return bool(
            batch is not None
            and batch.actor_id == user.pk
            and root_page.owner_id == user.pk
            and getattr(specific_cls, "author_creatable", False)
        )
    return False


class OwnershipPagePermissionPolicy(PagePermissionPolicy):
    """
    全站统一归属隔离页面权限策略。
    - 对拥有 subtree 'change' 权限的编辑/超管保持原生行为；
    - 对仅拥有 'add'/'publish'（无 'change'）的受限作者强制实施归属与声明式类型隔离。
    """

    def user_has_any_permission_for_instance(self, user, actions, instance):
        base_permission = self._base_user_has_permission(user)
        if base_permission is not None:
            return base_permission

        permissions = set()
        for perm in self.get_cached_permissions_for_user(user):
            if instance.pk == perm.page_id or instance.path.startswith(perm.page.path):
                permissions.add(perm.permission.codename)

        change_codename = self._get_permission_codename("change")
        if change_codename in permissions:
            return bool(self._get_permission_codenames(actions) & permissions)

        # 受限作者模式
        specific_cls = getattr(instance, "specific_class", None) or type(instance)
        specific_obj = getattr(instance, "specific_deferred", instance)
        is_recycled = bool(getattr(specific_obj, "is_recycled", False))
        if not is_recycled and instance.pk:
            is_recycled = instance.pk in _get_recycled_page_ids()

        is_creatable = bool(getattr(specific_cls, "author_creatable", False))
        is_allow_child = bool(getattr(specific_cls, "author_allow_child_creation", False))
        is_own = instance.owner_id == user.pk

        add_codename = self._get_permission_codename("add")
        publish_codename = self._get_permission_codename("publish")

        effective_actions = set()
        if add_codename in permissions:
            if is_allow_child and not is_recycled:
                effective_actions.add("add")
            if is_creatable and is_own:
                effective_actions.update({"change", "delete"})
        if publish_codename in permissions and is_creatable and is_own and not is_recycled:
            effective_actions.add("publish")

        return bool(set(actions) & effective_actions)

    def instances_user_has_any_permission_for(self, user, actions):
        base_queryset = self._base_queryset_for_user(user)
        if base_queryset is not None:
            return base_queryset

        actions = set(actions)
        codenames = self._get_permission_codenames(actions)
        add_codename = self._get_permission_codename("add")
        change_codename = self._get_permission_codename("change")
        publish_codename = self._get_permission_codename("publish")

        user_perms = list(self.get_cached_permissions_for_user(user))
        if not user_perms:
            return self.model._default_manager.none()

        # 拥有 change 权限的子树根（主编/全量编辑范围）
        editor_roots = self._deduplicate_roots(
            [perm.page for perm in user_perms if perm.permission.codename == change_codename]
        )
        editor_paths = tuple(page.path for page in editor_roots)

        # 1. 全量编辑子树内按 Wagtail 原生方式展开（在 editor_roots 覆盖范围内匹配请求的权限）
        editor_matching_pages = self._deduplicate_roots(
            [
                perm.page
                for perm in user_perms
                if perm.permission.codename in codenames and editor_paths and perm.page.path.startswith(editor_paths)
            ]
        )
        # 若某编辑子树根的父节点同时拥有其他权限（例如 parent 有 publish，child 有 change），
        # 则在 child 子树内部也应享受全量权限
        for perm in user_perms:
            if perm.permission.codename in codenames and editor_roots:
                for ed_root in editor_roots:
                    if ed_root.path.startswith(perm.page.path):
                        editor_matching_pages.append(ed_root)
        editor_matching_pages = self._deduplicate_roots(editor_matching_pages)
        q = self._descendants_q(editor_matching_pages)

        # 2. 受限作者子树（未被 editor_roots 覆盖的部分）
        restricted_add_roots = self._deduplicate_roots(
            [
                perm.page
                for perm in user_perms
                if perm.permission.codename == add_codename
                and not (editor_paths and perm.page.path.startswith(editor_paths))
            ]
        )
        restricted_pub_roots = self._deduplicate_roots(
            [
                perm.page
                for perm in user_perms
                if perm.permission.codename == publish_codename
                and not (editor_paths and perm.page.path.startswith(editor_paths))
            ]
        )

        if restricted_add_roots or restricted_pub_roots:
            creatable_ct_ids = get_author_creatable_content_type_ids()
            allow_child_ct_ids = get_author_allow_child_content_type_ids()
            recycled_ids = _get_recycled_page_ids()

            # 避免与 editor_roots 子树混淆：仅针对 editor_roots 之外的部分做隔离过滤
            not_editor_q = ~self._descendants_q(editor_roots) if editor_roots else Q()

            if "add" in actions and restricted_add_roots and allow_child_ct_ids:
                hall_q = (
                    self._descendants_q(restricted_add_roots)
                    & not_editor_q
                    & Q(content_type_id__in=allow_child_ct_ids)
                )
                if recycled_ids:
                    hall_q &= ~Q(id__in=recycled_ids)
                q |= hall_q

            if (actions & {"change", "delete"}) and restricted_add_roots and creatable_ct_ids:
                own_edit_q = (
                    self._descendants_q(restricted_add_roots)
                    & not_editor_q
                    & Q(owner=user)
                    & Q(content_type_id__in=creatable_ct_ids)
                )
                if recycled_ids:
                    own_edit_q &= ~Q(id__in=recycled_ids)
                q |= own_edit_q

            if "publish" in actions and restricted_pub_roots and creatable_ct_ids:
                pub_q = (
                    self._descendants_q(restricted_pub_roots)
                    & not_editor_q
                    & Q(owner=user)
                    & Q(content_type_id__in=creatable_ct_ids)
                )
                if recycled_ids:
                    pub_q &= ~Q(id__in=recycled_ids)
                q |= pub_q

        if not q:
            return self.model._default_manager.none()
        return self.model._default_manager.filter(q)

    def explorable_instances(self, user):
        base_queryset = self._base_queryset_for_user(user)
        if base_queryset is not None:
            return base_queryset

        user_perms = list(self.get_cached_permissions_for_user(user))
        if not user_perms:
            return self.model._default_manager.none()

        # 基础可浏览页面集合：
        # - 全量编辑子树内保留所有权限页；
        # - 受限子树内仅返回作者本人名下的 author_creatable 页面 + 允许建子页的大厅页
        explorable_pages = self.instances_user_has_any_permission_for(
            user, {"add", "change", "publish", "lock", "unlock", "bulk_delete"}
        )

        # 补充受限子树内作者自己拥有但可能只授 publish/add 的 creatable 页面
        creatable_ct_ids = get_author_creatable_content_type_ids()
        change_codename = self._get_permission_codename("change")
        editor_roots = self._deduplicate_roots(
            [p.page for p in user_perms if p.permission.codename == change_codename]
        )
        all_perm_pages = [perm.page for perm in user_perms]
        if creatable_ct_ids and all_perm_pages:
            own_q = self._descendants_q(self._deduplicate_roots(all_perm_pages)) & Q(
                owner=user, content_type_id__in=creatable_ct_ids
            )
            explorable_pages |= self.model._default_manager.filter(own_q)

        # 导航必须的祖先链（如 HomePage），使作者可逐级进入子栏目大厅
        steplen = self.model.base_page_model.steplen
        ancestor_paths = {
            page.path[:pos]
            for page in all_perm_pages
            for pos in range(steplen, len(page.path), steplen)
        }
        if ancestor_paths:
            explorable_pages |= self.model._default_manager.filter(path__in=ancestor_paths)

        fca_page = self.model.base_page_model.objects.first_common_ancestor_of(all_perm_pages)
        explorable_pages = explorable_pages.filter(path__startswith=fca_page.path)
        return explorable_pages


class OwnershipPagePermissionTester(PagePermissionTester):
    """
    全站统一归属隔离页面动作检测器。
    统一保护所有 CustomBasePage 子类及 HomePage。
    """

    def _is_full_editor(self) -> bool:
        return bool(self.user.is_superuser or ("change" in getattr(self, "permissions", set())))

    def _is_recycled(self) -> bool:
        specific_obj = getattr(self.page, "specific_deferred", self.page)
        if getattr(specific_obj, "is_recycled", False):
            return True
        if self.page.pk:
            from content_lifecycle.models import RecycleEntry

            return RecycleEntry.objects.filter(
                page_id=self.page.pk, status__in=["recycled", "restoring"]
            ).exists()
        return False

    def _has_recycled_ancestor(self) -> bool:
        ancestor_ids = list(self.page.get_ancestors().values_list("id", flat=True))
        if not ancestor_ids:
            return False
        from content_lifecycle.models import RecycleEntry

        if RecycleEntry.objects.filter(
            page_id__in=ancestor_ids, status__in=["recycled", "restoring"]
        ).exists():
            return True
        return any(getattr(a.specific_deferred, "is_recycled", False) for a in self.page.get_ancestors())

    def _is_own_creatable(self) -> bool:
        specific_cls = self.page.specific_class or type(self.page)
        return bool(
            getattr(specific_cls, "author_creatable", False)
            and self.page.owner_id == self.user.pk
        )

    def can_add_subpage(self):
        if not self.user.is_active:
            return False
        if self._is_recycled():
            return False
        if self._is_full_editor():
            return super().can_add_subpage()
        if "add" not in self.permissions:
            return False
        specific_class = self.page.specific_class
        if specific_class is None or not getattr(specific_class, "author_allow_child_creation", False):
            return False
        return any(
            getattr(sub_model, "author_creatable", False) and sub_model.can_create_at(self.page)
            for sub_model in specific_class.creatable_subpage_models()
        )

    def can_edit(self):
        if not self.user.is_active or self.page_is_root:
            return False
        if self._is_recycled() or self._has_recycled_ancestor():
            return False
        if self._is_full_editor():
            return super().can_edit()
        return ("add" in self.permissions) and self._is_own_creatable()

    def can_view_revisions(self):
        if not self.user.is_active or self.page_is_root:
            return False
        if self._is_recycled() or self._has_recycled_ancestor():
            return False
        if self._is_full_editor():
            return super().can_view_revisions()
        return self.can_edit() or self.can_publish()

    def can_publish(self):
        if not self.user.is_active or self.page_is_root:
            return False
        if self._is_recycled() or self._has_recycled_ancestor():
            return False
        if self._is_full_editor():
            return super().can_publish()
        return ("publish" in self.permissions) and self._is_own_creatable()

    def can_unpublish(self):
        if not self.user.is_active or (not self.page.live) or self.page_is_root or self.page_locked():
            return False
        if self._is_full_editor():
            return super().can_unpublish()
        return ("publish" in self.permissions) and self._is_own_creatable()

    def can_unschedule(self):
        return self.can_publish()

    def can_publish_subpage(self):
        if not self.user.is_active:
            return False
        if self._is_full_editor():
            return super().can_publish_subpage()
        return ("publish" in self.permissions) and self.can_add_subpage()

    def can_delete(self, ignore_bulk=False):
        if not self.user.is_active or self.page_is_root:
            return False
        if self._is_recycled() or self._has_recycled_ancestor():
            return False
        if self._is_full_editor():
            return super().can_delete(ignore_bulk=ignore_bulk)
        if "add" not in self.permissions or not self._is_own_creatable():
            return False
        if "bulk_delete" not in self.permissions and not self.page.is_leaf() and not ignore_bulk:
            return False
        descendants = self.page.get_descendants(inclusive=True)
        creatable_ct_ids = get_author_creatable_content_type_ids()
        if descendants.exclude(owner=self.user).exists():
            return False
        if creatable_ct_ids and descendants.exclude(content_type_id__in=creatable_ct_ids).exists():
            return False
        if "publish" not in self.permissions and descendants.live().exists():
            return False
        return True

    def can_set_view_restrictions(self):
        if not self._is_full_editor():
            return False
        return super().can_set_view_restrictions()

    def can_reorder_children(self):
        if not self._is_full_editor():
            return False
        return super().can_reorder_children()

    def can_move(self):
        if not self._is_full_editor():
            return False
        return super().can_move()

    def can_move_to(self, destination):
        if not self._is_full_editor():
            return False
        return super().can_move_to(destination)

    def can_lock(self):
        if not self._is_full_editor():
            return False
        return super().can_lock()

    def can_unlock(self):
        if not self._is_full_editor():
            return False
        return super().can_unlock()

    def can_copy(self):
        if self._is_recycled() or self._has_recycled_ancestor():
            return False
        return self.can_edit()

    def can_copy_to(self, destination, recursive=False):
        if self._is_full_editor():
            return super().can_copy_to(destination, recursive=recursive)
        if not self.can_copy() or not self.user.is_active:
            return False
        if recursive:
            return False
        specific_cls = self.page.specific_class
        if specific_cls is None or not getattr(specific_cls, "author_creatable", False):
            return False
        if not specific_cls.can_create_at(destination):
            return False
        dest_perms = destination.permissions_for_user(self.user)
        return dest_perms.can_add_subpage() and (specific_cls in destination.specific_class.creatable_subpage_models())


class RecycleBatchPermissionPolicy(BasePermissionPolicy):
    """
    受控回收批次只读视图权限策略：
    彻底关闭 ModelViewSet 原生 add / change / delete 表单旁路，仅允许有权用户按各自范围 view。
    """

    def user_has_permission(self, user, action):
        return self.user_has_any_permission(user, {action})

    def user_has_any_permission(self, user, actions):
        if not user or not getattr(user, "is_active", False) or not getattr(user, "is_authenticated", False):
            return False
        actions = set(actions)
        if "view" not in actions:
            return False
        if user.is_superuser:
            return True
        return policy_registry.get_by_type(Page).user_has_any_permission(
            user, {"add", "change", "publish", "bulk_delete"}
        )

    def user_has_permission_for_instance(self, user, action, instance):
        return self.user_has_any_permission_for_instance(user, {action}, instance)

    def user_has_any_permission_for_instance(self, user, actions, instance):
        if not user or not getattr(user, "is_active", False) or not getattr(user, "is_authenticated", False):
            return False
        if "view" not in set(actions):
            return False
        if user.is_superuser:
            return True
        root_page = Page.objects.filter(id=instance.root_page_id).first()
        if root_page is not None:
            return can_restore_recycled_page(user, root_page.specific_deferred, instance)
        return False

    def users_with_any_permission(self, actions):
        if "view" not in set(actions):
            return get_user_model().objects.filter(is_active=True, is_superuser=True)
        return policy_registry.get_by_type(Page).users_with_any_permission({"add", "change", "publish"})

    def instances_user_has_any_permission_for(self, user, actions):
        if not user or not getattr(user, "is_active", False) or not getattr(user, "is_authenticated", False):
            return self.model._default_manager.none()
        if "view" not in set(actions):
            return self.model._default_manager.none()
        if user.is_superuser:
            return self.model._default_manager.all()

        page_policy = policy_registry.get_by_type(Page)
        user_perms = list(page_policy.get_cached_permissions_for_user(user))
        change_codename = page_policy._get_permission_codename("change")
        add_codename = page_policy._get_permission_codename("add")

        editor_roots = page_policy._deduplicate_roots(
            [p.page for p in user_perms if p.permission.codename == change_codename]
        )
        add_roots = page_policy._deduplicate_roots(
            [p.page for p in user_perms if p.permission.codename == add_codename]
        )

        creatable_ct_ids = get_author_creatable_content_type_ids()
        q = Q()
        if add_roots and creatable_ct_ids:
            own_creatable_ids = Page.objects.filter(
                page_policy._descendants_q(add_roots)
                & Q(owner_id=user.pk, content_type_id__in=creatable_ct_ids)
            ).values_list("id", flat=True)
            q |= Q(actor_id=user.pk, root_page_id__in=own_creatable_ids)

        if editor_roots:
            editable_ids = Page.objects.filter(page_policy._descendants_q(editor_roots)).values_list("id", flat=True)
            q |= Q(root_page_id__in=editable_ids)

        if not q:
            return self.model._default_manager.none()
        return self.model._default_manager.filter(q)


def ensure_author_role_infrastructure() -> dict[str, object]:
    """
    幂等初始化 'Authors' 受控作者权限组与双区媒体集合：
    1. Authors 组授予 wagtailadmin.access_admin（不设 is_staff）；
    2. 在站点根 HomePage（若存在，否则站点首节点）授予 add_page + publish_page（绝不授 change_page）；
    3. 建立 '作者个人工作区'（Authors 仅授 add/choose，策略层强隔离上传者本人）与
       '全站公共素材库'（Authors 仅授 choose，策略层仅放行 visibility=public）。
    """
    authors_group, _ = Group.objects.get_or_create(name="Authors")

    access_admin_perm = Permission.objects.filter(
        content_type__app_label="wagtailadmin",
        codename="access_admin",
    ).first()
    if access_admin_perm:
        authors_group.permissions.add(access_admin_perm)

    # 页面权限：优先挂载在 HomePage（depth=2），若未初始化站点则挂载在 Root
    home_ct = ContentType.objects.filter(app_label="home", model="homepage").first()
    target_root = None
    if home_ct:
        target_root = Page.objects.filter(content_type=home_ct).order_by("path").first()
    if target_root is None:
        target_root = Page.get_first_root_node()

    if target_root is not None:
        add_page_perm = Permission.objects.filter(content_type__app_label="wagtailcore", codename="add_page").first()
        pub_page_perm = Permission.objects.filter(content_type__app_label="wagtailcore", codename="publish_page").first()
        for perm in (add_page_perm, pub_page_perm):
            if perm:
                GroupPagePermission.objects.get_or_create(
                    group=authors_group,
                    page=target_root,
                    permission=perm,
                )
        # 确保 Authors 不误持 change_page / lock_page / unlock_page / bulk_delete_page
        GroupPagePermission.objects.filter(
            group=authors_group,
            permission__codename__in=["change_page", "lock_page", "unlock_page", "bulk_delete_page"],
        ).delete()

    root_col = Collection.get_first_root_node()
    if root_col is None:
        root_col = Collection.add_root(name="Root")

    workspace_col = root_col.get_children().filter(name="作者个人工作区").first()
    if workspace_col is None:
        workspace_col = root_col.add_child(name="作者个人工作区")

    public_col = root_col.get_children().filter(name="全站公共素材库").first()
    if public_col is None:
        public_col = root_col.add_child(name="全站公共素材库")

    img_perms = {
        p.codename: p
        for p in Permission.objects.filter(content_type__app_label="wagtailimages")
    }
    doc_perms = {
        p.codename: p
        for p in Permission.objects.filter(content_type__app_label="wagtaildocs")
    }

    for codename in ("add_image", "choose_image"):
        if codename in img_perms:
            GroupCollectionPermission.objects.get_or_create(
                group=authors_group, collection=workspace_col, permission=img_perms[codename]
            )
    for codename in ("add_document", "choose_document"):
        if codename in doc_perms:
            GroupCollectionPermission.objects.get_or_create(
                group=authors_group, collection=workspace_col, permission=doc_perms[codename]
            )
    # 确保 Authors 在个人工作区无 change_* 权限
    GroupCollectionPermission.objects.filter(
        group=authors_group,
        collection=workspace_col,
        permission__codename__in=["change_image", "change_document"],
    ).delete()

    # 全站公共素材库：Authors 仅可 choose
    if "choose_image" in img_perms:
        GroupCollectionPermission.objects.get_or_create(
            group=authors_group, collection=public_col, permission=img_perms["choose_image"]
        )
    if "choose_document" in doc_perms:
        GroupCollectionPermission.objects.get_or_create(
            group=authors_group, collection=public_col, permission=doc_perms["choose_document"]
        )
    GroupCollectionPermission.objects.filter(
        group=authors_group,
        collection=public_col,
        permission__codename__in=["add_image", "change_image", "add_document", "change_document"],
    ).delete()

    # Editors / Moderators 自动享有新集合全部媒体管理权限
    for grp in Group.objects.filter(name__in=["Editors", "Moderators"]):
        for col in (workspace_col, public_col):
            for p in list(img_perms.values()) + list(doc_perms.values()):
                if p.codename in ("add_image", "change_image", "choose_image", "add_document", "change_document", "choose_document"):
                    GroupCollectionPermission.objects.get_or_create(
                        group=grp, collection=col, permission=p
                    )

    return {
        "authors_group": authors_group,
        "target_root_page": target_root,
        "workspace_collection": workspace_col,
        "public_collection": public_col,
    }
