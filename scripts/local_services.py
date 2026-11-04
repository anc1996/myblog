"""Prepare and verify only this project's local WSL services with PostgreSQL; never migrate Django."""

import argparse
from ipaddress import IPv4Address
import json
from pathlib import Path
import secrets
import subprocess
import sys
import urllib.request
from uuid import uuid4

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import dotenv_values
import psycopg
from psycopg import sql
from psycopg.errors import UniqueViolation
from pymongo import MongoClient
from redis import Redis


ROOT = Path("/mnt/f/openclaw/workspace/wagtail/myblog")
SECRET_DIR = Path("/opt/myblog-services/secrets")
DBS = ("myblog_dev", "myblog_test")
PROJECT = "myblog-services"
OWNER = "anc1996-myblog"
PORTS = {"postgres": (5432,), "redis": (6379,), "mongo": (27017,), "rustfs": (9000, 9001)}


def require(condition, message):
    """Acceptance checks must remain active under Python's optimization flags."""
    if not condition:
        raise ValueError(message)


def inspect_json(kind, names, fields):
    # Project only non-secret metadata: full container inspect includes credentials.
    template = "{" + ",".join(
        f'"{key}":{{{{json {field}}}}}' for key, field in fields.items()
    ) + "}"
    result = subprocess.run(
        ["docker", "--host=unix:///var/run/docker.sock", kind, "inspect",
         "--format", template, *names],
        check=True, capture_output=True, text=True, timeout=15,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def verified_hosts():
    """Fail closed unless all four endpoints belong to this local Compose stack."""
    network_name = f"{PROJECT}_default"
    networks = inspect_json("network", [network_name], {
        "id": ".Id", "labels": ".Labels", "driver": ".Driver",
        "scope": ".Scope", "containers": ".Containers",
    })
    require(len(networks) == 1, "Expected exactly one project network")
    network = networks[0]
    labels = network["labels"] or {}
    require(
        labels.get("com.docker.compose.project") == PROJECT
        and labels.get("com.docker.compose.network") == "default"
        and network["driver"] == "bridge" and network["scope"] == "local",
        "Unexpected Compose project network",
    )
    containers = inspect_json(
        "container", [f"{PROJECT}-{service}-1" for service in PORTS], {
            "id": ".Id", "labels": ".Config.Labels", "running": ".State.Running",
            "ports": ".NetworkSettings.Ports", "bindings": ".HostConfig.PortBindings",
            "networks": ".NetworkSettings.Networks",
        },
    )
    require(len(containers) == len(PORTS), "Missing project containers")
    hosts = {}
    for service, container in zip(PORTS, containers):
        labels = container["labels"] or {}
        require(
            labels.get("com.docker.compose.project") == PROJECT
            and labels.get("com.docker.compose.service") == service
            and labels.get("com.docker.compose.container-number") == "1"
            and labels.get("com.docker.compose.oneoff", "").lower() == "false"
            and container["running"] is True,
            "Unexpected or stopped project container",
        )
        expected_ports = {
            f"{port}/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(port)}]
            for port in PORTS[service]
        }
        for field in ("ports", "bindings"):
            published = {key: value for key, value in (container[field] or {}).items() if value}
            require(published == expected_ports, "Unexpected published service ports")
        attachments = container["networks"] or {}
        require(set(attachments) == {network_name}, "Unexpected container networks")
        attachment = attachments[network_name]
        address = IPv4Address(attachment["IPAddress"])
        require(
            attachment["NetworkID"] == network["id"]
            and not (address.is_unspecified or address.is_loopback or address.is_multicast),
            "Invalid project container IP",
        )
        member = (network["containers"] or {}).get(container["id"], {})
        require(
            member.get("IPv4Address") == f"{address}/{attachment['IPPrefixLen']}",
            "Container IP does not match project network membership",
        )
        hosts[service] = str(address)
    require(len(set(hosts.values())) == len(PORTS), "Duplicate project container IPs")
    return hosts


def write_env(path, values):
    """Write python-dotenv client material without expanding dollar expressions."""
    text = "\n".join(
        key if value is None else
        f"{key}='{str(value).replace(chr(92), chr(92) * 2).replace(chr(39), chr(92) + chr(39))}'"
        for key, value in values.items()
    ) + "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def write_compose_env(path, values):
    """Compose raw env files preserve quotes, dollars and backslashes literally."""
    for value in values.values():
        require(
            not any(character in str(value) for character in "\r\n\0"),
            "Container credentials must not contain CR, LF or NUL",
        )
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)


def prepare(source):
    """准备本地开发环境的服务凭据与 Docker Compose 环境变量。

    接纳已迁移的 POSTGRES_* 格式，也可将授权旧版 MYSQL_* 作为一次性输入平滑转换；
    默认使用 postgres 管理员角色与已授权密码，并为 MongoDB 独立保留 root 身份。
    """
    source_values = dotenv_values(source, interpolate=False)

    # 提取 PostgreSQL 密码：优先支持新键 POSTGRES_PASSWORD，兼顾一次性输入的旧键 MYSQL_PASSWORD
    postgres_password = source_values.get("POSTGRES_PASSWORD") or source_values.get("MYSQL_PASSWORD")
    if not postgres_password:
        raise ValueError("Missing PostgreSQL database password in the reference file")
    postgres_user = source_values.get("POSTGRES_USER", "postgres")

    # 提取 Redis 密码
    redis_password = source_values.get("REDIS_PASSWORD")
    if not redis_password:
        raise ValueError("Missing Redis password in the reference file")

    # 提取并验证 MongoDB 独立凭据：必须保持 root 原值，严禁重写为 postgres
    mongo_user = source_values.get("MONGO_USER") or "root"
    if mongo_user != "root":
        raise ValueError("MongoDB administrator identity must remain root")
    mongo_password = source_values.get("MONGO_PASSWORD") or postgres_password

    # 提取 S3 / RustFS 凭据
    s3_key = source_values.get("S3_ACCESS_KEY_ID") or mongo_user
    s3_secret = source_values.get("S3_SECRET_ACCESS_KEY") or postgres_password

    # 检查所有凭据字符，严禁包含 CR、LF 或 NUL 避免环境变量注入
    all_credentials = (
        postgres_user, postgres_password,
        mongo_user, mongo_password,
        redis_password, s3_key, s3_secret,
    )
    for cred in all_credentials:
        require(
            not any(character in str(cred) for character in "\r\n\0"),
            "Container credentials must not contain CR, LF or NUL",
        )
    require(
        not SECRET_DIR.exists() or not any(SECRET_DIR.iterdir()),
        "prepare requires an empty secrets directory; existing credentials were not changed",
    )
    SECRET_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    SECRET_DIR.chmod(0o700)

    # 集中存储于受限的 admin.env
    values = {
        "POSTGRES_USER": postgres_user,
        "POSTGRES_PASSWORD": postgres_password,
        "MONGO_USER": mongo_user,
        "MONGO_PASSWORD": mongo_password,
        "REDIS_PASSWORD": redis_password,
        "S3_ACCESS_KEY_ID": s3_key,
        "S3_SECRET_ACCESS_KEY": s3_secret,
    }
    write_env(SECRET_DIR / "admin.env", values)

    # 写入 Compose 容器使用的 raw env 文件
    write_compose_env(SECRET_DIR / "postgres.env", {
        "POSTGRES_USER": postgres_user,
        "POSTGRES_PASSWORD": postgres_password,
        "POSTGRES_DB": "postgres",
    })
    write_compose_env(SECRET_DIR / "mongo.env", {
        "MONGO_INITDB_ROOT_USERNAME": mongo_user,
        "MONGO_INITDB_ROOT_PASSWORD": mongo_password,
    })
    write_compose_env(SECRET_DIR / "redis.env", {"REDISCLI_AUTH": redis_password})
    write_compose_env(SECRET_DIR / "rustfs.env", {
        "RUSTFS_ACCESS_KEY": s3_key,
        "RUSTFS_SECRET_KEY": s3_secret,
        "RUSTFS_CONSOLE_ENABLE": "true",
    })
    # Bind-mounted config is readable by Redis's unprivileged container UID;
    # the parent directory remains root-only on the host.
    config = SECRET_DIR / "redis.conf"
    config.write_text(
        "bind 0.0.0.0\nprotected-mode yes\nport 6379\ndatabases 16\n"
        "dir /data\nappendonly yes\nappendfsync everysec\n"
        "maxmemory 128mb\nmaxmemory-policy noeviction\n"
        f"requirepass {json.dumps(redis_password)}\n",
        encoding="utf-8",
    )
    config.chmod(0o644)
    print("Local service credentials prepared; no values displayed")


def credentials():
    """读取管理凭据，兼顾新旧字段键名，绝不输出凭据值。"""
    raw = dotenv_values(SECRET_DIR / "admin.env", interpolate=False)
    values = dict(raw)
    if "POSTGRES_PASSWORD" not in values and "MYSQL_PASSWORD" in values:
        values["POSTGRES_PASSWORD"] = values["MYSQL_PASSWORD"]
    if "POSTGRES_USER" not in values:
        values["POSTGRES_USER"] = values.get("MYSQL_USER", "postgres")
        if values["POSTGRES_USER"] == "root":
            values["POSTGRES_USER"] = "postgres"
    if "MONGO_USER" not in values:
        values["MONGO_USER"] = "root"
    if "MONGO_PASSWORD" not in values:
        values["MONGO_PASSWORD"] = values.get("MYSQL_PASSWORD") or values.get("POSTGRES_PASSWORD", "")
    return values


def postgres_client(values, hosts, dbname="postgres", autocommit=True):
    """创建指定 PostgreSQL 数据库的 psycopg3 连接，绝不泄漏连接字符串。"""
    return psycopg.connect(
        host=hosts["postgres"],
        port=5432,
        user=values["POSTGRES_USER"],
        password=values["POSTGRES_PASSWORD"],
        dbname=dbname,
        connect_timeout=10,
        autocommit=autocommit,
    )


def s3_client(values, hosts, *, anonymous=False):
    # Only verified local endpoints are used; bypass ambient proxy settings.
    return boto3.client(
        "s3", endpoint_url=f"http://{hosts['rustfs']}:9000",
        aws_access_key_id=None if anonymous else values["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=None if anonymous else values["S3_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
        config=Config(
            signature_version=UNSIGNED if anonymous else "s3v4",
            s3={"addressing_style": "path"}, proxies={},
            connect_timeout=10, read_timeout=10,
        ),
    )


def clients(values, hosts):
    pg = postgres_client(values, hosts, dbname="postgres", autocommit=True)
    mongo = MongoClient(
        hosts["mongo"], 27017, username=values["MONGO_USER"],
        password=values["MONGO_PASSWORD"], authSource="admin",
        serverSelectionTimeoutMS=10000, directConnection=True,
    )
    return pg, mongo, s3_client(values, hosts)


def bucket_exists(s3, bucket):
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        # Authorization and transport failures must never mean "create a bucket".
        if exc.response["Error"]["Code"] in ("404", "NoSuchBucket", "NotFound"):
            return False
        raise
    return True


def require_empty_bucket(s3, bucket):
    require(
        not s3.list_objects_v2(Bucket=bucket, MaxKeys=1).get("Contents"),
        "Target bucket contains objects; nothing was deleted",
    )


def check_empty_pg_database(conn, dbname):
    """检查指定 PostgreSQL 数据库内不存在用户表、视图、物化视图、序列或分区表。"""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
            "AND n.nspname NOT LIKE 'pg_toast%' "
            "AND n.nspname NOT LIKE 'pg_temp_%' "
            "AND c.relkind IN ('r', 'v', 'm', 'S', 'p', 'f')"
        )
        count = cursor.fetchone()[0]
        require(count == 0, f"Target PostgreSQL database {dbname} is not empty ({count} user objects)")


def resource_preflight(pg, mongo, s3, values, hosts, *, existing):
    """在执行写入或探针前，全面核查 PostgreSQL 数据库及外部服务的归属与状态。"""
    for name in DBS:
        with pg.cursor() as cursor:
            cursor.execute(
                "SELECT pg_encoding_to_char(encoding), datcollate, datctype "
                "FROM pg_database WHERE datname = %s",
                (name,),
            )
            row = cursor.fetchone()
            require(row is not None or not existing, f"Missing target PostgreSQL database: {name}")
            if row is not None:
                encoding, _, _ = row
                require(encoding == "UTF8", f"Unexpected database encoding for {name}: expected UTF8, got {encoding}")
        if row is not None:
            with postgres_client(values, hosts, dbname=name, autocommit=True) as db_conn:
                check_empty_pg_database(db_conn, name)
        collections = set(mongo[name].list_collection_names())
        if collections:
            require(collections == {"bootstrap_metadata"}, "Unexpected target Mongo collections")
            owner = mongo[name].bootstrap_metadata.find_one({"_id": OWNER})
            require(
                owner is not None and owner.get("project") == OWNER
                and owner.get("purpose") == name,
                "Mongo ownership marker conflict",
            )
        else:
            require(not existing, "Missing Mongo ownership marker")
    for purpose, index in (("dev", 9), ("test", 11)):
        redis = Redis(
            host=hosts["redis"], port=6379, password=values["REDIS_PASSWORD"],
            db=index, socket_timeout=10,
        )
        try:
            marker = redis.get(f"myblog:{purpose}:bootstrap:owner")
            require(
                marker == OWNER.encode() or (marker is None and not existing),
                "Redis ownership marker conflict",
            )
        finally:
            redis.close()
        bucket = f"myblog-{purpose}"
        present = bucket_exists(s3, bucket)
        require(present or not existing, "Missing target S3 bucket")
        if present:
            require_empty_bucket(s3, bucket)


def provision():
    """初始化数据库、元数据标记与私有存储桶，绝不创建业务表或执行应用迁移。"""
    hosts = verified_hosts()
    values = credentials()
    pg, mongo, s3 = clients(values, hosts)
    try:
        resource_preflight(pg, mongo, s3, values, hosts, existing=False)
        for name in DBS:
            with pg.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                if not cursor.fetchone():
                    # 仅在 autocommit 模式下执行，使用 psycopg.sql.Identifier 安全引用库名
                    cursor.execute(
                        sql.SQL("CREATE DATABASE {} WITH ENCODING = 'UTF8'").format(sql.Identifier(name))
                    )
            mongo[name].bootstrap_metadata.update_one(
                {"_id": OWNER},
                {"$setOnInsert": {"project": OWNER, "purpose": name}},
                upsert=True,
            )
        for purpose, index in (("dev", 9), ("test", 11)):
            redis = Redis(
                host=hosts["redis"], port=6379, password=values["REDIS_PASSWORD"],
                db=index, socket_timeout=10,
            )
            key = f"myblog:{purpose}:bootstrap:owner"
            try:
                existing = redis.get(key)
                require(existing in (None, OWNER.encode()), "Redis ownership marker conflict")
                redis.set(key, OWNER, nx=True)
                require(redis.get(key) == OWNER.encode(), "Redis ownership marker conflict")
            finally:
                redis.close()
            bucket = f"myblog-{purpose}"
            if not bucket_exists(s3, bucket):
                s3.create_bucket(Bucket=bucket)
            require_empty_bucket(s3, bucket)
        print("Dev/test resources provisioned; no application tables created")
    finally:
        pg.close()
        mongo.close()
        s3.close()


def activate():
    """在本地服务栈全面通过探针验证后，更新本项目的 .env.local 和 .env.test 客户端配置。

    保留项目无关客户端环境变量，清除旧的 MYSQL_* 与 MINIO_* 遗留键，
    写入 POSTGRES_* 以及独立的 Mongo/Redis/S3 服务凭据。
    """
    verify()
    write_client_env(credentials())


def write_client_env(values):
    """Write client configuration after verification; callers own service validation."""
    for filename, purpose, index in (
        (".env.local", "dev", 9), (".env.test", "test", 11),
    ):
        path = ROOT / filename
        env = dict(dotenv_values(path, interpolate=False)) if path.exists() else {}
        env.update({
            "DJANGO_SECRET_KEY": env.get("DJANGO_SECRET_KEY") or secrets.token_urlsafe(48),
            # PostgreSQL 配置（与 settings/base.py 严格对齐）
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": "5432",
            "POSTGRES_USER": values["POSTGRES_USER"],
            "POSTGRES_PASSWORD": values["POSTGRES_PASSWORD"],
            "POSTGRES_DB": f"myblog_{purpose}",
            "POSTGRES_TEST_DB": "test_myblog",
            # MongoDB 独立凭据：必须使用独立 MONGO_USER/MONGO_PASSWORD，不得重写为 postgres
            "MONGO_HOST": "127.0.0.1",
            "MONGO_PORT": "27017",
            "MONGO_NAME": f"myblog_{purpose}",
            "MONGO_USER": values["MONGO_USER"],
            "MONGO_PASSWORD": values["MONGO_PASSWORD"],
            "MONGO_AUTH_SOURCE": "admin",
            # Redis 配置
            "REDIS_HOST": "127.0.0.1",
            "REDIS_PORT": "6379",
            "REDIS_DB": str(index),
            "REDIS_PASSWORD": values["REDIS_PASSWORD"],
            "REDIS_KEY_PREFIX": f"myblog:{purpose}",
            # RustFS S3 配置
            "S3_ENDPOINT_URL": "http://127.0.0.1:9000",
            "S3_BUCKET": f"myblog-{purpose}",
            "S3_REGION": "us-east-1",
            "S3_ACCESS_KEY_ID": values["S3_ACCESS_KEY_ID"],
            "S3_SECRET_ACCESS_KEY": values["S3_SECRET_ACCESS_KEY"],
        })
        # 移除已废弃的 MySQL 与 MinIO 键
        env = {
            key: value for key, value in env.items()
            if not key.startswith("MYSQL_") and not key.startswith("MINIO_")
        }
        write_env(path, env)
    print("Ignored project .env.local/.env.test now target local WSL services")


def verify_pg_database_probe(dbname, values, hosts):
    """在指定数据库内执行临时表 CRUD、事务回滚和条件唯一索引验证，全部 temporary 不残留。"""
    with postgres_client(values, hosts, dbname=dbname, autocommit=False) as conn:
        with conn.cursor() as cursor:
            # 1. 创建临时表 (仅当前会话可见，连接断开自动丢弃)
            cursor.execute(
                "CREATE TEMPORARY TABLE myblog_probe ("
                "id INT, "
                "key_name TEXT, "
                "value TEXT, "
                "is_active BOOLEAN"
                ")"
            )
            # PostgreSQL also rolls back DDL. Keep the session-local table across
            # the later rollback, while its rows remain transaction-controlled.
            conn.commit()
            # 2. 中文与 Emoji 字符写入与查询验证 (Create & Read)
            cursor.execute(
                "INSERT INTO myblog_probe (id, key_name, value, is_active) "
                "VALUES (%s, %s, %s, %s)",
                (1, "greeting", "WSL 服务验证 🎯", True),
            )
            cursor.execute("SELECT value FROM myblog_probe WHERE id = 1")
            require(cursor.fetchone()[0] == "WSL 服务验证 🎯", "Probe readback failed")

            # 3. 更新与删除验证 (Update & Delete)
            cursor.execute(
                "UPDATE myblog_probe SET value = %s WHERE id = 1",
                ("WSL 更新验证 🚀",),
            )
            cursor.execute("SELECT value FROM myblog_probe WHERE id = 1")
            require(cursor.fetchone()[0] == "WSL 更新验证 🚀", "Probe update failed")

            cursor.execute("DELETE FROM myblog_probe WHERE id = 1")
            cursor.execute("SELECT count(*) FROM myblog_probe WHERE id = 1")
            require(cursor.fetchone()[0] == 0, "Probe delete failed")

            # 4. 事务回滚验证 (Transaction Rollback)
            cursor.execute(
                "INSERT INTO myblog_probe (id, key_name, value, is_active) "
                "VALUES (%s, %s, %s, %s)",
                (99, "tx_probe", "未提交临时数据 🛑", True),
            )
        # 显式回滚事务
        conn.rollback()

        with conn.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM myblog_probe WHERE id = 99")
            require(cursor.fetchone()[0] == 0, "Transaction rollback failed to discard row")

            # 5. 临时条件唯一索引 (Partial Unique Index) 重复冲突验证
            cursor.execute(
                "CREATE UNIQUE INDEX probe_active_key_idx ON myblog_probe (key_name) "
                "WHERE is_active = TRUE"
            )
            cursor.execute(
                "INSERT INTO myblog_probe (id, key_name, value, is_active) "
                "VALUES (%s, %s, %s, %s)",
                (10, "uniq_key", "active_val_1", True),
            )
            # 非激活记录允许相同 key 重复
            cursor.execute(
                "INSERT INTO myblog_probe (id, key_name, value, is_active) "
                "VALUES (%s, %s, %s, %s)",
                (11, "uniq_key", "inactive_val_1", False),
            )
            cursor.execute(
                "INSERT INTO myblog_probe (id, key_name, value, is_active) "
                "VALUES (%s, %s, %s, %s)",
                (12, "uniq_key", "inactive_val_2", False),
            )
            # 激活状态插入重复 key 必须触发唯一约束冲突
            conflict_detected = False
            try:
                with conn.transaction():
                    cursor.execute(
                        "INSERT INTO myblog_probe (id, key_name, value, is_active) "
                        "VALUES (%s, %s, %s, %s)",
                        (13, "uniq_key", "active_val_conflict", True),
                    )
            except UniqueViolation:
                conflict_detected = True
            require(conflict_detected, "Partial unique index did not trigger expected UniqueViolation")

            # 清理临时表
            cursor.execute("DROP TABLE pg_temp.myblog_probe")
        conn.commit()

    with postgres_client(values, hosts, dbname=dbname, autocommit=True) as db_conn:
        check_empty_pg_database(db_conn, dbname)
    print("PostgreSQL", dbname, "temporary CRUD/rollback/partial-index probe OK")


def verify():
    """综合验证 PostgreSQL、MongoDB、Redis 与 RustFS S3 本地服务各项功能探针。"""
    hosts = verified_hosts()
    values = credentials()
    pg, mongo, s3 = clients(values, hosts)
    try:
        resource_preflight(pg, mongo, s3, values, hosts, existing=True)
        with pg.cursor() as cursor:
            cursor.execute("SELECT version(), current_setting('server_version_num')")
            version_str, version_num = cursor.fetchone()
            require(
                int(version_num) >= 180000,
                f"PostgreSQL 18.0 or newer is required (found {version_str.split()[1]})",
            )
            print("PostgreSQL", version_str.split()[1], "server OK")

        for name in DBS:
            verify_pg_database_probe(name, values, hosts)

        mongo.admin.command("ping")
        roles = mongo.admin.command("connectionStatus")["authInfo"]["authenticatedUserRoles"]
        require(any(role["role"] == "root" for role in roles), "Mongo root authentication failed")
        print("MongoDB", mongo.server_info()["version"], "authenticated root OK")
        token = "probe:" + uuid4().hex
        for purpose, index in (("dev", 9), ("test", 11)):
            name = f"myblog_{purpose}"
            collection = mongo[name].bootstrap_metadata
            try:
                collection.insert_one({"_id": token, "probe": True})
                probe = collection.find_one({"_id": token})
                require(probe is not None and probe.get("probe") is True, "Mongo probe readback failed")
            finally:
                collection.delete_one({"_id": token, "probe": True})
            redis = Redis(
                host=hosts["redis"], password=values["REDIS_PASSWORD"],
                db=index, socket_timeout=10,
            )
            key = f"myblog:{purpose}:{token}"
            try:
                require(redis.ping(), "Redis authenticated ping failed")
                require(redis.acl_whoami() == "default", "Unexpected Redis identity")
                require(
                    redis.get(f"myblog:{purpose}:bootstrap:owner") == OWNER.encode(),
                    "Redis ownership marker conflict",
                )
                require(redis.set(key, token, ex=60, nx=True), "Redis probe key already exists")
                require(redis.get(key) == token.encode(), "Redis probe readback failed")
                print("Redis", redis.info("server")["redis_version"], "DB", index, "read/write OK")
            finally:
                try:
                    # Also clean up a SET whose reply was lost, without deleting another value.
                    redis.eval(
                        "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                        "return redis.call('DEL', KEYS[1]) else return 0 end",
                        1, key, token,
                    )
                finally:
                    redis.close()
            bucket = f"myblog-{purpose}"
            try:
                s3.get_bucket_policy(Bucket=bucket)
            except ClientError as exc:
                require(
                    exc.response["Error"]["Code"] in ("NoSuchBucketPolicy", "NoSuchPolicy"),
                    "Could not confirm private bucket policy",
                )
            else:
                raise ValueError("Expected private bucket without policy")
            payload = b"myblog private storage smoke"
            require_empty_bucket(s3, bucket)
            try:
                s3.put_object(Bucket=bucket, Key=token, Body=payload)
                response = s3.get_object(Bucket=bucket, Key=token, Range="bytes=7-13")
                try:
                    require(response["Body"].read() == payload[7:14], "S3 range readback failed")
                    require(
                        response["ResponseMetadata"]["HTTPStatusCode"] == 206,
                        "S3 did not return partial content",
                    )
                finally:
                    response["Body"].close()
                anonymous = s3_client(values, hosts, anonymous=True)
                try:
                    try:
                        response = anonymous.get_object(Bucket=bucket, Key=token)
                    except ClientError as exc:
                        require(
                            exc.response["Error"]["Code"] == "AccessDenied",
                            "Unexpected anonymous S3 response",
                        )
                    else:
                        try:
                            raise ValueError("Anonymous object GET unexpectedly succeeded")
                        finally:
                            response["Body"].close()
                finally:
                    anonymous.close()
            finally:
                # Delete only this run's UUID key, including a PUT whose reply was lost.
                s3.delete_object(Bucket=bucket, Key=token)
            require_empty_bucket(s3, bucket)
            print("RustFS S3", bucket, "private upload/range/read/delete OK")
        # Bypass ambient HTTP proxies so health checks use the verified container.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{hosts['rustfs']}:9000/health", timeout=10) as response:
            require(response.status == 200, "RustFS health check failed")
        print("All local service probes passed; application migrations NOT executed")
    finally:
        pg.close()
        mongo.close()
        s3.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "provision", "verify", "activate"))
    parser.add_argument("--source-env", type=Path)
    args = parser.parse_args()
    try:
        if Path.cwd() != ROOT or not sys.executable.startswith("/root/anaconda3/envs/wagtailblog-test/"):
            raise RuntimeError("Use the authorized WSL cwd and Conda interpreter")
        if args.action == "prepare":
            if not args.source_env:
                parser.error("prepare requires --source-env")
            prepare(args.source_env)
        else:
            globals()[args.action]()
    except Exception as exc:
        # Driver exceptions can contain connection details; output types only.
        print(f"Local service operation failed ({type(exc).__name__}); no credentials logged", file=sys.stderr)
        sys.exit(1)
