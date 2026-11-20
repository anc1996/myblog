"""
content_blocks Wagtail 8.1 管理后台钩子：
1. 注册 Vditor 粘贴传图与复杂表格实时预览管理端路由；
2. 注册 Draftail 富文本 `underline` (<u>) 特性，消除未知特性告警；
3. 显式加载 widget_adapters 确保 Telepath 适配器已注册。
"""

from django.urls import path
from wagtail import hooks

from .admin_views import vditor_image_upload, table_live_preview
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
            "content-blocks/table-preview/",
            table_live_preview,
            name="content_blocks_table_preview",
        ),
    ]


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
