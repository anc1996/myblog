"""
全站通用多页面 Markdown 导入框架 (markdown_import)。

纯接口与注册解耦设计：
- 导入引擎只依赖 contracts / registry 协议，不反向硬编码 BlogPage / GuidePage / ProjectPage；
- 业务页面类型只需注册对应的 MarkdownImportAdapterContract，即可被客户端和 API 统一识别。
"""

from .contracts import (
    DraftSpec,
    ImportFieldDef,
    ImportIssue,
    ImportMediaAssetRef,
    ImportSchema,
    MarkdownImportAdapterContract,
)
from .registry import (
    clear_import_adapters,
    get_import_adapter,
    list_import_adapters,
    register_import_adapter,
)

__all__ = [
    "DraftSpec",
    "ImportFieldDef",
    "ImportIssue",
    "ImportMediaAssetRef",
    "ImportSchema",
    "MarkdownImportAdapterContract",
    "clear_import_adapters",
    "get_import_adapter",
    "list_import_adapters",
    "register_import_adapter",
]
