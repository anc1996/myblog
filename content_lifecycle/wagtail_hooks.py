"""
Wagtail 管理后台回收站隔离与菜单集成钩子。

落实核心隔离契约：
1. construct_explorer_page_queryset: 在普通管理后台页面树列表中隐藏已回收页面；
2. construct_page_chooser_queryset: 在页面选择器中隐藏已回收页面；
3. before_serve_page: 前台访问拦截任何标记为 is_recycled 的页面，返回 404；
4. before_delete_page & before_bulk_action: 拦截单页与批量删除，统一转入受控回收站，杜绝底库物理硬删；
5. 注册回收站管理菜单与安全操作视图。
"""

from django.http import Http404, HttpResponse
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect
from django.middleware.csrf import get_token
from django.utils.html import escape
from wagtail import hooks
from wagtail.admin import messages
from wagtail.admin.viewsets.model import ModelViewSet
from wagtail.models import Page
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
        if batch.status != "recycled":
            messages.error(request, "该批次不处于已回收状态，无法恢复！")
            return redirect(self.get_url_name("index"))

        try:
            root_page = Page.objects.get(id=batch.root_page_id).specific
            perms = root_page.permissions_for_user(request.user)
            if not (perms.can_edit() or perms.can_delete() or request.user.is_superuser):
                raise PermissionDenied("当前用户无权恢复此页面")
        except Page.DoesNotExist:
            messages.error(request, "目标根页面在数据库中不存在，无法恢复！")
            return redirect(self.get_url_name("index"))

        from content_lifecycle.recycle import RecycleService
        if request.method != "POST":
            try:
                plan = RecycleService.plan_restore(batch.batch_id)
                if not plan.get("can_restore_directly", True):
                    messages.error(request, f"恢复失败: 存在未处置的恢复冲突: {plan.get('conflicts')}")
                    return redirect(self.get_url_name("index"))
            except Exception as plan_exc:
                messages.error(request, f"恢复失败: {plan_exc}")
                return redirect(self.get_url_name("index"))

            csrf_token = get_token(request)
            safe_title = escape(batch.root_page_title)
            cancel_url = reverse(self.get_url_name("index"))
            html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>确认恢复页面</title></head>
<body style="font-family:sans-serif;padding:30px;line-height:1.6;">
    <h2>确认恢复页面</h2>
    <p>确认将 <strong>'{safe_title}'</strong> （共 {batch.total_pages} 篇页面）原位恢复为未发布草稿？</p>
    <form method="POST">
        <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
        <button type="submit" style="background:#0284c7;color:#fff;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;">确认恢复</button>
        <a href="{cancel_url}" style="margin-left:12px;color:#64748b;text-decoration:none;">取消</a>
    </form>
</body>
</html>"""
            return HttpResponse(html)

        try:
            res = RecycleService.restore_page_or_subtree(batch.batch_id, actor=request.user)
            messages.success(request, f"成功将 '{batch.root_page_title}' （共 {len(res.get('restored_pages', []))} 篇页面）原位恢复为未发布草稿！")
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, f"恢复失败: {exc}")
        except Exception as exc:
            messages.error(request, f"恢复失败: {exc}")
        return redirect(self.get_url_name("index"))

    def purge_view(self, request, pk):
        batch = get_object_or_404(RecycleBatch, pk=pk)
        if batch.status != "recycled":
            messages.error(request, "该批次不处于已回收状态，旧销毁链接已失效")
            return redirect(self.get_url_name("index"))

        try:
            root_page = Page.objects.get(id=batch.root_page_id).specific
            perms = root_page.permissions_for_user(request.user)
            entries = batch.entries.all()
            has_live = any(e.was_live for e in entries)
            if has_live:
                if not ((perms.can_delete() and perms.can_publish()) or request.user.is_superuser):
                    raise PermissionDenied("当前用户无权永久销毁曾发布页面（需具备发布权限或管理员权限）")
            else:
                if not (perms.can_delete() or request.user.is_superuser):
                    raise PermissionDenied("当前用户无权永久销毁此页面")
        except Page.DoesNotExist:
            messages.error(request, "目标页面已被销毁，无法重复操作")
            return redirect(self.get_url_name("index"))

        if request.method != "POST":
            csrf_token = get_token(request)
            safe_title = escape(batch.root_page_title)
            cancel_url = reverse(self.get_url_name("index"))
            html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>确认永久销毁</title></head>
<body style="font-family:sans-serif;padding:30px;line-height:1.6;">
    <h2 style="color:#dc2626;">确认永久销毁</h2>
    <p><strong>警告：</strong>永久删除将彻底物理销毁页面 <strong>'{safe_title}'</strong> 及其在 PostgreSQL 与 MongoDB 中的全部历史版本，不可撤销！</p>
    <form method="POST">
        <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
        <button type="submit" style="background:#dc2626;color:#fff;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;">确认彻底物理销毁</button>
        <a href="{cancel_url}" style="margin-left:12px;color:#64748b;text-decoration:none;">取消</a>
    </form>
</body>
</html>"""
            return HttpResponse(html)

        from content_lifecycle.recycle import RecycleService
        try:
            res = RecycleService.purge_page_or_subtree(batch.root_page_id, actor=request.user, batch_id=batch.batch_id)
            from external_content.outbox import OutboxProcessor
            from external_content.models import OutboxEvent
            OutboxProcessor.process_pending_events(batch_size=50)

            purged_ids = [n["page_id"] for n in res.get("purged_summaries", [])] or [batch.root_page_id]
            events = list(OutboxEvent.objects.filter(event_type="page.purged", page_id__in=purged_ids))
            all_processed = bool(events) and all(e.status == "processed" for e in events)

            if all_processed:
                messages.success(request, f"已永久物理销毁页面 '{batch.root_page_title}' 及其全部历史版本。")
            else:
                unprocessed = [str(e.page_id) for e in events if e.status != "processed"]
                messages.warning(request, f"页面已从数据库移除，但 MongoDB 历史正文清理未完成（未完成页面 ID: {', '.join(unprocessed)}）。")
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, f"销毁失败: {exc}")
        except Exception as exc:
            messages.error(request, f"销毁失败: {exc}")
        return redirect(self.get_url_name("index"))


recycle_batch_viewset = RecycleBatchViewSet("recycle_bin")


@hooks.register("register_admin_viewset")
def register_recycle_batch_viewset():
    return recycle_batch_viewset


@hooks.register("construct_explorer_page_queryset")
def hide_recycled_pages_in_explorer(parent_page, queryset, request):
    recycled_pks = RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
    return queryset.exclude(id__in=recycled_pks)


@hooks.register("construct_page_chooser_queryset")
def hide_recycled_pages_in_chooser(queryset, request):
    recycled_pks = RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
    return queryset.exclude(id__in=recycled_pks)


@hooks.register("before_serve_page")
def block_recycled_pages_front_serve(page, request, serve_args, serve_kwargs):
    if getattr(page, "is_recycled", False):
        raise Http404("页面已移入回收站")
    for ancestor in page.get_ancestors():
        if getattr(ancestor, "is_recycled", False):
            raise Http404("父级页面已移入回收站")


@hooks.register("before_delete_page")
def intercept_admin_page_delete(request, page):
    if request.method != "POST":
        return None

    from content_lifecycle.recycle import RecycleService
    from wagtail.admin.views.pages.utils import get_valid_next_url_from_request

    parent = page.get_parent()
    display_title = page.get_admin_display_title()
    try:
        RecycleService.recycle_page_or_subtree(page, actor=request.user)
        messages.success(request, f"页面 '{display_title}' 已成功移入受控回收站。")
    except Exception as exc:
        messages.error(request, f"移入回收站失败: {exc}")

    next_url = get_valid_next_url_from_request(request)
    if next_url and f"/admin/pages/{page.id}/edit/" not in next_url:
        return redirect(next_url)
    if parent:
        return redirect("wagtailadmin_explore", parent.id)
    return redirect("wagtailadmin_home")


@hooks.register("before_bulk_action")
def intercept_admin_bulk_page_delete(request, action_type, objects, bulk_action):
    if action_type != "delete" or request.method != "POST":
        return None

    if not issubclass(bulk_action.model, Page):
        return None

    from content_lifecycle.recycle import RecycleService

    selected_pages = [p.specific for p in objects]
    selected_ids = {p.id for p in selected_pages}
    top_level_pages = []
    for p in selected_pages:
        ancestor_ids = set(p.get_ancestors().values_list("id", flat=True))
        if not (ancestor_ids & selected_ids):
            top_level_pages.append(p)

    recycled_count = 0
    errors = []
    for p in top_level_pages:
        if getattr(p, "is_recycled", False):
            continue
        try:
            res = RecycleService.recycle_page_or_subtree(p, actor=request.user)
            recycled_count += len(res.get("recycled_pages", []))
        except Exception as exc:
            errors.append(f"{p.get_admin_display_title()}: {exc}")

    if recycled_count > 0:
        messages.success(request, f"成功将 {recycled_count} 篇页面移入受控回收站。")
    if errors:
        messages.error(request, f"部分页面回收失败: {'; '.join(errors)}")

    return redirect(bulk_action.next_url)


@hooks.register("register_log_actions")
def register_myblog_log_actions(actions):
    actions.register_action(
        "myblog.recycle",
        "移入回收站",
        "页面已移入受控回收站",
    )
    actions.register_action(
        "myblog.restore_draft",
        "从回收站恢复草稿",
        "从回收站恢复生成全新草稿版本",
    )
