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

    【配置属性与变量解析】：
    - model (Model): 绑定的主模型为 RecycleBatch（回收操作批次主表）；
    - menu_label (str): 管理后台左侧导航栏显示的菜单名称（"受控回收站"）；
    - icon / menu_icon (str): 菜单图标，使用 Wagtail 内置的 "bin"（垃圾桶）；
    - menu_order (int): 菜单在导航栏中的排列顺序序号（220，置于页面树与设置之间）；
    - add_to_admin_menu (bool): True 声明自动注入左侧主管理菜单；
    - list_display (list[str]): 列表表格呈现的列字段名，包含：
      * root_page_title: 根页面标题；
      * total_pages: 子树包含页面总数；
      * recycled_at: 移入回收站时间；
      * retention_status: 30 天保留倒计时（自定义方法，<=3天标红预警）；
      * actor_username: 执行回收的操作人；
      * status: 当前状态（已回收 / 已恢复 / 已永久删除）；
      * actions: 交互操作列（自定义方法，动态渲染一键恢复与彻底销毁按钮）。
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

    def _lazy_cleanup_expired_batches(self):
        """
        【惰性自愈扫描机制（Lazy Self-Healing Sweep）】

        【教学点】：
        在本地开发环境（如 WSL2 或离线容器）中，通常不会常驻运行 Celery 或 Linux Cron 定时任务。
        通过在管理员每次打开回收站列表（index_view）时“顺手”扫描一次，
        系统能够以极低开销实现“无需外部定时器常驻”的超期 30 天自动物理销毁与容量回收！

        【核心变量】：
        - cutoff: 30 天时间截断点（当前时间减去 30 天）；
        - expired: 查询出所有 status='recycled' 且回收时间早于 cutoff 的过期批次；
        - cleaned_count: 累计清理的批次数；
        - OutboxProcessor.process_pending_events: 顺手触发外发信箱处理器，物理抹除 Mongo 中的历史正文。
        """
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
        """扩展 ModelViewSet 的专属操作子路由：注册恢复与销毁的处理视图"""
        urlpatterns = super().get_urlpatterns()
        return urlpatterns + [
            path("<int:pk>/restore/", self.restore_view, name="restore"),
            path("<int:pk>/purge/", self.purge_view, name="purge"),
        ]

    def index_view(self, request):
        """回收站列表页入口：先执行惰性到期自愈清理，再调用父类渲染管理列表"""
        self._lazy_cleanup_expired_batches()
        return super().index_view(request)

    def restore_view(self, request, pk):
        """
        【一键恢复交互视图（含冲突检测表单与安全提单）】

        【教学点与安全流程】：
        1. 批次有效性核验：只有 status == 'recycled' 的批次才允许恢复，防止重放攻击；
        2. 权限精细化核验：核验当前登录人是否拥有该页面的编辑或删除权限；
        3. GET 阶段预检冲突（Dry-Run Plan）：
           - 调用 RecycleService.plan_restore() 进行冲突推演；
           - 若存在 slug_collision（网址冲突），动态向 HTML 表单注入输入框，提示管理员输入新 slug；
           - 若存在 parent_missing（父级也处于回收站），动态提供“自动连带恢复父栏目”勾选框；
        4. POST 阶段执行恢复：
           - 解析用户提交的改名与父级参数，调用 RecycleService.restore_page_or_subtree；
           - 强制恢复为未发布草稿态，弹窗提示成功恢复的页面总数。
        """
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
        """
        【彻底物理销毁交互视图（不可逆粉碎操作）】

        【教学点与权限升级防御】：
        1. 权限升级准则：若该批次中包含曾正式发布过的页面（any(e.was_live for e in entries)），
           必须具备“发布权限（can_publish）”或“超级管理员”，普通低权限编辑坚决禁止销毁；
        2. 二次确认警示页：GET 请求展示带有高危红色背景的确认页，警示将永久擦除 PG、Mongo 与 S3 数据；
        3. POST 销毁与对账：
           - 调用 RecycleService.purge_page_or_subtree 从数据库移除；
           - 同步触发 OutboxProcessor 物理清除 MongoDB 历史正文快照；
           - 核查 OutboxEvent 事件处理状态，若全部处理完成提示彻底销毁，若 Mongo 未完成则提示告警待对账。
        """
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
    """
    【钩子 1：管理后台资源管理器页面树隔离】

    【教学点】：
    当编辑者浏览 Wagtail 页面树形目录时，通过本钩子动态排除已被回收的页面。
    这样既保留了数据库中的原树物理节点，又确保管理后台视觉上清爽干净。
    """
    recycled_pks = RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
    return queryset.exclude(id__in=recycled_pks)


@hooks.register("construct_page_chooser_queryset")
def hide_recycled_pages_in_chooser(queryset, request):
    """
    【钩子 2：页面选择器弹窗隔离】

    【教学点】：
    当作者在 StreamField 积木中（例如精选关联文章积木 FeaturedPostsBlock）挑选关联页面时，
    通过本钩子过滤掉回收站页面，杜绝外链引用到失效废弃内容。
    """
    recycled_pks = RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
    return queryset.exclude(id__in=recycled_pks)


@hooks.register("before_serve_page")
def block_recycled_pages_front_serve(page, request, serve_args, serve_kwargs):
    """
    【钩子 3：公网路由交付前哨拦截（双态防御机制）】

    【教学点】：
    当前台 HTTP 请求到达具体页面前，检查该页面自身或其任一祖先是否已被移入回收站：
    1. 匿名公众读者：100% 返回标准的 HTTP 404 Not Found，保护站点隐私与 SEO，不暴露内部系统状态；
    2. 已登录后台工作人员：展示专属警示面板，告知被移入回收站的具体时间，并提供跳转后台一键恢复的直达链接。
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
    """
    【钩子 4：单页面原生删除拦截 —— 将物理硬删拦截为受控回收】

    【教学点】：
    Wagtail 默认的页面删除按钮会执行物理级 SQL DELETE。
    本钩子在删除执行前拦截该 POST 请求，调用 RecycleService.recycle_page_or_subtree 执行原树软删除，
    从而实现全站“彻底杜绝物理误删”。
    """
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
    """
    【钩子 5：批量删除操作拦截】

    【教学点与算法优化】：
    当管理员在页面表格勾选多篇页面执行“批量删除”时：
    1. 拓扑剪枝（Top-Level Filtering）：如果同时勾选了父栏目和它的子文章，代码通过祖先集合运算
       (ancestor_ids & selected_ids)，仅提取出最外层的顶层节点；
    2. 统一转入回收站：交由 RecycleService 自动级联整棵子树，避免重复回收引发的冲突报错。
    """
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
            recycled_count += getattr(res, "total_pages", 1)
        except Exception as exc:
            errors.append(f"{p.get_admin_display_title()}: {exc}")

    if recycled_count > 0:
        messages.success(request, f"成功将 {recycled_count} 篇页面移入受控回收站。")
    if errors:
        messages.error(request, f"部分页面回收失败: {'; '.join(errors)}")

    return redirect(bulk_action.next_url)


@hooks.register("register_log_actions")
def register_myblog_log_actions(actions):
    """
    【钩子 6：Wagtail 审计日志动作类型扩展】
    在 Wagtail 原生审计日志中注册回收站动作标识，实现企业级操作溯源。
    """
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
