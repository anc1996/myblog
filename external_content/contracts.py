"""
MongoDB 外置字段（intro / body）版本引用与存储契约。

落实 PostgreSQL Wagtail Revision 与 MongoDB 不可变版本的绑定规则：
1. 每个外置字段独立保存不可变版本快照；
2. Revision 存储精确的 FieldVersionPointer；
3. 读取时按 Revision 中的版本 ID 与内容摘要（content_hash）校验，
   绝不通过 Mongo 自行推选“最新版本”，防止草稿泄露。

本模块为纯接口契约与数据结构，导入时绝对无外部数据库或网络 IO。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


class ExternalContentError(Exception):
    """外置内容基础异常"""


class VersionNotFoundError(ExternalContentError):
    """请求的不可变字段版本在外部存储中不存在"""


class ContentHashMismatchError(ExternalContentError):
    """外部存储内容摘要与 Revision 登记的 content_hash 不一致"""


class SchemaVersionUnsupportedError(ExternalContentError):
    """数据 schema 版本过旧且无兼容转换规则"""


class IdempotencyConflictError(ExternalContentError):
    """相同的 operation_id 提交了不一致的字段负载内容"""


@dataclass(frozen=True)
class FieldVersionPointer:
    """记录在 PostgreSQL Wagtail Revision 中的版本指针"""
    document_uuid: UUID
    field_name: str  # 'intro' | 'body'
    version_id: str
    schema_version: int
    content_hash: str  # SHA-256


@dataclass(frozen=True)
class FieldVersionPayload:
    """持久化在 MongoDB 中的完整不可变字段版本对象"""
    version_id: str
    document_uuid: UUID
    model_label: str
    field_name: str
    schema_version: int
    block_schema_versions: dict[str, int]
    content_hash: str
    payload: Any
    created_at: datetime
    operation_id: str
    extra_metadata: dict[str, Any] = field(default_factory=dict)
