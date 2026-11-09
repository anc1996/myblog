"""
comments.api.urls
评论系统 API 路由。
"""

from django.urls import path
from . import views

urlpatterns = [
    path("pages/<int:page_id>/comments/", views.list_comments_api, name="list_comments"),
    path("pages/<int:page_id>/comments/post/", views.post_comment_api, name="post_comment"),
    path("comments/<int:root_id>/replies/", views.list_replies_api, name="list_replies"),
    path("comments/<int:comment_id>/edit/", views.edit_comment_api, name="edit_comment"),
    path("comments/<int:comment_id>/delete/", views.delete_comment_api, name="delete_comment"),
    path("comments/<int:comment_id>/react/", views.react_comment_api, name="react_comment"),
]
