"""
comments.wagtail_hooks
Wagtail 8.1 评论审核控制台钩子 (C-06 契约)。

落实后台安全隔离：
1. 剪除原生 add/edit/delete/copy 路由，杜绝后台直接修改用户原文或物理硬删；
2. 标题点击平滑导向只读 inspect 详情页；
3. 挂载受控的 approve、reject、hide 与批量审核 bulk_moderate 动作；
4. 注册独立权限 comments.moderate_comment。
"""

import html
import logging
from django.contrib.admin.utils import quote
from django.db.models import Q
from django.http import HttpResponseNotAllowed, JsonResponse
from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from wagtail import hooks
from wagtail.admin import messages
from wagtail.admin.admin_url_finder import ModelAdminURLFinder
from wagtail.admin.menu import MenuItem
from wagtail.admin.ui.tables import Column
from wagtail.admin.utils import get_valid_next_url_from_request
from wagtail.admin.viewsets.model import ModelViewSet
from wagtail.admin.views import generic
from wagtail.admin.views.generic import history as generic_history
from wagtail.admin.widgets.button import ListingButton
from wagtail.models import Page

from .access import (
    can_user_access_page_comment_settings,
    can_user_manage_guest_comments,
)
from .models import Comment, CommentVersion, CommentReviewAction, PageCommentSettings
from .services import CommentService

logger = logging.getLogger(__name__)


def format_current_safe_html(obj: Comment):
    """格式化当前公开生效版本正文"""
    if obj.approved_version_id:
        ver = CommentVersion.objects.filter(id=obj.approved_version_id).first()
        if ver:
            return mark_safe(
                f"<div style='background:#f8fafc;padding:12px;border:1px solid #e2e8f0;border-radius:6px;'>{ver.safe_html}</div>"
            )
    return "（暂无已生效公开版本）"


def format_pending_raw_content(obj: Comment):
    """格式化待审新版本正文（SEC-01：强制 html.escape 转义防 XSS）"""
    if obj.pending_version_id:
        ver = CommentVersion.objects.filter(id=obj.pending_version_id).first()
        if ver:
            escaped_raw = html.escape(ver.raw_content, quote=True)
            risk_html = ""
            if ver.risk_reasons:
                reasons_text = html.escape(", ".join(str(r) for r in ver.risk_reasons), quote=True)
                risk_html = f"<p style='margin:6px 0 0;color:#b45309;font-size:12px;'>风控触发标记：{reasons_text}</p>"
            return mark_safe(
                f"<div style='background:#fffbeb;padding:12px;border:1px solid #fde68a;border-radius:6px;color:#92400e;'>"
                f"<p><strong>待审版本 V{ver.version_number}：</strong></p>"
                f"<pre style='white-space:pre-wrap;word-break:break-word;'>{escaped_raw}</pre>"
                f"{risk_html}</div>"
            )
    return "（无待审修改）"


class CommentViewPermissionMixin:
    """允许具备 comments.moderate_comment 或 comments.view_comment 权限的用户访问评论后台只读/审核视图"""

    def user_has_any_permission(self, permissions):
        user = self.request.user
        if user.is_superuser or user.has_perm("comments.moderate_comment") or user.has_perm("comments.view_comment"):
            return True
        return super().user_has_any_permission(permissions)


class CommentIndexView(CommentViewPermissionMixin, generic.IndexView):
    """定制评论管理列表：标题链接指向 inspect 只读页，提供操作按钮"""
    template_name = "comments/admin/comment_index.html"

    def get_add_url(self):
        return None

    def get_edit_url(self, instance):
        return None

    def get_delete_url(self, instance):
        return None

    def get_copy_url(self, instance):
        return None

    def get_queryset(self):
        qs = super().get_queryset()
        page_type = self.request.GET.get("page_type", "all")
        if page_type == "blog_page":
            qs = qs.filter(page__content_type__model="blogpage")
        elif page_type == "guide":
            qs = qs.filter(page__content_type__model="guidepage")
        elif page_type == "project":
            qs = qs.filter(page__content_type__model="projectpage")

        page_id = self.request.GET.get("page_id")
        if page_id and page_id.isdigit():
            qs = qs.filter(page_id=int(page_id))

        if self.request.GET.get("pending_queue") == "1":
            qs = qs.filter(Q(pending_version__isnull=False) | Q(status="pending"))

        author_key = self.request.GET.get("author_key")
        if author_key:
            qs = qs.filter(Q(author_subject_key=author_key) | Q(author__username=author_key))

        ip = self.request.GET.get("ip")
        if ip:
            qs = qs.filter(ip_address=ip)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        base_qs = Comment.objects.all()
        page_id = self.request.GET.get("page_id")
        if page_id and page_id.isdigit():
            base_qs = base_qs.filter(page_id=int(page_id))

        context["current_page_type"] = self.request.GET.get("page_type", "all")
        context["current_page_id"] = page_id or ""
        context["current_author_key"] = self.request.GET.get("author_key", "")
        context["current_ip"] = self.request.GET.get("ip", "")
        context["current_status"] = self.request.GET.get("status", "")
        context["current_pending_queue"] = self.request.GET.get("pending_queue") == "1"
        context["tab_counts"] = {
            "all": base_qs.count(),
            "blog_page": base_qs.filter(page__content_type__model="blogpage").count(),
            "guide": base_qs.filter(page__content_type__model="guidepage").count(),
            "project": base_qs.filter(page__content_type__model="projectpage").count(),
            "pending": base_qs.filter(Q(pending_version__isnull=False) | Q(status="pending")).count(),
        }
        return context


class CommentInspectView(CommentViewPermissionMixin, generic.InspectView):
    """
    定制评论只读检查详情页。
    1. 禁用 edit/delete 链接，消除 ModelViewSet 剪除编辑/删除路由引起的 NoReverseMatch；
    2. 为计算字段提供中文标签与安全的展示方法；
    3. 展示完整版本列表与审计日志，便于管理员核对。
    """
    template_name = "comments/admin/comment_inspect.html"
    edit_url_name = None
    delete_url_name = None

    CUSTOM_FIELD_LABELS = {
        "id": "评论 ID",
        "page_display": "所属页面",
        "author_display": "作者",
        "current_safe_html": "当前公开生效正文",
        "pending_raw_content": "待审核新版本正文",
        "versions_history": "全部内容版本记录",
        "audit_history": "审核与治理审计记录",
    }

    def get_edit_url(self):
        return None

    def get_delete_url(self):
        return None

    def get_field_label(self, field_name, field):
        if field_name in self.CUSTOM_FIELD_LABELS:
            return self.CUSTOM_FIELD_LABELS[field_name]
        return super().get_field_label(field_name, field)

    def get_page_display_value(self):
        obj = self.object
        if obj.page_id and obj.page:
            comments_url = f"{reverse('comments:index')}?page_id={obj.page_id}"
            return format_html(
                '{} <span style="color:#64748b;">(Page #{})</span> '
                '<a href="{}" class="button button-small button-secondary" style="margin-left:8px;">查看该页全部评论</a>',
                obj.page.title,
                obj.page_id,
                comments_url,
            )
        return format_html(
            '{} <span style="color:#dc2626;">[页面已永久销毁]</span>',
            obj.page_title_snapshot or "未知页面",
        )

    def get_author_display_value(self):
        obj = self.object
        name = obj.author_display
        if obj.author_subject_key:
            trace_url = f"{reverse('comments:index')}?author_key={obj.author_subject_key}"
            return format_html(
                '{} <code style="margin-left:6px;">{}</code> '
                '<a href="{}" class="button button-small button-secondary" style="margin-left:8px;">全站溯源</a>',
                name,
                obj.author_subject_key,
                trace_url,
            )
        return name

    def get_current_safe_html_display_value(self):
        return format_current_safe_html(self.object)

    def get_pending_raw_content_display_value(self):
        return format_pending_raw_content(self.object)

    def get_deletion_reason_display_value(self):
        return self.object.get_deletion_reason_display() or "-"

    def get_ip_address_display_value(self):
        user = self.request.user
        if not (user.is_superuser or user.has_perm("comments.govern_comment_globally") or user.has_perm("comments.moderate_comment")):
            return "（无 IP 溯源权限）"
        if not self.object.ip_address:
            return "-"
        ip_url = f"{reverse('comments:index')}?ip={self.object.ip_address}"
        return format_html(
            '<code>{}</code> <a href="{}" class="button button-small button-secondary" style="margin-left:8px;">按 IP 溯源</a>',
            self.object.ip_address,
            ip_url,
        )

    def get_versions_history_display_value(self):
        versions = list(self.object.versions.select_related("reviewed_by").order_by("-version_number"))
        if not versions:
            return "（暂无版本记录）"
        rows = []
        for ver in versions:
            escaped_raw = html.escape(ver.raw_content, quote=True)
            reviewer = html.escape(ver.reviewed_by.username, quote=True) if ver.reviewed_by else "-"
            created = ver.created_at.strftime("%Y-%m-%d %H:%M:%S")
            status_label = html.escape(ver.get_status_display(), quote=True)
            rows.append(
                f"<div style='border:1px solid #e2e8f0;border-radius:6px;padding:10px;margin-bottom:8px;background:#fff;'>"
                f"<div style='font-size:12px;color:#475569;margin-bottom:6px;'>"
                f"<strong>V{ver.version_number}</strong> (ID={ver.id}) · 状态：{status_label} · 提交时间：{created} · 审核人：{reviewer}"
                f"</div>"
                f"<pre style='margin:0;white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:8px;border-radius:4px;'>{escaped_raw}</pre>"
                f"</div>"
            )
        return mark_safe("".join(rows))

    def get_audit_history_display_value(self):
        logs = list(CommentReviewAction.objects.filter(comment=self.object).select_related("actor").order_by("-created_at")[:20])
        if not logs:
            return "（暂无审核/治理审计记录）"
        rows = []
        for log in logs:
            actor = html.escape(log.actor.username, quote=True) if log.actor else "系统"
            action = html.escape(log.action, quote=True)
            reason = html.escape(log.reason, quote=True) if log.reason else "-"
            ts = log.created_at.strftime("%Y-%m-%d %H:%M:%S")
            ver_str = f"V{log.version_number}" if log.version_number else "-"
            rows.append(
                f"<li><code>{ts}</code> · <strong>{actor}</strong> 执行 <code>{action}</code> ({ver_str}) "
                f"[{html.escape(log.previous_status, quote=True)} → {html.escape(log.new_status, quote=True)}] · 说明：{reason}</li>"
            )
        return mark_safe(f"<ul style='margin:0;padding-left:18px;'>{''.join(rows)}</ul>")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["can_moderate"] = user.is_superuser or user.has_perm("comments.moderate_comment")
        context["can_govern_globally"] = user.is_superuser or user.has_perm("comments.govern_comment_globally")
        return context


class CommentHistoryView(CommentViewPermissionMixin, generic_history.HistoryView):
    """定制评论历史视图：禁用 edit 链接避免 NoReverseMatch"""
    edit_url_name = None
    header_buttons = []


class CommentViewSet(ModelViewSet):
    """Wagtail 8.1 全站评论管理与审核视图集"""
    model = Comment
    menu_label = "全站评论"
    icon = "comment-dots"
    menu_icon = "comment-dots"
    menu_order = 240
    add_to_admin_menu = True
    copy_view_enabled = False
    inspect_view_enabled = True
    index_view_class = CommentIndexView
    inspect_view_class = CommentInspectView
    history_view_class = CommentHistoryView

    list_display = [
        "id",
        Column("page_display", label="所属页面"),
        Column("author_display", label="作者"),
        Column("content_snippet", label="正文预览"),
        "status",
        "is_publicly_visible",
        "reply_count",
        "like_count",
        "created_at",
        Column("actions_column", label="审核操作"),
    ]
    list_filter = ["status", "is_publicly_visible", "created_at"]
    search_fields = ["author__username", "page_title_snapshot"]

    inspect_view_fields = [
        "id",
        "page_display",
        "author_display",
        "status",
        "is_publicly_visible",
        "is_pinned",
        "deletion_reason",
        "state_generation",
        "current_safe_html",
        "pending_raw_content",
        "versions_history",
        "audit_history",
        "reply_count",
        "like_count",
        "dislike_count",
        "can_edit_until",
        "ip_address",
        "created_at",
    ]

    @property
    def url_finder_class(self):
        inspect_url_name = self.get_url_name("inspect")

        class CommentAdminURLFinder(ModelAdminURLFinder):
            def construct_edit_url(self, instance):
                return reverse(inspect_url_name, args=(quote(instance.pk),))

            def get_edit_url(self, instance):
                if self.user and not (
                    self.user.is_superuser
                    or self.user.has_perm("comments.moderate_comment")
                    or self.user.has_perm("comments.view_comment")
                ):
                    return None
                return self.construct_edit_url(instance)

        return CommentAdminURLFinder

    def get_common_view_kwargs(self, **kwargs):
        view_kwargs = super().get_common_view_kwargs(**kwargs)
        view_kwargs.update({
            "add_url_name": None,
            "edit_url_name": None,
            "delete_url_name": None,
            "usage_url_name": None,
        })
        return view_kwargs

    @property
    def menu_item_class(self):
        def is_shown(_self, request):
            user = request.user
            return bool(
                user.is_superuser
                or user.has_perm("comments.moderate_comment")
                or user.has_perm("comments.view_comment")
            )

        return type(
            "CommentMenuItem",
            (MenuItem,),
            {"is_shown": is_shown},
        )

    def page_display(self, obj):
        if obj.page:
            return obj.page.title
        return f"{obj.page_title_snapshot} [页面已永久销毁]"
    page_display.short_description = "所属页面"

    def author_display(self, obj):
        return obj.author_display
    author_display.short_description = "作者"

    def content_snippet(self, obj):
        ver_id = obj.pending_version_id or obj.approved_version_id or obj.latest_version_id
        if ver_id:
            ver = CommentVersion.objects.filter(id=ver_id).first()
            if ver:
                status_tag = f"[{ver.get_status_display()}] " if obj.pending_version_id else ""
                return f"{status_tag}{ver.raw_content[:60]}"
        return "（无内容）"
    content_snippet.short_description = "正文预览"

    def current_safe_html(self, obj):
        return format_current_safe_html(obj)
    current_safe_html.short_description = "当前公开生效正文"

    def pending_raw_content(self, obj):
        return format_pending_raw_content(obj)
    pending_raw_content.short_description = "待审核新版本正文"

    def actions_column(self, obj):
        actions_html = []
        if obj.pending_version_id:
            approve_url = reverse(self.get_url_name("approve"), args=[obj.id])
            reject_url = reverse(self.get_url_name("reject"), args=[obj.id])
            actions_html.append(f"""
            <form method="POST" action="{approve_url}" style="display:inline-block;margin-right:4px;">
                <input type="hidden" name="csrfmiddlewaretoken" value="" class="js-csrf-token">
                <input type="hidden" name="expected_version_id" value="{obj.pending_version_id}">
                <button type="submit" class="button button-small" style="background:#16a34a;color:#fff;">批准</button>
            </form>
            <form method="POST" action="{reject_url}" style="display:inline-block;margin-right:4px;">
                <input type="hidden" name="csrfmiddlewaretoken" value="" class="js-csrf-token">
                <input type="hidden" name="expected_version_id" value="{obj.pending_version_id}">
                <button type="submit" class="button button-small" style="background:#ea580c;color:#fff;">驳回</button>
            </form>
            """)
        elif obj.status == "active":
            hide_url = reverse(self.get_url_name("hide"), args=[obj.id])
            pin_url = reverse(self.get_url_name("pin"), args=[obj.id])
            pin_text = "取消置顶" if obj.is_pinned else "置顶"
            actions_html.append(f"""
            <form method="POST" action="{hide_url}" style="display:inline-block;margin-right:4px;">
                <input type="hidden" name="csrfmiddlewaretoken" value="" class="js-csrf-token">
                <button type="submit" class="button button-small button-secondary">下架隐藏</button>
            </form>
            <form method="POST" action="{pin_url}" style="display:inline-block;">
                <input type="hidden" name="csrfmiddlewaretoken" value="" class="js-csrf-token">
                <button type="submit" class="button button-small button-secondary">{pin_text}</button>
            </form>
            """)
        return mark_safe("".join(actions_html)) if actions_html else "-"
    actions_column.short_description = "审核操作"

    def get_urlpatterns(self):
        """剪除原生 add/edit/delete/copy 路由，挂载审核专用受控路由"""
        conv = self.pk_path_converter
        return [
            path("", self.index_view, name="index"),
            path("results/", self.index_results_view, name="index_results"),
            path(f"inspect/<{conv}:pk>/", self.inspect_view, name="inspect"),
            path(f"history/<{conv}:pk>/", self.history_view, name="history"),
            path(f"history-results/<{conv}:pk>/", self.history_results_view, name="history_results"),
            path(f"<int:pk>/approve/", self.approve_view, name="approve"),
            path(f"<int:pk>/reject/", self.reject_view, name="reject"),
            path(f"<int:pk>/hide/", self.hide_view, name="hide"),
            path(f"<int:pk>/pin/", self.pin_view, name="pin"),
            path("page-settings/<int:page_id>/", self.page_settings_view, name="page_settings"),
            path("page-comments/<int:page_id>/", self.page_comments_view, name="page_comments"),
            path("batch-hide/", self.batch_hide_view, name="batch_hide"),
            path("restrict-subject/", self.restrict_subject_view, name="restrict_subject"),
            path("lift-restriction/", self.lift_restriction_view, name="lift_restriction"),
        ]

    def _check_moderator_permission(self, request):
        if not (request.user.is_superuser or request.user.has_perm("comments.moderate_comment")):
            raise PermissionDenied("当前用户缺少评论审核权限 (comments.moderate_comment)。")

    def _check_global_governance_permission(self, request):
        if not (request.user.is_superuser or request.user.has_perm("comments.govern_comment_globally")):
            raise PermissionDenied("当前用户缺少全站评论治理权限 (comments.govern_comment_globally)。")

    def _redirect_back(self, request):
        next_url = get_valid_next_url_from_request(request)
        if next_url:
            return redirect(next_url)
        return redirect(self.get_url_name("index"))

    def _is_json_request(self, request):
        return (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or request.POST.get("format") == "json"
            or request.GET.get("format") == "json"
        )

    def approve_view(self, request, pk):
        self._check_moderator_permission(request)
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        ver_id = request.POST.get("expected_version_id")
        expected_ver_id = int(ver_id) if ver_id and ver_id.isdigit() else None
        try:
            CommentService.moderate_comment(
                comment_id=pk,
                action="approve",
                reviewer=request.user,
                expected_version_id=expected_ver_id,
                require_expected_version=True,
            )
            if self._is_json_request(request):
                return JsonResponse({"status": "success", "message": f"评论 #{pk} 审核通过上线！"})
            messages.success(request, f"评论 #{pk} 已成功审核通过上线！")
        except Exception as exc:
            if self._is_json_request(request):
                return JsonResponse({"status": "error", "message": str(exc)}, status=400)
            messages.error(request, f"审核失败: {exc}")
        return self._redirect_back(request)

    def reject_view(self, request, pk):
        self._check_moderator_permission(request)
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        ver_id = request.POST.get("expected_version_id")
        expected_ver_id = int(ver_id) if ver_id and ver_id.isdigit() else None
        try:
            CommentService.moderate_comment(
                comment_id=pk,
                action="reject",
                reviewer=request.user,
                expected_version_id=expected_ver_id,
                require_expected_version=True,
            )
            if self._is_json_request(request):
                return JsonResponse({"status": "success", "message": f"评论 #{pk} 待审修改已驳回。"})
            messages.success(request, f"评论 #{pk} 的待审修改已被驳回。")
        except Exception as exc:
            if self._is_json_request(request):
                return JsonResponse({"status": "error", "message": str(exc)}, status=400)
            messages.error(request, f"驳回失败: {exc}")
        return self._redirect_back(request)

    def hide_view(self, request, pk):
        self._check_moderator_permission(request)
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        try:
            CommentService.moderate_comment(
                comment_id=pk,
                action="hide",
                reviewer=request.user,
            )
            if self._is_json_request(request):
                return JsonResponse({"status": "success", "message": f"评论 #{pk} 已下架隐藏。"})
            messages.success(request, f"评论 #{pk} 已下架隐藏。")
        except Exception as exc:
            if self._is_json_request(request):
                return JsonResponse({"status": "error", "message": str(exc)}, status=400)
            messages.error(request, f"隐藏失败: {exc}")
        return self._redirect_back(request)

    def pin_view(self, request, pk):
        self._check_moderator_permission(request)
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        try:
            comment = CommentService.toggle_pin_comment(
                comment_id=pk,
                actor=request.user,
            )
            action_str = "置顶" if comment.is_pinned else "取消置顶"
            if self._is_json_request(request):
                return JsonResponse({"status": "success", "is_pinned": comment.is_pinned, "message": f"评论 #{pk} 已{action_str}。"})
            messages.success(request, f"评论 #{pk} 已{action_str}。")
        except Exception as exc:
            if self._is_json_request(request):
                return JsonResponse({"status": "error", "message": str(exc)}, status=400)
            messages.error(request, f"置顶操作失败: {exc}")
        return self._redirect_back(request)

    def page_settings_view(self, request, page_id):
        if request.method not in ("GET", "HEAD", "POST"):
            return JsonResponse({"status": "error", "message": "Method not allowed"}, status=405)

        page = Page.objects.filter(id=page_id).first()
        if page is None:
            return JsonResponse({"status": "error", "message": "目标页面不存在"}, status=404)

        if not can_user_access_page_comment_settings(request.user, page):
            return JsonResponse({"status": "error", "message": "当前用户无权查看或修改该页面的评论设置。"}, status=403)

        can_manage_guest = can_user_manage_guest_comments(request.user, page)
        if request.method == "POST":
            has_guest_param = "allow_guest_comments" in request.POST
            if has_guest_param and not can_manage_guest:
                return JsonResponse(
                    {"status": "error", "message": "仅超级管理员、全站评论治理员或栏目主编可修改受控访客留言开关。"},
                    status=403,
                )

            allow_comments = request.POST.get("allow_comments") == "true"
            close_reason = request.POST.get("close_reason", "").strip()
            auto_moderate = request.POST.get("auto_moderate_new") == "true"
            allow_guest_val = (request.POST.get("allow_guest_comments") == "true") if has_guest_param else None

            try:
                settings_obj = CommentService.update_page_comment_settings(
                    page_id=page_id,
                    actor=request.user,
                    allow_comments=allow_comments,
                    close_reason=close_reason,
                    auto_moderate_new=auto_moderate,
                    allow_guest_comments=allow_guest_val,
                )
            except PermissionDenied as exc:
                return JsonResponse({"status": "error", "message": str(exc)}, status=403)
            except ValidationError as exc:
                return JsonResponse({"status": "error", "message": str(exc)}, status=400)

            return JsonResponse({
                "status": "success",
                "page_id": page_id,
                "allow_comments": settings_obj.allow_comments,
                "allow_guest_comments": settings_obj.allow_guest_comments,
                "close_reason": settings_obj.close_reason,
                "auto_moderate_new": settings_obj.auto_moderate_new,
                "can_manage_guest_comments": can_manage_guest,
            })

        settings_obj = PageCommentSettings.objects.filter(page_id=page_id).first()
        return JsonResponse({
            "status": "success",
            "page_id": page_id,
            "allow_comments": settings_obj.allow_comments if settings_obj else True,
            "allow_guest_comments": settings_obj.allow_guest_comments if settings_obj else False,
            "close_reason": settings_obj.close_reason if settings_obj else "",
            "auto_moderate_new": settings_obj.auto_moderate_new if settings_obj else False,
            "can_manage_guest_comments": can_manage_guest,
        })

    def page_comments_view(self, request, page_id):
        page = Page.objects.filter(id=page_id).first()
        if page is None:
            return JsonResponse({"status": "error", "message": "目标页面不存在"}, status=404)
        if not (
            can_user_access_page_comment_settings(request.user, page)
            or (
                request.user.is_authenticated
                and request.user.is_active
                and (
                    request.user.has_perm("comments.moderate_comment")
                    or request.user.has_perm("comments.view_comment")
                )
            )
        ):
            return JsonResponse({"status": "error", "message": "当前用户无权查看该页面的评论面板。"}, status=403)
        filt = request.GET.get("filter", "all")
        qs = Comment.objects.filter(page_id=page_id)
        if filt == "pending":
            qs = qs.filter(pending_version__isnull=False)
        elif filt == "active":
            qs = qs.filter(status="active", is_publicly_visible=True)

        comments = qs.select_related(
            "author", "author__member_profile", "pending_version", "approved_version"
        ).order_by("-is_pinned", "-created_at")[:50]
        items = []
        for c in comments:
            ver = c.pending_version or c.approved_version
            raw = ver.raw_content if ver else ""
            items.append({
                "id": c.id,
                "author_name": c.author_display,
                "status": c.status,
                "is_publicly_visible": c.is_publicly_visible,
                "is_pinned": c.is_pinned,
                "pending_version_id": c.pending_version_id,
                "pending_version_number": c.pending_version.version_number if c.pending_version else None,
                "content_snippet": raw[:120] if raw else "（无内容）",
                "created_at": c.created_at.strftime("%Y-%m-%d %H:%M"),
            })
        can_moderate = bool(request.user.is_superuser or request.user.has_perm("comments.moderate_comment"))
        return JsonResponse({"status": "success", "page_id": page_id, "can_moderate": can_moderate, "items": items})

    def batch_hide_view(self, request):
        try:
            self._check_global_governance_permission(request)
        except PermissionDenied as exc:
            return JsonResponse({"status": "error", "message": str(exc)}, status=403)
        if request.method != "POST":
            return JsonResponse({"status": "error", "message": "Method not allowed"}, status=405)
        c_ids = request.POST.getlist("comment_ids") or request.POST.getlist("comment_ids[]")
        if not c_ids:
            return JsonResponse({"status": "error", "message": "未选择评论"}, status=400)
        ids = [int(i) for i in c_ids if str(i).isdigit()]
        res = CommentService.batch_hide_comments(actor=request.user, comment_ids=ids)
        return JsonResponse({"status": "success", "result": res})

    def restrict_subject_view(self, request):
        try:
            self._check_global_governance_permission(request)
        except PermissionDenied as exc:
            return JsonResponse({"status": "error", "message": str(exc)}, status=403)
        if request.method != "POST":
            return JsonResponse({"status": "error", "message": "Method not allowed"}, status=405)
        kind = request.POST.get("kind", "user")
        subject_key = request.POST.get("subject_key", "").strip()
        reason = request.POST.get("reason", "管理后台限制").strip()
        if not subject_key:
            return JsonResponse({"status": "error", "message": "主体标识不能为空"}, status=400)
        try:
            sub = CommentService.restrict_subject(kind=kind, subject_key=subject_key, hours=24, reason=reason, actor=request.user)
            return JsonResponse({
                "status": "success",
                "subject_key": sub.subject_key,
                "restricted_until": sub.restricted_until.strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as e:
            return JsonResponse({"status": "error", "message": str(e)}, status=400)

    def lift_restriction_view(self, request):
        try:
            self._check_global_governance_permission(request)
        except PermissionDenied as exc:
            return JsonResponse({"status": "error", "message": str(exc)}, status=403)
        if request.method != "POST":
            return JsonResponse({"status": "error", "message": "Method not allowed"}, status=405)
        kind = request.POST.get("kind", "user")
        subject_key = request.POST.get("subject_key", "").strip()
        reason = request.POST.get("reason", "管理后台提前解封").strip()
        if not subject_key:
            return JsonResponse({"status": "error", "message": "主体标识不能为空"}, status=400)
        try:
            lifted = CommentService.lift_restriction(
                kind=kind,
                subject_key=subject_key,
                actor=request.user,
                reason=reason,
            )
            return JsonResponse({"status": "success", "lifted": lifted, "subject_key": subject_key})
        except Exception as exc:
            return JsonResponse({"status": "error", "message": str(exc)}, status=400)


comment_viewset = CommentViewSet("comments")


@hooks.register("register_admin_viewset")
def register_comment_viewset():
    return comment_viewset


@hooks.register("register_permissions")
def register_comment_permissions():
    """向 Wagtail 后台角色管理暴露评论专属审核权限"""
    from django.contrib.auth.models import Permission
    return Permission.objects.filter(
        content_type__app_label="comments",
        codename__in=[
            "moderate_comment",
            "govern_comment_globally",
            "view_comment",
            "change_communitysettings",
        ],
    )


@hooks.register("register_page_listing_more_buttons")
def page_listing_comment_button(page, user, next_url=None):
    """在 Wagtail Explorer 页面列表每行后追加 [💬 评论管理] 快捷跳转动作"""
    if getattr(page.specific_class, "supports_comments", False):
        if user.is_superuser or user.has_perm("comments.moderate_comment"):
            url = f"{reverse('comments:index')}?page_id={page.id}"
            yield ListingButton(
                "💬 评论管理",
                url=url,
                priority=25,
                attrs={"title": f"管理《{page.title}》的评论与互动"},
            )
