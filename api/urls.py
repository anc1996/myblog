"""
api.urls
API 路由分发器。
"""

from django.urls import path, include
from . import views

app_name = "api_v1"

urlpatterns = [
    # 搜索与发现
    path("search/", views.search_api, name="search"),
    # 博客文章
    path("posts/", views.post_list_api, name="post_list"),
    path("posts/<slug:slug>/", views.post_detail_api, name="post_detail"),
    # 专题技术专栏与指南
    path("guides/", views.guides_list_api, name="guide_list"),
    path("guides/<slug:slug>/", views.guide_detail_api, name="guide_detail"),
    # 时间线归档
    path("archives/", views.timeline_archives_api, name="timeline_archives"),
    # 全站通用评论互动
    path("", include("comments.api.urls")),
]
