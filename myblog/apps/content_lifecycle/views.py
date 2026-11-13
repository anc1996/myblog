"""
全站通用 Wagtail 页面管理与受控回收站安全视图覆写 (Phase 4)。

统一收口 Wagtail 原生页面视图的已知旁路，不针对具体页面类型硬编码：
1. OwnershipAddSubpageView: 过滤掉受限作者无权创建的栏目页/单页模型；
2. OwnershipCreateView: 创建保存时强制绑定 owner = request.user；
3. OwnershipPreviewOnCreateView: 禁止受限作者直接访问非 author_creatable 模型或非授权父页的新建预览端点；
4. OwnershipChooseParentView: 过滤掉不可创建类型的父页候选与非 author_allow_child_creation 父节点；
5. OwnershipCopyView: 隐藏并禁止受限作者的 Alias 别名创建与直接复制上线；
6. OwnershipPageViewSet: 替换 Wagtail 默认 PageViewSet，全站 Page 类型自动生效。
"""

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import TemplateView
from wagtail.admin.ui.tables import BaseColumn
from wagtail.admin.views import generic
from wagtail.admin.views.pages.choose_parent import GenericChooseParentView
from wagtail.admin.views.pages.copy import CopyView
from wagtail.admin.views.pages.create import AddSubpageView, CreateView
from wagtail.admin.views.pages.preview import PreviewOnCreateView
from wagtail.admin.viewsets.pages import PageViewSet
from wagtail.models import Page

from content_lifecycle.permissions import (
    get_author_allow_child_content_type_ids,
    user_is_restricted_author_for_page,
)


class OwnershipAddSubpageView(AddSubpageView):
    """按操作人模式过滤允许新建的子页面类型列表。"""

    def dispatch(self, request, parent_page_id, *args, **kwargs):
        self.parent_page = get_object_or_404(Page, id=parent_page_id).specific
        if not self.parent_page.permissions_for_user(request.user).can_add_subpage():
            raise PermissionDenied

        is_restricted = user_is_restricted_author_for_page(request.user, self.parent_page)
        creatable_models = []
        for model in type(self.parent_page).creatable_subpage_models():
            if not model.can_create_at(self.parent_page):
                continue
            if is_restricted and not getattr(model, "author_creatable", False):
                continue
            creatable_models.append(model)

        if not creatable_models:
            raise PermissionDenied("当前栏目下没有允许当前账号创建的页面类型。")

        self.page_types = []
        for model in creatable_models:
            self.page_types.append(
                (
                    model.get_verbose_name(),
                    model._meta.app_label,
                    model._meta.model_name,
                    model.get_page_description(),
                )
            )
        self.page_types.sort(key=lambda page_type: page_type[0].lower())

        if len(self.page_types) == 1:
            _, app_label, model_name, _ = self.page_types[0]
            return redirect("wagtailadmin_pages:add", app_label, model_name, self.parent_page.id)

        return TemplateView.dispatch(self, request, parent_page_id, *args, **kwargs)


class OwnershipCreateView(CreateView):
    """服务端创建固定绑定 owner = request.user，防止伪造提交。"""

    def save_action(self):
        self.page = self.form.save(commit=False)
        self.page.owner = self.request.user
        return super().save_action()

    def publish_action(self):
        self.page = self.form.save(commit=False)
        self.page.owner = self.request.user
        return super().publish_action()

    def submit_action(self):
        self.page = self.form.save(commit=False)
        self.page.owner = self.request.user
        return super().submit_action()


class OwnershipPreviewOnCreateView(PreviewOnCreateView):
    """封堵 PreviewOnCreateView 对非授权栏目/非 author_creatable 模型的预览绕过。"""

    def get_object(self):
        model_class = self.content_type.model_class()
        parent_page = get_object_or_404(Page, id=self.parent_object_id).specific
        if (
            model_class is None
            or not issubclass(model_class, Page)
            or model_class not in parent_page.creatable_subpage_models()
            or not model_class.can_create_at(parent_page)
        ):
            raise PermissionDenied
        if user_is_restricted_author_for_page(self.request.user, parent_page):
            if (
                getattr(parent_page, "is_recycled", False)
                or not getattr(parent_page, "author_allow_child_creation", False)
                or not getattr(model_class, "author_creatable", False)
            ):
                raise PermissionDenied("受限作者无权预览创建此页面类型。")
        page = super().get_object()
        page.owner = self.request.user
        return page


class OwnershipChooseParentView(GenericChooseParentView):
    """收口 /admin/pages/create/<app>/<model> 选择父页面视图。"""

    def dispatch(self, request, *args, **kwargs):
        from django.contrib.contenttypes.models import ContentType

        target_model = ContentType.objects.get_by_natural_key(
            kwargs.get("app_label"),
            kwargs.get("model_name"),
        ).model_class()
        if not request.user.is_superuser:
            change_codename = self.permission_policy._get_permission_codename("change")
            has_any_change = any(
                p.permission.codename == change_codename
                for p in self.permission_policy.get_cached_permissions_for_user(request.user)
            )
            if not has_any_change and not getattr(target_model, "author_creatable", False):
                raise PermissionDenied("受限作者无权创建此页面类型。")
        return super().dispatch(request, *args, **kwargs)

    def get_valid_parent_pages(self, user):
        parents = super().get_valid_parent_pages(user)
        if user.is_superuser:
            return parents
        from content_lifecycle.models import RecycleEntry

        recycled_ids = set(
            RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
        )
        if recycled_ids:
            parents = parents.exclude(id__in=recycled_ids)

        allow_child_ct_ids = set(get_author_allow_child_content_type_ids())
        target_is_creatable = bool(getattr(self.model, "author_creatable", False))
        allowed_ids = []
        for parent in parents:
            if not user_is_restricted_author_for_page(user, parent, policy=self.permission_policy):
                allowed_ids.append(parent.id)
            elif target_is_creatable and parent.content_type_id in allow_child_ct_ids:
                allowed_ids.append(parent.id)
        return parents.filter(id__in=allowed_ids)


class OwnershipCopyView(CopyView):
    """收口页面复制：对受限作者屏蔽并阻断 Alias 别名与直接复制上线。"""

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if user_is_restricted_author_for_page(self.request.user, self.page):
            form.fields.pop("alias", None)
            form.fields.pop("publish_copies", None)
            if "copy_subpages" in form.fields:
                form.fields.pop("copy_subpages", None)
        return form


class OwnershipPageViewSet(PageViewSet):
    """替换 Wagtail 默认 base_page_viewset 的通用页面视图集。"""

    add_view_class = OwnershipCreateView
    add_subpage_view_class = OwnershipAddSubpageView
    choose_parent_view_class = OwnershipChooseParentView
    copy_view_class = OwnershipCopyView
    preview_on_add_view_class = OwnershipPreviewOnCreateView


class RecycleBatchIndexView(generic.IndexView):
    """受控回收站列表视图：关闭增删改表单，仅展示当前操作人可见的批次。"""

    any_permission_required = ["view"]

    def get_add_url(self):
        return None

    def get_edit_url(self, instance):
        return None

    def get_delete_url(self, instance):
        return None

    def get_base_queryset(self):
        return self.permission_policy.instances_user_has_any_permission_for(self.request.user, ["view"])


class RecycleBatchInspectView(generic.InspectView):
    """受控回收站详情视图：仅限具备该批次查看权限的操作人访问。"""

    any_permission_required = ["view"]

    def get_object(self, queryset=None):
        obj = super().get_object(queryset=queryset)
        if not self.permission_policy.user_has_permission_for_instance(self.request.user, "view", obj):
            raise PermissionDenied("无权查看该回收批次详情。")
        return obj


class RecycleBatchActionsColumn(BaseColumn):
    """按请求用户角色动态渲染回收站操作列：非超管绝不渲染'彻底销毁'按钮。"""

    def render_cell_html(self, instance, parent_context):
        from django.urls import reverse
        from django.utils.safestring import mark_safe

        request = parent_context.get("request")
        user = getattr(request, "user", None) if request else None
        if instance.status == "restored":
            return mark_safe('<span style="color:#16a34a;font-weight:600;">已恢复为草稿</span>')
        if instance.status != "recycled":
            return mark_safe('<span style="color:#94a3b8;">已彻底销毁</span>')

        buttons = []
        try:
            restore_url = reverse("recycle_bin:restore", args=[instance.pk])
            buttons.append(
                f'<a href="{restore_url}" class="button button-small button-secondary" style="margin-right:8px;">一键恢复</a>'
            )
        except Exception:
            pass
        if user and getattr(user, "is_superuser", False):
            try:
                purge_url = reverse("recycle_bin:purge", args=[instance.pk])
                buttons.append(
                    f'<a href="{purge_url}" class="button button-small button-danger" style="background:#dc2626;color:#fff;">彻底销毁</a>'
                )
            except Exception:
                pass
        return mark_safe("".join(buttons) or "待恢复处理")
