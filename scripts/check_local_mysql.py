"""Run Django/Wagtail checks on empty local MySQL databases without migrations."""

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
    config = {key: value for key, value in vars(base).items() if key.isupper()}
    config.update(
        SECRET_KEY="process-only-service-compatibility-check",
        DEBUG=False,
        DATABASES={
            alias: {
                "ENGINE": "django.db.backends.mysql",
                "NAME": name, "HOST": "127.0.0.1", "PORT": "3306",
                "USER": env["MYSQL_USER"], "PASSWORD": env["MYSQL_PASSWORD"],
                "OPTIONS": {"charset": "utf8mb4"},
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

    if not Path(home.__file__).resolve().is_relative_to(ROOT):
        raise RuntimeError("Unexpected application import path")
    django.setup()
    try:
        for alias in settings.DATABASES:
            connection = connections[alias]
            connection.ensure_connection()
            connection.check_database_version_supported()
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT VERSION(), DATABASE(), @@server_uuid, "
                    "@@sql_mode, @@default_storage_engine"
                )
                version, database, server_uuid, sql_mode, engine = cursor.fetchone()
                assert database == settings.DATABASES[alias]["NAME"]
                assert "STRICT_TRANS_TABLES" in sql_mode or "STRICT_ALL_TABLES" in sql_mode
                assert engine == "InnoDB"
                cursor.execute(
                    "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
                    [database],
                )
                assert cursor.fetchone()[0] == 0
                print("Django MySQL", alias, version, database, server_uuid, "zero tables")
        call_command("check", databases=list(settings.DATABASES), fail_level="WARNING")
        print("Django/Wagtail MySQL checks passed; no application migrations")
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
        print(f"Django MySQL verification failed ({type(exc).__name__}); credentials withheld", file=sys.stderr)
        sys.exit(1)
