"""
Wagtail WAGTAILEMBEDS_FINDERS 适配器 (finder.py)。
直接复用 EmbedProviderRegistry，确保 Wagtail 原生 Embed 解析与 CustomEmbedBlock 行为一致。
"""

from __future__ import annotations

from wagtail.embeds.exceptions import EmbedUnsupportedProviderException
from wagtail.embeds.finders.base import EmbedFinder

from .providers import render_embed_wrapper_html, resolve_embed_spec


class RegistryEmbedFinder(EmbedFinder):
    def accept(self, url: str) -> bool:
        spec, _ = resolve_embed_spec(url)
        return spec is not None

    def find_embed(self, url: str, max_width: int | None = None, title: str = "") -> dict:
        spec, err = resolve_embed_spec(url, title=title)
        if not spec:
            raise EmbedUnsupportedProviderException(err or "Unsupported embed URL")

        embed_type = "video" if spec.kind == "video" else "rich"
        height = 562 if spec.kind == "video" else (96 if spec.kind == "audio" else 360)
        return {
            "title": spec.title,
            "provider_name": spec.provider_name,
            "type": embed_type,
            "width": max_width or 960,
            "height": height,
            "thumbnail_url": "",
            "html": render_embed_wrapper_html(spec.url, title=spec.title),
        }
