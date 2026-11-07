"""
discovery.urls
全站通用 RSS 2.0 / Atom 1.0 订阅路由配置。
"""

from django.urls import path
from . import feeds

app_name = "discovery"

urlpatterns = [
    # 默认/全站订阅
    path("", feeds.public_feed_view, {"dimension": "all"}, name="feed_all"),
    # 分维度 RSS 订阅
    path("type/<slug:slug>/", feeds.public_feed_view, {"dimension": "type"}, name="feed_type"),
    path("tag/<slug:slug>/", feeds.public_feed_view, {"dimension": "tag"}, name="feed_tag"),
    path("author/<slug:slug>/", feeds.public_feed_view, {"dimension": "author"}, name="feed_author"),
    path("topic/<slug:slug>/", feeds.public_feed_view, {"dimension": "topic"}, name="feed_topic"),
    # Atom 1.0 专属路径映射
    path("atom/", feeds.public_feed_view, {"dimension": "all"}, name="feed_atom_all"),
    path("atom/type/<slug:slug>/", feeds.public_feed_view, {"dimension": "type"}, name="feed_atom_type"),
    path("atom/tag/<slug:slug>/", feeds.public_feed_view, {"dimension": "tag"}, name="feed_atom_tag"),
    path("atom/author/<slug:slug>/", feeds.public_feed_view, {"dimension": "author"}, name="feed_atom_author"),
    path("atom/topic/<slug:slug>/", feeds.public_feed_view, {"dimension": "topic"}, name="feed_atom_topic"),
]
