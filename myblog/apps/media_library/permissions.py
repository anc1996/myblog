"""
受控作者媒体隔离权限策略 (AuthorIsolatedMediaPermissionPolicy / P0-2)。

解决 Wagtail 原生 CollectionOwnershipPermissionPolicy 在 choose 动作上不检查 owner 的问题：
1. 对具备 collection 'change' 权限的主编/管理员保持全量权限；
2. 对工作区集合（具备 add 无 change）：无论是 choose、change 还是 delete，
   一律强制过滤 uploaded_by_user == user；
3. 对公共只读素材库集合（仅具备 choose，无 add/change）：
   仅允许选用 visibility == PUBLIC 的公开资产，严禁越权查看未公开草稿媒体。
"""

from django.contrib.auth import get_user_model
from django.db.models import Q
from wagtail.permission_policies.collections import CollectionOwnershipPermissionPolicy

from media_library.models import VisibilityChoices


class AuthorIsolatedMediaPermissionPolicy(CollectionOwnershipPermissionPolicy):
    """针对 CustomImage 与 CustomDocument 的归属与可见性隔离权限策略。"""

    def user_has_any_permission_for_instance(self, user, actions, instance):
        if not (user and user.is_active and user.is_authenticated):
            return False
        if user.is_superuser:
            return True

        known_actions = set(actions) & {"add", "choose", "change"}
        if "delete" in actions:
            known_actions.add("change")
        if not known_actions:
            return False

        col = getattr(instance, "collection", None)
        if self._check_perm(user, ["change"], collection=col):
            return self._check_perm(user, known_actions, collection=col)

        owner_id = getattr(instance, f"{self.owner_field_name}_id", None)
        is_owner = owner_id is not None and owner_id == user.pk
        has_add = self._check_perm(user, ["add"], collection=col)

        if "change" in known_actions and is_owner and has_add:
            return True
        if "choose" in known_actions and self._check_perm(user, ["choose"], collection=col):
            if has_add:
                return is_owner
            return getattr(instance, "visibility", None) == VisibilityChoices.PUBLIC
        return False

    def instances_user_has_any_permission_for(self, user, actions):
        if not (user and user.is_active and user.is_authenticated):
            return self.model._default_manager.none()
        if user.is_superuser:
            return self.model._default_manager.all()

        known_actions = set(actions) & {"change", "choose"}
        if "delete" in actions:
            known_actions.add("change")
        if not known_actions:
            return self.model._default_manager.none()

        change_cols = self._collections_with_perm(user, ["change"])
        add_cols = self._collections_with_perm(user, ["add"])
        choose_cols = self._collections_with_perm(user, ["choose"])

        perm_filter = Q()
        if "change" in known_actions:
            perm_filter |= Q(collection__in=change_cols)
            perm_filter |= Q(collection__in=add_cols) & Q(**{self.owner_field_name: user})

        if "choose" in known_actions:
            editor_choose_cols = choose_cols.filter(id__in=change_cols.values("id"))
            workspace_choose_cols = choose_cols.filter(id__in=add_cols.values("id")).exclude(
                id__in=change_cols.values("id")
            )
            public_ro_choose_cols = choose_cols.exclude(id__in=change_cols.values("id")).exclude(
                id__in=add_cols.values("id")
            )
            perm_filter |= Q(collection__in=editor_choose_cols)
            perm_filter |= Q(collection__in=workspace_choose_cols) & Q(**{self.owner_field_name: user})
            perm_filter |= Q(collection__in=public_ro_choose_cols, visibility=VisibilityChoices.PUBLIC)

        if not perm_filter:
            return self.model._default_manager.none()
        return self.model._default_manager.filter(perm_filter)

    def users_with_any_permission_for_instance(self, actions, instance):
        known_actions = set(actions) & {"choose", "change"}
        if "delete" in actions:
            known_actions.add("change")
        if not known_actions:
            return get_user_model().objects.filter(is_active=True, is_superuser=True)

        filter_expr = self._users_with_perm_filter({"change"}, collection=instance.collection)
        owner = getattr(instance, self.owner_field_name, None)
        if owner is not None and self._check_perm(owner, {"add"}, collection=instance.collection):
            if "change" in known_actions or (
                "choose" in known_actions and self._check_perm(owner, {"choose"}, collection=instance.collection)
            ):
                filter_expr |= Q(pk=owner.pk)
        if "choose" in known_actions and getattr(instance, "visibility", None) == VisibilityChoices.PUBLIC:
            filter_expr |= self._users_with_perm_filter({"choose"}, collection=instance.collection)

        return get_user_model().objects.filter(filter_expr).distinct()
