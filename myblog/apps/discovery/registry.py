"""
页面类型适配器注册表 (TypeAdapter Registry)。

严格解耦契约：
1. discovery 模块严禁直接反向导入 blog.models 或 guide.models；
2. 业务页面类型通过实现 TypeAdapterContract 并在各自 App 的 ready() 中主动注册；
3. 注册表支持动态查询与遍历，为发布投影与全站搜索提供统一数据源。
"""

import logging
from typing import Any
from discovery.contracts import AnalyticsCapabilities, EntryAnalyticsSpec, TypeAdapterContract

logger = logging.getLogger(__name__)

_ADAPTER_REGISTRY: dict[str, TypeAdapterContract] = {}
_ENTRY_ANALYTICS_REGISTRY: dict[str, EntryAnalyticsSpec] = {}


def register_type_adapter(adapter: Any) -> None:
    """注册页面类型适配器"""
    type_key = getattr(adapter, "type_key", None)
    if not type_key:
        raise ValueError(f"TypeAdapter must define a non-empty 'type_key': {adapter}")
    _ADAPTER_REGISTRY[type_key] = adapter
    logger.debug("Registered TypeAdapter for '%s' (%s)", type_key, getattr(adapter, "model_label", ""))


def get_type_adapter(type_key: str) -> TypeAdapterContract | None:
    """根据 type_key 获取已注册的适配器"""
    return _ADAPTER_REGISTRY.get(type_key)


def list_type_adapters() -> list[TypeAdapterContract]:
    """获取所有已注册的适配器列表"""
    return list(_ADAPTER_REGISTRY.values())


def get_feed_enabled_type_keys() -> list[str]:
    """获取所有开启了 RSS/Atom 订阅能力的页面类型 key 列表"""
    keys = []
    for k, adapter in _ADAPTER_REGISTRY.items():
        caps = getattr(adapter, "capabilities", None)
        if caps and getattr(caps, "feed_enabled", False):
            keys.append(k)
    return keys


def get_archive_enabled_type_keys() -> list[str]:
    """获取所有开启了时间线归档能力的页面类型 key 列表"""
    keys = []
    for k, adapter in _ADAPTER_REGISTRY.items():
        caps = getattr(adapter, "capabilities", None)
        if caps and getattr(caps, "archive_enabled", False):
            keys.append(k)
    return keys


def get_adapter_by_public_type_slug(slug: str) -> TypeAdapterContract | None:
    """根据公开对外暴露的类型别名 (public_type_slug) 查找适配器"""
    for adapter in _ADAPTER_REGISTRY.values():
        caps = getattr(adapter, "capabilities", None)
        if caps and getattr(caps, "public_type_slug", None) == slug:
            return adapter
    return None


def get_public_type_slug_map() -> dict[str, str]:
    """获取 public_type_slug 到内部 type_key 的映射字典"""
    mapping = {}
    for k, adapter in _ADAPTER_REGISTRY.items():
        caps = getattr(adapter, "capabilities", None)
        if caps and getattr(caps, "public_type_slug", None):
            mapping[caps.public_type_slug] = k
    return mapping


def get_type_key_to_public_slug_map() -> dict[str, str]:
    """获取内部 type_key 到公开 public_type_slug 的映射字典"""
    mapping = {}
    for k, adapter in _ADAPTER_REGISTRY.items():
        caps = getattr(adapter, "capabilities", None)
        if caps and getattr(caps, "public_type_slug", None):
            mapping[k] = caps.public_type_slug
    return mapping


def is_author_discovery_enabled(adapter: Any) -> bool:
    """严格判定适配器是否显式声明接入全站作者专栏与按作者发现维度。"""
    if adapter is None:
        return False
    caps = getattr(adapter, "capabilities", None)
    if caps is None:
        return False
    dims = getattr(caps, "supported_dimensions", ()) or ()
    return "author" in dims


def get_author_enabled_type_keys() -> list[str]:
    """获取所有显式声明支持 'author' 维度的页面类型 type_key 列表。"""
    return [
        k for k, adapter in _ADAPTER_REGISTRY.items()
        if is_author_discovery_enabled(adapter)
    ]


def get_analytics_capabilities(type_key: str) -> AnalyticsCapabilities | None:
    """根据 type_key 获取页面类型适配器声明的数据分析能力"""
    adapter = _ADAPTER_REGISTRY.get(type_key)
    if adapter is None:
        return None
    caps = getattr(adapter, "analytics_capabilities", None)
    return caps if isinstance(caps, AnalyticsCapabilities) else None


def list_analytics_adapters() -> list[TypeAdapterContract]:
    """获取所有声明了 analytics_capabilities 的页面类型适配器列表"""
    return [
        adapter
        for adapter in _ADAPTER_REGISTRY.values()
        if getattr(adapter, "analytics_capabilities", None) is not None
    ]


def register_entry_analytics(spec: EntryAnalyticsSpec) -> None:
    """注册聚合入口（如首页、作者专栏、站内搜索）的数据分析能力声明"""
    entry_kind = getattr(spec, "entry_kind", None)
    if not entry_kind:
        raise ValueError(f"EntryAnalyticsSpec must define a non-empty 'entry_kind': {spec}")
    _ENTRY_ANALYTICS_REGISTRY[entry_kind] = spec
    logger.debug("Registered EntryAnalyticsSpec for '%s' (%s)", entry_kind, getattr(spec, "model_label", ""))


def get_entry_analytics(entry_kind: str) -> EntryAnalyticsSpec | None:
    """根据 entry_kind 获取已注册的聚合入口分析声明"""
    return _ENTRY_ANALYTICS_REGISTRY.get(entry_kind)


def list_entry_analytics() -> list[EntryAnalyticsSpec]:
    """获取所有已注册的聚合入口分析声明列表"""
    return list(_ENTRY_ANALYTICS_REGISTRY.values())


def clear_adapters() -> None:
    """清空适配器注册表（仅供测试套件重置使用）"""
    _ADAPTER_REGISTRY.clear()
    _ENTRY_ANALYTICS_REGISTRY.clear()
