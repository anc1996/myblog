from django.db import models
from wagtail.models import Page


class HomePage(Page):
    author_creatable = False
    author_allow_child_creation = False

    # 约束首页下可创建的子页面类型
    subpage_types = [
        "blog.BlogIndexPage",
        "blog.BlogPage",
        "guides.GuideIndexPage",
        "portfolio.PortfolioIndexPage",
        "taxonomy.TagIndexPage",
    ]

    def permissions_for_user(self, user):
        from content_lifecycle.permissions import OwnershipPagePermissionTester
        return OwnershipPagePermissionTester(user, self)

    def get_context(self, request, *args, **kwargs):
        context = super().get_context(request, *args, **kwargs)
        from blog.models import BlogPage, BlogIndexPage
        from guides.models import GuidePage
        from portfolio.models import ProjectPage
        from discovery.models import PublishedSearchTag, PublishedSearchDocument
        from django.db.models import Count

        # 1. 最新发布博客文章 (前 6 篇)
        context["latest_articles"] = (
            BlogPage.objects.filter(live=True, is_recycled=False)
            .select_related("featured_image")
            .order_by("-authored_on", "-id")[:6]
        )

        # 2. 精选指南与专栏 (前 4 篇)
        context["featured_guides"] = (
            GuidePage.objects.filter(live=True, is_recycled=False)
            .select_related("topic")
            .order_by("-last_published_at", "-id")[:4]
        )

        # 3. 精选开源项目 (前 3 项)
        context["featured_projects"] = (
            ProjectPage.objects.filter(live=True, is_recycled=False)
            .order_by("-last_published_at", "-id")[:3]
        )

        # 4. 博客核心栏目
        context["columns"] = (
            BlogIndexPage.objects.filter(live=True, is_recycled=False)
            .order_by("-id")[:6]
        )

        # 5. 全站热门技术标签 (前 15 个活跃标签)
        eligible_docs = PublishedSearchDocument.objects.filter(is_live=True, is_recycled=False)
        tag_counts = (
            PublishedSearchTag.objects.filter(document__in=eligible_docs)
            .values("tag_name", "tag_slug")
            .annotate(count=Count("document_id", distinct=True))
            .filter(count__gte=1)
            .order_by("-count", "tag_name")[:15]
        )
        context["popular_tags"] = list(tag_counts)
        return context
