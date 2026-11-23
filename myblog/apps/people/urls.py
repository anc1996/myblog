"""
people.urls
全站作者专栏大厅、远程联想与个人专栏路由（挂载于 /authors/）。
"""

from django.urls import path
from people import views

app_name = "people"

urlpatterns = [
    path("", views.author_directory_view, name="author_directory"),
    path("lookup/", views.author_lookup_view, name="author_lookup"),
    path("<str:slug>/", views.author_detail_view, name="author_detail"),
]
