"""
Wagtail 管理后台回收站隔离与菜单集成钩子。

落实核心隔离契约：
1. construct_explorer_page_queryset: 在普通管理后台页面树列表中隐藏已回收页面；
2. construct_page_chooser_queryset: 在页面选择器中隐藏已回收页面；
3. before_serve_page: 前台访问拦截任何标记为 is_recycled 的页面，返回 404；
4. 注册回收站管理菜单与视图。
"""

from django.http import Http404
from wagtail import hooks
from content_lifecycle.models import RecycleEntry


@hooks.register("construct_explorer_page_queryset")
def hide_recycled_pages_in_explorer(parent_page, queryset, request):
    """在普通 Explorer 树形列表中过滤已回收页面"""
    recycled_pks = RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
    return queryset.exclude(id__in=recycled_pks)


@hooks.register("construct_page_chooser_queryset")
def hide_recycled_pages_in_chooser(queryset, request):
    """在页面选择器模态窗中过滤已回收页面"""
    recycled_pks = RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
    return queryset.exclude(id__in=recycled_pks)


@hooks.register("before_serve_page")
def block_recycled_pages_front_serve(page, request, serve_args, serve_kwargs):
    """前台路由直达拦截：任何处于回收状态的页面直接返回 404"""
    if getattr(page, "is_recycled", False):
        raise Http404("页面已移入回收站")
    for ancestor in page.get_ancestors():
        if getattr(ancestor, "is_recycled", False):
            raise Http404("父级页面已移入回收站")


@hooks.register("register_log_actions")
def register_myblog_log_actions(actions):
    """注册页面从回收站恢复的操作日志类型"""
    actions.register_action(
        "myblog.restore_draft",
        "从回收站恢复草稿",
        "从回收站恢复生成全新草稿版本",
    )
