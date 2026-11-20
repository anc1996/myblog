"""
Draftail 富文本 Embed 转换器扩展：
在 <embed embedtype="media" url="..." title="..."/> 与 Draft.js Entity 之间完整双向保留作者输入的自定义标题。
"""

from __future__ import annotations

from draftjs_exporter import DOM
from wagtail.admin.rich_text.converters import editor_html
from wagtail.admin.rich_text.converters.contentstate_models import Entity
from wagtail.admin.rich_text.converters.html_to_contentstate import (
    AtomicBlockEntityElementHandler,
)
from wagtail.embeds import embeds
from wagtail.embeds.exceptions import EmbedException
from wagtail.embeds.rich_text import MediaEmbedHandler as BaseMediaEmbedHandler

from .providers import render_embed_wrapper_html, resolve_embed_spec


def controlled_media_embed_entity(props):
    attrs = {
        "embedtype": "media",
        "url": props.get("url"),
    }
    title = (props.get("title") or "").strip()
    if title:
        attrs["title"] = title
    return DOM.create_element("embed", attrs)


class ControlledMediaEmbedElementHandler(AtomicBlockEntityElementHandler):
    def create_entity(self, name, attrs, state, contentstate):
        url = attrs.get("url", "")
        custom_title = (attrs.get("title") or "").strip()
        spec, _ = resolve_embed_spec(url, title=custom_title)
        if spec:
            embed_data = {
                "embedType": "video" if spec.kind == "video" else "rich",
                "url": spec.url,
                "providerName": spec.provider_name,
                "authorName": "",
                "thumbnail": "",
                "title": custom_title or spec.title,
            }
            return Entity("EMBED", "IMMUTABLE", embed_data)
        try:
            embed_obj = embeds.get_embed(url)
            embed_data = {
                "embedType": embed_obj.type,
                "url": embed_obj.url,
                "providerName": embed_obj.provider_name,
                "authorName": embed_obj.author_name,
                "thumbnail": embed_obj.thumbnail_url,
                "title": custom_title or embed_obj.title,
            }
        except EmbedException:
            embed_data = {"url": url, "title": custom_title}
        return Entity("EMBED", "IMMUTABLE", embed_data)


ControlledContentstateMediaConversionRule = {
    "from_database_format": {
        'embed[embedtype="media"]': ControlledMediaEmbedElementHandler(),
    },
    "to_database_format": {"entity_decorators": {"EMBED": controlled_media_embed_entity}},
}


class ControlledEditorMediaEmbedHandler:
    @staticmethod
    def get_db_attributes(tag):
        attrs = {"url": tag.get("data-url", "")}
        if tag.get("data-title"):
            attrs["title"] = tag["data-title"]
        return attrs

    @staticmethod
    def expand_db_attributes(attrs):
        return render_embed_wrapper_html(attrs.get("url", ""), title=attrs.get("title", ""))


ControlledEditorHTMLEmbedConversionRule = [
    editor_html.EmbedTypeRule("media", ControlledEditorMediaEmbedHandler)
]


class ControlledMediaEmbedHandler(BaseMediaEmbedHandler):
    @staticmethod
    def expand_db_attributes(attrs: dict) -> str:
        return render_embed_wrapper_html(attrs.get("url", ""), title=attrs.get("title", ""))
