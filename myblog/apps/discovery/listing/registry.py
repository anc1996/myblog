"""
专属筛选器与动态分桶注册表 (Listing Filter Registry)。

落实 RFC A-03 规范：
1. 允许业务应用注册模型专属筛选条件（如 Guide 专栏专属的主题 Topic 和难度 Difficulty）；
2. ListingFilterDefinition 声明 key, query_param, facet_key 及过滤与分桶回调；
3. 消除公共 discovery 模块对 blog/guides/portfolio 等具体业务 App 的反向静态依赖。
"""

import logging
from typing import Any, Callable
from django.db.models import QuerySet

logger = logging.getLogger(__name__)


class ListingFilterDefinition:
    """单个专属筛选器的定义与处理器契约"""
    def __init__(
        self,
        key: str,
        query_param: str,
        facet_key: str | None = None,
        apply_filter_func: Callable[[QuerySet, Any], QuerySet] | None = None,
        compute_facet_func: Callable[[QuerySet], list[dict[str, Any]]] | None = None,
    ):
        self.key = key
        self.query_param = query_param
        self.facet_key = facet_key
        self._apply_filter_func = apply_filter_func
        self._compute_facet_func = compute_facet_func

    def apply_filter(self, qs: QuerySet, value: Any) -> QuerySet:
        if self._apply_filter_func:
            return self._apply_filter_func(qs, value)
        return qs

    def compute_facet(self, qs: QuerySet) -> list[dict[str, Any]]:
        if self._compute_facet_func:
            return self._compute_facet_func(qs)
        return []


_FILTER_REGISTRY: dict[str, ListingFilterDefinition] = {}
_FACET_KEY_MAP: dict[str, ListingFilterDefinition] = {}


def register_listing_filter(definition: ListingFilterDefinition) -> None:
    """注册专属筛选器定义"""
    _FILTER_REGISTRY[definition.key] = definition
    if definition.facet_key:
        _FACET_KEY_MAP[definition.facet_key] = definition


def get_listing_filter(key: str) -> ListingFilterDefinition | None:
    """按 filter key 获取专属筛选器"""
    return _FILTER_REGISTRY.get(key)


def get_listing_filter_by_facet_key(facet_key: str) -> ListingFilterDefinition | None:
    """按 facet key 获取对应的专属筛选器定义"""
    return _FACET_KEY_MAP.get(facet_key)
