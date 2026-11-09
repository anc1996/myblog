"""
专题指南专属筛选器定义与注册 (Guide Listing Filters)。

落实 RFC A-03 规范：
1. 注册 guides.topic 与 guides.difficulty 专属筛选器；
2. 绑定 query_param 与 facet_key，实现同快照精确分桶；
3. 纯基于 PostgreSQL PublishedSearchDocument.extra_data 进行无延迟过滤。
"""

from typing import Any
from django.db.models import QuerySet
from discovery.listing.registry import ListingFilterDefinition, register_listing_filter
from guides.models import GuideTopic


def apply_topic_filter(qs: QuerySet, value: Any) -> QuerySet:
    if not value:
        return qs
    return qs.filter(extra_data__topic_slug=str(value).strip())


def compute_topic_facet(qs: QuerySet) -> list[dict[str, Any]]:
    results = []
    for topic in GuideTopic.objects.all():
        cnt = qs.filter(extra_data__topic_slug=topic.slug).count()
        if cnt > 0:
            results.append({
                "slug": topic.slug,
                "name": topic.name,
                "count": cnt,
            })
    return results


def apply_difficulty_filter(qs: QuerySet, value: Any) -> QuerySet:
    if not value:
        return qs
    return qs.filter(extra_data__difficulty=str(value).strip())


def compute_difficulty_facet(qs: QuerySet) -> list[dict[str, Any]]:
    from guides.models import DIFFICULTY_CHOICES
    results = []
    for code, label in DIFFICULTY_CHOICES:
        cnt = qs.filter(extra_data__difficulty=code).count()
        if cnt > 0:
            results.append({
                "code": code,
                "label": label,
                "count": cnt,
            })
    return results


def register_guide_listing_filters():
    register_listing_filter(ListingFilterDefinition(
        key="guides.topic",
        query_param="topic",
        facet_key="topics",
        apply_filter_func=apply_topic_filter,
        compute_facet_func=compute_topic_facet,
    ))
    register_listing_filter(ListingFilterDefinition(
        key="guides.difficulty",
        query_param="difficulty",
        facet_key="difficulties",
        apply_filter_func=apply_difficulty_filter,
        compute_facet_func=compute_difficulty_facet,
    ))
