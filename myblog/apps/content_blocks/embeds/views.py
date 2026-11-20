"""
Draftail「插入内嵌元素」自定义后台视图。
复用 EmbedProviderRegistry 并向 Draftail 返回用户输入的真实媒体标题，同时展示支持的平台清单。
"""

from __future__ import annotations

import html
from django.utils.text import Truncator
from wagtail.admin.auth import require_admin_access
from wagtail.admin.modal_workflow import render_modal_workflow
from wagtail.embeds.models import Embed

from .forms import ControlledEmbedForm
from .providers import list_supported_providers


def _build_editor_preview_html(title: str, provider_name: str, kind: str, url: str) -> str:
    icon = "🎵" if kind == "audio" else ("🎬" if kind == "video" else "🔗")
    safe_title = html.escape(title)
    safe_provider = html.escape(provider_name)
    safe_url = html.escape(Truncator(url).chars(80))
    return (
        f'<div data-embedtype="media" data-url="{html.escape(url, quote=True)}" '
        f'data-title="{html.escape(title, quote=True)}">'
        f"{icon} [{safe_provider}] {safe_title} ({safe_url})"
        f"</div>"
    )


@require_admin_access
def controlled_embed_chooser(request):
    initial = {}
    if request.GET.get("url"):
        initial["url"] = request.GET.get("url")
    if request.GET.get("title"):
        initial["title"] = request.GET.get("title")
    form = ControlledEmbedForm(prefix="embed-chooser", initial=initial)
    return render_modal_workflow(
        request,
        "content_blocks/embeds/chooser.html",
        None,
        {"form": form, "providers": list_supported_providers()},
        json_data={"step": "chooser"},
    )


@require_admin_access
def controlled_embed_chooser_upload(request):
    if request.method != "POST":
        return controlled_embed_chooser(request)

    form = ControlledEmbedForm(request.POST, prefix="embed-chooser")
    if not form.is_valid():
        return render_modal_workflow(
            request,
            "content_blocks/embeds/chooser.html",
            None,
            {"form": form, "providers": list_supported_providers()},
            json_data={"step": "chooser"},
        )

    spec = form.cleaned_data["_embed_spec"]
    custom_title = form.cleaned_data["title"].strip()
    url = spec.url
    embed_type = "video" if spec.kind == "video" else "rich"
    height = 562 if spec.kind == "video" else (96 if spec.kind == "audio" else 360)

    Embed.objects.update_or_create(
        url=url,
        max_width=None,
        defaults={
            "type": embed_type,
            "title": custom_title,
            "provider_name": spec.provider_name,
            "width": 960,
            "height": height,
            "html": "",
        },
    )

    embed_html = _build_editor_preview_html(custom_title, spec.provider_name, spec.kind, url)
    embed_data = {
        "embedType": embed_type,
        "url": url,
        "providerName": spec.provider_name,
        "authorName": "",
        "thumbnail": "",
        "title": custom_title,
    }
    return render_modal_workflow(
        request,
        None,
        None,
        None,
        json_data={
            "step": "embed_chosen",
            "embed_html": embed_html,
            "embed_data": embed_data,
        },
    )
