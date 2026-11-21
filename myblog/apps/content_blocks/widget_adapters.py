"""
Wagtail 8.1 Telepath 控件适配器注册。
让 StreamField 在新增/复制/排序积木块时动态激活 Vditor、Mermaid 分栏与复杂表格分栏控件。
"""

from django import forms
from django.utils.functional import cached_property
from wagtail.admin.telepath import register
from wagtail.admin.telepath.widgets import WidgetAdapter
from wagtail.blocks.struct_block import StructBlockAdapter

from .blocks.media import CustomAudioBlock, CustomVideoBlock
from .widgets import (
    MermaidSplitWidget,
    TableJsonSplitWidget,
    VditorMarkdownWidget,
    cb_admin_static,
)


class VditorMarkdownWidgetAdapter(WidgetAdapter):
    js_constructor = "content_blocks.widgets.VditorMarkdownWidget"


class MermaidSplitWidgetAdapter(WidgetAdapter):
    js_constructor = "content_blocks.widgets.MermaidSplitWidget"


class TableJsonSplitWidgetAdapter(WidgetAdapter):
    js_constructor = "content_blocks.widgets.TableJsonSplitWidget"


class MediaSplitStructBlockAdapter(StructBlockAdapter):
    """为音视频 StructBlock 分栏表单注入共享播放器与后台同步脚本，不改变客户端块构造器。"""

    @cached_property
    def media(self):
        extra = forms.Media(
            css={
                "all": (
                    cb_admin_static("content_blocks/css/media-blocks.css"),
                    cb_admin_static("content_blocks/css/admin-split-editors.css"),
                )
            },
            js=(
                cb_admin_static("content_blocks/js/media-player.js"),
                cb_admin_static("content_blocks/js/admin-media-split.js"),
            ),
        )
        return super().media + extra


register(VditorMarkdownWidgetAdapter(), VditorMarkdownWidget)
register(MermaidSplitWidgetAdapter(), MermaidSplitWidget)
register(TableJsonSplitWidgetAdapter(), TableJsonSplitWidget)
register(MediaSplitStructBlockAdapter(), CustomAudioBlock)
register(MediaSplitStructBlockAdapter(), CustomVideoBlock)
