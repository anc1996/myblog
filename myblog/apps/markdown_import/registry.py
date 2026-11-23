"""
页面导入适配器注册表 (ImportAdapter Registry)。

参照 discovery/registry.py 模式，各业务 App 在 ready() 时主动注册自身导入规则；
公共导入层与客户端自动获取全部已注册页面类型。
"""

from typing import Any
from .contracts import MarkdownImportAdapterContract

_IMPORT_ADAPTERS: dict[str, MarkdownImportAdapterContract] = {}
_REQUIRED_METHODS = ("describe", "normalize", "build", "publish_warnings")


def register_import_adapter(adapter: Any) -> None:
    """注册页面导入适配器；拒绝空键、冲突键及未完整实现协议的对象。"""
    type_key = str(getattr(adapter, "type_key", "") or "").strip()
    if not type_key:
        raise ValueError(f"ImportAdapter 必须声明非空 type_key: {adapter!r}")
    if type_key in _IMPORT_ADAPTERS:
        raise ValueError(f"ImportAdapter type_key 重复注册: '{type_key}'")
    model_label = str(getattr(adapter, "model_label", "") or "").strip()
    if not model_label or "." not in model_label:
        raise ValueError(f"ImportAdapter '{type_key}' 必须声明合法 model_label（如 'blog.BlogPage'）")
    for method in _REQUIRED_METHODS:
        if not callable(getattr(adapter, method, None)):
            raise TypeError(f"ImportAdapter '{type_key}' 缺少必需方法: {method}()")
    _IMPORT_ADAPTERS[type_key] = adapter


def get_import_adapter(type_key: str) -> MarkdownImportAdapterContract | None:
    """按 type_key 读取已注册的导入适配器。"""
    return _IMPORT_ADAPTERS.get(str(type_key or "").strip())


def list_import_adapters() -> list[MarkdownImportAdapterContract]:
    """按注册顺序返回所有已注册的导入适配器列表。"""
    return list(_IMPORT_ADAPTERS.values())


def clear_import_adapters() -> None:
    """清空导入适配器注册表（仅限单元测试重置使用）。"""
    _IMPORT_ADAPTERS.clear()
