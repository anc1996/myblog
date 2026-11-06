"""
api.urls
API 路由分发器。
"""

from django.urls import path
from . import views

app_name = "api_v1"

urlpatterns = [
    path("search/", views.search_api, name="search"),
    path("posts/", views.post_list_api, name="post_list"),
    path("posts/<slug:slug>/", views.post_detail_api, name="post_detail"),
]
