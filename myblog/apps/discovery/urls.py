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
    # 分维度 RSS 订阅 (使用 str:slug 兼容中英文标签与Unicode slug，BLK-06)
    path("type/<str:slug>/", feeds.public_feed_view, {"dimension": "type"}, name="feed_type"),
    path("tag/<str:slug>/", feeds.public_feed_view, {"dimension": "tag"}, name="feed_tag"),
    path("author/<str:slug>/", feeds.public_feed_view, {"dimension": "author"}, name="feed_author"),
    path("topic/<str:slug>/", feeds.public_feed_view, {"dimension": "topic"}, name="feed_topic"),
    # Atom 1.0 专属路径映射
    path("atom/", feeds.public_feed_view, {"dimension": "all"}, name="feed_atom_all"),
    path("atom/type/<str:slug>/", feeds.public_feed_view, {"dimension": "type"}, name="feed_atom_type"),
    path("atom/tag/<str:slug>/", feeds.public_feed_view, {"dimension": "tag"}, name="feed_atom_tag"),
    path("atom/author/<str:slug>/", feeds.public_feed_view, {"dimension": "author"}, name="feed_atom_author"),
    path("atom/topic/<str:slug>/", feeds.public_feed_view, {"dimension": "topic"}, name="feed_atom_topic"),
]
