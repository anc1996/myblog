"""Prepare and verify only this project's local WSL services; never migrate Django."""

import argparse
from ipaddress import IPv4Address
import json
from pathlib import Path
import secrets
import subprocess
import sys
import urllib.request
from uuid import uuid4

import MySQLdb
import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import dotenv_values
from pymongo import MongoClient
from redis import Redis


ROOT = Path("/mnt/f/openclaw/workspace/wagtail/myblog")
SECRET_DIR = Path("/opt/myblog-services/secrets")
DBS = ("myblog_dev", "myblog_test")
PROJECT = "myblog-services"
OWNER = "anc1996-myblog"
PORTS = {"mysql": (3306,), "redis": (6379,), "mongo": (27017,), "rustfs": (9000, 9001)}


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
    source_values = dotenv_values(source, interpolate=False)
    required = ("MYSQL_USER", "MYSQL_PASSWORD", "REDIS_PASSWORD")
    if not all(source_values.get(key) for key in required):
        raise ValueError("Missing service credentials in the reference file")
    if source_values["MYSQL_USER"] != "root":
        raise ValueError("Expected the authorized MySQL root credentials")
    for key in required:
        require(
            not any(character in source_values[key] for character in "\r\n\0"),
            "Container credentials must not contain CR, LF or NUL",
        )
    SECRET_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    SECRET_DIR.chmod(0o700)
    # Mongo had no source auth. Reuse MySQL's administrator identity as requested.
    values = {key: source_values[key] for key in required}
    # The new local S3 service uses the same authorized administrator identity.
    values.update({
        "S3_ACCESS_KEY_ID": values["MYSQL_USER"],
        "S3_SECRET_ACCESS_KEY": values["MYSQL_PASSWORD"],
    })
    write_env(SECRET_DIR / "admin.env", values)
    write_compose_env(SECRET_DIR / "mysql.env", {
        "MYSQL_ROOT_PASSWORD": values["MYSQL_PASSWORD"], "MYSQL_ROOT_HOST": "%",
    })
    write_compose_env(SECRET_DIR / "mongo.env", {
        "MONGO_INITDB_ROOT_USERNAME": values["MYSQL_USER"],
        "MONGO_INITDB_ROOT_PASSWORD": values["MYSQL_PASSWORD"],
    })
    write_compose_env(SECRET_DIR / "redis.env", {"REDISCLI_AUTH": values["REDIS_PASSWORD"]})
    write_compose_env(SECRET_DIR / "rustfs.env", {
        "RUSTFS_ACCESS_KEY": values["S3_ACCESS_KEY_ID"],
        "RUSTFS_SECRET_KEY": values["S3_SECRET_ACCESS_KEY"],
        "RUSTFS_CONSOLE_ENABLE": "true",
    })
    # Bind-mounted config is readable by Redis's unprivileged container UID;
    # the parent directory remains root-only on the host.
    config = SECRET_DIR / "redis.conf"
    config.write_text(
        "bind 0.0.0.0\nprotected-mode yes\nport 6379\ndatabases 16\n"
        "dir /data\nappendonly yes\nappendfsync everysec\n"
        "maxmemory 128mb\nmaxmemory-policy noeviction\n"
        f"requirepass {json.dumps(values['REDIS_PASSWORD'])}\n",
        encoding="utf-8",
    )
    config.chmod(0o644)
    print("Local service credentials prepared; no values displayed")


def credentials():
    return dotenv_values(SECRET_DIR / "admin.env", interpolate=False)


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
    mysql = MySQLdb.connect(
        host=hosts["mysql"], port=3306, user=values["MYSQL_USER"],
        passwd=values["MYSQL_PASSWORD"], charset="utf8mb4",
        connect_timeout=10, autocommit=True,
    )
    mongo = MongoClient(
        hosts["mongo"], 27017, username=values["MYSQL_USER"],
        password=values["MYSQL_PASSWORD"], authSource="admin",
        serverSelectionTimeoutMS=10000, directConnection=True,
    )
    return mysql, mongo, s3_client(values, hosts)


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


def resource_preflight(mysql, mongo, s3, values, hosts, *, existing):
    """Read all target resources before writing any bootstrap or probe data."""
    for name in DBS:
        with mysql.cursor() as cursor:
            cursor.execute(
                "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
                "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s", (name,),
            )
            schema = cursor.fetchone()
            require(schema is not None or not existing, "Missing target MySQL database")
            if schema is not None:
                require(
                    schema == ("utf8mb4", "utf8mb4_0900_ai_ci"),
                    "Unexpected target MySQL database encoding",
                )
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (name,),
            )
            require(cursor.fetchone()[0] == 0, "Target MySQL database is not empty")
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
    hosts = verified_hosts()
    values = credentials()
    mysql, mongo, s3 = clients(values, hosts)
    try:
        resource_preflight(mysql, mongo, s3, values, hosts, existing=False)
        for name in DBS:
            with mysql.cursor() as cursor:
                # Database names are fixed constants, never user-supplied SQL.
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
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
        mysql.close()
        mongo.close()
        s3.close()


def activate():
    """Switch client files only after the local stack has passed verification."""
    verify()
    values = credentials()
    for filename, purpose, index in (
        (".env.local", "dev", 9), (".env.test", "test", 11),
    ):
        path = ROOT / filename
        env = dict(dotenv_values(path, interpolate=False)) if path.exists() else {}
        env.update({
            **values, "DJANGO_SECRET_KEY": env.get("DJANGO_SECRET_KEY") or secrets.token_urlsafe(48),
            "MYSQL_HOST": "127.0.0.1", "MYSQL_PORT": "3306",
            "MYSQL_NAME": f"myblog_{purpose}", "MYSQL_TEST_NAME": "myblog_test",
            "MONGO_HOST": "127.0.0.1", "MONGO_PORT": "27017",
            "MONGO_NAME": f"myblog_{purpose}", "MONGO_USER": values["MYSQL_USER"],
            "MONGO_PASSWORD": values["MYSQL_PASSWORD"], "MONGO_AUTH_SOURCE": "admin",
            "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "6379", "REDIS_DB": str(index),
            "REDIS_KEY_PREFIX": f"myblog:{purpose}",
            "S3_ENDPOINT_URL": "http://127.0.0.1:9000", "S3_BUCKET": f"myblog-{purpose}",
            "S3_REGION": "us-east-1",
        })
        # Remove legacy keys even if the existing administrator file retained them.
        env = {key: value for key, value in env.items() if not key.startswith("MINIO_")}
        write_env(path, env)
    print("Ignored project .env.local/.env.test now target local WSL services")


def verify():
    hosts = verified_hosts()
    values = credentials()
    mysql, mongo, s3 = clients(values, hosts)
    try:
        resource_preflight(mysql, mongo, s3, values, hosts, existing=True)
        with mysql.cursor() as cursor:
            cursor.execute("SELECT VERSION(), @@server_uuid, @@sql_mode, @@default_storage_engine")
            version, server_uuid, mode, engine = cursor.fetchone()
            require(
                tuple(int(x) for x in version.split("-")[0].split(".")[:2]) >= (8, 4),
                "MySQL 8.4 or newer is required",
            )
            require(
                "STRICT_TRANS_TABLES" in mode or "STRICT_ALL_TABLES" in mode,
                "MySQL strict mode is required",
            )
            require(engine == "InnoDB", "InnoDB is required")
            print("MySQL", version, "UUID", server_uuid, "strict/InnoDB OK")
            for name in DBS:
                cursor.execute(
                    "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
                    "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s", (name,),
                )
                require(
                    cursor.fetchone() == ("utf8mb4", "utf8mb4_0900_ai_ci"),
                    "Unexpected MySQL database encoding",
                )
                cursor.execute(
                    "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (name,),
                )
                require(cursor.fetchone()[0] == 0, "Target MySQL database is not empty")
            # Connection-local TEMPORARY table tests write/read without app schema.
            cursor.execute("USE myblog_test")
            try:
                cursor.execute("CREATE TEMPORARY TABLE myblog_probe (value VARCHAR(64))")
                cursor.execute("INSERT INTO myblog_probe VALUES (%s)", ("WSL 服务验证 🎯",))
                cursor.execute("SELECT value FROM myblog_probe")
                require(cursor.fetchone()[0] == "WSL 服务验证 🎯", "MySQL probe readback failed")
            finally:
                cursor.execute("DROP TEMPORARY TABLE IF EXISTS myblog_probe")
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
        mysql.close()
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
