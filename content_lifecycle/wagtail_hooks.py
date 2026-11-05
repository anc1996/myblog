"""
Wagtail 管理后台回收站隔离与菜单集成钩子。

落实核心隔离契约：
1. construct_explorer_page_queryset: 在普通管理后台页面树列表中隐藏已回收页面；
2. construct_page_chooser_queryset: 在页面选择器中隐藏已回收页面；
3. before_serve_page: 前台访问拦截任何标记为 is_recycled 的页面，返回 404；
4. 注册回收站管理菜单与视图。
"""

from django.http import Http404
from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect
from django.utils.safestring import mark_safe
from wagtail import hooks
from wagtail.admin import messages
from wagtail.admin.viewsets.model import ModelViewSet
from content_lifecycle.models import RecycleBatch, RecycleEntry


class RecycleBatchViewSet(ModelViewSet):
    """
    Wagtail 8.1 管理后台【受控回收站】专属管理视图集。
    在左侧菜单栏常驻“受控回收站”，提供回收批次列表查看、一键精准恢复与彻底物理销毁入口。
    """
    model = RecycleBatch
    menu_label = "受控回收站"
    icon = "bin"
    menu_icon = "bin"
    menu_order = 220
    add_to_admin_menu = True
    list_display = ["root_page_title", "total_pages", "recycled_at", "actor_username", "status", "actions"]
    list_filter = ["status", "recycled_at"]
    search_fields = ["root_page_title", "actor_username"]
    inspect_view_enabled = True
    inspect_view_fields = ["batch_id", "root_page_title", "total_pages", "recycled_at", "actor_username", "status"]
    form_fields = ["status"]

    def get_urlpatterns(self):
        urlpatterns = super().get_urlpatterns()
        return urlpatterns + [
            path("<int:pk>/restore/", self.restore_view, name="restore"),
            path("<int:pk>/purge/", self.purge_view, name="purge"),
        ]

    def restore_view(self, request, pk):
        batch = get_object_or_404(RecycleBatch, pk=pk)
        from content_lifecycle.recycle import RecycleService
        try:
            res = RecycleService.restore_page_or_subtree(batch.batch_id, actor=request.user)
            messages.success(request, f"成功将 '{batch.root_page_title}' （共 {len(res.get('restored_pages', []))} 篇页面）原位恢复为未发布草稿！")
        except Exception as exc:
            messages.error(request, f"恢复失败: {exc}")
        return redirect(self.get_url_name("index"))

    def purge_view(self, request, pk):
        batch = get_object_or_404(RecycleBatch, pk=pk)
        from content_lifecycle.recycle import RecycleService
        try:
            res = RecycleService.purge_page_or_subtree(batch.root_page_id, actor=request.user)
            from external_content.outbox import OutboxProcessor
            OutboxProcessor.process_pending_events(batch_size=50)
            messages.success(request, f"已永久物理销毁页面 '{batch.root_page_title}' 及其全部历史版本。")
        except Exception as exc:
            messages.error(request, f"销毁失败: {exc}")
        return redirect(self.get_url_name("index"))


recycle_batch_viewset = RecycleBatchViewSet("recycle_bin")


@hooks.register("register_admin_viewset")
def register_recycle_batch_viewset():
    return recycle_batch_viewset


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
