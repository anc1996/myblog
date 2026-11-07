"""
comments.wagtail_hooks
Wagtail 8.1 评论审核控制台钩子 (C-06 契约)。

落实后台安全隔离：
1. 剪除原生 add/edit/delete/copy 路由，杜绝后台直接修改用户原文或物理硬删；
2. 标题点击平滑导向只读 inspect 详情页；
3. 挂载受控的 approve、reject、hide 与批量审核 bulk_moderate 动作；
4. 注册独立权限 comments.moderate_comment。
"""

from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.safestring import mark_safe
from wagtail import hooks
from wagtail.admin import messages
from wagtail.admin.viewsets.model import ModelViewSet
from wagtail.admin.views import generic

from .models import Comment, CommentVersion
from .services import CommentService


class CommentIndexView(generic.IndexView):
    """定制评论管理列表：标题链接指向 inspect 只读页，提供操作按钮"""
    def get_add_url(self):
        return None

    def get_edit_url(self, instance):
        return None

    def get_delete_url(self, instance):
        return None

    def get_copy_url(self, instance):
        return None


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
                return mark_safe(f"<div style='background:#fffbeb;padding:12px;border:1px solid #fde68a;border-radius:6px;color:#92400e;'><p><strong>待审版本 V{ver.version_number}：</strong></p><pre>{ver.raw_content}</pre></div>")
        return "（无待审修改）"
    pending_raw_content.short_description = "待审核新版本正文"

    def actions_column(self, obj):
        actions_html = []
        if obj.pending_version_id:
            approve_url = reverse(self.get_url_name("approve"), args=[obj.id])
            reject_url = reverse(self.get_url_name("reject"), args=[obj.id])
            actions_html.append(f"""
            <form method="POST" action="{approve_url}" style="display:inline-block;margin-right:4px;">
                <input type="hidden" name="expected_version_id" value="{obj.pending_version_id}">
                <button type="submit" class="button button-small" style="background:#16a34a;color:#fff;">批准</button>
            </form>
            <form method="POST" action="{reject_url}" style="display:inline-block;margin-right:4px;">
                <input type="hidden" name="expected_version_id" value="{obj.pending_version_id}">
                <button type="submit" class="button button-small" style="background:#ea580c;color:#fff;">驳回</button>
            </form>
            """)
        elif obj.status == "active":
            hide_url = reverse(self.get_url_name("hide"), args=[obj.id])
            actions_html.append(f"""
            <form method="POST" action="{hide_url}" style="display:inline-block;">
                <button type="submit" class="button button-small button-secondary">下架隐藏</button>
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
        ]

    def _check_moderator_permission(self, request):
        if not (request.user.is_superuser or request.user.has_perm("comments.moderate_comment")):
            raise PermissionDenied("当前用户缺少评论审核权限 (comments.moderate_comment)。")

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
            messages.success(request, f"评论 #{pk} 已成功审核通过上线！")
        except Exception as exc:
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
            messages.success(request, f"评论 #{pk} 的待审修改已被驳回。")
        except Exception as exc:
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
            messages.success(request, f"评论 #{pk} 已下架隐藏。")
        except Exception as exc:
            messages.error(request, f"隐藏失败: {exc}")
        return redirect(self.get_url_name("index"))


comment_viewset = CommentViewSet("comments")


@hooks.register("register_admin_viewset")
def register_comment_viewset():
    return comment_viewset


@hooks.register("register_permissions")
def register_comment_permissions():
    """向 Wagtail 后台角色管理暴露评论专属审核权限"""
    from django.contrib.auth.models import Permission
    return Permission.objects.filter(content_type__app_label="comments", codename__in=["moderate_comment", "view_comment"])
