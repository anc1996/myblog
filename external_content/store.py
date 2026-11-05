"""
MongoDB 不可变正文版本存储层 (MongoContentStore)。

负责正文 (intro / body) 独立版本快照的持久化与读取：
1. 每个字段版本完全独立保存为不可变对象，绝不修改已有文档；
2. 每次写入根据 payload 自动计算 SHA-256 摘要 content_hash；
3. 幂等控制：同一 (operation_id, field_name) 重复调用，内容一致则幂等返回，内容冲突则抛出 IdempotencyConflictError；
4. 读取时必须根据 Revision 记录的准确 version_id 和 expected_hash 进行双重校验。
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from django.conf import settings

from external_content.contracts import (
    FieldVersionPointer,
    FieldVersionPayload,
    VersionNotFoundError,
    ContentHashMismatchError,
    IdempotencyConflictError,
)

logger = logging.getLogger(__name__)


def calculate_payload_hash(payload: Any) -> str:
    """计算正文负载的确定性 SHA-256 摘要哈希"""
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class MongoContentStore:
    """MongoDB 字段版本只读/只增存储适配器"""

    _shared_client = None
    _ensured_dbs = set()

    def __init__(self, db_client=None, db_name=None):
        self._custom_client = db_client
        self._db_name = db_name
        self._db = None

    def close(self):
        """安全关闭底层 MongoClient 连接"""
        if self._custom_client is not None:
            try:
                self._custom_client.close()
            except Exception:
                pass
            self._custom_client = None
        self._db = None

    def _get_db(self):
        if self._db is None:
            if self._custom_client is not None:
                client = self._custom_client
            else:
                if MongoContentStore._shared_client is None:
                    cfg = getattr(settings, "MONGO_SETTINGS", {})
                    MongoContentStore._shared_client = MongoClient(
                        cfg.get("HOST", "127.0.0.1"),
                        cfg.get("PORT", 27017),
                        username=cfg.get("USER", "root"),
                        password=cfg.get("PASSWORD", ""),
                        authSource=cfg.get("AUTH_SOURCE", "admin"),
                        serverSelectionTimeoutMS=3000,
                    )
                client = MongoContentStore._shared_client

            cfg = getattr(settings, "MONGO_SETTINGS", {})
            db_name = self._db_name or cfg.get("DATABASE", "myblog_dev")
            self._db = client[db_name]
            if db_name not in MongoContentStore._ensured_dbs:
                self._ensure_indexes()
                MongoContentStore._ensured_dbs.add(db_name)
        return self._db

    def _ensure_indexes(self):
        try:
            col = self._db["content_field_versions"]
            col.create_index(
                [("document_uuid", ASCENDING), ("field_name", ASCENDING), ("version_id", ASCENDING)],
                unique=True,
            )
            col.create_index(
                [("operation_id", ASCENDING), ("field_name", ASCENDING)],
                unique=True,
                sparse=True,
            )
            col.create_index([("created_at", ASCENDING)])
        except Exception as exc:
            logger.warning("Failed to ensure MongoDB indexes: %s", exc)

    def save_field_version(
        self,
        document_uuid: UUID | str,
        field_name: str,
        payload: Any,
        model_label: str,
        operation_id: str,
        version_id: str | None = None,
        schema_version: int = 1,
        block_schema_versions: dict[str, int] | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> FieldVersionPointer:
        """
        保存新的不可变字段版本快照，支持幂等重放。
        """
        db = self._get_db()
        col = db["content_field_versions"]
        doc_uuid_str = str(document_uuid)
        content_hash = calculate_payload_hash(payload)

        # 1. 幂等检查：同 (operation_id, field_name) 是否已存在
        if operation_id:
            existing = col.find_one({"operation_id": operation_id, "field_name": field_name})
            if existing:
                if existing["content_hash"] == content_hash and existing["document_uuid"] == doc_uuid_str:
                    logger.info("Idempotent replay for operation %s, field %s", operation_id, field_name)
                    return FieldVersionPointer(
                        document_uuid=UUID(doc_uuid_str),
                        field_name=field_name,
                        version_id=existing["version_id"],
                        schema_version=existing["schema_version"],
                        content_hash=content_hash,
                    )
                else:
                    raise IdempotencyConflictError(
                        f"Operation {operation_id} for field {field_name} already exists with different payload hash"
                    )

        # 2. 生成新版本 ID
        if not version_id:
            import uuid
            version_id = f"v_{uuid.uuid4().hex[:16]}"

        record = {
            "version_id": version_id,
            "document_uuid": doc_uuid_str,
            "model_label": model_label,
            "field_name": field_name,
            "schema_version": schema_version,
            "block_schema_versions": block_schema_versions or {},
            "content_hash": content_hash,
            "payload": payload,
            "created_at": datetime.now(timezone.utc),
            "operation_id": operation_id,
            "extra_metadata": extra_metadata or {},
        }

        try:
            col.insert_one(record)
        except DuplicateKeyError as exc:
            if operation_id:
                existing_dup = col.find_one({"operation_id": operation_id, "field_name": field_name})
                if existing_dup and existing_dup["content_hash"] == content_hash:
                    return FieldVersionPointer(
                        document_uuid=UUID(doc_uuid_str),
                        field_name=field_name,
                        version_id=existing_dup["version_id"],
                        schema_version=existing_dup["schema_version"],
                        content_hash=content_hash,
                    )
            raise IdempotencyConflictError(f"Duplicate key conflict in MongoDB: {exc}")

        return FieldVersionPointer(
            document_uuid=UUID(doc_uuid_str),
            field_name=field_name,
            version_id=version_id,
            schema_version=schema_version,
            content_hash=content_hash,
        )

    def get_field_version(
        self,
        document_uuid: UUID | str,
        field_name: str,
        version_id: str,
        expected_hash: str | None = None,
    ) -> FieldVersionPayload:
        """
        按精准版本 ID 获取字段快照，并校验哈希完整性。
        """
        db = self._get_db()
        col = db["content_field_versions"]
        doc_uuid_str = str(document_uuid)

        record = col.find_one({
            "document_uuid": doc_uuid_str,
            "field_name": field_name,
            "version_id": version_id,
        })
        if not record:
            raise VersionNotFoundError(
                f"Field version not found: document={doc_uuid_str}, field={field_name}, version={version_id}"
            )

        if expected_hash and record["content_hash"] != expected_hash:
            raise ContentHashMismatchError(
                f"Content hash mismatch for version {version_id}: expected {expected_hash}, got {record['content_hash']}"
            )

        return FieldVersionPayload(
            version_id=record["version_id"],
            document_uuid=UUID(record["document_uuid"]),
            model_label=record["model_label"],
            field_name=record["field_name"],
            schema_version=record["schema_version"],
            block_schema_versions=record.get("block_schema_versions", {}),
            content_hash=record["content_hash"],
            payload=record["payload"],
            created_at=record["created_at"],
            operation_id=record.get("operation_id", ""),
            extra_metadata=record.get("extra_metadata", {}),
        )
