"""
全站统一业务页面后台表单基类 (CustomBasePageAdminForm)。

为所有继承 CustomBasePage 的业务页面提供统一拦截与作者署名核验（P1-4）：
- 超管或具备 subtree change 权限的主编/管理员可自由调整署名；
- 受限作者（仅具备 add/publish 权限）保存或发布页面时，禁止将他人设为第一作者或主要作者。
"""

from django.core.exceptions import ValidationError
from wagtail.admin.forms.pages import WagtailAdminPageForm


class CustomBasePageAdminForm(WagtailAdminPageForm):
    """全站 CustomBasePage 统一后台表单基类。"""

    def _validate_restricted_authors_formset(self) -> None:
        for_user = getattr(self, "for_user", None)
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

        authors_formset = self.formsets.get("authors") if hasattr(self, "formsets") else None
        if not authors_formset:
            return

        from people.models import Person

        linked_person = Person.objects.filter(linked_user_id=for_user.pk).first()
        active_rows = []
        for idx, form in enumerate(authors_formset.forms):
            cdata = getattr(form, "cleaned_data", None)
            if not cdata or cdata.get("DELETE"):
                continue
            person = cdata.get("person")
            if person is None:
                continue
            order_val = cdata.get("ORDER")
            sort_key = order_val if order_val is not None else idx
            active_rows.append((sort_key, idx, person, cdata.get("role") or ""))

        if not active_rows:
            return

        active_rows.sort(key=lambda item: (item[0], item[1]))
        first_person = active_rows[0][2]

        if linked_person is not None:
            if first_person.pk != linked_person.pk:
                raise ValidationError("受限作者保存页面时，第一署名作者必须为您本人关联的公开人物名片。")
            for _, _, person, role in active_rows:
                if role == "primary_author" and person.pk != linked_person.pk:
                    raise ValidationError("受限作者禁止将其他人员设为主要作者 (primary_author)。")
        else:
            raise ValidationError("当前账号尚未由管理员绑定公开人物名片，受限作者不可擅自填写或冒用他人人物署名。")

    def is_valid(self):
        valid = super().is_valid()
        if valid:
            try:
                self._validate_restricted_authors_formset()
            except ValidationError as exc:
                self.add_error(None, exc)
                return False
        return valid
