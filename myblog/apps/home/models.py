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
        latest_articles = list(
            BlogPage.objects.filter(live=True, is_recycled=False)
            .select_related("featured_image")
            .order_by("-authored_on", "-id")[:6]
        )
        context["latest_articles"] = latest_articles

        # 2. 精选指南与专栏 (前 4 篇)
        featured_guides = list(
            GuidePage.objects.filter(live=True, is_recycled=False)
            .select_related("topic")
            .order_by("-last_published_at", "-id")[:4]
        )
        context["featured_guides"] = featured_guides

        # 3. 精选开源项目 (前 3 项)
        featured_projects = list(
            ProjectPage.objects.filter(live=True, is_recycled=False)
            .order_by("-last_published_at", "-id")[:3]
        )
        context["featured_projects"] = featured_projects

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

        # 6. 首页精选推荐轮播数据（合并最新指南、开源项目与博客，适配全站 Splide 轮播组件）
        carousel_items = []
        for g in featured_guides[:3]:
            carousel_items.append({
                "badge": g.topic.name if g.topic else "专题指南",
                "title": g.title,
                "url": g.get_url(request) or g.url,
                "summary": g.summary or "体系化技术实操指南",
                "date_label": g.authored_on.strftime("%Y-%m-%d") if g.authored_on else "",
            })
        for p in featured_projects[:3]:
            carousel_items.append({
                "badge": p.primary_language or "开源作品",
                "title": p.title,
                "url": p.get_url(request) or p.url,
                "summary": p.summary or "开源实战项目",
                "date_label": p.authored_on.strftime("%Y-%m-%d") if p.authored_on else "",
            })
        for a in latest_articles[:4]:
            intro_text = a.intro.raw_text if getattr(a, "intro", None) else ""
            carousel_items.append({
                "badge": "技术博客",
                "title": a.title,
                "url": a.get_url(request) or a.url,
                "summary": intro_text or a.title,
                "date_label": a.authored_on.strftime("%Y-%m-%d") if a.authored_on else "",
            })
        context["featured_carousel_items"] = carousel_items[:8]
        return context
