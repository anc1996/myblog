"""
Audio 与 Video 的受控 SnippetViewSet + 双标签 ChooserViewSet（支持页面内直接上传）。
"""

from __future__ import annotations

from django.db import transaction
from django.utils.functional import cached_property
from wagtail.permissions import policy_registry
from wagtail.snippets.views.chooser import (
    ChooseResultsView,
    ChooseView,
    SnippetChooserViewSet,
    SnippetCreateView,
)
from wagtail.snippets.views.snippets import (
    CreateView as SnippetIndexCreateView,
    EditView as SnippetIndexEditView,
    SnippetViewSet,
)

from media_library.forms import (
    AudioAdminEditorForm,
    AudioUploadForm,
    VideoAdminEditorForm,
    VideoUploadForm,
)
from media_library.models import Audio, Video, VisibilityChoices


class AuthorIsolatedChooseView(ChooseView):
    def get_object_list(self):
        policy = policy_registry.get_by_type(self.model_class)
        return policy.instances_user_has_any_permission_for(self.request.user, ["choose"]).order_by("-created_at")


class AuthorIsolatedChooseResultsView(ChooseResultsView):
    def get_object_list(self):
        policy = policy_registry.get_by_type(self.model_class)
        return policy.instances_user_has_any_permission_for(self.request.user, ["choose"]).order_by("-created_at")


class MediaSnippetCreateView(SnippetCreateView):
    """页面选择弹窗内直接上传音视频：自动绑定 uploaded_by_user、storage_key 与默认 RESTRICTED。"""

    def save_form(self, form):
        with transaction.atomic():
            instance = form.save(commit=False)
            instance.uploaded_by_user = self.request.user
            instance.visibility = VisibilityChoices.RESTRICTED
            instance.save()
            if instance.file and not instance.storage_key:
                instance.storage_key = instance.file.name
                instance.save(update_fields=["storage_key"])
        return instance


class AudioChooserViewSet(SnippetChooserViewSet):
    choose_view_class = AuthorIsolatedChooseView
    choose_results_view_class = AuthorIsolatedChooseResultsView
    create_view_class = MediaSnippetCreateView
    creation_form_class = AudioUploadForm
    creation_tab_id = "upload"
    create_action_label = "上传并选用"
    create_action_clicked_label = "上传中..."


class VideoChooserViewSet(SnippetChooserViewSet):
    choose_view_class = AuthorIsolatedChooseView
    choose_results_view_class = AuthorIsolatedChooseResultsView
    create_view_class = MediaSnippetCreateView
    creation_form_class = VideoUploadForm
    creation_tab_id = "upload"
    create_action_label = "上传并选用"
    create_action_clicked_label = "上传中..."


class MediaSnippetSideCreateView(SnippetIndexCreateView):
    def get_form_class(self):
        user = self.request.user
        can_manage = user.is_superuser or user.has_perm(
            f"{self.model._meta.app_label}.change_{self.model._meta.model_name}"
        )
        if self.model is Audio:
            return AudioAdminEditorForm if can_manage else AudioUploadForm
        return VideoAdminEditorForm if can_manage else VideoUploadForm

    def save_form(self, form):
        with transaction.atomic():
            instance = form.save(commit=False)
            if not instance.uploaded_by_user_id:
                instance.uploaded_by_user = self.request.user
            if not self.request.user.is_superuser and not self.request.user.has_perm(
                f"{self.model._meta.app_label}.change_{self.model._meta.model_name}"
            ):
                instance.visibility = VisibilityChoices.RESTRICTED
            instance.save()
            if instance.file and not instance.storage_key:
                instance.storage_key = instance.file.name
                instance.save(update_fields=["storage_key"])
        return instance


class MediaSnippetSideEditView(SnippetIndexEditView):
    def get_form_class(self):
        user = self.request.user
        can_manage = user.is_superuser or user.has_perm(
            f"{self.model._meta.app_label}.change_{self.model._meta.model_name}"
        )
        if self.model is Audio:
            return AudioAdminEditorForm if can_manage else AudioUploadForm
        return VideoAdminEditorForm if can_manage else VideoUploadForm


class AudioSnippetViewSet(SnippetViewSet):
    model = Audio
    icon = "media"
    menu_label = "音频库"
    chooser_viewset_class = AudioChooserViewSet
    add_view_class = MediaSnippetSideCreateView
    edit_view_class = MediaSnippetSideEditView
    list_display = ("title", "visibility", "uploaded_by_user", "created_at")
    search_fields = ("title",)

    @cached_property
    def permission_policy(self):
        return policy_registry.get_by_type(Audio)

    def get_form_class(self, for_update=False):
        # SnippetViewSet 默认 form_class 根据权限分层：普通作者用 AudioUploadForm（不含 visibility）
        return AudioAdminEditorForm


class VideoSnippetViewSet(SnippetViewSet):
    model = Video
    icon = "media"
    menu_label = "视频库"
    chooser_viewset_class = VideoChooserViewSet
    add_view_class = MediaSnippetSideCreateView
    edit_view_class = MediaSnippetSideEditView
    list_display = ("title", "visibility", "uploaded_by_user", "created_at")
    search_fields = ("title",)

    @cached_property
    def permission_policy(self):
        return policy_registry.get_by_type(Video)

    def get_form_class(self, for_update=False):
        return VideoAdminEditorForm
