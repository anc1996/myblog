"""
全局会话代次与强制改密守卫中间件 (IdentitySessionGuardMiddleware)。

每次请求直接核查 PostgreSQL 中的 user.is_active、MemberProfile.session_epoch 与 must_change_password：
1. 停用、撤权或改密后 session_epoch 不一致，或缺失 session_epoch 的会话立即注销；
2. 处于 must_change_password=True 的账号除准确白名单路由外，页面请求 302 至改密页，API 返回 403 JSON。
"""

from django.conf import settings
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import Resolver404, resolve

from identity.models import MemberProfile, User


class IdentitySessionGuardMiddleware:
    """位于 AuthenticationMiddleware 之后的实时会话有效性守卫。"""

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _is_api_request(request) -> bool:
        return request.path_info.startswith("/api/")

    @staticmethod
    def _is_must_change_allowlisted(request) -> bool:
        static_url = getattr(settings, "STATIC_URL", "/static/") or "/static/"
        if request.method in ("GET", "HEAD") and request.path_info.startswith(static_url):
            return True

        try:
            match = resolve(request.path_info)
        except Resolver404:
            return False

        view_name = match.view_name or ""
        if view_name == "identity:force_password_change" and request.method in ("GET", "HEAD", "POST"):
            return True
        if view_name == "identity:logout" and request.method == "POST":
            return True
        if view_name == "wagtailadmin_logout" and request.method == "POST":
            return True
        if view_name in ("avatar_gateway", "media_library:avatar_gateway", "identity:avatar_gateway") and request.method in ("GET", "HEAD"):
            return True

        extra_allowlist = getattr(settings, "IDENTITY_CAPTCHA_ALLOWLIST_URL_NAMES", ())
        if view_name in extra_allowlist and request.method in ("GET", "HEAD", "POST"):
            return True
        return False

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            user_row = User.objects.filter(pk=user.pk).values("is_active").first()
            profile_row = MemberProfile.objects.filter(user_id=user.pk).values(
                "session_epoch",
                "must_change_password",
            ).first()

            session_epoch = request.session.get("session_epoch")
            session_valid = (
                user_row is not None
                and user_row["is_active"] is True
                and profile_row is not None
                and session_epoch is not None
                and int(session_epoch) == int(profile_row["session_epoch"])
            )

            if not session_valid:
                logout(request)
                if self._is_api_request(request):
                    return JsonResponse(
                        {
                            "status": "error",
                            "code": "session_invalid",
                            "message": "会话已失效或账号已停用，请重新登录。",
                        },
                        status=403,
                    )
                return self.get_response(request)

            if profile_row["must_change_password"] and not self._is_must_change_allowlisted(request):
                if self._is_api_request(request):
                    return JsonResponse(
                        {
                            "status": "error",
                            "code": "must_change_password",
                            "message": "管理员已重置您的密码，请先修改密码后再继续操作。",
                        },
                        status=403,
                    )
                return redirect("identity:force_password_change")

        return self.get_response(request)
