"""Pure logic tests for local services credential flow, SQL escaping, and client env activation."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from psycopg import sql

from scripts import local_services


class LocalServicesLogicTests(unittest.TestCase):
    def setUp(self):
        # Every test is isolated, including rejection paths that unexpectedly succeed.
        # Individual cases must never fall back to real /opt or project client files.
        self.test_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(local_services, "SECRET_DIR", self.test_root / "secrets"))
        self.enterContext(patch.object(local_services, "ROOT", self.test_root))

    def test_prepare_cannot_overwrite_existing_credentials(self):
        directory = local_services.SECRET_DIR
        directory.mkdir()
        existing = directory / "admin.env"
        existing.write_text("UNCHANGED=fixture\n", encoding="utf-8")
        source = self.test_root / "input.env"
        source.write_text(
            "POSTGRES_PASSWORD=fixture\nREDIS_PASSWORD=fixture\n", encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            local_services.prepare(source)
        self.assertEqual(existing.read_text(encoding="utf-8"), "UNCHANGED=fixture\n")
        self.assertEqual(list(directory.iterdir()), [existing])

    def test_ports_definition(self):
        """PORTS must define postgres 5432 and not mysql."""
        self.assertIn("postgres", local_services.PORTS)
        self.assertNotIn("mysql", local_services.PORTS)
        self.assertEqual(local_services.PORTS["postgres"], (5432,))

    def test_prepare_legacy_mysql_input_conversion(self):
        """Legacy MYSQL_* credentials are accepted as one-time input and mapped to PostgreSQL."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_file = tmp_path / "reference.env"
            source_file.write_text(
                "MYSQL_USER=root\n"
                "MYSQL_PASSWORD=shared_secret_123\n"
                "REDIS_PASSWORD=redis_secret_456\n",
                encoding="utf-8",
            )
            original_secret_dir = local_services.SECRET_DIR
            try:
                local_services.SECRET_DIR = tmp_path / "secrets"
                local_services.prepare(source_file)

                admin_env = local_services.credentials()
                self.assertEqual(admin_env["POSTGRES_USER"], "postgres")
                self.assertEqual(admin_env["POSTGRES_PASSWORD"], "shared_secret_123")
                # Mongo 必须独立并保持 root 原值，不得被重写为 postgres
                self.assertEqual(admin_env["MONGO_USER"], "root")
                self.assertEqual(admin_env["MONGO_PASSWORD"], "shared_secret_123")
                self.assertEqual(admin_env["REDIS_PASSWORD"], "redis_secret_456")
                self.assertEqual(admin_env["S3_ACCESS_KEY_ID"], "root")
                self.assertEqual(admin_env["S3_SECRET_ACCESS_KEY"], "shared_secret_123")
            finally:
                local_services.SECRET_DIR = original_secret_dir

    def test_prepare_migrated_postgres_input(self):
        """Migrated POSTGRES_* credentials with independent Mongo credentials are fully accepted."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_file = tmp_path / "migrated.env"
            source_file.write_text(
                "POSTGRES_USER=postgres\n"
                "POSTGRES_PASSWORD=pg_secret_789\n"
                "MONGO_USER=root\n"
                "MONGO_PASSWORD=mongo_secret_999\n"
                "REDIS_PASSWORD=redis_secret_456\n"
                "S3_ACCESS_KEY_ID=custom_s3_key\n"
                "S3_SECRET_ACCESS_KEY=custom_s3_secret\n",
                encoding="utf-8",
            )
            original_secret_dir = local_services.SECRET_DIR
            try:
                local_services.SECRET_DIR = tmp_path / "secrets"
                local_services.prepare(source_file)

                admin_env = local_services.credentials()
                self.assertEqual(admin_env["POSTGRES_USER"], "postgres")
                self.assertEqual(admin_env["POSTGRES_PASSWORD"], "pg_secret_789")
                self.assertEqual(admin_env["MONGO_USER"], "root")
                self.assertEqual(admin_env["MONGO_PASSWORD"], "mongo_secret_999")
                self.assertEqual(admin_env["S3_ACCESS_KEY_ID"], "custom_s3_key")
                self.assertEqual(admin_env["S3_SECRET_ACCESS_KEY"], "custom_s3_secret")
            finally:
                local_services.SECRET_DIR = original_secret_dir

    def test_prepare_rejects_non_root_mongo(self):
        """Mongo user must remain root; any other user raises ValueError."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_file = tmp_path / "bad_mongo.env"
            source_file.write_text(
                "POSTGRES_PASSWORD=pg_secret\n"
                "MONGO_USER=postgres\n"
                "REDIS_PASSWORD=redis_secret\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                local_services.prepare(source_file)

    def test_credential_newline_injection_guard(self):
        """Credentials with CRLF or NUL characters must be rejected."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_file = tmp_path / "injection.env"
            source_file.write_text(
                "POSTGRES_PASSWORD=secret\n"
                "REDIS_PASSWORD=\"bad\nline\"\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                local_services.prepare(source_file)

    def test_sql_identifier_database_creation(self):
        """psycopg.sql.Identifier must safely format database creation query."""
        db_name = "myblog_dev"
        query = sql.SQL("CREATE DATABASE {} WITH ENCODING = 'UTF8'").format(
            sql.Identifier(db_name)
        )
        self.assertIn("CREATE DATABASE", query.as_string())
        self.assertIn('"myblog_dev"', query.as_string())

    def test_activate_environment_keys_cleanup_and_preservation(self):
        """Activation removes MYSQL_/MINIO_ keys, retains unrelated keys, and writes POSTGRES_*."""
        from dotenv import dotenv_values
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            local_env = tmp_path / ".env.local"
            local_env.write_text(
                "MYBLOG_ENV=local\n"
                "CUSTOM_FLAG=true\n"
                "MYSQL_HOST=127.0.0.1\n"
                "MYSQL_PASSWORD=old_pass\n"
                "MINIO_ROOT_USER=old_minio\n",
                encoding="utf-8",
            )

            test_values = {
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": "new_pg_password",
                "MONGO_USER": "root",
                "MONGO_PASSWORD": "new_pg_password",
                "REDIS_PASSWORD": "redis_password",
                "S3_ACCESS_KEY_ID": "root",
                "S3_SECRET_ACCESS_KEY": "new_pg_password",
            }

            with patch.object(local_services, "ROOT", tmp_path):
                local_services.write_client_env(test_values)

            updated = dotenv_values(local_env, interpolate=False)
            self.assertEqual(updated.get("MYBLOG_ENV"), "local")
            self.assertEqual(updated.get("CUSTOM_FLAG"), "true")
            self.assertFalse(any(k.startswith("MYSQL_") for k in updated))
            self.assertFalse(any(k.startswith("MINIO_") for k in updated))
            self.assertEqual(updated.get("POSTGRES_HOST"), "127.0.0.1")
            self.assertEqual(updated.get("POSTGRES_PORT"), "5432")
            self.assertEqual(updated.get("POSTGRES_DB"), "myblog_dev")
            self.assertEqual(updated.get("POSTGRES_TEST_DB"), "test_myblog")
            self.assertEqual(updated.get("MONGO_USER"), "root")


if __name__ == "__main__":
    unittest.main()
