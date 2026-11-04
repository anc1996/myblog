from django.template.response import TemplateResponse
from discovery.contracts import QuerySpec
from discovery.query import ContentQuery
from content_lifecycle.access import AccessContext, AccessMode


def search(request):
    """全站统一中文搜索与组合筛选入口"""
    search_query = request.GET.get("query", "").strip()
    tag_id_str = request.GET.get("tag", "").strip()
    tag_ids = [int(tag_id_str)] if tag_id_str.isdigit() else []

    type_key = request.GET.get("type", "").strip()
    type_keys = [type_key] if type_key else []

    year_str = request.GET.get("year", "").strip()
    year = int(year_str) if year_str.isdigit() else None

    month_str = request.GET.get("month", "").strip()
    month = int(month_str) if month_str.isdigit() else None

    secondary = request.GET.get("secondary", "none").strip()
    expand = request.GET.get("expand", "auto").strip()
    cursor = request.GET.get("cursor", None)

    spec = QuerySpec(
        q=search_query or None,
        type_keys=type_keys,
        tag_ids=tag_ids,
        year=year,
        month=month,
        secondary=secondary,
        expand=expand,
        cursor=cursor,
        facets=["types", "tags", "years"],
    )

    access_context = AccessContext(
        user=request.user,
        mode=AccessMode.PUBLIC,
    )

    result = ContentQuery.execute(spec, access_context)

    # SEARCH-MODE-02: 模式 A 命中少于 5 条时的手动展开判断
    a_count = len([it for it in result.items if it.match_mode == "A"])
    show_expand_button = (
        (1 <= a_count < 5)
        and not result.secondary_used
        and bool(search_query)
    )

    return TemplateResponse(
        request,
        "search/search.html",
        {
            "search_query": search_query,
            "query_result": result,
            "items": result.items,
            "total_count": result.total_count,
            "facets": result.facets,
            "archive_groups": result.archive_groups,
            "next_cursor": result.next_cursor,
            "cursor_reset": result.cursor_reset,
            "show_expand_button": show_expand_button,
            "selected_type": type_key,
            "selected_tag": tag_id_str,
            "selected_year": year_str,
        },
    )
