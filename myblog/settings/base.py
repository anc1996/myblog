"""
myblog 项目 Django 基础配置 (base settings)。

基于 Django 6.1 与 Wagtail 8.1 架构。
本配置文件为开发环境、测试环境与生产环境的共享基础配置。

配置约定与安全准则：
1. 本地 .env 文件为明确输入，绝对不覆盖已存在的进程环境变量；
2. 导入配置模块仅读取静态声明，绝对不隐式连接数据库或执行迁移；
3. 遵循严格分层架构：PostgreSQL（主关系库与版本元数据）、MongoDB（外置正文脱水快照）、
   RustFS/S3（私有对象存储）与 Redis（流控与缓存）。
"""

# 项目目录路径计算
# PROJECT_DIR 指向 /myblog (包含 settings, static, templates, apps 的目录)
# BASE_DIR 指向仓库根目录
from pathlib import Path
import os
import sys

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = PROJECT_DIR.parent

# 统一自举导入路径：确保 BASE_DIR 与 APPS_DIR 注入 sys.path (E-01)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from myblog.bootstrap import configure_app_imports
APPS_DIR = configure_app_imports()


# ==============================================================================
# 已安装应用注册列表 (INSTALLED_APPS)
# ==============================================================================

INSTALLED_APPS = [
    # 1. 业务核心子应用 (收敛于 myblog/apps/* 目录下)
    "identity.apps.IdentityConfig",
    "captcha.apps.CaptchaConfig",
    "media_library.apps.MediaLibraryConfig",
    "content_blocks.apps.ContentBlocksConfig",
    "external_content.apps.ExternalContentConfig",
    "content_lifecycle.apps.ContentLifecycleConfig",
    "discovery.apps.DiscoveryConfig",
    "people.apps.PeopleConfig",
    "taxonomy.apps.TaxonomyConfig",
    "blog.apps.BlogConfig",
    "guides.apps.GuidesConfig",
    "comments.apps.CommentsConfig",
    "portfolio.apps.PortfolioConfig",
    "site_settings.apps.SiteSettingsConfig",
    "home",
    "search",

    # 2. Wagtail 8.1 核心与扩展组件
    "wagtail.contrib.forms",
    "wagtail.contrib.redirects",
    "wagtail.contrib.settings",
    "wagtail.embeds",
    "wagtail.sites",
    "wagtail.users",
    "wagtail.snippets",
    "wagtail.documents",
    "wagtail.images",
    "wagtail.search",
    "wagtail.admin",
    "wagtail",

    # 3. 第三方优质工具库
    "modelcluster",
    "taggit",
    "django_filters",

    # 4. Django 原生核心组件
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Wagtail 在 PostgreSQL 下的全文检索与 GIN 倒排索引必须依赖此应用
    "django.contrib.postgres",
    # S3 / RustFS 对象存储驱动
    "storages",
]

# ==============================================================================
# HTTP 中间件管道 (MIDDLEWARE)
# ==============================================================================

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "identity.middleware.IdentitySessionGuardMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "wagtail.contrib.redirects.middleware.RedirectMiddleware",
]

ROOT_URLCONF = "myblog.urls"

# ==============================================================================
# 模板引擎配置 (TEMPLATES)
# ==============================================================================

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            PROJECT_DIR / "templates",
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "myblog.wsgi.application"


# ==============================================================================
# 关系型数据库配置：PostgreSQL 18
# ==============================================================================

# 本地环境变量文件由 MYBLOG_ENV_FILE 环境变量指定，默认为 .env.local。
# 进程外部环境变量拥有最高优先级，避免配置文件覆盖运维注入的凭据。
_env_filename = os.environ.get("MYBLOG_ENV_FILE", ".env.local")
_env_path = Path(_env_filename)
if not _env_path.is_absolute():
    _env_path = BASE_DIR / _env_path
_database_env = {**dotenv_values(_env_path, interpolate=False), **os.environ}
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _database_env.get("POSTGRES_DB", "myblog_dev"),
        "HOST": _database_env.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": _database_env.get("POSTGRES_PORT", "5432"),
        "USER": _database_env.get("POSTGRES_USER", "postgres"),
        "PASSWORD": _database_env.get("POSTGRES_PASSWORD", ""),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"connect_timeout": 10},
        # 自动化单元测试数据库：与手工探针库严格物理隔离，默认名为 test_myblog
        "TEST": {"NAME": _database_env.get("POSTGRES_TEST_DB", "test_myblog")},
    }
}

# ==============================================================================
# MongoDB 配置：用于外置正文字段版本（FieldVersionPayload）脱水存储
# ==============================================================================

MONGO_SETTINGS = {
    "HOST": _database_env.get("MONGO_HOST", "127.0.0.1"),
    "PORT": int(_database_env.get("MONGO_PORT", 27017)),
    "USER": _database_env.get("MONGO_USER", "root"),
    "PASSWORD": _database_env.get("MONGO_PASSWORD", ""),
    "AUTH_SOURCE": _database_env.get("MONGO_AUTH_SOURCE", "admin"),
    "DATABASE": _database_env.get("MONGO_NAME", "myblog_dev"),
}

# ==============================================================================
# Redis 缓存与流控配置
# ==============================================================================

_redis_host = _database_env.get("REDIS_HOST", "127.0.0.1")
_redis_port = _database_env.get("REDIS_PORT", "6379")
_redis_password = _database_env.get("REDIS_PASSWORD", "")
_redis_url = f"redis://:{_redis_password}@{_redis_host}:{_redis_port}/1" if _redis_password else f"redis://{_redis_host}:{_redis_port}/1"

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": _redis_url,
        "KEY_PREFIX": "myblog",
        "TIMEOUT": 300,
    }
}


# ==============================================================================
# 密码安全校验规则 (Password validation)
# ==============================================================================

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# ==============================================================================
# 国际化与本地化设置 (Internationalization & Localization)
# ==============================================================================

# 默认语言：简体中文
LANGUAGE_CODE = "zh-hans"

# 时区：亚洲/上海 (中国标准时间 UTC+8)
TIME_ZONE = "Asia/Shanghai"

# 开启 Django 翻译与国际化支持
USE_I18N = True

# 开启数据库时区感知存储
USE_TZ = True


# ==============================================================================
# 静态资源与媒体文件配置 (Static & Media files)
# ==============================================================================

STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

STATICFILES_DIRS = [
    PROJECT_DIR / "static",
]

STATIC_ROOT = BASE_DIR / "static"
STATIC_URL = "/static/"

MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"

# ==============================================================================
# 对象存储后端配置 (STORAGES)
# S3_BUCKET 存在时启用受控私有存储 ProtectedRustFSStorage，否则降级为本地文件存储
# ==============================================================================

_s3_bucket = _database_env.get("S3_BUCKET")
if _s3_bucket:
    STORAGES = {
        "default": {
            "BACKEND": "media_library.storage.ProtectedRustFSStorage",
            "OPTIONS": {
                "bucket_name": _s3_bucket,
                "endpoint_url": _database_env.get("S3_ENDPOINT_URL", "http://127.0.0.1:9000"),
                "access_key": _database_env.get("S3_ACCESS_KEY_ID"),
                "secret_key": _database_env.get("S3_SECRET_ACCESS_KEY"),
                "region_name": _database_env.get("S3_REGION", "us-east-1"),
                "file_overwrite": False,
                "default_acl": None,
                "querystring_auth": False,
                "signature_version": "s3v4",
                "addressing_style": "path",
            },
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
else:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }

# 提高单表单最大字段数上限：Django 默认限制为 1000，复杂的 Wagtail 积木流页面极易超限，调整为 10000
DATA_UPLOAD_MAX_NUMBER_FIELDS = 10_000


# ==============================================================================
# Wagtail CMS 核心业务配置
# ==============================================================================

# 站点对外名称
WAGTAIL_SITE_NAME = "myblog"

# Wagtail 8.1 工作流终审版本防篡改保护 (H-06)
WAGTAIL_WORKFLOW_REQUIRE_REAPPROVAL_ON_EDIT = True
WAGTAIL_FINISH_WORKFLOW_ACTION = "content_lifecycle.workflows.publish_approved_revision"

# 自定义用户模型
AUTH_USER_MODEL = "identity.User"

# 自定义 Wagtail 媒体模型（带 ACL 访问控制与 RustFS 私有存储）
WAGTAILIMAGES_IMAGE_MODEL = "media_library.CustomImage"
WAGTAILDOCS_DOCUMENT_MODEL = "media_library.CustomDocument"

# 全文搜索后端
WAGTAILSEARCH_BACKENDS = {
    "default": {
        "BACKEND": "wagtail.search.backends.database",
    }
}

# 管理后台通知邮件与绝对路径前缀基准 URL（末尾不加斜杠）
WAGTAILADMIN_BASE_URL = "http://example.com"

# 文档库允许上传的文件扩展名白名单（防范任意文件上传安全风险）
WAGTAILDOCS_EXTENSIONS = ['csv', 'docx', 'key', 'odt', 'pdf', 'pptx', 'rtf', 'txt', 'xlsx', 'zip']

# 单个文档最大上传大小限制（10MB）
WAGTAILDOCS_MAX_UPLOAD_SIZE = 10 * 1024 * 1024

# 前台会员中心登录入口
LOGIN_URL = "/account/login/"

# 公共人机验证码默认实现 (Phase 2: Pillow 13.0.0 + Redis Lua 原子核销)
IDENTITY_CHALLENGE_VERIFIER = "captcha.providers.PillowRedisCaptchaVerifier"
IDENTITY_CAPTCHA_ALLOWLIST_URL_NAMES = (
    "api_v1:captcha_challenge_issue",
    "api_v1:captcha_challenge_image",
)
