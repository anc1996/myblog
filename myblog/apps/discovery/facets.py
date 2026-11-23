"""
全站内容发现分桶统计与年月归档服务 (Facets & Archive Groups)。

落实契约：
1. 在单次只读快照 (REPEATABLE READ) 内，从统一的合法结果集合中计算精确统计；
2. 支持类型聚合、标签聚合与年份聚合；
3. 仅统计当前授权且发布的内容，杜绝泄漏未发布草稿或回收站信息。
"""

from typing import Any
from django.db.models import Count
from discovery.models import PublishedSearchAuthor, PublishedSearchTag
from people.author_identity import roles_for_filter_key
from people.models import Person


NAVIGATION_TYPE_KEYS = ("blog_index", "guide_index", "portfolio_index", "tag_index")

TYPE_DISPLAY_LABELS = {
    "blog_page": "博客文章",
    "guide": "专题指南",
    "project": "开源项目",
    "navigation": "栏目导航页",
    "blog_index": "博客专栏页",
    "guide_index": "指南专栏页",
    "portfolio_index": "作品集栏目页",
    "tag_index": "标签大厅页",
}


def get_type_display_label(type_key: str) -> str:
    """获取页面类型的友好中文显示名称"""
    return TYPE_DISPLAY_LABELS.get(type_key, type_key)


def _tag_name_priority(name: str, count: int) -> tuple[int, int, int, str]:
    """
    大小写展示名称优先级排序键：
    优先选择驼峰/首字母大写的规范形式（如 PostgreSQL > postgresql，Django > django）。
    """
    has_upper = any(c.isupper() for c in name)
    has_lower = any(c.islower() for c in name)
    starts_upper = bool(name and name[0].isupper())
    if starts_upper and has_lower:
        tier = 3
    elif has_upper and has_lower:
        tier = 2
    elif has_upper:
        tier = 1
    else:
        tier = 0
    return (tier, 1 if starts_upper else 0, count, name)


def compute_facets(
    qs,
    requested_facets: list[str],
    *,
    active_author_role: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
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
        type_items = []
        nav_count = 0
        for item in type_counts:
            tk = item["type_key"]
            cnt = item["count"]
            if tk.endswith("_index"):
                nav_count += cnt
            type_items.append({
                "key": tk,
                "label": get_type_display_label(tk),
                "count": cnt,
            })
        if nav_count > 0:
            type_items.append({
                "key": "navigation",
                "label": get_type_display_label("navigation"),
                "count": nav_count,
            })
        facets_result["types"] = type_items

    # 2. 标签聚合 (COUNT DISTINCT document_id)
    # 按小写标签名去重合并，优先保留首字母大写或驼峰展示名称，累加 count 并记录所有 tag_ids
    if "tags" in requested_facets:
        tag_counts = (
            PublishedSearchTag.objects.filter(document__in=qs)
            .values("tag_id", "tag_name", "tag_slug")
            .annotate(count=Count("document_id", distinct=True))
            .order_by("-count", "tag_name")
        )
        merged_tags: dict[str, dict[str, Any]] = {}
        for item in tag_counts:
            raw_name = (item["tag_name"] or "").strip()
            norm_key = raw_name.lower() or f"id:{item['tag_id']}"
            tid = item["tag_id"]
            slug = item["tag_slug"] or ""
            cnt = int(item["count"] or 0)
            prio = _tag_name_priority(raw_name, cnt)

            if norm_key not in merged_tags:
                merged_tags[norm_key] = {
                    "id": tid,
                    "tag_ids": [tid],
                    "name": raw_name,
                    "slug": slug,
                    "slugs": [slug] if slug else [],
                    "count": cnt,
                    "_best_prio": prio,
                }
            else:
                entry = merged_tags[norm_key]
                if tid not in entry["tag_ids"]:
                    entry["tag_ids"].append(tid)
                if slug and slug not in entry["slugs"]:
                    entry["slugs"].append(slug)
                entry["count"] += cnt
                if prio > entry["_best_prio"]:
                    entry["_best_prio"] = prio
                    entry["id"] = tid
                    entry["name"] = raw_name
                    if slug:
                        entry["slug"] = slug

        normalized_list = []
        for entry in merged_tags.values():
            entry.pop("_best_prio", None)
            normalized_list.append(entry)
        normalized_list.sort(key=lambda x: (-x["count"], x["name"].lower(), x["id"]))
        facets_result["tags"] = normalized_list

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

    # 4. 作者聚合 (按规范 person_id/author_slug 去重计 COUNT DISTINCT document_id，再取当前 Person.name)
    if "authors" in requested_facets:
        author_qs = PublishedSearchAuthor.objects.filter(document__in=qs)
        if active_author_role:
            role_codes = roles_for_filter_key(active_author_role)
            if role_codes:
                author_qs = author_qs.filter(role__in=role_codes)
            else:
                author_qs = author_qs.none()

        raw_author_counts = list(
            author_qs.values("person_id", "author_slug")
            .annotate(count=Count("document_id", distinct=True))
            .filter(count__gte=1)
        )
        person_ids = [int(r["person_id"]) for r in raw_author_counts]
        persons_by_id = {
            p["id"]: p
            for p in Person.objects.filter(id__in=person_ids).values(
                "id", "name", "slug", "sort_key", "is_system_anonymous"
            )
        }
        author_items: list[dict[str, Any]] = []
        for row in raw_author_counts:
            pid = int(row["person_id"])
            p = persons_by_id.get(pid)
            if not p:
                continue
            author_items.append({
                "id": pid,
                "key": p["slug"],
                "slug": p["slug"],
                "label": p["name"],
                "count": int(row["count"] or 0),
                "sort_key": p["sort_key"] or p["name"].lower(),
                "is_anonymous": bool(p["is_system_anonymous"]),
            })
        author_items.sort(key=lambda x: (-x["count"], x["sort_key"], x["slug"]))
        for it in author_items:
            it.pop("sort_key", None)
        facets_result["authors"] = author_items[:20]

    # 5. 专属筛选器注册分桶动态聚合 (A-03)
    try:
        from discovery.listing.registry import get_listing_filter_by_facet_key
        for f_key in requested_facets:
            if f_key not in ("types", "tags", "years", "authors"):
                filter_def = get_listing_filter_by_facet_key(f_key)
                if filter_def:
                    facets_result[f_key] = filter_def.compute_facet(qs)
    except Exception:
        pass

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
