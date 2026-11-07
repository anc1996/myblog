from django.db import models
from wagtail.models import Page


class HomePage(Page):
    # 约束首页下可创建的子页面类型
    subpage_types = [
        "blog.BlogIndexPage",
        "blog.BlogPage",
        "guides.GuideIndexPage",
        "portfolio.PortfolioIndexPage",
    ]

    def get_context(self, request, *args, **kwargs):
        context = super().get_context(request, *args, **kwargs)
        from blog.models import BlogPage, BlogIndexPage
        context["latest_articles"] = (
            BlogPage.objects.filter(live=True, is_recycled=False)
            .order_by("-authored_on", "-id")[:10]
        )
        context["columns"] = (
            BlogIndexPage.objects.filter(live=True, is_recycled=False)
            .order_by("-id")
        )
        return context
