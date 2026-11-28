"""
P0-4 原生生产环境配置与部署安全门禁测试。

验证：
1. myblog.settings.production 严格 fail-closed：缺失/弱/不安全 SECRET_KEY 或空/通配 ALLOWED_HOSTS 必须抛出 ImproperlyConfigured；
2. 合法生产环境变量下 DEBUG=False、ManifestStaticFilesStorage、STATIC_ROOT 指向 gitignored staticfiles/、
   X_FRAME_OPTIONS="SAMEORIGIN"（兼容 Wagtail 预览）、HTTPS/HSTS/Cookie 安全参数与 USE_X_ACCEL_REDIRECT 默认开启；
3. myblog.wsgi 默认指向 myblog.settings.production；
4. manage.py check --deploy --settings=myblog.settings.production --fail-level WARNING 零错误零告警；
5. 隔离临时目录运行 collectstatic --noinput（ManifestStaticFilesStorage）无缺失引用错误。
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BASE_DIR = Path(__file__).resolve().parents[1]
VALID_PROD_SECRET = "prod-test-secret-key-with-more-than-fifty-unique-chars-1234567890"


def _run_py(code: str, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in (
        "DJANGO_SETTINGS_MODULE",
        "DJANGO_SECRET_KEY",
        "SECRET_KEY",
        "ALLOWED_HOSTS",
        "CSRF_TRUSTED_ORIGINS",
        "WAGTAILADMIN_BASE_URL",
        "DJANGO_STATIC_ROOT",
        "USE_X_ACCEL_REDIRECT",
    ):
        env.pop(key, None)
    env.setdefault("MYBLOG_ENV_FILE", ".env.test")
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class ProductionSettingsTests(unittest.TestCase):
    def test_fail_closed_without_secret_key_or_allowed_hosts(self):
        code = (
            "from myblog.bootstrap import configure_app_imports; configure_app_imports(); "
            "import django; from django.conf import settings; _ = settings.SECRET_KEY"
        )
        # 1. 缺失 SECRET_KEY
        with tempfile.NamedTemporaryFile("w", suffix=".env") as empty_env:
            res_no_secret = _run_py(
                code,
                {
                    "MYBLOG_ENV_FILE": empty_env.name,
                    "DJANGO_SETTINGS_MODULE": "myblog.settings.production",
                    "ALLOWED_HOSTS": "blog.example.com",
                },
            )
        self.assertNotEqual(res_no_secret.returncode, 0)
        self.assertIn("ImproperlyConfigured", res_no_secret.stderr)

        # 2. 使用 django-insecure- 前缀
        res_insecure = _run_py(
            code,
            {
                "DJANGO_SETTINGS_MODULE": "myblog.settings.production",
                "DJANGO_SECRET_KEY": "django-insecure-" + ("x" * 50),
                "ALLOWED_HOSTS": "blog.example.com",
            },
        )
        self.assertNotEqual(res_insecure.returncode, 0)
        self.assertIn("ImproperlyConfigured", res_insecure.stderr)

        # 3. 缺失或通配 ALLOWED_HOSTS
        res_wildcard = _run_py(
            code,
            {
                "DJANGO_SETTINGS_MODULE": "myblog.settings.production",
                "DJANGO_SECRET_KEY": VALID_PROD_SECRET,
                "ALLOWED_HOSTS": "*",
            },
        )
        self.assertNotEqual(res_wildcard.returncode, 0)
        self.assertIn("ImproperlyConfigured", res_wildcard.stderr)

    def test_valid_production_settings_and_wsgi_default(self):
        code = (
            "import json, os; "
            "from myblog.bootstrap import configure_app_imports; configure_app_imports(); "
            "import myblog.wsgi; "
            "from django.conf import settings; "
            "print(json.dumps({"
            "'module': os.environ.get('DJANGO_SETTINGS_MODULE'),"
            "'debug': settings.DEBUG,"
            "'allowed_hosts': settings.ALLOWED_HOSTS,"
            "'staticfiles_backend': settings.STORAGES['staticfiles']['BACKEND'],"
            "'static_root_name': str(settings.STATIC_ROOT.name),"
            "'x_frame': settings.X_FRAME_OPTIONS,"
            "'hsts': settings.SECURE_HSTS_SECONDS,"
            "'session_secure': settings.SESSION_COOKIE_SECURE,"
            "'csrf_secure': settings.CSRF_COOKIE_SECURE,"
            "'x_accel': settings.USE_X_ACCEL_REDIRECT,"
            "'conn_health': settings.DATABASES['default'].get('CONN_HEALTH_CHECKS'),"
            "'wagtail_url': settings.WAGTAILADMIN_BASE_URL"
            "}))"
        )
        import json

        res = _run_py(
            code,
            {
                "DJANGO_SECRET_KEY": VALID_PROD_SECRET,
                "ALLOWED_HOSTS": "blog.example.com, 127.0.0.1",
                "CSRF_TRUSTED_ORIGINS": "https://blog.example.com",
                "WAGTAILADMIN_BASE_URL": "https://blog.example.com/",
            },
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        payload = json.loads(res.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["module"], "myblog.settings.production")
        self.assertFalse(payload["debug"])
        self.assertEqual(payload["allowed_hosts"], ["blog.example.com", "127.0.0.1"])
        self.assertEqual(
            payload["staticfiles_backend"],
            "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
        )
        self.assertEqual(payload["static_root_name"], "staticfiles")
        self.assertEqual(payload["x_frame"], "SAMEORIGIN")
        self.assertGreaterEqual(payload["hsts"], 31536000)
        self.assertTrue(payload["session_secure"])
        self.assertTrue(payload["csrf_secure"])
        self.assertFalse(payload["x_accel"])
        self.assertTrue(payload["conn_health"])
        self.assertEqual(payload["wagtail_url"], "https://blog.example.com")

        # 显式传入 USE_X_ACCEL_REDIRECT=1 时可开启内部代理头
        res_xaccel = _run_py(
            code,
            {
                "DJANGO_SECRET_KEY": VALID_PROD_SECRET,
                "ALLOWED_HOSTS": "blog.example.com",
                "USE_X_ACCEL_REDIRECT": "1",
            },
        )
        self.assertEqual(res_xaccel.returncode, 0, msg=res_xaccel.stderr)
        payload_xaccel = json.loads(res_xaccel.stdout.strip().splitlines()[-1])
        self.assertTrue(payload_xaccel["x_accel"])

    def test_manage_check_deploy_and_isolated_collectstatic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env.setdefault("MYBLOG_ENV_FILE", ".env.test")
            env["DJANGO_SETTINGS_MODULE"] = "myblog.settings.production"
            env["DJANGO_SECRET_KEY"] = VALID_PROD_SECRET
            env["ALLOWED_HOSTS"] = "blog.example.com,127.0.0.1"
            env["CSRF_TRUSTED_ORIGINS"] = "https://blog.example.com"
            env["WAGTAILADMIN_BASE_URL"] = "https://blog.example.com"
            env["DJANGO_STATIC_ROOT"] = tmpdir

            chk = subprocess.run(
                [sys.executable, "-B", "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
                cwd=str(BASE_DIR),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(chk.returncode, 0, msg=chk.stdout + "\n" + chk.stderr)

            col = subprocess.run(
                [sys.executable, "-B", "manage.py", "collectstatic", "--noinput", "--clear"],
                cwd=str(BASE_DIR),
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(col.returncode, 0, msg=col.stdout + "\n" + col.stderr)
            self.assertTrue((Path(tmpdir) / "staticfiles.json").is_file())
