"""
全站内容发现分桶统计与年月归档服务 (Facets & Archive Groups)。

落实契约：
1. 在单次只读快照 (REPEATABLE READ) 内，从统一的合法结果集合中计算精确统计；
2. 支持类型聚合、标签聚合与年份聚合；
3. 仅统计当前授权且发布的内容，杜绝泄漏未发布草稿或回收站信息。
"""

from typing import Any
from django.db.models import Count
from discovery.models import PublishedSearchTag


def compute_facets(qs, requested_facets: list[str]) -> dict[str, list[dict[str, Any]]]:
    """计算请求维度的精确分桶统计"""
    facets_result: dict[str, list[dict[str, Any]]] = {}

    if not requested_facets:
        return facets_result

    # 1. 页面类型聚合
    if "types" in requested_facets:
        type_counts = (
            qs.values("type_key")
            .annotate(count=Count("id"))
            .order_by("-count", "type_key")
        )
        facets_result["types"] = [
            {"key": item["type_key"], "count": item["count"]}
            for item in type_counts
        ]

    # 2. 标签聚合 (COUNT DISTINCT document_id)
    if "tags" in requested_facets:
        tag_counts = (
            PublishedSearchTag.objects.filter(document__in=qs)
            .values("tag_id", "tag_name", "tag_slug")
            .annotate(count=Count("document_id", distinct=True))
            .order_by("-count", "tag_name")
        )
        facets_result["tags"] = [
            {
                "id": item["tag_id"],
                "name": item["tag_name"],
                "slug": item["tag_slug"],
                "count": item["count"],
            }
            for item in tag_counts
        ]

    # 3. 年份聚合
    if "years" in requested_facets:
        year_counts = (
            qs.values("authored_on__year")
            .annotate(count=Count("id"))
            .order_by("-authored_on__year")
        )
        facets_result["years"] = [
            {"year": item["authored_on__year"], "count": item["count"]}
            for item in year_counts
            if item["authored_on__year"] is not None
        ]

    return facets_result


def compute_archive_groups(qs) -> list[dict[str, Any]]:
    """生成年月归档统计列表"""
    archive_rows = (
        qs.values("authored_on__year", "authored_on__month")
        .annotate(count=Count("id"))
        .order_by("-authored_on__year", "-authored_on__month")
    )
    return [
        {
            "year": r["authored_on__year"],
            "month": r["authored_on__month"],
            "count": r["count"],
        }
        for r in archive_rows
        if r["authored_on__year"] is not None and r["authored_on__month"] is not None
    ]
