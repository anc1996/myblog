"""
Markdown 导入专用身份认证、配对码兑换与签名 plan_token 工具。

安全契约：
1. 仅支持两种合法入口：
   - Wagtail 后台已登录 Session（需要 CSRF 保护）；
   - `Authorization: Bearer <token>` 导入专用短期令牌（仅存 SHA-256 摘要，校验撤销、过期、账号与会话代次）；
2. plan_token 采用服务端 HMAC-SHA256 签名，绑定 actor_id、site_id、parent_id、type_key、
   schema_version、input_digest 与有效期，防止客户端绕过预检直接窜改提交参数。
"""

from datetime import timedelta
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from wagtail.models import Site

from .models import MarkdownImportPairCode, MarkdownImportToken

TOKEN_PREFIX = "mdimp_"
PLAN_TOKEN_TTL_SECONDS = 3600
DEFAULT_TOKEN_TTL_DAYS = 14
PAIR_CODE_TTL_MINUTES = 10


def hash_secret(raw_value: str) -> str:
    return hashlib.sha256((raw_value or "").encode("utf-8")).hexdigest()


def resolve_request_site(request) -> Site:
    site = Site.find_for_request(request)
    if site:
        return site
    site = Site.objects.filter(is_default_site=True).first() or Site.objects.first()
    if not site:
        raise PermissionDenied("未配置可用的 Wagtail 站点。")
    return site


def _get_current_session_epoch(user) -> int:
    """直接从 PostgreSQL 读取账号最新 session_epoch（防内存缓存过期）。"""
    if not user or not getattr(user, "pk", None):
        return 0
    from identity.models import MemberProfile
    epoch = MemberProfile.objects.filter(user_id=user.pk).values_list("session_epoch", flat=True).first()
    return int(epoch) if epoch is not None else 1


def _assert_actor_eligible(user, *, require_session_guard: bool = True) -> None:
    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        raise PermissionDenied("未登录或账号已停用。")
    if not user.has_perm("wagtailadmin.access_admin"):
        raise PermissionDenied("当前账号无权访问创作后台。")
    if require_session_guard:
        from identity.models import MemberProfile
        profile = MemberProfile.objects.filter(user_id=user.pk).first()
        if profile is not None:
            if getattr(profile, "must_change_password", False):
                raise PermissionDenied("账号需要先修改临时密码后才能执行导入。")
            if getattr(profile, "is_suspended", False):
                raise PermissionDenied("账号当前处于限制状态，禁止执行导入。")


def issue_import_token(
    *,
    user,
    site: Site,
    name: str = "本地导入客户端",
    ttl_days: int = DEFAULT_TOKEN_TTL_DAYS,
) -> tuple[MarkdownImportToken, str]:
    _assert_actor_eligible(user)
    plaintext = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    token_obj = MarkdownImportToken.objects.create(
        user=user,
        site=site,
        name=(name or "本地导入客户端")[:100],
        prefix=plaintext[:12],
        token_hash=hash_secret(plaintext),
        is_active=True,
        session_epoch=_get_current_session_epoch(user),
        expires_at=timezone.now() + timedelta(days=max(1, int(ttl_days))),
    )
    return token_obj, plaintext


def issue_pair_code(*, user, site: Site) -> tuple[MarkdownImportPairCode, str]:
    _assert_actor_eligible(user)
    raw_code = f"PAIR-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
    code_obj = MarkdownImportPairCode.objects.create(
        user=user,
        site=site,
        code_hash=hash_secret(raw_code),
        session_epoch=_get_current_session_epoch(user),
        expires_at=timezone.now() + timedelta(minutes=PAIR_CODE_TTL_MINUTES),
    )
    return code_obj, raw_code


def exchange_pair_code(*, raw_code: str, site: Site, client_name: str = "桌面导入向导") -> tuple[MarkdownImportToken, str]:
    normalized = (raw_code or "").strip().upper()
    if not normalized:
        raise PermissionDenied("配对码不能为空。")
    now = timezone.now()
    with transaction.atomic():
        code_obj = (
            MarkdownImportPairCode.objects.select_for_update()
            .select_related("user", "site")
            .filter(code_hash=hash_secret(normalized), site=site)
            .first()
        )
        if not code_obj or code_obj.used_at is not None or code_obj.expires_at <= now:
            raise PermissionDenied("配对码无效、已过期或已被使用。")
        _assert_actor_eligible(code_obj.user)
        if int(code_obj.session_epoch) != _get_current_session_epoch(code_obj.user):
            raise PermissionDenied("账号密码或权限已变更，原配对码已失效，请重新生成。")
        code_obj.used_at = now
        code_obj.save(update_fields=["used_at"])
        return issue_import_token(user=code_obj.user, site=site, name=client_name)


def authenticate_import_request(request) -> tuple[Any, Site, str]:
    """
    鉴权当前请求并返回 (actor, site, auth_mode)：
    auth_mode 为 "bearer"（免 CSRF）或 "session"（要求 CSRF 保护）。
    """
    site = resolve_request_site(request)
    auth_header = (request.META.get("HTTP_AUTHORIZATION") or "").strip()
    if auth_header:
        parts = auth_header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise PermissionDenied("Authorization 请求头格式必须为 Bearer <token>。")
        raw_token = parts[1].strip()
        if not raw_token.startswith(TOKEN_PREFIX):
            raise PermissionDenied("仅接受专用 Markdown 导入令牌。")
        now = timezone.now()
        token_obj = (
            MarkdownImportToken.objects.select_related("user", "site")
            .filter(
                token_hash=hash_secret(raw_token),
                site=site,
                is_active=True,
                revoked_at__isnull=True,
                expires_at__gt=now,
            )
            .first()
        )
        if not token_obj:
            raise PermissionDenied("导入令牌无效、已吊销或已过期。")
        _assert_actor_eligible(token_obj.user)
        if int(token_obj.session_epoch) != _get_current_session_epoch(token_obj.user):
            raise PermissionDenied("账号密码、权限或会话代次已变更，原导入令牌已失效。")
        MarkdownImportToken.objects.filter(pk=token_obj.pk).update(last_used_at=now)
        return token_obj.user, site, "bearer"

    user = getattr(request, "user", None)
    _assert_actor_eligible(user)
    return user, site, "session"


def sign_plan_token(payload: dict[str, Any]) -> str:
    """生成带过期时间的 HMAC-SHA256 预检计划签名令牌。"""
    data = dict(payload)
    data["exp"] = int(time.time()) + PLAN_TOKEN_TTL_SECONDS
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    sig = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        encoded.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    import base64
    b64 = base64.urlsafe_b64encode(encoded.encode("utf-8")).decode("ascii")
    return f"{b64}.{sig}"


def verify_plan_token(token: str) -> dict[str, Any]:
    """校验签名与有效期，还原预检计划载荷。"""
    import base64
    try:
        b64, sig = (token or "").split(".", 1)
        raw_json = base64.urlsafe_b64decode(b64.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise ValueError("plan_token 格式无效。") from exc

    expected_sig = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        raw_json.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("plan_token 签名校验失败，请重新预检。")

    data = json.loads(raw_json)
    if int(data.get("exp", 0)) < int(time.time()):
        raise ValueError("plan_token 已过期，请重新预检。")
    return data
