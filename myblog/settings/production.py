from pathlib import Path
import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import _database_env


def _prod_env(name: str, default: str = "") -> str:
    """优先读取实时进程环境变量，再回退到 base.py 已加载的 MYBLOG_ENV_FILE 映射。"""
    if name in os.environ:
        return str(os.environ[name]).strip()
    val = _database_env.get(name, default)
    return str(val).strip() if val is not None else default


def _parse_bool(raw: str, default: bool = False) -> bool:
    if not raw:
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"Invalid boolean setting value: {raw!r}")


def _parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


DEBUG = False

SECRET_KEY = _prod_env("DJANGO_SECRET_KEY") or _prod_env("SECRET_KEY")
ALLOWED_HOSTS = _parse_csv(_prod_env("ALLOWED_HOSTS"))
CSRF_TRUSTED_ORIGINS = _parse_csv(_prod_env("CSRF_TRUSTED_ORIGINS"))

_wagtail_admin_base_url = _prod_env("WAGTAILADMIN_BASE_URL").rstrip("/")
if _wagtail_admin_base_url:
    WAGTAILADMIN_BASE_URL = _wagtail_admin_base_url

# 生产静态目录独立落入 gitignored 的 staticfiles/，避免污染源码 static/ 目录
STATIC_ROOT = Path(_prod_env("DJANGO_STATIC_ROOT") or str(BASE_DIR / "staticfiles"))
MEDIA_ROOT = Path(_prod_env("DJANGO_MEDIA_ROOT") or str(BASE_DIR / "media"))

# ManifestStaticFilesStorage is recommended in production, to prevent
# outdated JavaScript / CSS assets being served from cache
# (e.g. after a Wagtail upgrade).
# See https://docs.djangoproject.com/en/6.1/ref/contrib/staticfiles/#manifeststaticfilesstorage
STORAGES = {
    **STORAGES,
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
    },
}

# HTTPS / 反向代理 / Cookie / 响应头安全加固（覆盖 manage.py check --deploy 全部安全项）
SECURE_SSL_REDIRECT = _parse_bool(_prod_env("DJANGO_SECURE_SSL_REDIRECT"), True)
if _parse_bool(_prod_env("DJANGO_BEHIND_TLS_PROXY"), True):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = _parse_bool(_prod_env("DJANGO_USE_X_FORWARDED_HOST"), False)

SESSION_COOKIE_SECURE = _parse_bool(_prod_env("DJANGO_SESSION_COOKIE_SECURE"), True)
CSRF_COOKIE_SECURE = _parse_bool(_prod_env("DJANGO_CSRF_COOKIE_SECURE"), True)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

SECURE_HSTS_SECONDS = int(_prod_env("DJANGO_SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = _parse_bool(_prod_env("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS"), True)
SECURE_HSTS_PRELOAD = _parse_bool(_prod_env("DJANGO_SECURE_HSTS_PRELOAD"), True)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
# Wagtail 后台预览面板使用同源 iframe，严禁设为 DENY
X_FRAME_OPTIONS = "SAMEORIGIN"
SILENCED_SYSTEM_CHECKS = [*globals().get("SILENCED_SYSTEM_CHECKS", []), "security.W019"]

# 生产 Nginx X-Accel-Redirect 内部代理开关：
# 默认关闭（由 Django 网关走受控 Range/FileResponse）；仅在完成内部 S3 SigV4 签名代理并实测（第 5 步）后显式设为 1 开启
USE_X_ACCEL_REDIRECT = _parse_bool(_prod_env("USE_X_ACCEL_REDIRECT"), False)

# PostgreSQL 持久连接与健康检查
_conn_max_age = int(_prod_env("POSTGRES_CONN_MAX_AGE", "60"))
_default_db_cfg = {**DATABASES["default"], "CONN_MAX_AGE": _conn_max_age}
if _conn_max_age > 0:
    _default_db_cfg["CONN_HEALTH_CHECKS"] = True
DATABASES = {**DATABASES, "default": _default_db_cfg}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": _prod_env("DJANGO_LOG_LEVEL", "INFO").upper(),
    },
    "loggers": {
        "django.security": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

try:
    from .local import *  # noqa: F401,F403
except ImportError:
    pass

# 最终 fail-closed 生产安全门槛校验（在可选 local.py 之后执行，防止意外降级）
if DEBUG:
    raise ImproperlyConfigured("Production settings must keep DEBUG=False.")
if (
    not SECRET_KEY
    or SECRET_KEY.startswith("django-insecure-")
    or len(SECRET_KEY) < 50
    or len(set(SECRET_KEY)) < 5
):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY (or SECRET_KEY) must be set to a strong >=50-char secret in production."
    )
if not ALLOWED_HOSTS or any(host == "*" for host in ALLOWED_HOSTS):
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS must list explicit hostnames/IPs (wildcard '*' is forbidden in production)."
    )
