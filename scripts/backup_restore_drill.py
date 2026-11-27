"""
myblog 三端联合备份与隔离恢复演练工具（PostgreSQL 18 + MongoDB 9 + RustFS）。

设计原则：
1. 不将任何密码、Access Key 或 Secret 写入备份清单或 Git 追踪目录；
2. 备份默认写入未追踪目录 output/backups/<timestamp>/，按 SHA-256 校验落盘产物；
3. 恢复演练强制隔离目标库/桶，严禁覆盖 myblog_dev / myblog_staging / myblog_test / 生产库；
4. 恢复后执行三端交叉对账：PG 业务表计数、MongoDB 字段版本哈希、RustFS 存储键存在性。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import dotenv_values
import psycopg
from psycopg import sql
from pymongo import MongoClient

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

POSTGRES_CONTAINER = "myblog-services-postgres-1"
MONGO_CONTAINER = "myblog-services-mongo-1"
_FORBIDDEN_RESTORE_TARGETS = {
    "myblog_dev",
    "myblog-dev",
    "myblog_staging",
    "myblog-staging",
    "myblog_test",
    "myblog-test",
    "test_myblog",
    "test_myblog_staging",
    "postgres",
    "admin",
    "local",
    "config",
    "wagtailblog",
    "wagtailblog3",
}
_SAFE_DB_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_SAFE_BUCKET_IDENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")


def load_env_config(env_file: Path | str) -> dict[str, str]:
    env_path = Path(env_file)
    if not env_path.is_absolute():
        env_path = ROOT_DIR / env_path
    if not env_path.is_file():
        raise FileNotFoundError(f"Env file not found: {env_path}")
    vals = dotenv_values(env_path, interpolate=False)
    return {str(k): str(v or "") for k, v in vals.items() if k}


def assert_safe_drill_targets(
    *,
    source_pg: str,
    source_mongo: str,
    source_bucket: str,
    target_pg: str,
    target_mongo: str,
    target_bucket: str,
) -> None:
    """Fail-Closed 隔离校验：演练恢复目标必须是非生产、非开发、非暂存、非源库的独立名称。"""
    for src_label, src_val, pattern in (
        ("source_pg", source_pg, _SAFE_DB_IDENT_RE),
        ("source_mongo", source_mongo, _SAFE_DB_IDENT_RE),
        ("source_bucket", source_bucket, _SAFE_BUCKET_IDENT_RE),
    ):
        src_clean = (src_val or "").strip()
        if not pattern.fullmatch(src_clean):
            raise ValueError(f"{src_label} ({src_val!r}) 包含非法字符或不符合标识符规范")

    for label, val, src in (
        ("target_pg", target_pg, source_pg),
        ("target_mongo", target_mongo, source_mongo),
        ("target_bucket", target_bucket, source_bucket),
    ):
        cleaned = (val or "").strip()
        lower = cleaned.lower()
        if not cleaned:
            raise ValueError(f"{label} 不能为空")
        ident_re = _SAFE_BUCKET_IDENT_RE if label == "target_bucket" else _SAFE_DB_IDENT_RE
        if not ident_re.fullmatch(cleaned):
            raise ValueError(f"{label} ({cleaned!r}) 仅允许安全标识符字符，禁止连接串/URI/特殊字符")
        if lower == (src or "").strip().lower():
            raise ValueError(f"{label} ({cleaned}) 不得与源库/源桶相同")
        if lower in _FORBIDDEN_RESTORE_TARGETS or "prod" in lower or "wagtailblog" in lower:
            raise ValueError(f"{label} ({cleaned}) 命中受保护目标黑名单，拒绝覆盖恢复")
        if "drill" not in lower:
            raise ValueError(f"{label} ({cleaned}) 必须显式包含 'drill' 标识以防止误写业务库")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        while True:
            chunk = fp.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _safe_s3_rel_path(key: str) -> Path:
    """严格校验 S3 Key 映射为相对路径：拒绝空段、'.'、'..'、反斜杠、盘符及前导斜杠。"""
    raw = str(key or "")
    if not raw or raw.startswith("/") or "\\" in raw or "\x00" in raw:
        raise ValueError(f"不安全的 S3 对象键: {key!r}")
    parts = raw.split("/")
    if any(p in ("", ".", "..") for p in parts) or ":" in parts[0]:
        raise ValueError(f"S3 对象键包含越界或非法路径段: {key!r}")
    return Path(*parts)


def _resolve_under_base(base_dir: Path, rel_path: str | Path) -> Path:
    """校验路径严格落在 base_dir 之下的普通文件位置，拒绝越界与符号链接。"""
    base_resolved = base_dir.resolve()
    rel = Path(rel_path)
    if rel.is_absolute() or ".." in rel.parts or "." in rel.parts or "\\" in str(rel_path):
        raise ValueError(f"非法的备份相对路径: {rel_path!r}")
    candidate = base_dir / rel
    # 逐层检查现存祖先与目标本身均不是符号链接
    cur = base_dir
    if cur.is_symlink():
        raise ValueError(f"备份根目录不允许为符号链接: {cur}")
    for part in rel.parts:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise ValueError(f"备份路径不允许包含符号链接: {cur}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base_resolved) or resolved == base_resolved:
        raise ValueError(f"路径越界逃逸备份根目录: {rel_path!r}")
    return candidate


def _assert_local_docker_endpoints(cfg: dict[str, str]) -> None:
    """保证配置端点与本地 Docker 容器一致，防止备份/清理与恢复跨实例错位操作。"""
    pg_host = (cfg.get("POSTGRES_HOST") or "127.0.0.1").strip().lower()
    pg_port = str(cfg.get("POSTGRES_PORT") or "5432").strip()
    mongo_host = (cfg.get("MONGO_HOST") or "127.0.0.1").strip().lower()
    mongo_port = str(cfg.get("MONGO_PORT") or "27017").strip()
    if pg_host not in ("127.0.0.1", "localhost") or pg_port != "5432":
        raise ValueError(f"PostgreSQL 端点 ({pg_host}:{pg_port}) 与本地容器不一致，拒绝执行容器操作")
    if mongo_host not in ("127.0.0.1", "localhost") or mongo_port != "27017":
        raise ValueError(f"MongoDB 端点 ({mongo_host}:{mongo_port}) 与本地容器不一致，拒绝执行容器操作")


def _calculate_payload_hash(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _make_s3_client(cfg: dict[str, str]):
    return boto3.client(
        "s3",
        endpoint_url=cfg.get("S3_ENDPOINT_URL", "http://127.0.0.1:9000"),
        aws_access_key_id=cfg.get("S3_ACCESS_KEY_ID", ""),
        aws_secret_access_key=cfg.get("S3_SECRET_ACCESS_KEY", ""),
        region_name=cfg.get("S3_REGION", "us-east-1"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _pg_connect(cfg: dict[str, str], dbname: str):
    return psycopg.connect(
        host=cfg.get("POSTGRES_HOST", "127.0.0.1"),
        port=int(cfg.get("POSTGRES_PORT", "5432") or 5432),
        user=cfg.get("POSTGRES_USER", "postgres"),
        password=cfg.get("POSTGRES_PASSWORD", ""),
        dbname=dbname,
        autocommit=True,
    )


def _mongo_client(cfg: dict[str, str]) -> MongoClient:
    return MongoClient(
        host=cfg.get("MONGO_HOST", "127.0.0.1"),
        port=int(cfg.get("MONGO_PORT", "27017") or 27017),
        username=cfg.get("MONGO_USER", "root"),
        password=cfg.get("MONGO_PASSWORD", ""),
        authSource=cfg.get("MONGO_AUTH_SOURCE", "admin"),
        serverSelectionTimeoutMS=5000,
    )


def run_backup(
    *,
    env_file: Path | str,
    backup_dir: Path,
) -> dict[str, Any]:
    """只读备份 PostgreSQL（custom dump）、MongoDB（gzip archive）与 RustFS 全量对象到 backup_dir。"""
    cfg = load_env_config(env_file)
    _assert_local_docker_endpoints(cfg)
    src_pg = cfg["POSTGRES_DB"]
    src_mongo = cfg["MONGO_NAME"]
    src_bucket = cfg["S3_BUCKET"]

    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    s3_dir = backup_dir / "s3_objects"
    s3_dir.mkdir(parents=True, exist_ok=True)

    # 1. PostgreSQL pg_dump (-Fc) 流式输出到本地
    pg_dump_path = backup_dir / "postgres.dump"
    pg_cmd = [
        "docker", "exec", "-i",
        "-e", f"PGPASSWORD={cfg.get('POSTGRES_PASSWORD', '')}",
        POSTGRES_CONTAINER,
        "pg_dump",
        "-h", "127.0.0.1",
        "-p", "5432",
        "-U", cfg.get("POSTGRES_USER", "postgres"),
        "-Fc",
        src_pg,
    ]
    with pg_dump_path.open("wb") as fp:
        pg_proc = subprocess.run(pg_cmd, stdout=fp, stderr=subprocess.PIPE, check=False)
    if pg_proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {pg_proc.stderr.decode('utf-8', 'replace')}")

    # 2. MongoDB mongodump (--archive --gzip) 流式输出到本地
    mongo_dump_path = backup_dir / "mongo.archive.gz"
    mongo_cmd = [
        "docker", "exec", "-i",
        MONGO_CONTAINER,
        "mongodump",
        "--host", "127.0.0.1",
        "--port", "27017",
        "--username", cfg.get("MONGO_USER", "root"),
        "--password", cfg.get("MONGO_PASSWORD", ""),
        "--authenticationDatabase", cfg.get("MONGO_AUTH_SOURCE", "admin"),
        "--db", src_mongo,
        "--archive",
        "--gzip",
    ]
    with mongo_dump_path.open("wb") as fp:
        m_proc = subprocess.run(mongo_cmd, stdout=fp, stderr=subprocess.PIPE, check=False)
    if m_proc.returncode != 0:
        raise RuntimeError(f"mongodump failed: {m_proc.stderr.decode('utf-8', 'replace')}")

    # 3. RustFS S3 全量对象下载与 SHA-256 记录
    s3 = _make_s3_client(cfg)
    paginator = s3.get_paginator("list_objects_v2")
    s3_items: list[dict[str, Any]] = []
    seen_rel_paths: set[str] = set()
    for page in paginator.paginate(Bucket=src_bucket):
        for obj in page.get("Contents", []) or []:
            key = str(obj["Key"])
            rel = _safe_s3_rel_path(key)
            rel_posix = PurePosixPath(*rel.parts).as_posix()
            if rel_posix in seen_rel_paths:
                raise ValueError(f"检测到 S3 备份相对路径冲突: {rel_posix}")
            seen_rel_paths.add(rel_posix)
            local_obj = _resolve_under_base(s3_dir, rel)
            local_obj.parent.mkdir(parents=True, exist_ok=True)
            body = s3.get_object(Bucket=src_bucket, Key=key)["Body"].read()
            local_obj.write_bytes(body)
            s3_items.append({
                "key": key,
                "rel_path": rel_posix,
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            })

    s3_manifest = {
        "bucket": src_bucket,
        "object_count": len(s3_items),
        "total_bytes": sum(it["size"] for it in s3_items),
        "objects": s3_items,
    }
    (backup_dir / "s3_manifest.json").write_text(
        json.dumps(s3_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "postgres_db": src_pg,
            "mongo_db": src_mongo,
            "s3_bucket": src_bucket,
        },
        "artifacts": {
            "postgres_dump": {
                "file": "postgres.dump",
                "size": pg_dump_path.stat().st_size,
                "sha256": _sha256_file(pg_dump_path),
            },
            "mongo_archive": {
                "file": "mongo.archive.gz",
                "size": mongo_dump_path.stat().st_size,
                "sha256": _sha256_file(mongo_dump_path),
            },
            "s3_manifest": {
                "file": "s3_manifest.json",
                "object_count": len(s3_items),
                "total_bytes": s3_manifest["total_bytes"],
            },
        },
    }
    (backup_dir / "backup_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def _verify_backup_artifacts(backup_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads((backup_dir / "backup_manifest.json").read_text(encoding="utf-8"))
    s3_manifest = json.loads((backup_dir / "s3_manifest.json").read_text(encoding="utf-8"))

    pg_meta = manifest["artifacts"]["postgres_dump"]
    pg_path = backup_dir / pg_meta["file"]
    if not pg_path.is_file() or _sha256_file(pg_path) != pg_meta["sha256"]:
        raise ValueError("postgres.dump SHA-256 校验失败")

    mongo_meta = manifest["artifacts"]["mongo_archive"]
    mongo_path = backup_dir / mongo_meta["file"]
    if not mongo_path.is_file() or _sha256_file(mongo_path) != mongo_meta["sha256"]:
        raise ValueError("mongo.archive.gz SHA-256 校验失败")

    for item in s3_manifest.get("objects", []):
        _safe_s3_rel_path(item["key"])
        obj_path = _resolve_under_base(backup_dir / "s3_objects", item["rel_path"])
        if not obj_path.is_file() or _sha256_file(obj_path) != item["sha256"]:
            raise ValueError(f"S3 backup object checksum mismatch: {item['key']}")

    return manifest, s3_manifest


def _snapshot_pg_summary(cfg: dict[str, str], dbname: str) -> dict[str, Any]:
    with _pg_connect(cfg, dbname) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM wagtailcore_page")
            total_pages = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE p.live) FROM blog_blogpage b JOIN wagtailcore_page p ON p.id = b.page_ptr_id")
            row = cur.fetchone()
            blog_total, blog_live = int(row[0]), int(row[1])
            cur.execute("SELECT COUNT(*) FROM media_library_customimage")
            images = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM media_library_customdocument")
            docs = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM comments_comment")
            comments = int(cur.fetchone()[0])
            cur.execute("SELECT COALESCE(SUM(total_pv), 0) FROM analytics_pageanalyticstotal")
            pv_sum = int(cur.fetchone()[0])
            cur.execute(
                "SELECT p.id, r.content FROM blog_blogpage b "
                "JOIN wagtailcore_page p ON p.id = b.page_ptr_id "
                "JOIN wagtailcore_revision r ON r.id = p.live_revision_id "
                "WHERE p.live = TRUE AND p.live_revision_id IS NOT NULL"
            )
            pointers: list[dict[str, Any]] = []
            for pid, raw_content in cur.fetchall():
                cdict = raw_content if isinstance(raw_content, dict) else json.loads(raw_content)
                for ptr in cdict.get("_external_field_pointers", []) or []:
                    pointers.append({"page_id": int(pid), **ptr})
            storage_keys: list[str] = []
            for tbl in (
                "media_library_customimage",
                "media_library_customdocument",
                "media_library_audio",
                "media_library_video",
            ):
                cur.execute(f"SELECT storage_key FROM {tbl} WHERE COALESCE(storage_key, '') <> ''")
                storage_keys.extend(str(r[0]) for r in cur.fetchall())
    return {
        "total_pages": total_pages,
        "blog_total": blog_total,
        "blog_live": blog_live,
        "images": images,
        "documents": docs,
        "comments": comments,
        "pv_sum": pv_sum,
        "pointers": pointers,
        "storage_keys": storage_keys,
    }


def _cleanup_drill_targets(
    cfg: dict[str, str],
    *,
    source_pg: str,
    source_mongo: str,
    source_bucket: str,
    target_pg: str,
    target_mongo: str,
    target_bucket: str,
) -> None:
    assert_safe_drill_targets(
        source_pg=source_pg,
        source_mongo=source_mongo,
        source_bucket=source_bucket,
        target_pg=target_pg,
        target_mongo=target_mongo,
        target_bucket=target_bucket,
    )
    _assert_local_docker_endpoints(cfg)
    with _pg_connect(cfg, "postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (target_pg,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(target_pg)))
    mc = _mongo_client(cfg)
    try:
        mc.drop_database(target_mongo)
    finally:
        mc.close()
    s3 = _make_s3_client(cfg)
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=target_bucket):
            objs = [{"Key": o["Key"]} for o in (page.get("Contents") or [])]
            if objs:
                s3.delete_objects(Bucket=target_bucket, Delete={"Objects": objs})
        s3.delete_bucket(Bucket=target_bucket)
    except ClientError:
        pass


def build_mongorestore_command(
    cfg: dict[str, str],
    *,
    source_mongo: str,
    target_mongo: str,
) -> list[str]:
    """构建带严格 --nsInclude / --nsFrom / --nsTo 范围约束的 mongorestore 命令，绝不放行其他库集合。"""
    if not _SAFE_DB_IDENT_RE.fullmatch(source_mongo or "") or not _SAFE_DB_IDENT_RE.fullmatch(target_mongo or ""):
        raise ValueError(f"非法的 MongoDB 库名: source={source_mongo!r}, target={target_mongo!r}")
    return [
        "docker", "exec", "-i",
        MONGO_CONTAINER,
        "mongorestore",
        "--host", "127.0.0.1",
        "--port", "27017",
        "--username", cfg.get("MONGO_USER", "root"),
        "--password", cfg.get("MONGO_PASSWORD", ""),
        "--authenticationDatabase", cfg.get("MONGO_AUTH_SOURCE", "admin"),
        "--archive",
        "--gzip",
        "--drop",
        f"--nsInclude={source_mongo}.*",
        f"--nsFrom={source_mongo}.*",
        f"--nsTo={target_mongo}.*",
    ]


def run_restore_drill(
    *,
    env_file: Path | str,
    backup_dir: Path,
    target_pg: str = "myblog_drill_tmp",
    target_mongo: str = "myblog_drill_tmp",
    target_bucket: str = "myblog-drill-tmp",
    cleanup_after: bool = True,
) -> dict[str, Any]:
    """将 backup_dir 还原到完全隔离的临时库/桶并执行三端交叉核验。"""
    cfg = load_env_config(env_file)
    _assert_local_docker_endpoints(cfg)
    backup_dir = Path(backup_dir)
    manifest, s3_manifest = _verify_backup_artifacts(backup_dir)
    src_pg = manifest["source"]["postgres_db"]
    src_mongo = manifest["source"]["mongo_db"]
    src_bucket = manifest["source"]["s3_bucket"]
    if (
        src_pg != cfg.get("POSTGRES_DB")
        or src_mongo != cfg.get("MONGO_NAME")
        or src_bucket != cfg.get("S3_BUCKET")
    ):
        raise ValueError(
            "备份清单源标识与当前 env_file 不一致，拒绝执行恢复或清理："
            f"manifest=({src_pg!r}, {src_mongo!r}, {src_bucket!r}) vs "
            f"env=({cfg.get('POSTGRES_DB')!r}, {cfg.get('MONGO_NAME')!r}, {cfg.get('S3_BUCKET')!r})"
        )

    assert_safe_drill_targets(
        source_pg=src_pg,
        source_mongo=src_mongo,
        source_bucket=src_bucket,
        target_pg=target_pg,
        target_mongo=target_mongo,
        target_bucket=target_bucket,
    )

    # 预清理并建出干净隔离演练库/桶
    _cleanup_drill_targets(
        cfg,
        source_pg=src_pg,
        source_mongo=src_mongo,
        source_bucket=src_bucket,
        target_pg=target_pg,
        target_mongo=target_mongo,
        target_bucket=target_bucket,
    )
    try:
        with _pg_connect(cfg, "postgres") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("CREATE DATABASE {} WITH ENCODING 'UTF8'").format(sql.Identifier(target_pg))
                )

        # 1. PostgreSQL pg_restore
        pg_dump_path = backup_dir / "postgres.dump"
        pg_restore_cmd = [
            "docker", "exec", "-i",
            "-e", f"PGPASSWORD={cfg.get('POSTGRES_PASSWORD', '')}",
            POSTGRES_CONTAINER,
            "pg_restore",
            "-h", "127.0.0.1",
            "-p", "5432",
            "-U", cfg.get("POSTGRES_USER", "postgres"),
            "--no-owner",
            "--no-privileges",
            f"--dbname={target_pg}",
        ]
        with pg_dump_path.open("rb") as fp:
            pr = subprocess.run(pg_restore_cmd, stdin=fp, stderr=subprocess.PIPE, check=False)
        if pr.returncode != 0:
            raise RuntimeError(f"pg_restore failed: {pr.stderr.decode('utf-8', 'replace')}")

        # 2. MongoDB mongorestore (--nsInclude + --nsFrom -> --nsTo)
        mongo_dump_path = backup_dir / "mongo.archive.gz"
        mongo_restore_cmd = build_mongorestore_command(
            cfg,
            source_mongo=src_mongo,
            target_mongo=target_mongo,
        )
        with mongo_dump_path.open("rb") as fp:
            mr = subprocess.run(mongo_restore_cmd, stdin=fp, stderr=subprocess.PIPE, check=False)
        if mr.returncode != 0:
            raise RuntimeError(f"mongorestore failed: {mr.stderr.decode('utf-8', 'replace')}")

        # 3. RustFS 恢复至隔离桶
        s3 = _make_s3_client(cfg)
        s3.create_bucket(Bucket=target_bucket)
        for item in s3_manifest.get("objects", []):
            obj_path = _resolve_under_base(backup_dir / "s3_objects", item["rel_path"])
            raw = obj_path.read_bytes()
            s3.put_object(Bucket=target_bucket, Key=item["key"], Body=raw)

        # 4. 跨存储交叉核验：对比源库与恢复演练库、核验 Mongo 指针哈希、核验 S3 物理对象
        src_pg_sum = _snapshot_pg_summary(cfg, src_pg)
        dst_pg_sum = _snapshot_pg_summary(cfg, target_pg)

        count_keys = ("total_pages", "blog_total", "blog_live", "images", "documents", "comments", "pv_sum")
        pg_mismatches = {
            k: {"source": src_pg_sum[k], "restored": dst_pg_sum[k]}
            for k in count_keys
            if src_pg_sum[k] != dst_pg_sum[k]
        }

        mc = _mongo_client(cfg)
        try:
            src_mongo_count = mc[src_mongo]["content_field_versions"].count_documents({})
            dst_col = mc[target_mongo]["content_field_versions"]
            dst_mongo_count = dst_col.count_documents({})
            mongo_missing = 0
            mongo_hash_mismatch = 0
            for ptr in dst_pg_sum["pointers"]:
                doc = dst_col.find_one({
                    "document_uuid": str(ptr["document_uuid"]),
                    "field_name": ptr["field_name"],
                    "version_id": ptr["version_id"],
                })
                if doc is None:
                    mongo_missing += 1
                    continue
                actual_hash = _calculate_payload_hash(doc.get("payload"))
                if actual_hash != doc.get("content_hash") or (ptr.get("content_hash") and actual_hash != ptr["content_hash"]):
                    mongo_hash_mismatch += 1
            # 同步校验恢复库内所有草稿与历史字段版本的载荷哈希完整性
            all_docs_hash_mismatch = 0
            for doc in dst_col.find({}, {"payload": 1, "content_hash": 1}):
                if _calculate_payload_hash(doc.get("payload")) != doc.get("content_hash"):
                    all_docs_hash_mismatch += 1
        finally:
            mc.close()

        restored_s3_keys: set[str] = set()
        s3_hash_mismatch_keys: list[str] = []
        expected_s3_hashes = {item["key"]: item["sha256"] for item in s3_manifest.get("objects", [])}
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=target_bucket):
            for obj in page.get("Contents", []) or []:
                key = str(obj["Key"])
                restored_s3_keys.add(key)
                body = s3.get_object(Bucket=target_bucket, Key=key)["Body"].read()
                if key in expected_s3_hashes and hashlib.sha256(body).hexdigest() != expected_s3_hashes[key]:
                    s3_hash_mismatch_keys.append(key)
        missing_s3_keys = [k for k in dst_pg_sum["storage_keys"] if k not in restored_s3_keys]
        missing_manifest_s3_keys = sorted(set(expected_s3_hashes.keys()) - restored_s3_keys)
        extra_s3_keys = sorted(restored_s3_keys - set(expected_s3_hashes.keys()))

        passed = (
            not pg_mismatches
            and src_mongo_count == dst_mongo_count
            and mongo_missing == 0
            and mongo_hash_mismatch == 0
            and all_docs_hash_mismatch == 0
            and not missing_s3_keys
            and not missing_manifest_s3_keys
            and not extra_s3_keys
            and not s3_hash_mismatch_keys
        )
        return {
            "passed": passed,
            "source": {"postgres": src_pg, "mongo": src_mongo, "s3_bucket": src_bucket},
            "restored": {"postgres": target_pg, "mongo": target_mongo, "s3_bucket": target_bucket},
            "pg_counts": {k: dst_pg_sum[k] for k in count_keys},
            "pg_mismatches": pg_mismatches,
            "mongo": {
                "source_docs": src_mongo_count,
                "restored_docs": dst_mongo_count,
                "checked_live_pointers": len(dst_pg_sum["pointers"]),
                "missing_pointers": mongo_missing,
                "hash_mismatches": mongo_hash_mismatch,
                "all_docs_hash_mismatches": all_docs_hash_mismatch,
            },
            "rustfs": {
                "expected_manifest_objects": len(expected_s3_hashes),
                "restored_bucket_objects": len(restored_s3_keys),
                "referenced_storage_keys": len(dst_pg_sum["storage_keys"]),
                "missing_referenced_keys": missing_s3_keys,
                "missing_manifest_keys": missing_manifest_s3_keys,
                "extra_keys": extra_s3_keys,
                "hash_mismatch_keys": s3_hash_mismatch_keys,
            },
            "cleaned_up": cleanup_after,
        }
    finally:
        if cleanup_after:
            _cleanup_drill_targets(
                cfg,
                source_pg=src_pg,
                source_mongo=src_mongo,
                source_bucket=src_bucket,
                target_pg=target_pg,
                target_mongo=target_mongo,
                target_bucket=target_bucket,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="myblog 三端联合备份与隔离恢复演练工具")
    parser.add_argument("--env-file", default=".env.staging", help="源环境配置（默认 .env.staging）")
    parser.add_argument(
        "--backup-dir",
        default=str(ROOT_DIR / "output" / "backups" / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")),
        help="备份输出/读取目录（位于未追踪 output/）",
    )
    parser.add_argument("--backup", action="store_true", help="执行只读三端备份")
    parser.add_argument("--drill-restore", action="store_true", help="执行隔离恢复与交叉对账演练")
    parser.add_argument("--target-pg-db", default="myblog_drill_tmp")
    parser.add_argument("--target-mongo-db", default="myblog_drill_tmp")
    parser.add_argument("--target-s3-bucket", default="myblog-drill-tmp")
    parser.add_argument("--keep-drill-targets", action="store_true", help="演练后保留临时隔离库/桶")
    args = parser.parse_args()

    if not (args.backup or args.drill_restore):
        parser.error("请至少指定 --backup 或 --drill-restore 之一。")

    bdir = Path(args.backup_dir)
    out: dict[str, Any] = {}
    if args.backup:
        out["backup"] = run_backup(env_file=args.env_file, backup_dir=bdir)
    if args.drill_restore:
        out["restore_drill"] = run_restore_drill(
            env_file=args.env_file,
            backup_dir=bdir,
            target_pg=args.target_pg_db,
            target_mongo=args.target_mongo_db,
            target_bucket=args.target_s3_bucket,
            cleanup_after=not args.keep_drill_targets,
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if "restore_drill" in out and not out["restore_drill"]["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
