"""
页面类型适配器注册表 (TypeAdapter Registry)。

严格解耦契约：
1. discovery 模块严禁直接反向导入 blog.models 或 guide.models；
2. 业务页面类型通过实现 TypeAdapterContract 并在各自 App 的 ready() 中主动注册；
3. 注册表支持动态查询与遍历，为发布投影与全站搜索提供统一数据源。
"""

import logging
from typing import Any
from discovery.contracts import TypeAdapterContract

logger = logging.getLogger(__name__)

_ADAPTER_REGISTRY: dict[str, TypeAdapterContract] = {}


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


def clear_adapters() -> None:
    """清空适配器注册表（仅供测试套件重置使用）"""
    _ADAPTER_REGISTRY.clear()
