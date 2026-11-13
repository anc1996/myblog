"""
Wagtail 管理后台回收站隔离与菜单集成钩子。

落实核心隔离契约：
1. construct_explorer_page_queryset: 在普通管理后台页面树列表中隐藏已回收页面；
2. construct_page_chooser_queryset: 在页面选择器中隐藏已回收页面；
3. before_serve_page: 前台访问拦截任何标记为 is_recycled 的页面（访客 404 / 编辑者友好引导）；
4. before_delete_page & before_bulk_action: 拦截单页与批量删除，统一转入受控回收站，杜绝底库物理硬删；
5. 注册回收站管理菜单、30天超期惰性清理与冲突处理安全操作视图。
"""

from django.http import Http404, HttpResponse
from django.core.exceptions import PermissionDenied, ValidationError
from django.dispatch import receiver
from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect
from django.middleware.csrf import get_token
from django.utils.html import escape
from wagtail import hooks
from wagtail.admin import messages
from wagtail.admin.viewsets.model import ModelViewSet
from wagtail.models import Page
from wagtail.signals import init_new_page
from content_lifecycle.models import CustomBasePage, RecycleBatch, RecycleEntry
from content_lifecycle.permissions import (
    can_restore_recycled_page,
    user_is_restricted_author_for_page,
)
from content_lifecycle.views import (
    OwnershipPageViewSet,
    RecycleBatchActionsColumn,
    RecycleBatchIndexView,
    RecycleBatchInspectView,
)


class RecycleBatchViewSet(ModelViewSet):
    """
    Wagtail 8.1 管理后台【受控回收站】专属管理视图集。
    在左侧菜单栏常驻“受控回收站”，仅提供按归属过滤的列表查看、一键精准恢复与超管专属彻底物理销毁入口，
    彻底剪除原生 add/edit/delete 表单旁路并关闭打开列表时的自动清库。
    """
    model = RecycleBatch
    menu_label = "受控回收站"
    icon = "bin"
    menu_icon = "bin"
    menu_order = 220
    add_to_admin_menu = True
    index_view_class = RecycleBatchIndexView
    inspect_view_class = RecycleBatchInspectView
    list_display = [
        "root_page_title",
        "total_pages",
        "recycled_at",
        "retention_status",
        "actor_username",
        "status",
        RecycleBatchActionsColumn("actions", label="回收站操作"),
    ]
    list_filter = ["status", "recycled_at"]
    search_fields = ["root_page_title", "actor_username"]
    inspect_view_enabled = True
    inspect_view_fields = ["batch_id", "root_page_title", "total_pages", "recycled_at", "actor_username", "status"]

    def get_urlpatterns(self):
        """仅注册受控只读列表、详情以及恢复与超管销毁视图，剪除 add/edit/delete 旁路。"""
        conv = self.pk_path_converter
        return [
            path("", self.index_view, name="index"),
            path("results/", self.index_results_view, name="index_results"),
            path(f"inspect/<{conv}:pk>/", self.inspect_view, name="inspect"),
            path("<int:pk>/restore/", self.restore_view, name="restore"),
            path("<int:pk>/purge/", self.purge_view, name="purge"),
        ]

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
            if not can_restore_recycled_page(request.user, root_page, batch):
                raise PermissionDenied("当前用户无权恢复此回收批次")
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
        【彻底物理销毁交互视图（仅限超级管理员）】
        """
        if not request.user.is_authenticated or not request.user.is_superuser:
            raise PermissionDenied("彻底物理销毁仅限超级管理员执行")

        batch = get_object_or_404(RecycleBatch, pk=pk)
        if batch.status != "recycled":
            messages.error(request, "该批次不处于已回收状态，旧销毁链接已失效")
            return redirect(self.get_url_name("index"))

        try:
            Page.objects.get(id=batch.root_page_id)
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

            purged_ids = [n["page_id"] for n in res.get("nodes", [])] or [batch.root_page_id]
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
ownership_page_viewset = OwnershipPageViewSet()


@hooks.register("register_admin_viewset")
def register_recycle_batch_viewset():
    return recycle_batch_viewset


@hooks.register("register_admin_viewset", order=0)
def register_ownership_page_viewset():
    return ownership_page_viewset


@hooks.register("before_create_page")
def guard_restricted_author_page_create(request, parent_page, page_class):
    """通用建页门禁：受限作者只能在 author_allow_child_creation 父页下创建 author_creatable 模型。"""
    if user_is_restricted_author_for_page(request.user, parent_page):
        if (
            getattr(parent_page, "is_recycled", False)
            or not getattr(parent_page, "author_allow_child_creation", False)
            or not getattr(page_class, "author_creatable", False)
        ):
            raise PermissionDenied("受限作者仅可在授权栏目大厅下创建允许的内容页面。")


@hooks.register("before_publish_page")
def guard_page_publish_lifecycle(request, page):
    """通用发布门禁：触发 CustomBasePage.validate_for_publish 核查回收状态、权限与受限媒体归属。"""
    specific = page.specific
    if hasattr(specific, "validate_for_publish"):
        try:
            specific.validate_for_publish(user=request.user, revision=specific.get_latest_revision())
        except ValidationError as exc:
            msg = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
            messages.error(request, msg)
            return redirect("wagtailadmin_pages:edit", page.id)


@hooks.register("before_copy_page")
def guard_restricted_author_copy(request, page):
    """通用复制门禁：禁止受限作者创建 Alias 别名或直接复制上线。"""
    if user_is_restricted_author_for_page(request.user, page):
        if not page.permissions_for_user(request.user).can_copy():
            raise PermissionDenied("无权复制此页面。")
        if request.method == "POST":
            if request.POST.get("alias") or request.POST.get("publish_copies") or request.POST.get("copy_subpages"):
                raise PermissionDenied("受限作者禁止创建页面别名、批量复制子页或直接复制发布。")


@hooks.register("after_copy_page")
def enforce_copied_page_ownership(request, page, new_page):
    """受限作者复制产物强约束：归属本人、全新未发布草稿。"""
    if user_is_restricted_author_for_page(request.user, page):
        updates = {}
        if new_page.owner_id != request.user.pk:
            updates["owner_id"] = request.user.pk
        if new_page.live:
            updates["live"] = False
            updates["live_revision_id"] = None
            updates["has_unpublished_changes"] = True
        if updates:
            Page.objects.filter(pk=new_page.pk).update(**updates)


@hooks.register("before_bulk_action", order=-1)
def guard_page_bulk_actions(request, action_type, objects, bulk_action):
    """通用批量动作门禁：任一选中页面未通过对象级权限或发布门禁则整批拒绝。"""
    if not issubclass(bulk_action.model, Page):
        return None
    if action_type not in ("publish", "unpublish", "delete"):
        return None

    raw_ids = [int(i) for i in request.GET.getlist("id") if str(i).isdigit()]
    check_pages = list(Page.objects.filter(pk__in=raw_ids)) if raw_ids else list(objects)
    if not check_pages:
        return None

    for page in check_pages:
        perms = page.permissions_for_user(request.user)
        if action_type == "publish":
            if not perms.can_publish():
                raise PermissionDenied("批量发布包含无权发布的页面，已整批拒绝。")
            specific = page.specific
            if hasattr(specific, "validate_for_publish"):
                try:
                    specific.validate_for_publish(user=request.user, revision=specific.get_latest_revision())
                except ValidationError as exc:
                    msg = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
                    messages.error(request, msg)
                    return redirect(bulk_action.next_url)
        elif action_type == "unpublish":
            if not perms.can_unpublish():
                raise PermissionDenied("批量下架包含无权下架的页面，已整批拒绝。")
        elif action_type == "delete":
            if not perms.can_delete():
                raise PermissionDenied("批量删除包含无权删除的页面，已整批拒绝。")

    return None


@receiver(init_new_page)
def prefill_linked_person_on_new_page(sender, page, parent, **kwargs):
    """通用作者关联预填 (P1-4)：为已绑定 linked_user 的创作者自动预填第一署名人物。"""
    if not isinstance(page, CustomBasePage):
        return
    owner = getattr(page, "owner", None)
    if not owner or not getattr(owner, "is_authenticated", False):
        return
    if not hasattr(page, "authors"):
        return
    try:
        if list(page.authors.all()):
            return
    except Exception:
        pass

    from people.models import Person

    person = Person.objects.filter(linked_user_id=owner.pk).first()
    if not person:
        return
    rel_descriptor = getattr(type(page), "authors", None)
    rel_model = getattr(getattr(rel_descriptor, "rel", None), "related_model", None)
    if rel_model is not None:
        role_kwargs = {}
        try:
            role_field = rel_model._meta.get_field("role")
            if role_field.choices:
                role_kwargs["role"] = "primary_author"
        except Exception:
            pass
        page.authors.add(rel_model(person=person, sort_order=0, **role_kwargs))


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


def can_user_view_recycled_notice(user, page) -> bool:
    """判断访问已回收页面原前台 URL 的用户是否有权查看受控回收站恢复引导。"""
    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    has_admin = bool(getattr(user, "is_staff", False) or user.has_perm("wagtailadmin.access_admin"))
    if not has_admin:
        return False
    if not user_is_restricted_author_for_page(user, page):
        return True
    return getattr(page, "owner_id", None) == user.pk


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
        if can_user_view_recycled_notice(user, page):
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
