"""
全站统一业务页面后台表单基类 (CustomBasePageAdminForm)。

为所有继承 CustomBasePage 的业务页面提供统一拦截与作者署名核验（P1-4）：
- 超管或具备 subtree change 权限的主编/管理员可自由调整署名；
- 受限作者（仅具备 add/publish 权限）保存或发布页面时，禁止将他人设为第一作者或主要作者。
"""

from django.core.exceptions import ValidationError
from wagtail.admin.forms.pages import WagtailAdminPageForm
from content_lifecycle.models import (
    MAX_RESTRICTED_AUTHOR_ROWS,
    validate_restricted_author_attributions,
    validate_universal_author_attributions,
)


class CustomBasePageAdminForm(WagtailAdminPageForm):
    """全站 CustomBasePage 统一后台表单基类。"""

    def _validate_restricted_authors_formset(self) -> None:
        for_user = getattr(self, "for_user", None)
        authors_formset = self.formsets.get("authors") if hasattr(self, "formsets") else None
        if authors_formset is None:
            return

        from people.models import Person

        author_entries = []
        for idx, form in enumerate(authors_formset.forms):
            cdata = getattr(form, "cleaned_data", None)
            if not cdata or cdata.get("DELETE"):
                continue
            person = cdata.get("person")
            if person is None:
                continue
            author_entries.append({
                "person_id": person.pk,
                "is_system_anonymous": bool(getattr(person, "is_system_anonymous", False)),
                "role": cdata.get("role") or "",
                "sort_order": cdata.get("ORDER"),
                "orig_idx": idx,
                "exists": True,
            })

        # 全角色通用校验：限制 50 人、禁止重复人物、禁止显式选入系统「佚名」
        validate_universal_author_attributions(
            author_entries,
            max_authors=MAX_RESTRICTED_AUTHOR_ROWS,
        )

        if not for_user or not getattr(for_user, "is_authenticated", False) or getattr(for_user, "is_superuser", False):
            return

        from content_lifecycle.permissions import user_is_restricted_author_for_page

        check_page = getattr(self, "parent_page", None)
        if check_page is None and getattr(self.instance, "pk", None):
            check_page = self.instance.get_parent()
        if check_page is None:
            check_page = self.instance

        if not user_is_restricted_author_for_page(for_user, check_page):
            return

        linked_person = Person.objects.filter(
            linked_user_id=for_user.pk,
            is_system_anonymous=False,
        ).first()
        is_draft_save = bool(getattr(self, "is_deferred_validation", False))
        validate_restricted_author_attributions(
            author_entries,
            linked_person_id=linked_person.pk if linked_person is not None else None,
            allow_empty_draft=is_draft_save,
            max_authors=MAX_RESTRICTED_AUTHOR_ROWS,
        )

    def is_valid(self):
        valid = super().is_valid()
        if valid:
            try:
                self._validate_restricted_authors_formset()
            except ValidationError as exc:
                self.add_error(None, exc)
                return False
        return valid
