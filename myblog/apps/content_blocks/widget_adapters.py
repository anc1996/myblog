"""
Wagtail 8.1 Telepath 控件适配器注册。
让 StreamField 在新增/复制/排序积木块时动态激活 Vditor、Mermaid 分栏与复杂表格分栏控件。
"""

from wagtail.admin.telepath import register
from wagtail.admin.telepath.widgets import WidgetAdapter

from .widgets import VditorMarkdownWidget, MermaidSplitWidget, TableJsonSplitWidget


class VditorMarkdownWidgetAdapter(WidgetAdapter):
    js_constructor = "content_blocks.widgets.VditorMarkdownWidget"


class MermaidSplitWidgetAdapter(WidgetAdapter):
    js_constructor = "content_blocks.widgets.MermaidSplitWidget"


class TableJsonSplitWidgetAdapter(WidgetAdapter):
    js_constructor = "content_blocks.widgets.TableJsonSplitWidget"


register(VditorMarkdownWidgetAdapter(), VditorMarkdownWidget)
register(MermaidSplitWidgetAdapter(), MermaidSplitWidget)
register(TableJsonSplitWidgetAdapter(), TableJsonSplitWidget)
