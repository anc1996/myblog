"""
通用多页面 Markdown 导入数据合同与适配器协议。

本模块为纯 Python 数据结构与 Protocol，导入时绝对不触发 Django ORM 或外部网络 IO。
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ImportFieldDef:
    """向本地客户端或后台界面描述某个页面类型的可填字段。"""
    name: str
    label: str
    field_type: str  # "string" | "text" | "date" | "integer" | "select" | "url" | "tags" | "image_ref"
    required: bool = False
    default: Any = None
    choices: tuple[tuple[str, str], ...] = ()
    help_text: str = ""


@dataclass(frozen=True)
class ImportIssue:
    """预检、解析或发布门禁提示（不混用阻断错误与仅发布前提醒）。"""
    code: str
    message: str
    field: str = ""
    line: int | None = None
    severity: str = "error"  # "error" | "warning" | "publish_blocker"


@dataclass(frozen=True)
class ImportMediaAssetRef:
    """Markdown 或 Front Matter 中提取出的受控本地媒体引用清单项。"""
    asset_key: str
    source_path: str
    media_type: str  # "image" | "document"
    alt: str = ""
    title: str = ""
    context: str = "markdown"  # "markdown" | "cover" | "step" | "attachment"


@dataclass(frozen=True)
class ImportSchema:
    """某个已注册页面类型暴露给客户端的表单描述与能力清单。"""
    type_key: str
    label: str
    model_label: str
    schema_version: int
    fields: tuple[ImportFieldDef, ...]
    allowed_parent_models: tuple[str, ...]
    template_markdown: str = ""


@dataclass(frozen=True)
class DraftSpec:
    """经过适配器规范化后的页面草稿中间规范（尚未落库）。"""
    type_key: str
    title: str
    slug_hint: str
    authored_on: date
    summary_text: str = ""
    tags: tuple[str, ...] = ()
    extra_fields: dict[str, Any] = field(default_factory=dict)
    stream_blocks: list[dict[str, Any]] = field(default_factory=list)
    media_refs: tuple[ImportMediaAssetRef, ...] = ()
    issues: tuple[ImportIssue, ...] = ()
    title_source: str = "unknown"  # "yaml" | "h1" | "ai" | "filename" | "fallback" | "unknown"


@runtime_checkable
class MarkdownImportAdapterContract(Protocol):
    """每个可导入页面类型必须实现的适配器协议。"""
    type_key: str
    model_label: str
    schema_version: int

    def describe(self, actor: Any = None, parent: Any = None) -> ImportSchema: ...

    def normalize(
        self,
        parsed_doc: dict[str, Any],
        edits: dict[str, Any],
        context: dict[str, Any],
    ) -> DraftSpec: ...

    def build(
        self,
        draft_spec: DraftSpec,
        resolved_assets: dict[str, Any],
    ) -> Any: ...

    def publish_warnings(self, draft_spec: DraftSpec) -> list[ImportIssue]: ...
