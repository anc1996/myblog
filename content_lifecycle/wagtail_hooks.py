"""
Wagtail 管理后台回收站隔离与菜单集成钩子。

落实核心隔离契约：
1. construct_explorer_page_queryset: 在普通管理后台页面树列表中隐藏已回收页面；
2. construct_page_chooser_queryset: 在页面选择器中隐藏已回收页面；
3. before_serve_page: 前台访问拦截任何标记为 is_recycled 的页面（访客 404 / 编辑者友好引导）；
4. before_delete_page & before_bulk_action: 拦截单页与批量删除，统一转入受控回收站，杜绝底库物理硬删；
5. 注册回收站管理菜单、30天超期惰性清理与冲突处理安全操作视图。
"""

from datetime import timedelta
from django.http import Http404, HttpResponse
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe
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
    list_display = ["root_page_title", "total_pages", "recycled_at", "retention_status", "actor_username", "status", "actions"]
    list_filter = ["status", "recycled_at"]
    search_fields = ["root_page_title", "actor_username"]
    inspect_view_enabled = True
    inspect_view_fields = ["batch_id", "root_page_title", "total_pages", "recycled_at", "actor_username", "status"]
    form_fields = ["status"]

    def retention_status(self, obj):
        """显示 30 天保留倒计时（<=3 天标红预警）"""
        if obj.status != "recycled":
            return "-"
        elapsed_days = (timezone.now() - obj.recycled_at).days
        remaining_days = max(0, 30 - elapsed_days)
        if remaining_days <= 3:
            return mark_safe(f'<span style="color:#dc2626;font-weight:bold;">已滞留 {elapsed_days} 天 (剩余 {remaining_days} 天)</span>')
        return f"已滞留 {elapsed_days} 天 (剩余 {remaining_days} 天)"

    retention_status.short_description = "保留倒计时(30天)"

    def _lazy_cleanup_expired_batches(self):
        """WSL2 / 本地开机自愈扫描：访问回收站列表时顺手扫描并物理销毁超期 30 天的废弃批次"""
        from content_lifecycle.recycle import RecycleService
        from external_content.outbox import OutboxProcessor

        cutoff = timezone.now() - timedelta(days=30)
        expired = RecycleBatch.objects.filter(status="recycled", recycled_at__lte=cutoff)
        cleaned_count = 0
        for b in expired:
            try:
                RecycleService.purge_page_or_subtree(b.root_page_id, actor=None, batch_id=b.batch_id)
                cleaned_count += 1
            except Exception:
                pass
        if cleaned_count > 0:
            try:
                OutboxProcessor.process_pending_events(batch_size=50)
            except Exception:
                pass

    def get_urlpatterns(self):
        urlpatterns = super().get_urlpatterns()
        return urlpatterns + [
            path("<int:pk>/restore/", self.restore_view, name="restore"),
            path("<int:pk>/purge/", self.purge_view, name="purge"),
        ]

    def index_view(self, request):
        self._lazy_cleanup_expired_batches()
        return super().index_view(request)

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

            # 冲突检测展示与表单注入
            conflicts = plan.get("conflicts", [])
            conflict_inputs_html = ""
            if conflicts:
                conflict_inputs_html += "<div style='background:#fef2f2;border:1px solid #fecaca;padding:16px;border-radius:6px;margin:16px 0;'>"
                conflict_inputs_html += "<h3 style='color:#991b1b;margin-top:0;'>⚠️ 检测到以下恢复冲突，请确认处理方式：</h3><ul style='color:#7f1d1d;'>"
                for c in conflicts:
                    pid = c.get("page_id")
                    ctype = c.get("type")
                    cmsg = escape(c.get("message", ""))
                    conflict_inputs_html += f"<li>{cmsg}</li>"
                    if ctype == "slug_collision":
                        sugg = escape(c.get("suggested_slug", f"slug-{pid}-restored"))
                        conflict_inputs_html += f"""
                        <div style='margin:8px 0 12px 16px;'>
                            <label style='font-weight:600;display:block;'>指定新网址 (Slug)：</label>
                            <input type='text' name='slug_{pid}' value='{sugg}' style='padding:6px 10px;border:1px solid #cbd5e1;border-radius:4px;width:300px;' required>
                        </div>
                        """
                    elif ctype == "parent_missing_or_recycled":
                        conflict_inputs_html += f"""
                        <div style='margin:8px 0 12px 16px;'>
                            <p style='margin:4px 0;'><strong>处理选项：</strong></p>
                            <label style='display:block;margin-bottom:6px;'>
                                <input type='checkbox' name='restore_parent_batch' value='1' checked> 自动连带恢复上级栏目
                            </label>
                            <label style='display:block;'>
                                或输入其他目标栏目 ID：<input type='number' name='target_parent_id' placeholder='留空则连带恢复上级' style='padding:4px 8px;border:1px solid #cbd5e1;border-radius:4px;width:150px;'>
                            </label>
                        </div>
                        """
                conflict_inputs_html += "</ul></div>"

            html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>确认恢复页面</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;padding:30px;line-height:1.6;background:#f8fafc;">
    <div style="background:#fff;padding:24px 32px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1);max-width:680px;margin:auto;">
        <h2 style="margin-top:0;color:#0f172a;">确认恢复页面</h2>
        <p style="color:#475569;">确认将 <strong>'{safe_title}'</strong> （共 {batch.total_pages} 篇页面）原位恢复为未发布草稿？</p>
        <form method="POST">
            <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
            {conflict_inputs_html}
            <div style="margin-top:20px;">
                <button type="submit" style="background:#0284c7;color:#fff;border:none;padding:8px 20px;border-radius:4px;cursor:pointer;font-weight:500;">确认执行恢复</button>
                <a href="{cancel_url}" style="margin-left:12px;color:#64748b;text-decoration:none;">取消</a>
            </div>
        </form>
    </div>
</body>
</html>"""
            return HttpResponse(html)

        # POST 请求参数解析
        slug_overrides = {}
        target_parent_id = None
        restore_parent_batch = request.POST.get("restore_parent_batch") == "1"
        if request.POST.get("target_parent_id"):
            try:
                target_parent_id = int(request.POST.get("target_parent_id"))
            except ValueError:
                target_parent_id = None

        for k, v in request.POST.items():
            if k.startswith("slug_") and v.strip():
                try:
                    pid = int(k.replace("slug_", ""))
                    slug_overrides[pid] = v.strip()
                except ValueError:
                    pass

        # 连带恢复父栏目批次处理
        if restore_parent_batch and not target_parent_id:
            try:
                root_p = Page.objects.get(id=batch.root_page_id).specific
                parent = root_p.get_parent()
                if parent and getattr(parent, "is_recycled", False):
                    p_batch = RecycleBatch.objects.filter(entries__page_id=parent.id, status="recycled").first()
                    if p_batch:
                        RecycleService.restore_page_or_subtree(p_batch.batch_id, actor=request.user)
            except Exception as p_exc:
                messages.warning(request, f"连带恢复父栏目出现提示: {p_exc}")

        try:
            res = RecycleService.restore_page_or_subtree(
                batch.batch_id,
                actor=request.user,
                slug_overrides=slug_overrides,
                target_parent_id=target_parent_id,
            )
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
<body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;padding:30px;line-height:1.6;background:#f8fafc;">
    <div style="background:#fff;padding:24px 32px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1);max-width:680px;margin:auto;">
        <h2 style="color:#dc2626;margin-top:0;">确认永久销毁</h2>
        <p style="color:#475569;"><strong>警告：</strong>永久删除将彻底物理销毁页面 <strong>'{safe_title}'</strong> 及其在 PostgreSQL 与 MongoDB 中的全部历史版本，不可撤销！</p>
        <form method="POST">
            <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
            <div style="margin-top:20px;">
                <button type="submit" style="background:#dc2626;color:#fff;border:none;padding:8px 20px;border-radius:4px;cursor:pointer;font-weight:500;">确认彻底物理销毁</button>
                <a href="{cancel_url}" style="margin-left:12px;color:#64748b;text-decoration:none;">取消</a>
            </div>
        </form>
    </div>
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
    """
    前台访问拦截与编辑引导契约：
    1. 未登录/普通访客访问已回收页面：100% 返回 HTTP 404，保护内容隐私与 SEO；
    2. 已登录后台编辑人员访问：返回专属友好提示面板，告知何时被移入回收站，并提供一键恢复直达按钮。
    """
    is_recycled = getattr(page, "is_recycled", False)
    if not is_recycled:
        for ancestor in page.get_ancestors():
            if getattr(ancestor, "is_recycled", False):
                is_recycled = True
                break

    if is_recycled:
        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False) and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            from content_lifecycle.models import RecycleEntry
            entry = RecycleEntry.objects.filter(page_id=page.id, status="recycled").select_related("batch").first()
            recycled_time = entry.batch.recycled_at.strftime("%Y-%m-%d %H:%M:%S") if entry and entry.batch and entry.batch.recycled_at else "近期"
            restore_url = "/admin/recycle_bin/"
            safe_title = escape(page.title)
            html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>页面已移入回收站 - {safe_title}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;background:#f8fafc;padding:50px;display:flex;justify-content:center;">
    <div style="background:#fff;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,0.08);max-width:560px;padding:36px;border-top:5px solid #f59e0b;">
        <h2 style="margin-top:0;color:#1e293b;">⚠️ 该页面当前处于【受控回收站】</h2>
        <p style="color:#64748b;line-height:1.6;">页面 <strong>'{safe_title}'</strong> 已于 <code>{recycled_time}</code> 被移入受控回收站，当前对公网访客表现为 404 Not Found。</p>
        <p style="color:#64748b;line-height:1.6;">您作为网站管理员/编辑者，可前往管理后台查阅回收批次并执行原位一键恢复。</p>
        <div style="margin-top:24px;">
            <a href="{restore_url}" style="background:#0284c7;color:#fff;text-decoration:none;padding:10px 20px;border-radius:6px;font-weight:500;display:inline-block;">前往受控回收站恢复</a>
        </div>
    </div>
</body>
</html>"""
            return HttpResponse(html, status=404)
        raise Http404("页面已移入回收站")


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
