"""Check database selection without opening a network or database connection."""

import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


SETTINGS = Path(__file__).resolve().parents[1] / "myblog" / "settings" / "base.py"


class DatabaseSettingsTests(unittest.TestCase):
    def config(self, env_file, **variables):
        with patch.dict(os.environ, {"MYBLOG_ENV_FILE": str(env_file), **variables}, clear=True):
            return runpy.run_path(str(SETTINGS))["DATABASES"]["default"]

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


if __name__ == "__main__":
    unittest.main()
