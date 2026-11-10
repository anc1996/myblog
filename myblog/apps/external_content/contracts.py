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
    version_id: str  # 版本的唯一标识符（UUID），用于精准索引和检索该不可变快照
    document_uuid: UUID  # 页面全局逻辑唯一标识，页面移动、改名或回收恢复均保持不变
    model_label: str  # 页面模型的类标签（如 "blog.BlogPage"），标识字段归属的模型类型
    field_name: str  # 脱水外置的具体字段名（如 "body" 或 "intro"）
    schema_version: int  # 整体字段数据结构的模式版本号，用于未来格式升级与数据兼容
    block_schema_versions: dict[str, int]  # 各 StreamField 积木块的细粒度版本字典（如 {"code": 2, "rich_text": 1}）
    content_hash: str  # 正文载荷的 SHA-256 哈希值，用于幂等去重与防篡改完整性核验
    payload: Any  # 持久化在 Mongo 中的原始积木 JSON 数据（列表/字典结构），用于反序列化水合
    created_at: datetime  # 该版本的创建时间戳（带时区的 UTC 时间）
    operation_id: str  # 触发本次写操作的追踪 ID（如 Outbox 任务或 Trace ID），用于幂等防重与全链路对账
    extra_metadata: dict[str, Any] = field(default_factory=dict)  # 扩展元数据字典（如字数统计、审计标记等），预留扩展空间
