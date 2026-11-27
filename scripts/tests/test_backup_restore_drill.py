"""三端联合备份与恢复演练脚本的离线安全护栏与校验测试。"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import backup_restore_drill as drill


class BackupRestoreDrillSafetyTests(unittest.TestCase):
    def test_assert_safe_drill_targets_rejects_protected_and_non_drill_names(self):
        drill.assert_safe_drill_targets(
            source_pg="myblog_staging",
            source_mongo="myblog_staging",
            source_bucket="myblog-staging",
            target_pg="myblog_drill_tmp",
            target_mongo="myblog_drill_tmp",
            target_bucket="myblog-drill-tmp",
        )

        for bad_pg, bad_mongo, bad_bucket in (
            ("myblog_dev", "myblog_drill_tmp", "myblog-drill-tmp"),
            ("myblog_staging", "myblog_drill_tmp", "myblog-drill-tmp"),
            ("myblog_drill_tmp", "myblog_staging", "myblog-drill-tmp"),
            ("myblog_drill_tmp", "myblog_drill_tmp", "myblog-staging"),
            ("myblog_drill_tmp", "myblog_drill_tmp", "myblog-dev"),
            ("other_db", "myblog_drill_tmp", "myblog-drill-tmp"),
            ("myblog_prod_drill", "myblog_drill_tmp", "myblog-drill-tmp"),
            ("dbname=myblog_dev application_name=drill", "myblog_drill_tmp", "myblog-drill-tmp"),
            ("postgresql://postgres@127.0.0.1/myblog_dev?x=drill", "myblog_drill_tmp", "myblog-drill-tmp"),
            ("myblog_drill_tmp", "myblog_drill_tmp.*", "myblog-drill-tmp"),
            ("myblog_drill_tmp", "myblog_drill_tmp", "myblog_drill_tmp"),
        ):
            with self.assertRaises(ValueError):
                drill.assert_safe_drill_targets(
                    source_pg="myblog_staging",
                    source_mongo="myblog_staging",
                    source_bucket="myblog-staging",
                    target_pg=bad_pg,
                    target_mongo=bad_mongo,
                    target_bucket=bad_bucket,
                )

        cmd = drill.build_mongorestore_command(
            {"MONGO_USER": "root", "MONGO_PASSWORD": "x", "MONGO_AUTH_SOURCE": "admin"},
            source_mongo="myblog_staging",
            target_mongo="myblog_drill_tmp",
        )
        self.assertIn("--nsInclude=myblog_staging.*", cmd)
        self.assertIn("--nsFrom=myblog_staging.*", cmd)
        self.assertIn("--nsTo=myblog_drill_tmp.*", cmd)

    def test_source_mismatch_aborts_before_cleanup_or_restore(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bdir = Path(tmpdir)
            (bdir / "s3_objects").mkdir()
            (bdir / "postgres.dump").write_bytes(b"pgdata")
            (bdir / "mongo.archive.gz").write_bytes(b"mongodata")
            (bdir / "s3_manifest.json").write_text('{"bucket":"myblog-staging","object_count":0,"total_bytes":0,"objects":[]}', encoding="utf-8")
            (bdir / "backup_manifest.json").write_text(
                '{"source":{"postgres_db":"myblog_staging","mongo_db":"myblog_staging","s3_bucket":"myblog-staging"},'
                '"artifacts":{"postgres_dump":{"file":"postgres.dump","sha256":"'
                + drill._sha256_file(bdir / "postgres.dump")
                + '"},"mongo_archive":{"file":"mongo.archive.gz","sha256":"'
                + drill._sha256_file(bdir / "mongo.archive.gz")
                + '"}}}',
                encoding="utf-8",
            )
            fake_cfg = {
                "POSTGRES_HOST": "127.0.0.1",
                "POSTGRES_PORT": "5432",
                "POSTGRES_DB": "myblog_staging",
                "MONGO_HOST": "127.0.0.1",
                "MONGO_PORT": "27017",
                "MONGO_NAME": "myblog_drill_tmp",
                "S3_BUCKET": "myblog-staging",
            }
            with (
                patch.object(drill, "load_env_config", return_value=fake_cfg),
                patch.object(drill, "_cleanup_drill_targets") as mock_cleanup,
                patch("scripts.backup_restore_drill.subprocess.run") as mock_run,
            ):
                with self.assertRaises(ValueError):
                    drill.run_restore_drill(env_file=".env.staging", backup_dir=bdir)
                mock_cleanup.assert_not_called()
                mock_run.assert_not_called()

    def test_verify_backup_artifacts_detects_tampered_checksum(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bdir = Path(tmpdir)
            (bdir / "s3_objects").mkdir()
            (bdir / "postgres.dump").write_bytes(b"pgdata")
            (bdir / "mongo.archive.gz").write_bytes(b"mongodata")
            (bdir / "s3_objects" / "a.png").write_bytes(b"png")

            (bdir / "s3_manifest.json").write_text(
                '{"bucket":"b","object_count":1,"total_bytes":3,"objects":[{"key":"a.png","rel_path":"a.png","size":3,"sha256":"'
                + drill._sha256_file(bdir / "s3_objects" / "a.png")
                + '"}]}',
                encoding="utf-8",
            )
            (bdir / "backup_manifest.json").write_text(
                '{"source":{"postgres_db":"s","mongo_db":"s","s3_bucket":"s"},"artifacts":{"postgres_dump":{"file":"postgres.dump","sha256":"'
                + drill._sha256_file(bdir / "postgres.dump")
                + '"},"mongo_archive":{"file":"mongo.archive.gz","sha256":"'
                + drill._sha256_file(bdir / "mongo.archive.gz")
                + '"}}}',
                encoding="utf-8",
            )
            drill._verify_backup_artifacts(bdir)

            (bdir / "postgres.dump").write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                drill._verify_backup_artifacts(bdir)

    def test_s3_path_traversal_endpoint_mismatch_and_cleanup_guard(self):
        for unsafe_key in ("../escape.png", "a/../b.png", "/abs.png", "a\\b.png", ""):
            with self.assertRaises(ValueError):
                drill._safe_s3_rel_path(unsafe_key)

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / "s3_objects"
            base.mkdir()
            with self.assertRaises(ValueError):
                drill._resolve_under_base(base, "../outside.txt")

        with self.assertRaises(ValueError):
            drill._assert_local_docker_endpoints({"POSTGRES_HOST": "10.0.0.8", "POSTGRES_PORT": "5432"})
        with self.assertRaises(ValueError):
            drill._assert_local_docker_endpoints({"POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "5432", "MONGO_HOST": "10.0.0.8", "MONGO_PORT": "27017"})
        with self.assertRaises(ValueError):
            drill._cleanup_drill_targets(
                {"POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "5432", "MONGO_HOST": "127.0.0.1", "MONGO_PORT": "27017"},
                source_pg="myblog_staging",
                source_mongo="myblog_staging",
                source_bucket="myblog-staging",
                target_pg="myblog_staging",
                target_mongo="myblog_drill_tmp",
                target_bucket="myblog-drill-tmp",
            )
