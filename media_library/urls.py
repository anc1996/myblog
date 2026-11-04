"""
媒体网关路由定义。
"""

from django.urls import path
from media_library.delivery import serve_media_asset, serve_rendition_asset

urlpatterns = [
    path("media-gateway/<str:asset_type>/<uuid:asset_id>/", serve_media_asset, name="media_gateway_asset"),
    path("media-gateway/rendition/<int:rendition_id>/", serve_rendition_asset, name="media_gateway_rendition"),
]
