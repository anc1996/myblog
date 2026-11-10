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
from django.db.models import Q
from django.http import JsonResponse
from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.safestring import mark_safe
from wagtail import hooks
from wagtail.admin import messages
from wagtail.admin.viewsets.model import ModelViewSet
from wagtail.admin.views import generic
from wagtail.admin.widgets.button import ListingButton

from .models import Comment, CommentVersion, PageCommentSettings
from .services import CommentService

logger = logging.getLogger(__name__)

class CommentIndexView(generic.IndexView):
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
        context["current_status"] = self.request.GET.get("status", "")
        context["tab_counts"] = {
            "all": base_qs.count(),
            "blog_page": base_qs.filter(page__content_type__model="blogpage").count(),
            "guide": base_qs.filter(page__content_type__model="guidepage").count(),
            "project": base_qs.filter(page__content_type__model="projectpage").count(),
            "pending": base_qs.filter(pending_version__isnull=False).count(),
        }
        return context


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

    list_display = [
        "id",
        "page_display",
        "author_display",
        "content_snippet",
        "status",
        "is_publicly_visible",
        "reply_count",
        "like_count",
        "created_at",
        "actions_column",
    ]
    list_filter = ["status", "is_publicly_visible", "created_at"]
    search_fields = ["author__username", "page_title_snapshot"]

    inspect_view_fields = [
        "id",
        "page_display",
        "author_display",
        "status",
        "is_publicly_visible",
        "current_safe_html",
        "pending_raw_content",
        "reply_count",
        "like_count",
        "dislike_count",
        "can_edit_until",
        "ip_address",
        "created_at",
    ]

    def page_display(self, obj):
        if obj.page:
            return obj.page.title
        return f"{obj.page_title_snapshot} [页面已永久销毁]"
    page_display.short_description = "所属页面"

    def author_display(self, obj):
        return obj.author.username if obj.author else "匿名/已注销"
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
        if obj.approved_version_id:
            ver = CommentVersion.objects.filter(id=obj.approved_version_id).first()
            if ver:
                return mark_safe(f"<div style='background:#f8fafc;padding:12px;border:1px solid #e2e8f0;border-radius:6px;'>{ver.safe_html}</div>")
        return "（暂无已生效公开版本）"
    current_safe_html.short_description = "当前公开生效正文"

    def pending_raw_content(self, obj):
        if obj.pending_version_id:
            ver = CommentVersion.objects.filter(id=obj.pending_version_id).first()
            if ver:
                # SEC-01 关键修复：防止待审原文存在未净化脚本引起存储型 XSS
                escaped_raw = html.escape(ver.raw_content, quote=True)
                return mark_safe(f"<div style='background:#fffbeb;padding:12px;border:1px solid #fde68a;border-radius:6px;color:#92400e;'><p><strong>待审版本 V{ver.version_number}：</strong></p><pre>{escaped_raw}</pre></div>")
        return "（无待审修改）"
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
            path(f"<int:pk>/approve/", self.approve_view, name="approve"),
            path(f"<int:pk>/reject/", self.reject_view, name="reject"),
            path(f"<int:pk>/hide/", self.hide_view, name="hide"),
            path(f"<int:pk>/pin/", self.pin_view, name="pin"),
            path("page-settings/<int:page_id>/", self.page_settings_view, name="page_settings"),
            path("page-comments/<int:page_id>/", self.page_comments_view, name="page_comments"),
            path("batch-hide/", self.batch_hide_view, name="batch_hide"),
            path("restrict-subject/", self.restrict_subject_view, name="restrict_subject"),
        ]

    def _check_moderator_permission(self, request):
        if not (request.user.is_superuser or request.user.has_perm("comments.moderate_comment")):
            raise PermissionDenied("当前用户缺少评论审核权限 (comments.moderate_comment)。")

    def _is_json_request(self, request):
        return (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or request.POST.get("format") == "json"
            or request.GET.get("format") == "json"
        )

    def approve_view(self, request, pk):
        self._check_moderator_permission(request)
        if request.method != "POST":
            return redirect(self.get_url_name("index"))
        ver_id = request.POST.get("expected_version_id")
        expected_ver_id = int(ver_id) if ver_id and ver_id.isdigit() else None
        try:
            CommentService.moderate_comment(
                comment_id=pk,
                action="approve",
                reviewer=request.user,
                expected_version_id=expected_ver_id,
            )
            if self._is_json_request(request):
                return JsonResponse({"status": "success", "message": f"评论 #{pk} 审核通过上线！"})
            messages.success(request, f"评论 #{pk} 已成功审核通过上线！")
        except Exception as exc:
            if self._is_json_request(request):
                return JsonResponse({"status": "error", "message": str(exc)}, status=400)
            messages.error(request, f"审核失败: {exc}")
        return redirect(self.get_url_name("index"))

    def reject_view(self, request, pk):
        self._check_moderator_permission(request)
        if request.method != "POST":
            return redirect(self.get_url_name("index"))
        ver_id = request.POST.get("expected_version_id")
        expected_ver_id = int(ver_id) if ver_id and ver_id.isdigit() else None
        try:
            CommentService.moderate_comment(
                comment_id=pk,
                action="reject",
                reviewer=request.user,
                expected_version_id=expected_ver_id,
            )
            if self._is_json_request(request):
                return JsonResponse({"status": "success", "message": f"评论 #{pk} 待审修改已驳回。"})
            messages.success(request, f"评论 #{pk} 的待审修改已被驳回。")
        except Exception as exc:
            if self._is_json_request(request):
                return JsonResponse({"status": "error", "message": str(exc)}, status=400)
            messages.error(request, f"驳回失败: {exc}")
        return redirect(self.get_url_name("index"))

    def hide_view(self, request, pk):
        self._check_moderator_permission(request)
        if request.method != "POST":
            return redirect(self.get_url_name("index"))
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
        return redirect(self.get_url_name("index"))

    def pin_view(self, request, pk):
        self._check_moderator_permission(request)
        if request.method != "POST":
            return redirect(self.get_url_name("index"))
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
        return redirect(self.get_url_name("index"))

    def page_settings_view(self, request, page_id):
        self._check_moderator_permission(request)
        if request.method == "POST":
            allow_comments = request.POST.get("allow_comments") == "true"
            close_reason = request.POST.get("close_reason", "").strip()
            auto_moderate = request.POST.get("auto_moderate_new") == "true"

            settings_obj, created = PageCommentSettings.objects.get_or_create(
                page_id=page_id,
                defaults={
                    "allow_comments": allow_comments,
                    "close_reason": close_reason,
                    "auto_moderate_new": auto_moderate,
                },
            )
            if not created:
                settings_obj.allow_comments = allow_comments
                settings_obj.close_reason = close_reason
                settings_obj.auto_moderate_new = auto_moderate
                settings_obj.state_generation += 1
                settings_obj.save()

            return JsonResponse({
                "status": "success",
                "page_id": page_id,
                "allow_comments": settings_obj.allow_comments,
                "close_reason": settings_obj.close_reason,
                "auto_moderate_new": settings_obj.auto_moderate_new,
            })

        settings_obj = PageCommentSettings.objects.filter(page_id=page_id).first()
        return JsonResponse({
            "page_id": page_id,
            "allow_comments": settings_obj.allow_comments if settings_obj else True,
            "close_reason": settings_obj.close_reason if settings_obj else "",
            "auto_moderate_new": settings_obj.auto_moderate_new if settings_obj else False,
        })

    def page_comments_view(self, request, page_id):
        self._check_moderator_permission(request)
        filt = request.GET.get("filter", "all")
        qs = Comment.objects.filter(page_id=page_id)
        if filt == "pending":
            qs = qs.filter(pending_version__isnull=False)
        elif filt == "active":
            qs = qs.filter(status="active", is_publicly_visible=True)

        comments = qs.select_related("author", "pending_version", "approved_version").order_by("-is_pinned", "-created_at")[:50]
        items = []
        for c in comments:
            ver = c.pending_version or c.approved_version
            raw = ver.raw_content if ver else ""
            items.append({
                "id": c.id,
                "author_name": c.author.username if c.author else (c.guest_name or "访客"),
                "status": c.status,
                "is_publicly_visible": c.is_publicly_visible,
                "is_pinned": c.is_pinned,
                "pending_version_id": c.pending_version_id,
                "pending_version_number": c.pending_version.version_number if c.pending_version else None,
                "content_snippet": raw[:120] if raw else "（无内容）",
                "created_at": c.created_at.strftime("%Y-%m-%d %H:%M"),
            })
        return JsonResponse({"status": "success", "page_id": page_id, "items": items})

    def batch_hide_view(self, request):
        self._check_moderator_permission(request)
        if request.method != "POST":
            return JsonResponse({"status": "error", "message": "Method not allowed"}, status=405)
        c_ids = request.POST.getlist("comment_ids") or request.POST.getlist("comment_ids[]")
        if not c_ids:
            return JsonResponse({"status": "error", "message": "未选择评论"}, status=400)
        ids = [int(i) for i in c_ids if str(i).isdigit()]
        res = CommentService.batch_hide_comments(actor=request.user, comment_ids=ids)
        return JsonResponse({"status": "success", "result": res})

    def restrict_subject_view(self, request):
        self._check_moderator_permission(request)
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


comment_viewset = CommentViewSet("comments")


@hooks.register("register_admin_viewset")
def register_comment_viewset():
    return comment_viewset


@hooks.register("register_permissions")
def register_comment_permissions():
    """向 Wagtail 后台角色管理暴露评论专属审核权限"""
    from django.contrib.auth.models import Permission
    return Permission.objects.filter(content_type__app_label="comments", codename__in=["moderate_comment", "view_comment"])


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
