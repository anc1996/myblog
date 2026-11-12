"""
前台会员中心与账号凭证视图 (register / login / logout / profile / force_password_change)。

安全边界：
1. 前台注册强制写入 is_staff=False, is_superuser=False, is_active=True，绝不接收提权或 Person 绑定参数；
2. 注册严格按“蜜罐 -> 表单基础校验 -> IP 配额限流 -> 人机挑战校验”顺序执行；
3. 登录先核查防暴力锁定 (429)，达阈值强制人机挑战 (503/400)，成功清理计数并在强制改密时重定向；
4. 个人中心资料修改仅接收 nickname/bio 白名单字段，绝不公开输出或允许前台修改 recovery_hint；
5. 退出登录严格限定 POST，所有 next 跳转均经 get_safe_next_url 过滤开放重定向。
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST, require_http_methods

from comments.models import Comment
from identity.account_lifecycle import AccountLifecycleService
from identity.avatars import upload_member_avatar
from identity.challenges import (
    ChallengeServiceUnavailable,
    ChallengeVerificationError,
    get_challenge_verifier,
)
from identity.credentials import (
    RateLimitExceeded,
    check_and_consume_register_quota,
    clear_login_failures,
    get_client_ip,
    get_safe_next_url,
    is_login_captcha_required,
    is_login_locked,
    record_login_failure,
    reserve_login_attempt,
)
from identity.forms import (
    AvatarUploadForm,
    LoginForm,
    ProfileUpdateForm,
    RegisterForm,
    SelfPasswordChangeForm,
)
from identity.models import MemberProfile, User

logger = logging.getLogger(__name__)

_CHALLENGE_UNAVAILABLE_MSG = "安全校验服务暂未就绪，请稍后再试"


def _issue_widget_challenge(request, scope: str):
    """
    若当前配置了支持图片签发的 CaptchaProvider，则为服务端渲染表单预签发挑战并准备绑定 Cookie。
    HEAD 请求不触发渲染与配额扣减；未配置真实 Provider 或测试使用简单桩时返回 (None, None)。
    """
    if request.method == "HEAD":
        return None, None
    verifier = get_challenge_verifier()
    if verifier is None or not hasattr(verifier, "issue"):
        return None, None
    prev_id = (request.POST.get("captcha_id") or "").strip() if request.method == "POST" else ""
    try:
        return verifier.issue(request=request, scope=scope, previous_captcha_id=prev_id)
    except (ChallengeServiceUnavailable, RateLimitExceeded):
        return None, None


def _uses_visual_captcha() -> bool:
    verifier = get_challenge_verifier()
    return verifier is not None and hasattr(verifier, "issue")


def _render_with_captcha_cookie(request, template_name: str, context: dict, *, status: int = 200, signed_cookie: str | None = None):
    context.setdefault("use_visual_captcha", _uses_visual_captcha())
    response = render(request, template_name, context, status=status)
    if signed_cookie:
        from captcha.service import apply_captcha_binding_cookie
        apply_captcha_binding_cookie(response, request, signed_cookie)
    return response


def _login_with_session_epoch(request, user, profile: MemberProfile) -> None:
    """执行 Django 会话登录并兜底写入当前账号的 session_epoch。"""
    if not hasattr(user, "backend"):
        backends = getattr(
            settings,
            "AUTHENTICATION_BACKENDS",
            ["django.contrib.auth.backends.ModelBackend"],
        )
        user.backend = backends[0]
    login(request, user)
    if hasattr(request, "session") and request.session is not None:
        request.session["session_epoch"] = profile.session_epoch


def _build_my_comments(user) -> list[dict]:
    """查询并整理当前会员最近 50 条评论的展示状态与所属页面。"""
    comments = (
        Comment.objects.filter(author=user)
        .select_related("page", "latest_version", "approved_version", "pending_version")
        .order_by("-created_at")[:50]
    )
    items: list[dict] = []
    for comment in comments:
        if comment.status == "deleted":
            status_label = "已删除"
            status_badge = "deleted"
            sub_status_note = ""
        elif comment.status == "rejected":
            status_label = "已驳回"
            status_badge = "rejected"
            sub_status_note = ""
        elif comment.status == "pending":
            status_label = "待审核"
            status_badge = "pending"
            sub_status_note = ""
        elif comment.status == "active" and comment.is_publicly_visible:
            status_label = "已公开"
            status_badge = "published"
            sub_status_note = "（新编辑待审核）" if comment.pending_version is not None else ""
        else:
            status_label = "待审核"
            status_badge = "pending"
            sub_status_note = ""

        page_obj = comment.page
        if page_obj is not None:
            spec_page = page_obj.specific_deferred
            is_recycled = bool(getattr(spec_page, "is_recycled", False))
            is_restricted = page_obj.get_view_restrictions().exists()
            is_public_ok = bool(page_obj.live and not is_recycled and not is_restricted)
            can_edit_page = False
            if not is_public_ok:
                try:
                    can_edit_page = bool(spec_page.permissions_for_user(user).can_edit())
                except Exception:
                    can_edit_page = False
            if is_public_ok or can_edit_page:
                page_title = page_obj.title
                page_url = (page_obj.url or "") if (page_obj.live and not is_recycled) else ""
                page_missing = False
            else:
                page_title = comment.page_title_snapshot or "（页面暂未公开）"
                page_url = ""
                page_missing = True
        else:
            page_title = comment.page_title_snapshot or "未知页面"
            page_url = ""
            page_missing = True

        version = comment.pending_version or comment.approved_version or comment.latest_version
        snippet = (version.raw_content[:120] if version and version.raw_content else "（无内容）")

        items.append(
            {
                "id": comment.id,
                "status_label": status_label,
                "status_badge": status_badge,
                "sub_status_note": sub_status_note,
                "page_title": page_title,
                "page_url": page_url,
                "page_missing": page_missing,
                "snippet": snippet,
                "created_at": comment.created_at,
            }
        )
    return items


@never_cache
@sensitive_post_parameters("password", "password_confirm")
@require_http_methods(["GET", "HEAD", "POST"])
def register_view(request):
    """前台会员注册视图。"""
    next_url = get_safe_next_url(request, default="")

    if request.method != "POST":
        form = RegisterForm()
        captcha_payload, signed_cookie = _issue_widget_challenge(request, "register")
        return _render_with_captcha_cookie(
            request,
            "identity/register.html",
            {
                "form": form,
                "next_url": next_url,
                "page_error": "",
                "captcha_payload": captcha_payload,
            },
            signed_cookie=signed_cookie,
        )

    # 1. 先检查蜜罐字段：填写则直接拒绝，绝不建账号或继续消耗配额
    if (request.POST.get("website") or "").strip():
        logger.warning("Register honeypot triggered from ip=%s", get_client_ip(request))
        captcha_payload, signed_cookie = _issue_widget_challenge(request, "register")
        return _render_with_captcha_cookie(
            request,
            "identity/register.html",
            {
                "form": RegisterForm(),
                "next_url": next_url,
                "page_error": "提交校验未通过，请刷新页面后重试。",
                "captcha_payload": captcha_payload,
            },
            status=400,
            signed_cookie=signed_cookie,
        )

    # 2. 表单基础字段与密码强度校验
    form = RegisterForm(request.POST)
    if not form.is_valid():
        captcha_payload, signed_cookie = _issue_widget_challenge(request, "register")
        return _render_with_captcha_cookie(
            request,
            "identity/register.html",
            {
                "form": form,
                "next_url": next_url,
                "page_error": "",
                "captcha_payload": captcha_payload,
            },
            status=400,
            signed_cookie=signed_cookie,
        )

    ip_address = get_client_ip(request)
    captcha_id = (form.cleaned_data.get("captcha_id") or "").strip()
    captcha_code = (form.cleaned_data.get("captcha_code") or "").strip()

    # 3. 检查并扣减同 IP 注册配额（Fail-Closed，Redis 不可用时同样返回 503）
    try:
        check_and_consume_register_quota(ip_address)
    except RateLimitExceeded as exc:
        response = render(
            request,
            "identity/register.html",
            {
                "form": form,
                "next_url": next_url,
                "page_error": str(exc) or "注册请求过于频繁，请稍后再试。",
            },
            status=429,
        )
        retry_after = int(getattr(exc, "retry_after", 0) or 0)
        if retry_after > 0:
            response["Retry-After"] = str(retry_after)
        return response
    except ChallengeServiceUnavailable:
        return render(
            request,
            "identity/register.html",
            {
                "form": form,
                "next_url": next_url,
                "page_error": _CHALLENGE_UNAVAILABLE_MSG,
            },
            status=503,
        )

    # 4. 校验并核销人机验证码
    try:
        verifier = get_challenge_verifier()
        if verifier is None:
            raise ChallengeServiceUnavailable(_CHALLENGE_UNAVAILABLE_MSG)
        verifier.verify_and_consume(
            request=request,
            expected_scope="register",
            captcha_id=captcha_id,
            code=captcha_code,
        )
    except ChallengeServiceUnavailable:
        return render(
            request,
            "identity/register.html",
            {
                "form": form,
                "next_url": next_url,
                "page_error": _CHALLENGE_UNAVAILABLE_MSG,
            },
            status=503,
        )
    except ChallengeVerificationError as exc:
        captcha_payload, signed_cookie = _issue_widget_challenge(request, "register")
        return _render_with_captcha_cookie(
            request,
            "identity/register.html",
            {
                "form": form,
                "next_url": next_url,
                "page_error": str(exc) or "安全验证码校验失败，请核对后重试。",
                "captcha_payload": captcha_payload,
            },
            status=400,
            signed_cookie=signed_cookie,
        )

    # 5. 在单一事务内创建普通会员账号与 MemberProfile（严禁任何提权参数）
    username = form.cleaned_data["username"]
    password = form.cleaned_data["password"]
    nickname = form.cleaned_data.get("nickname") or username[:50]
    recovery_hint = form.cleaned_data.get("recovery_hint") or ""

    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=username,
                password=password,
                is_staff=False,
                is_superuser=False,
                is_active=True,
            )
            profile, created = MemberProfile.objects.get_or_create(
                user=user,
                defaults={
                    "nickname": nickname,
                    "recovery_hint": recovery_hint,
                },
            )
            if not created:
                profile.nickname = nickname
                profile.recovery_hint = recovery_hint
                profile.save(update_fields=["nickname", "recovery_hint"])
    except IntegrityError:
        form.add_error("username", "该用户名已被占用，请更换后重试。")
        captcha_payload, signed_cookie = _issue_widget_challenge(request, "register")
        return _render_with_captcha_cookie(
            request,
            "identity/register.html",
            {
                "form": form,
                "next_url": next_url,
                "page_error": "",
                "captcha_payload": captcha_payload,
            },
            status=400,
            signed_cookie=signed_cookie,
        )

    _login_with_session_epoch(request, user, profile)
    target_url = get_safe_next_url(request, default=reverse("identity:profile"))
    return redirect(target_url)


@never_cache
@sensitive_post_parameters("password")
@require_http_methods(["GET", "HEAD", "POST"])
def login_view(request):
    """前台会员登录视图。"""
    next_url = get_safe_next_url(request, default="")
    ip_address = get_client_ip(request)

    if request.method != "POST":
        username_hint = (request.GET.get("username") or "").strip()
        try:
            captcha_required = is_login_captcha_required(username_hint, ip_address)
        except ChallengeServiceUnavailable:
            captcha_required = True
        captcha_payload, signed_cookie = _issue_widget_challenge(request, "login") if captcha_required else (None, None)
        form = LoginForm(initial={"username": username_hint} if username_hint else None)
        return _render_with_captcha_cookie(
            request,
            "identity/login.html",
            {
                "form": form,
                "next_url": next_url,
                "captcha_required": captcha_required,
                "captcha_payload": captcha_payload,
                "page_error": "",
            },
            signed_cookie=signed_cookie,
        )

    form = LoginForm(request.POST)
    raw_username = (request.POST.get("username") or "").strip()
    if not form.is_valid():
        try:
            captcha_required = is_login_captcha_required(raw_username, ip_address)
        except ChallengeServiceUnavailable:
            captcha_required = True
        captcha_payload, signed_cookie = _issue_widget_challenge(request, "login") if captcha_required else (None, None)
        return _render_with_captcha_cookie(
            request,
            "identity/login.html",
            {
                "form": form,
                "next_url": next_url,
                "captcha_required": captcha_required,
                "captcha_payload": captcha_payload,
                "page_error": "",
            },
            status=400,
            signed_cookie=signed_cookie,
        )

    username = form.cleaned_data["username"].strip()
    password = form.cleaned_data["password"]

    # 1. 先检查防暴力锁定状态（连续失败达阈值直接返回 429）
    try:
        locked, retry_after = is_login_locked(username, ip_address)
        captcha_required = is_login_captcha_required(username, ip_address)
        if not locked and not captcha_required:
            slot_status, slot_retry = reserve_login_attempt(username, ip_address, has_captcha=False)
            if slot_status == "locked":
                locked, retry_after = True, slot_retry
            elif slot_status == "captcha_required":
                captcha_required = True
    except ChallengeServiceUnavailable:
        return _render_with_captcha_cookie(
            request,
            "identity/login.html",
            {
                "form": form,
                "next_url": next_url,
                "captcha_required": True,
                "page_error": _CHALLENGE_UNAVAILABLE_MSG,
            },
            status=503,
        )
    if locked:
        wait_seconds = max(1, int(retry_after or 60))
        response = _render_with_captcha_cookie(
            request,
            "identity/login.html",
            {
                "form": form,
                "next_url": next_url,
                "captcha_required": captcha_required,
                "page_error": f"登录失败次数过多，请等待 {wait_seconds} 秒后再试。",
            },
            status=429,
        )
        response["Retry-After"] = str(wait_seconds)
        return response

    # 2. 若已触发人机验证码要求，先强制核销登录验证码
    if captcha_required:
        captcha_id = (form.cleaned_data.get("captcha_id") or "").strip()
        captcha_code = (form.cleaned_data.get("captcha_code") or "").strip()
        try:
            verifier = get_challenge_verifier()
            if verifier is None:
                raise ChallengeServiceUnavailable(_CHALLENGE_UNAVAILABLE_MSG)
            verifier.verify_and_consume(
                request=request,
                expected_scope="login",
                captcha_id=captcha_id,
                code=captcha_code,
            )
            slot_status, slot_retry = reserve_login_attempt(username, ip_address, has_captcha=True)
            if slot_status == "locked":
                wait_seconds = max(1, int(slot_retry or 60))
                response = _render_with_captcha_cookie(
                    request,
                    "identity/login.html",
                    {
                        "form": form,
                        "next_url": next_url,
                        "captcha_required": True,
                        "page_error": f"登录失败次数过多，请等待 {wait_seconds} 秒后再试。",
                    },
                    status=429,
                )
                response["Retry-After"] = str(wait_seconds)
                return response
        except ChallengeServiceUnavailable:
            return render(
                request,
                "identity/login.html",
                {
                    "form": form,
                    "next_url": next_url,
                    "captcha_required": True,
                    "page_error": _CHALLENGE_UNAVAILABLE_MSG,
                },
                status=503,
            )
        except ChallengeVerificationError as exc:
            captcha_payload, signed_cookie = _issue_widget_challenge(request, "login")
            return _render_with_captcha_cookie(
                request,
                "identity/login.html",
                {
                    "form": form,
                    "next_url": next_url,
                    "captcha_required": True,
                    "captcha_payload": captcha_payload,
                    "page_error": str(exc) or "安全验证码校验失败，请核对后重试。",
                },
                status=400,
                signed_cookie=signed_cookie,
            )

    # 3. 核验账号密码并检查未停用状态
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        try:
            record_login_failure(username, ip_address)
            locked_now, retry_after_now = is_login_locked(username, ip_address)
            captcha_required_now = is_login_captcha_required(username, ip_address)
        except ChallengeServiceUnavailable:
            return render(
                request,
                "identity/login.html",
                {
                    "form": form,
                    "next_url": next_url,
                    "captcha_required": True,
                    "page_error": _CHALLENGE_UNAVAILABLE_MSG,
                },
                status=503,
            )
        if locked_now:
            wait_seconds = max(1, int(retry_after_now or 60))
            err_msg = f"用户名或密码错误，失败次数过多已触发临时锁定，请 {wait_seconds} 秒后再试。"
        else:
            err_msg = "用户名或密码错误，或账号已被停用。"
        captcha_payload, signed_cookie = _issue_widget_challenge(request, "login") if captcha_required_now else (None, None)
        return _render_with_captcha_cookie(
            request,
            "identity/login.html",
            {
                "form": form,
                "next_url": next_url,
                "captcha_required": captcha_required_now,
                "captcha_payload": captcha_payload,
                "page_error": err_msg,
            },
            status=400,
            signed_cookie=signed_cookie,
        )

    # 4. 登录成功：清除失败计数、建会话、并在强制改密时重定向到改密页
    clear_login_failures(username, ip_address)
    profile = MemberProfile.get_or_create_for_user(user)
    _login_with_session_epoch(request, user, profile)

    if profile.must_change_password:
        return redirect("identity:force_password_change")

    target_url = get_safe_next_url(request, default=reverse("identity:profile"))
    return redirect(target_url)


@never_cache
@require_POST
def logout_view(request):
    """退出登录视图：仅允许 POST，防止 CSRF 强制退出或预取误退出。"""
    target_url = get_safe_next_url(request, default="/")
    logout(request)
    return redirect(target_url)


@never_cache
@sensitive_post_parameters("old_password", "new_password", "new_password_confirm")
@login_required(login_url="identity:login")
@require_http_methods(["GET", "HEAD", "POST"])
def profile_view(request):
    """个人中心视图：资料更新、独立头像上传、自助改密与我的评论列表。"""
    if not request.user.is_active:
        logout(request)
        return redirect("identity:login")

    profile = MemberProfile.get_or_create_for_user(request.user)
    if profile.must_change_password:
        return redirect("identity:force_password_change")

    profile_form = ProfileUpdateForm(
        initial={"nickname": profile.nickname, "bio": profile.bio}
    )
    avatar_form = AvatarUploadForm()
    password_form = SelfPasswordChangeForm()
    page_error = ""
    response_status = 200

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "update_profile":
            profile_form = ProfileUpdateForm(request.POST)
            if profile_form.is_valid():
                # 严格只写 nickname 与 bio 白名单字段，忽略任何越权参数
                profile.nickname = profile_form.cleaned_data["nickname"]
                profile.bio = profile_form.cleaned_data["bio"]
                profile.save(update_fields=["nickname", "bio", "updated_at"])
                messages.success(request, "个人资料已更新。")
                return redirect("identity:profile")
            response_status = 400

        elif action == "upload_avatar":
            avatar_form = AvatarUploadForm(request.POST, request.FILES)
            if avatar_form.is_valid():
                try:
                    upload_member_avatar(
                        user=request.user,
                        uploaded_file=avatar_form.cleaned_data["avatar"],
                    )
                except ValidationError as exc:
                    avatar_form.add_error("avatar", exc)
                    response_status = 400
                else:
                    messages.success(request, "头像已更新。")
                    return redirect("identity:profile")
            else:
                response_status = 400

        elif action == "change_password":
            password_form = SelfPasswordChangeForm(request.POST)
            if password_form.is_valid():
                try:
                    AccountLifecycleService.self_change_password(
                        user=request.user,
                        old_password=password_form.cleaned_data["old_password"],
                        new_password=password_form.cleaned_data["new_password"],
                        request=request,
                    )
                except ValidationError as exc:
                    password_form.add_error(None, exc)
                    response_status = 400
                except PermissionDenied as exc:
                    password_form.add_error(None, str(exc))
                    response_status = 403
                else:
                    messages.success(
                        request,
                        "密码已修改成功，当前会话已保持有效，其他设备上的旧会话已失效。",
                    )
                    return redirect("identity:profile")
            else:
                response_status = 400

        else:
            page_error = "不支持的操作类型，请通过页面表单提交。"
            response_status = 400

    my_comments = _build_my_comments(request.user)
    return render(
        request,
        "identity/profile.html",
        {
            "profile": profile,
            "profile_form": profile_form,
            "avatar_form": avatar_form,
            "password_form": password_form,
            "my_comments": my_comments,
            "page_error": page_error,
        },
        status=response_status,
    )


@never_cache
@sensitive_post_parameters("old_password", "new_password", "new_password_confirm")
@login_required(login_url="identity:login")
@require_http_methods(["GET", "HEAD", "POST"])
def force_password_change_view(request):
    """管理员重置密码后的强制改密页面。"""
    if not request.user.is_active:
        logout(request)
        return redirect("identity:login")

    profile = MemberProfile.get_or_create_for_user(request.user)
    page_error = ""
    response_status = 200

    if request.method == "POST":
        form = SelfPasswordChangeForm(request.POST)
        if form.is_valid():
            try:
                AccountLifecycleService.self_change_password(
                    user=request.user,
                    old_password=form.cleaned_data["old_password"],
                    new_password=form.cleaned_data["new_password"],
                    request=request,
                )
            except ValidationError as exc:
                form.add_error(None, exc)
                response_status = 400
            except PermissionDenied as exc:
                form.add_error(None, str(exc))
                response_status = 403
            else:
                messages.success(request, "密码修改成功，强制改密要求已解除。")
                return redirect("identity:profile")
        else:
            response_status = 400
    else:
        form = SelfPasswordChangeForm()

    return render(
        request,
        "identity/force_password_change.html",
        {
            "profile": profile,
            "form": form,
            "page_error": page_error,
        },
        status=response_status,
    )
