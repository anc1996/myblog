"""
discovery.timeline
全站年月时间线归档服务 (Timeline Archive)。
支持全站内容按年份、月份结构化折叠浏览，秒级扫描只读快照。
"""

from typing import Any
from django.shortcuts import render
from django.http import JsonResponse
from django.template.loader import render_to_string
from wagtail.models import Site

from discovery.models import PublishedSearchDocument
from discovery.eligibility import apply_public_eligibility_filter


def timeline_view(request):
    """时间线年月归档聚合页面视图 (决策四 A)"""
    site = Site.find_for_request(request)

    # 1. 基础合法候选集 (仅限已上线、未回收)
    qs = PublishedSearchDocument.objects.filter(is_live=True, is_recycled=False)
    if site:
        qs = qs.filter(site_id=site.id)
    qs = apply_public_eligibility_filter(qs)

    # 2. 读取可选年份/月份筛选参数
    req_year = request.GET.get("year")
    req_month = request.GET.get("month")

    selected_year = int(req_year) if req_year and req_year.isdigit() else None
    selected_month = int(req_month) if req_month and req_month.isdigit() else None

    # 3. 提取所有已发布内容的轻量元数据并按成文日期倒序
    all_articles = list(
        qs.values("page_id", "title", "url_path", "authored_on", "type_key")
        .order_by("-authored_on", "-page_id")
    )

    total_articles_count = len(all_articles)

    # 4. 构建年份与月份嵌套树形结构
    years_dict: dict[int, dict[str, Any]] = {}
    for a in all_articles:
        ad = a["authored_on"]
        if not ad:
            continue
        y = ad.year
        m = ad.month

        if y not in years_dict:
            years_dict[y] = {
                "year": y,
                "total_count": 0,
                "months": {},
            }
        years_dict[y]["total_count"] += 1

        if m not in years_dict[y]["months"]:
            years_dict[y]["months"][m] = {
                "month": m,
                "count": 0,
                "articles": [],
            }
        years_dict[y]["months"][m]["count"] += 1

        # 若未选年，或选了当年；若未选月，或选了当月，加入文章列表
        if (selected_year is None or selected_year == y) and (selected_month is None or selected_month == m):
            years_dict[y]["months"][m]["articles"].append(a)

    # 转为有序列表
    timeline_tree = []
    for y in sorted(years_dict.keys(), reverse=True):
        y_data = years_dict[y]
        m_list = []
        for m in sorted(y_data["months"].keys(), reverse=True):
            m_list.append(y_data["months"][m])
        y_data["months_list"] = m_list
        timeline_tree.append(y_data)

    context = {
        "timeline_tree": timeline_tree,
        "total_count": total_articles_count,
        "selected_year": selected_year,
        "selected_month": selected_month,
    }

    is_ajax = bool(
        request.headers.get("X-Listing-Response") == "fragment-v1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.GET.get("format") == "partial"
    )

    if is_ajax:
        html = render_to_string(
            "discovery/timeline/_timeline_content.html",
            context,
            request=request,
        )
        return JsonResponse({"html": html, "total_count": total_articles_count})

    return render(request, "discovery/timeline/timeline_page.html", context)
