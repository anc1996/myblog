"""Run Django/Wagtail checks on empty local PostgreSQL databases without migrations."""

from pathlib import Path
import sys

from dotenv import dotenv_values


ROOT = Path("/mnt/f/openclaw/workspace/wagtail/myblog")


def main():
    if not __debug__:
        raise RuntimeError("Verification must run without Python optimization")
    if Path.cwd() != ROOT or not sys.executable.startswith("/root/anaconda3/envs/wagtailblog-test/"):
        raise RuntimeError("Use the authorized WSL cwd and Conda interpreter")
    sys.path.insert(0, str(ROOT))
    from myblog.settings import base
    from django.conf import settings

    env = dotenv_values("/opt/myblog-services/secrets/admin.env", interpolate=False)
    postgres_user = env.get("POSTGRES_USER", "postgres")
    postgres_password = env.get("POSTGRES_PASSWORD") or env.get("MYSQL_PASSWORD", "")

    config = {key: value for key, value in vars(base).items() if key.isupper()}
    config.update(
        SECRET_KEY="process-only-service-compatibility-check",
        DEBUG=False,
        DATABASES={
            alias: {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": name,
                "HOST": "127.0.0.1",
                "PORT": "5432",
                "USER": postgres_user,
                "PASSWORD": postgres_password,
                "CONN_MAX_AGE": 0,
                "OPTIONS": {"connect_timeout": 10},
                "TEST": {"NAME": "test_myblog"},
            }
            for alias, name in (
                ("default", "myblog_dev"), ("verification_test", "myblog_test"),
            )
        },
    )
    settings.configure(**config)
    import django
    from django.core.management import call_command
    from django.db import connections
    import home

    if not Path(home.__file__).resolve().is_relative_to((ROOT / "myblog" / "apps").resolve()):
        raise RuntimeError("Unexpected application import path")
    django.setup()
    try:
        for alias in settings.DATABASES:
            connection = connections[alias]
            connection.ensure_connection()
            connection.check_database_version_supported()

            # 校验 PostgreSQL 特性：必须支持条件索引与行级排他锁 (select for update)
            features = connection.features
            assert features.supports_partial_indexes, f"{alias} must support partial indexes"
            assert features.has_select_for_update, f"{alias} must support select for update"

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT version(), current_database(), current_user, "
                    "current_setting('server_encoding')"
                )
                version_str, database, current_user, encoding = cursor.fetchone()
                assert database == settings.DATABASES[alias]["NAME"]
                assert encoding == "UTF8", f"{alias} must use UTF8"
                # 校验两个库在运行 Django check 前为纯净零业务表
                cursor.execute(
                    "SELECT count(*) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                    "AND n.nspname NOT LIKE 'pg_toast%' "
                    "AND n.nspname NOT LIKE 'pg_temp_%' "
                    "AND c.relkind IN ('r', 'v', 'm', 'S', 'p', 'f')"
                )
                assert cursor.fetchone()[0] == 0, f"{alias} contains unexpected tables"
                print("Django PostgreSQL", alias, version_str.split()[1], database, current_user, "zero tables")

        # 严格执行系统检查，fail_level WARNING，绝不屏蔽 W036
        call_command("check", databases=list(settings.DATABASES), fail_level="WARNING")
        print("Django/Wagtail PostgreSQL checks passed; no application migrations")
    finally:
        connections.close_all()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # System checks contain model diagnostics rather than connection secrets.
        from django.core.management.base import SystemCheckError
        if isinstance(exc, SystemCheckError):
            print(str(exc), file=sys.stderr)
        print(f"Django PostgreSQL verification failed ({type(exc).__name__}); credentials withheld", file=sys.stderr)
        sys.exit(1)
