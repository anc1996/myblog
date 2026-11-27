"""
people.wagtail_hooks
公开人物与作者名片的 Wagtail 8.1 受控 Snippet 管理与选择器保护。
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from wagtail import hooks
from wagtail.admin.forms import WagtailAdminModelForm
from wagtail.admin.panels import ObjectList
from wagtail.permission_policies import ModelPermissionPolicy
from wagtail.permissions import register_permission_policy
from wagtail.snippets.models import register_snippet
from wagtail.snippets.views.chooser import (
    ChooseResultsView,
    ChooseView,
    SnippetChooserViewSet,
)
from wagtail.snippets.views.snippets import (
    DeleteView as SnippetDeleteView,
    SnippetViewSet,
)

from people.models import Person


@receiver(pre_delete, sender=Person)
def guard_system_anonymous_person_delete(sender, instance: Person, **kwargs):
    """ORM 与批量删除兜底防线：永久禁止删除内置「佚名」名片。"""
    if getattr(instance, "is_system_anonymous", False) or getattr(instance, "slug", "") == "anonymous":
        raise PermissionDenied("系统内置「佚名」名片受永久保护，禁止删除。")


class PersonPermissionPolicy(ModelPermissionPolicy):
    """关闭后台 UI 的普通硬删入口，避免误删已引用作者或系统名片。"""

    def user_has_permission_for_instance(self, user, action, instance):
        if action == "delete":
            return False
        return super().user_has_permission_for_instance(user, action, instance)


class PersonAdminForm(WagtailAdminModelForm):
    """编辑系统「佚名」名片时禁用并拒绝 linked_user、email 与 show_public_comments。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if getattr(self.instance, "is_system_anonymous", False):
            for fname in ("linked_user", "email", "show_public_comments"):
                if fname in self.fields:
                    self.fields[fname].disabled = True
                    self.fields[fname].required = False

    def clean(self):
        cleaned = super().clean()
        if getattr(self.instance, "is_system_anonymous", False):
            cleaned["linked_user"] = None
            cleaned["email"] = ""
            cleaned["show_public_comments"] = False
        return cleaned


class PersonSnippetDeleteView(SnippetDeleteView):
    def dispatch(self, request, *args, **kwargs):
        raise PermissionDenied("公开人物名片已关闭后台硬删入口（系统内置「佚名」名片受永久保护）。")


class PersonChooseView(ChooseView):
    def get_object_list(self):
        return super().get_object_list().filter(is_system_anonymous=False)


class PersonChooseResultsView(ChooseResultsView):
    def get_object_list(self):
        return super().get_object_list().filter(is_system_anonymous=False)


class PersonChooserViewSet(SnippetChooserViewSet):
    choose_view_class = PersonChooseView
    choose_results_view_class = PersonChooseResultsView


class PersonSnippetViewSet(SnippetViewSet):
    model = Person
    menu_label = "公开作者名片"
    icon = "user"
    menu_order = 225
    add_to_admin_menu = False
    list_display = ["name", "slug", "initial", "linked_user", "is_system_anonymous"]
    list_filter = ["initial", "is_system_anonymous"]
    search_fields = ["name", "slug", "bio"]
    edit_handler = ObjectList(Person.panels, base_form_class=PersonAdminForm)
    delete_view_class = PersonSnippetDeleteView
    chooser_viewset_class = PersonChooserViewSet


register_permission_policy(Person, PersonPermissionPolicy(Person))
register_snippet(Person, PersonSnippetViewSet)


@hooks.register("before_delete_snippet")
def guard_person_snippet_delete(request, instances):
    for obj in instances:
        if isinstance(obj, Person):
            raise PermissionDenied("公开人物名片已关闭后台硬删入口（系统内置「佚名」名片受永久保护）。")
