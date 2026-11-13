"""Check database selection without opening a network or database connection."""

import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


SETTINGS = Path(__file__).resolve().parents[1] / "myblog" / "settings" / "base.py"


class DatabaseSettingsTests(unittest.TestCase):
    def load_settings(self, env_file, **variables):
        with patch.dict(os.environ, {"MYBLOG_ENV_FILE": str(env_file), **variables}, clear=True):
            return runpy.run_path(str(SETTINGS))

    def config(self, env_file, **variables):
        return self.load_settings(env_file, **variables)["DATABASES"]["default"]

    def test_unconfigured_database_stays_postgres_without_a_builtin_password(self):
        with tempfile.TemporaryDirectory() as directory:
            database = self.config(Path(directory) / "absent.env")
        self.assertEqual(database["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(database["USER"], "postgres")
        self.assertEqual(database["PASSWORD"], "")
        self.assertEqual(database["TEST"]["NAME"], "test_myblog")

    def test_environment_overrides_file_without_expanding_password_expressions(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "client.env"
            env_file.write_text(
                "POSTGRES_DB=myblog_dev\nPOSTGRES_USER=postgres\n"
                "POSTGRES_PASSWORD='fixture-${UNSET}-password'\n",
                encoding="utf-8",
            )
            database = self.config(env_file, POSTGRES_DB="myblog_test")
        self.assertEqual(database["NAME"], "myblog_test")
        self.assertEqual(database["PASSWORD"], "fixture-${UNSET}-password")

    def test_django_test_database_is_separate_from_service_test_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = self.config(
                Path(directory) / "absent.env", POSTGRES_DB="myblog_test",
                POSTGRES_TEST_DB="test_myblog_isolated",
            )
        self.assertEqual(database["TEST"]["NAME"], "test_myblog_isolated")
        self.assertNotEqual(database["NAME"], database["TEST"]["NAME"])

    def test_redis_db_and_key_prefix_read_from_env_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "test.env"
            env_file.write_text(
                "REDIS_HOST=127.0.0.1\nREDIS_PORT=6379\n"
                "REDIS_DB=11\nREDIS_KEY_PREFIX=myblog:test\n",
                encoding="utf-8",
            )
            ns = self.load_settings(env_file)
            self.assertTrue(ns["CACHES"]["default"]["LOCATION"].endswith("/11"))
            self.assertEqual(ns["CACHES"]["default"]["KEY_PREFIX"], "myblog:test")
            self.assertEqual(ns["REDIS_KEY_PREFIX"], "myblog:test")

            ns_override = self.load_settings(env_file, REDIS_DB="9", REDIS_KEY_PREFIX="myblog:dev:")
            self.assertTrue(ns_override["CACHES"]["default"]["LOCATION"].endswith("/9"))
            self.assertEqual(ns_override["CACHES"]["default"]["KEY_PREFIX"], "myblog:dev")
            self.assertEqual(ns_override["REDIS_KEY_PREFIX"], "myblog:dev")


if __name__ == "__main__":
    unittest.main()
