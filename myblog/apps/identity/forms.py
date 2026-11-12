"""
前台会员中心与账号认证表单定义。

设计要点：
1. RegisterForm 仅接收安全白名单注册字段，绝不暴露或绑定 is_staff/is_superuser/groups 等权限字段；
2. ProfileUpdateForm 严格限制仅可修改 nickname(<=50) 与 bio(<=300)，绝不允许修改 recovery_hint 或权限字段；
3. RegisterForm 与 LoginForm 的验证码字段设 required=False，确保未配置验证码提供方时请求能到达校验层并按 Fail-Closed 返回 503。
"""

from __future__ import annotations

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from identity.credentials import validate_safe_username
from identity.models import User


class RegisterForm(forms.Form):
    """前台会员注册表单。"""

    username = forms.CharField(
        label="用户名",
        max_length=32,
        required=True,
        help_text="3~32 位字母、数字或下划线组合，注册后用于登录与个人标识。",
        widget=forms.TextInput(
            attrs={
                "autocomplete": "username",
                "placeholder": "3~32 位字母、数字或下划线",
            }
        ),
        error_messages={"required": "请输入用户名。"},
    )
    password = forms.CharField(
        label="登录密码",
        required=True,
        strip=False,
        help_text="至少 8 位字符，且不可为纯数字或常见弱密码。",
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "new-password",
                "placeholder": "请输入登录密码",
            }
        ),
        error_messages={"required": "请输入登录密码。"},
    )
    password_confirm = forms.CharField(
        label="确认密码",
        required=True,
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "new-password",
                "placeholder": "请再次输入登录密码",
            }
        ),
        error_messages={"required": "请再次输入密码以确认。"},
    )
    nickname = forms.CharField(
        label="展示昵称",
        max_length=50,
        required=False,
        help_text="选填，留空则默认使用用户名作为展示名称。",
        widget=forms.TextInput(
            attrs={
                "placeholder": "选填，最多 50 个字符",
            }
        ),
    )
    recovery_hint = forms.CharField(
        label="人工核密线索",
        max_length=255,
        required=False,
        help_text="人工核密线索，仅忘记密码联系管理员核实使用，绝不公开",
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "选填，例如只有自己与管理员知道的核对信息",
            }
        ),
    )
    captcha_id = forms.CharField(
        label="安全验证标识",
        max_length=128,
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "请填写安全验证标识（Captcha ID）",
            }
        ),
    )
    captcha_code = forms.CharField(
        label="安全验证码",
        max_length=64,
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "请输入安全验证码",
            }
        ),
    )
    website = forms.CharField(
        label="Website",
        required=False,
        widget=forms.TextInput(
            attrs={
                "tabindex": "-1",
                "autocomplete": "off",
            }
        ),
    )

    def clean_website(self) -> str:
        val = (self.cleaned_data.get("website") or "").strip()
        if val:
            raise forms.ValidationError("提交校验未通过。")
        return ""

    def clean_username(self) -> str:
        raw_username = self.cleaned_data.get("username", "")
        clean_name = validate_safe_username(raw_username)
        if User.objects.filter(username__iexact=clean_name).exists():
            raise forms.ValidationError("该用户名已被占用，请更换后重试。")
        return clean_name

    def clean_nickname(self) -> str:
        return (self.cleaned_data.get("nickname") or "").strip()

    def clean_recovery_hint(self) -> str:
        return (self.cleaned_data.get("recovery_hint") or "").strip()

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        password_confirm = cleaned_data.get("password_confirm")

        if password and password_confirm and password != password_confirm:
            self.add_error("password_confirm", "两次输入的密码不一致，请核对后重试。")
            return cleaned_data

        if password and password_confirm:
            candidate_username = cleaned_data.get("username") or ""
            try:
                validate_password(password, user=User(username=candidate_username))
            except ValidationError as exc:
                self.add_error("password", exc)

        return cleaned_data


class LoginForm(forms.Form):
    """前台会员登录表单。"""

    username = forms.CharField(
        label="用户名",
        max_length=150,
        required=True,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "username",
                "placeholder": "请输入用户名",
            }
        ),
        error_messages={"required": "请输入用户名。"},
    )
    password = forms.CharField(
        label="登录密码",
        required=True,
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "current-password",
                "placeholder": "请输入密码",
            }
        ),
        error_messages={"required": "请输入密码。"},
    )
    captcha_id = forms.CharField(
        label="安全验证标识",
        max_length=128,
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "请输入安全验证标识",
            }
        ),
    )
    captcha_code = forms.CharField(
        label="安全验证码",
        max_length=64,
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "请输入安全验证码",
            }
        ),
    )


class ProfileUpdateForm(forms.Form):
    """个人中心基础资料修改表单（仅白名单字段 nickname 与 bio）。"""

    nickname = forms.CharField(
        label="展示昵称",
        max_length=50,
        required=False,
        help_text="最多 50 个字符，留空时将展示用户名。",
        widget=forms.TextInput(
            attrs={
                "placeholder": "请输入展示昵称（不超过 50 字）",
            }
        ),
    )
    bio = forms.CharField(
        label="个人简介",
        max_length=300,
        required=False,
        help_text="最多 300 个字符。",
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "简单介绍一下自己（不超过 300 字）",
            }
        ),
    )

    def clean_nickname(self) -> str:
        return (self.cleaned_data.get("nickname") or "").strip()

    def clean_bio(self) -> str:
        return (self.cleaned_data.get("bio") or "").strip()


class AvatarUploadForm(forms.Form):
    """个人中心独立头像上传表单。"""

    avatar = forms.FileField(
        label="选择头像图片",
        required=True,
        help_text="支持静态 JPG、PNG 或 WebP 图片，单文件不超过 2 MB。",
        widget=forms.ClearableFileInput(
            attrs={
                "accept": "image/jpeg,image/png,image/webp",
            }
        ),
        error_messages={"required": "请先选择要上传的头像文件。"},
    )


class SelfPasswordChangeForm(forms.Form):
    """用户自助修改密码与强制改密共用表单。"""

    old_password = forms.CharField(
        label="当前密码",
        required=True,
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "current-password",
                "placeholder": "请输入当前密码",
            }
        ),
        error_messages={"required": "请输入当前密码。"},
    )
    new_password = forms.CharField(
        label="新密码",
        required=True,
        strip=False,
        help_text="至少 8 位字符，且不能过于简单或与用户名过于相似。",
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "new-password",
                "placeholder": "请输入新密码",
            }
        ),
        error_messages={"required": "请输入新密码。"},
    )
    new_password_confirm = forms.CharField(
        label="确认新密码",
        required=True,
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "new-password",
                "placeholder": "请再次输入新密码",
            }
        ),
        error_messages={"required": "请再次输入新密码。"},
    )

    def clean(self):
        cleaned_data = super().clean()
        new_password = cleaned_data.get("new_password")
        new_password_confirm = cleaned_data.get("new_password_confirm")
        if new_password and new_password_confirm and new_password != new_password_confirm:
            self.add_error("new_password_confirm", "两次输入的新密码不一致，请核对后重试。")
        return cleaned_data
