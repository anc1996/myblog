"""
接管 Wagtail 原生 /admin/account/ 个人账户设置视图。

目的：
1. 移除 Wagtail 原生 AvatarSettingsPanel（避免直接写入 wagtailusers.UserProfile.avatar 绕过 Pillow 清洗与 RustFS 隔离）；
2. 替换为统一的 MemberProfile 头像上传面板，后台用户与前台会员走同一套重编码与 /avatar-gateway/ 交付流水线；
3. 将后台自助改密接管至 AccountLifecycleService.self_change_password，确保改密后原子递增 session_epoch 踢下线其他设备会话。
"""

from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from wagtail.admin.views.account import (
    AccountView,
    AvatarSettingsPanel,
    BaseSettingsPanel,
    ChangePasswordPanel,
    profile_tab,
)

from identity.account_lifecycle import AccountLifecycleService
from identity.avatars import sanitize_and_encode_avatar, upload_member_avatar
from identity.models import MemberProfile


class UnifiedAvatarUploadForm(forms.Form):
    """统一头像上传表单（复用 Pillow 13.0.0 纯像素重编码与 MemberProfile 存储）。"""

    avatar = forms.FileField(
        label="上传新头像（静态 JPG/PNG/WebP，不超过 2MB）",
        required=False,
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self._validated_file = None

    def clean_avatar(self):
        uploaded = self.cleaned_data.get("avatar")
        if not uploaded:
            return None
        # 在表单验证阶段即执行严格解码与尺寸/格式校验，避免提交报错后界面不显示错误
        sanitize_and_encode_avatar(uploaded)
        if hasattr(uploaded, "seek"):
            uploaded.seek(0)
        self._validated_file = uploaded
        return uploaded

    def save(self):
        uploaded = self._validated_file
        if uploaded and self.user:
            return upload_member_avatar(user=self.user, uploaded_file=uploaded)
        return MemberProfile.get_or_create_for_user(self.user) if self.user else None


class UnifiedAvatarSettingsPanel(BaseSettingsPanel):
    """替换 Wagtail 原生 AvatarSettingsPanel 的统一头像面板。"""

    name = "unified_avatar"
    title = "个人头像（统一安全存储）"
    tab = profile_tab
    order = 300
    form_class = UnifiedAvatarUploadForm
    template_name = "identity/admin/unified_avatar_panel.html"

    def get_form(self):
        if self.request.method == "POST":
            return self.form_class(
                self.request.POST,
                self.request.FILES,
                user=self.user,
                prefix=self.name,
            )
        return self.form_class(user=self.user, prefix=self.name)

    def get_context_data(self):
        ctx = super().get_context_data()
        member_profile = MemberProfile.get_or_create_for_user(self.user)
        ctx["member_profile"] = member_profile
        return ctx


class UnifiedAdminPasswordChangeForm(PasswordChangeForm):
    """接入 AccountLifecycleService 的后台自助改密表单，同步递增 session_epoch。"""

    def __init__(self, user, *args, request=None, **kwargs):
        self._request = request
        super().__init__(user, *args, **kwargs)

    def save(self, commit=True):
        if not commit:
            return super().save(commit=False)
        AccountLifecycleService.self_change_password(
            user=self.user,
            old_password=self.cleaned_data["old_password"],
            new_password=self.cleaned_data["new_password1"],
            request=self._request,
        )
        return self.user


class UnifiedChangePasswordPanel(ChangePasswordPanel):
    """替换原生 ChangePasswordPanel，使后台改密也同步踢下线其他设备会话。"""

    form_class = UnifiedAdminPasswordChangeForm

    def get_form(self):
        bind_form = False
        if self.request.method == "POST":
            bind_form = any(
                [
                    self.request.POST.get(self.name + "-old_password"),
                    self.request.POST.get(self.name + "-new_password1"),
                    self.request.POST.get(self.name + "-new_password2"),
                ]
            )
        if bind_form:
            return self.form_class(
                self.user,
                self.request.POST,
                request=self.request,
                prefix=self.name,
            )
        return self.form_class(self.user, request=self.request, prefix=self.name)


class CustomAccountView(AccountView):
    """
    定制版 Wagtail /admin/account/ 视图：
    剔除原生 AvatarSettingsPanel，挂载统一头像与会话感知改密面板。
    """

    def get_panels(self):
        panels = super().get_panels()
        filtered = []
        has_avatar_panel = False
        for panel in panels:
            if isinstance(panel, AvatarSettingsPanel) or getattr(panel, "name", "") == "avatar":
                if not has_avatar_panel:
                    filtered.append(UnifiedAvatarSettingsPanel(self.request, self.request.user, panel.profile))
                    has_avatar_panel = True
                continue
            if isinstance(panel, ChangePasswordPanel) and not isinstance(panel, UnifiedChangePasswordPanel):
                filtered.append(UnifiedChangePasswordPanel(self.request, self.request.user, panel.profile))
                continue
            filtered.append(panel)
        return filtered
