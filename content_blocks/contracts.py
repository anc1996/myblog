"""
全站公共内容积木（Block）契约接口。

所有 StreamField 基本 Block 统一遵守本契约：
1. validate(value, context): 校验数据结构与业务边界；
2. render(value, context): 渲染为安全的 HTML 表现；
3. extract_text(value): 提取供搜索倒排索引使用的纯文本；
4. extract_media_refs(value, path): 提取引用的媒体资源 ID 与路径，供 MEDIA-REF-01 保护索引使用。

本模块为纯接口契约与数据结构，导入时绝对无外部数据库或网络 IO。
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True)
class MediaReference:
    """从内容块中提取的媒体引用记录"""
    asset_id: UUID | str
    asset_type: str  # 'image' | 'document' | 'audio' | 'video'
    block_path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BlockValidationResult:
    """内容块校验结果"""
    is_valid: bool
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BlockRenderResult:
    """内容块渲染结果"""
    html: str
    is_safe: bool = True
    extra_media_tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BlockTextExtraction:
    """供全文搜索提取的文本段落"""
    plain_text: str
    logical_segments: list[str] = field(default_factory=list)
    is_sensitive: bool = False


@runtime_checkable
class BlockContract(Protocol):
    """公共 Block 必须满足的协议规范"""
    type_name: str
    schema_version: int

    def validate(self, value: Any, context: Any = None) -> BlockValidationResult:
        """校验块值合法性"""
        ...

    def render(self, value: Any, context: Any = None) -> BlockRenderResult:
        """生成安全渲染结果"""
        ...

    def extract_text(self, value: Any) -> BlockTextExtraction:
        """提取可搜索纯文本"""
        ...

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        """提取引用的所有媒体资源"""
        ...
