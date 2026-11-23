"""
全站 SEO 与爬虫收录协议 (Sitemap & Robots)。

功能规范：
1. /sitemap.xml：严格过滤回收站页面 (is_recycled=True)、草稿及未发布页面，仅包含公开在线页面；
2. /robots.txt：阻断爬虫抓取 /admin/、/django-admin/、/media-gateway/、/avatar-gateway/、/account/ 与 /api/，
   并引导合规爬虫抓取 /sitemap.xml。
"""

from urllib.parse import urlparse

from django.contrib.sitemaps import Sitemap as DjangoSitemap
from django.db.models import Exists, OuterRef
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from wagtail.contrib.sitemaps.sitemap_generator import Sitemap
from wagtail.contrib.sitemaps.views import sitemap
from wagtail.models import Site


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


class AuthorSitemap(DjangoSitemap):
    """
    当前站点活跃作者专栏 Sitemap。
    仅收录在本站点拥有 >= 1 篇正式公开且未回收/未受限内容的活跃作者（含活跃时的 anonymous）。
    """

    def __init__(self, request=None):
        self.request = request

    def _get_site(self):
        if getattr(self, "request", None) is not None:
            return Site.find_for_request(self.request)
        return Site.objects.filter(is_default_site=True).first()

    def get_protocol(self, protocol=None):
        site = self._get_site()
        if site and site.root_url:
            parsed = urlparse(site.root_url)
            if parsed.scheme:
                return parsed.scheme
        return protocol or "https"

    def get_domain(self, site=None):
        wsite = self._get_site()
        if wsite and wsite.root_url:
            parsed = urlparse(wsite.root_url)
            if parsed.netloc:
                return parsed.netloc
        if getattr(self, "request", None) is not None:
            return self.request.get_host()
        return super().get_domain(site)

    def items(self):
        from django.conf import settings
        from discovery.eligibility import apply_public_eligibility_filter
        from discovery.models import PublishedSearchAuthor, PublishedSearchDocument
        from discovery.registry import get_author_enabled_type_keys
        from people.models import Person

        if not getattr(settings, "AUTHOR_DISCOVERY_ENABLED", True):
            return Person.objects.none()

        site = self._get_site()
        site_today = timezone.localdate()
        author_types = get_author_enabled_type_keys()
        if not author_types:
            return Person.objects.none()

        eligible_docs = PublishedSearchDocument.objects.filter(
            is_live=True,
            is_recycled=False,
            type_key__in=author_types,
            authored_on__lte=site_today,
        )
        if site is not None:
            eligible_docs = eligible_docs.filter(site_id=site.id)
        eligible_docs = apply_public_eligibility_filter(eligible_docs)

        active_author_exists = PublishedSearchAuthor.objects.filter(
            person_id=OuterRef("pk"),
            document__in=eligible_docs,
        )
        return Person.objects.filter(Exists(active_author_exists)).order_by("initial_group_rank", "initial", "sort_key", "id")

    def location(self, obj):
        return reverse("people:author_detail", kwargs={"slug": obj.slug})


def sitemap_view(request):
    """Sitemap 路由交付视图"""
    return sitemap(
        request,
        sitemaps={
            "wagtail": PublicOnlySitemap,
            "authors": AuthorSitemap(request=request),
        },
    )


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
