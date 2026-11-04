from django.db import models

from wagtail.models import Page


class HomePage(Page):
    # 约束首页下可创建的子页面类型为博客栏目或文章
    subpage_types = ["blog.BlogIndexPage", "blog.BlogPage"]
