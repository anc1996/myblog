"""
guides.wagtail_hooks
Wagtail 8.1 管理后台集成钩子。
"""

from wagtail.snippets.models import register_snippet
from wagtail.snippets.views.snippets import SnippetViewSet
from .models import GuideTopic


class GuideTopicViewSet(SnippetViewSet):
    model = GuideTopic
    menu_label = "专栏主题"
    icon = "folder-open-inverse"
    menu_order = 230
    add_to_admin_menu = True
    list_display = ["name", "slug", "order", "description"]
    search_fields = ["name", "slug", "description"]


register_snippet(GuideTopic, GuideTopicViewSet)
