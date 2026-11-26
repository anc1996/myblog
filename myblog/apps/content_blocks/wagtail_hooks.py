"""
content_blocks Wagtail 8.1 管理后台钩子：
1. 注册 Vditor/RichText 图片上传、媒体元数据解析与复杂表格实时预览管理端路由；
2. 注入全局编辑器上传协调器与 RichText 粘贴/拖拽图片直传脚本；
3. 注册 Draftail 富文本 `underline` (<u>) 与受控 `embed` 特性；
4. 显式加载 widget_adapters 确保 Telepath 适配器已注册。
"""

import json
from django.conf import settings
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from wagtail import hooks

from .admin_views import (
    resolve_chosen_media,
    richtext_image_upload,
    table_live_preview,
    vditor_image_upload,
)
from .widgets import cb_admin_static
from . import widget_adapters  # noqa: F401


@hooks.register("register_admin_urls")
def register_content_blocks_admin_urls():
    return [
        path(
            "content-blocks/vditor-image-upload/",
            vditor_image_upload,
            name="content_blocks_vditor_image_upload",
        ),
        path(
            "content-blocks/richtext-image-upload/",
            richtext_image_upload,
            name="content_blocks_richtext_image_upload",
        ),
        path(
            "content-blocks/media-resolve/",
            resolve_chosen_media,
            name="content_blocks_media_resolve",
        ),
        path(
            "content-blocks/table-preview/",
            table_live_preview,
            name="content_blocks_table_preview",
        ),
    ]


@hooks.register("insert_editor_css")
def insert_content_blocks_editor_css():
    return format_html(
        '<link rel="stylesheet" href="{}">',
        cb_admin_static("content_blocks/css/admin-split-editors.css"),
    )


@hooks.register("insert_editor_js")
def insert_content_blocks_editor_js():
    config = {
        "richtextImageUploadUrl": reverse("content_blocks_richtext_image_upload"),
        "vditorImageUploadUrl": reverse("content_blocks_vditor_image_upload"),
        "mediaResolveUrl": reverse("content_blocks_media_resolve"),
        "imageChooserUrl": reverse("wagtailimages_chooser:choose"),
        "documentChooserUrl": reverse("wagtaildocs_chooser:choose"),
        "maxImageSize": int(getattr(settings, "WAGTAILIMAGES_MAX_UPLOAD_SIZE", 10 * 1024 * 1024)),
    }
    config_json = mark_safe(json.dumps(config).replace("<", "\\u003c"))
    return format_html(
        "<script>window.CB_EDITOR_CONFIG = Object.assign(window.CB_EDITOR_CONFIG || {{}}, {});</script>"
        '<script src="{}"></script>'
        '<script src="{}"></script>',
        config_json,
        cb_admin_static("content_blocks/js/admin-upload-coordinator.js"),
        cb_admin_static("content_blocks/js/admin-richtext-paste.js"),
    )


@hooks.register("register_rich_text_features")
def register_underline_feature(features):
    """注册 Draftail 下划线 (`underline`) 特性并映射为 <u> 标签"""
    from wagtail.admin.rich_text.converters.html_to_contentstate import (
        InlineStyleElementHandler,
    )
    from wagtail.admin.rich_text.editors.draftail import features as draftail_features

    feature_name = "underline"
    type_ = "UNDERLINE"
    tag = "u"

    control = {
        "type": type_,
        "label": "U",
        "description": "下划线",
    }
    features.register_editor_plugin(
        "draftail", feature_name, draftail_features.InlineStyleFeature(control)
    )
    db_conversion = {
        "from_database_format": {tag: InlineStyleElementHandler(type_)},
        "to_database_format": {"style_map": {type_: tag}},
    }
    features.register_converter_rule("contentstate", feature_name, db_conversion)


@hooks.register("register_rich_text_features")
def register_paragraph_alignment_features(features):
    """注册 Draftail 段落对齐特性（左对齐 / 居中 / 右对齐），零魔改保留原 <p> 语义。"""
    from wagtail.admin.rich_text.converters.html_to_contentstate import (
        BlockElementHandler,
    )
    from wagtail.admin.rich_text.editors.draftail import features as draftail_features

    for align_key, label, desc in (
        ("left", "左对齐", "段落左对齐"),
        ("center", "居中", "段落居中对齐"),
        ("right", "右对齐", "段落右对齐"),
    ):
        feature_name = f"align-{align_key}"
        cls_name = f"cb-align-{align_key}"
        features.register_editor_plugin(
            "draftail",
            feature_name,
            draftail_features.BlockFeature(
                {
                    "type": feature_name,
                    "label": label,
                    "description": desc,
                    "element": "p",
                }
            ),
        )
        features.register_converter_rule(
            "contentstate",
            feature_name,
            {
                "from_database_format": {
                    f'p[class="{cls_name}"]': BlockElementHandler(feature_name),
                },
                "to_database_format": {
                    "block_map": {
                        feature_name: {
                            "element": "p",
                            "props": {"class": cls_name},
                        }
                    }
                },
            },
        )


@hooks.register("register_rich_text_features", order=100)
def register_controlled_embed_feature(features):
    """
    接管 Wagtail 原生 embed (`▶`) 特性：
    在弹窗中同步采集 `媒体标题 / 无障碍描述` 与 URL，并与 CustomEmbedBlock 共用统一安全渲染。
    """
    from django.urls import reverse_lazy
    from wagtail.admin.rich_text.editors.draftail import features as draftail_features

    from .embeds.rich_text import (
        ControlledContentstateMediaConversionRule,
        ControlledEditorHTMLEmbedConversionRule,
        ControlledMediaEmbedHandler,
    )

    features.register_embed_type(ControlledMediaEmbedHandler)
    features.register_converter_rule(
        "editorhtml", "embed", ControlledEditorHTMLEmbedConversionRule
    )
    features.register_editor_plugin(
        "draftail",
        "embed",
        draftail_features.EntityFeature(
            {
                "type": "EMBED",
                "icon": "media",
                "description": "插入内嵌元素（支持自定义媒体标题）",
                "chooserUrls": {
                    "embedsChooser": reverse_lazy("wagtailembeds:chooser"),
                },
            },
            js=["wagtailembeds/js/embed-chooser-modal.js"],
        ),
    )
    features.register_converter_rule(
        "contentstate", "embed", ControlledContentstateMediaConversionRule
    )
