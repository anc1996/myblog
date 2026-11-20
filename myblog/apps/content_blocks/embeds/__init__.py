"""
全站统一内嵌媒体与外部链接框架 (content_blocks.embeds)。

作为唯一数据源同时驱动：
1. StreamField 独立积木 `CustomEmbedBlock`；
2. Draftail 富文本工具栏 `embed` (`▶` 插入内嵌元素) 与 `CustomRichTextBlock`；
3. Wagtail 原生 `WAGTAILEMBEDS_FINDERS`。
"""

from .providers import (
    EMBED_PROVIDERS,
    EmbedProvider,
    EmbedSpec,
    is_safe_embed_url,
    list_supported_providers,
    render_embed_wrapper_html,
    resolve_embed_spec,
)

__all__ = [
    "EMBED_PROVIDERS",
    "EmbedProvider",
    "EmbedSpec",
    "is_safe_embed_url",
    "list_supported_providers",
    "render_embed_wrapper_html",
    "resolve_embed_spec",
]
