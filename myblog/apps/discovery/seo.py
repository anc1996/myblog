"""
全站 SEO 与爬虫收录协议 (Sitemap & Robots)。

功能规范：
1. /sitemap.xml：严格过滤回收站页面 (is_recycled=True)、草稿及未发布页面，仅包含公开在线页面；
2. /robots.txt：阻断爬虫抓取 /admin/、/django-admin/、/media-gateway/、/avatar-gateway/、/account/ 与 /api/，
   并引导合规爬虫抓取 /sitemap.xml。
"""

from django.http import HttpResponse
from wagtail.contrib.sitemaps.sitemap_generator import Sitemap
from wagtail.contrib.sitemaps.views import sitemap


class PublicOnlySitemap(Sitemap):
    """
    全站公开页面 Sitemap。
    严格排除回收站页面 (is_recycled=True)、恢复中页面及祖先处于回收站中的子孙页面。
    必须返回 QuerySet 以满足 Wagtail sitemap 分页与迭代器要求。
    """

    def items(self):
        qs = super().items()
        from content_lifecycle.models import RecycleEntry
        from wagtail.models import Page

        recycled_ids = set(
            RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
        )

        from blog.models import BlogPage, BlogIndexPage
        from guides.models import GuidePage, GuideIndexPage
        from portfolio.models import ProjectPage, PortfolioIndexPage
        from taxonomy.models import TagIndexPage

        for model in (BlogPage, BlogIndexPage, GuidePage, GuideIndexPage, ProjectPage, PortfolioIndexPage, TagIndexPage):
            recycled_ids.update(model.objects.filter(is_recycled=True).values_list("id", flat=True))

        if recycled_ids:
            qs = qs.exclude(id__in=recycled_ids)
            ancestor_paths = Page.objects.filter(id__in=recycled_ids).values_list("path", flat=True)
            for path_prefix in ancestor_paths:
                qs = qs.exclude(path__startswith=path_prefix)

        return qs


def sitemap_view(request):
    """Sitemap 路由交付视图"""
    return sitemap(request, sitemaps={"wagtail": PublicOnlySitemap})


def robots_txt_view(request):
    """
    搜索引擎爬虫协议 (robots.txt)。
    阻断抓取后台、私密网关与账号中心，引导抓取公开页面与 sitemap.xml。
    """
    sitemap_url = request.build_absolute_uri("/sitemap.xml")
    content = f"""User-agent: *
Disallow: /admin/
Disallow: /django-admin/
Disallow: /media-gateway/
Disallow: /avatar-gateway/
Disallow: /documents/
Disallow: /account/
Disallow: /api/
Allow: /

Sitemap: {sitemap_url}
"""
    return HttpResponse(content, content_type="text/plain; charset=utf-8")
