"""
identity 模块 Wagtail 8.1 后台集成钩子。

1. get_avatar_url: 统一后台顶栏与历史记录头像指向 /avatar-gateway/ 或默认静态图，屏蔽 Gravatar 与旧 UserProfile.avatar；
2. before_delete_user / before_bulk_action: 拦截原生用户删除与批量启停，强制走 AccountLifecycleService；
3. 超级管理员专属“账号治理与安全重置”工作台 (/admin/identity/users/<id>/manage/)：
   支持查看仅超管可见的 recovery_hint 核密线索、停用/恢复、设置临时密码（强制改密）及带资产过户的彻底删除。
"""

from django.contrib import messages as django_messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import path, reverse
from wagtail import hooks
from wagtail.admin import messages as wagtail_messages
from wagtail.admin.widgets import Button

from identity.account_lifecycle import AccountLifecycleService
from identity.models import AccountAuditLog, AvatarPurgeTask, MemberProfile, User


@hooks.register("get_avatar_url")
def get_unified_member_avatar_url(user, size=50):
    """
    统一全站 Wagtail 后台头像来源：
    若用户有已激活的 MemberProfile 独立头像，返回 /avatar-gateway/；
    否则直接返回默认本地占位图，禁止回落至未清洗的 UserProfile.avatar 或外部 Gravatar。
    """
    if user is not None and getattr(user, "is_authenticated", False):
        try:
            profile = MemberProfile.objects.filter(user_id=user.pk).first()
            if profile and profile.avatar_token and profile.avatar_key and user.is_active:
                return profile.avatar_url
        except Exception:
            pass
    return static("wagtailadmin/images/default-user-avatar.png")


@hooks.register("register_user_listing_buttons")
def add_user_governance_button(user, request_user):
    """为超级管理员在用户列表挂载“账号治理/改密/注销”按钮。"""
    if not request_user or not request_user.is_superuser:
        return []
    return [
        Button(
            "账号治理",
            reverse("identity_admin_user_manage", args=[user.pk]),
            icon_name="cog",
            priority=10,
        )
    ]


@hooks.register("before_delete_user")
def intercept_wagtail_user_delete(request, user):
    """
    拦截 Wagtail 原生 /admin/users/<id>/delete/ 删除入口，
    统一导流到支持资产过户校验与强制确认的超级管理员账号治理工作台。
    """
    wagtail_messages.warning(
        request,
        "系统已启用受控账号清理保护，请在“账号治理”工作台确认资产过户后再执行停用或彻底删除。",
    )
    return redirect("identity_admin_user_manage", user_id=user.pk)


@hooks.register("before_create_user")
def guard_wagtail_user_create(request):
    """仅超级管理员可在后台新建账号，防止非超管员工借助原生用户表单赋权。"""
    if not request.user.is_superuser:
        raise PermissionDenied("仅超级管理员可在后台创建账号。")
    return None


@hooks.register("before_edit_user")
def guard_wagtail_user_edit(request, user):
    """
    收口 Wagtail 原生 /admin/users/<id>/edit/ 旁路：
    1. 非超管员工禁止编辑他人账号（含密码重置与权限组修改）；
    2. 超级管理员修改他人密码也必须走受控账号治理工作台，确保强制改密标志与审计日志完整。
    """
    if not request.user.is_superuser and request.user.pk != user.pk:
        raise PermissionDenied("仅超级管理员可修改他人账号。")
    if request.method == "POST" and request.user.pk != user.pk:
        if request.POST.get("password1") or request.POST.get("password2"):
            wagtail_messages.error(
                request,
                "请使用“账号治理”工作台重置他人临时密码，以确保触发强制改密与安全审计。",
            )
            return redirect("identity_admin_user_manage", user_id=user.pk)
    return None


@hooks.register("before_bulk_action")
def intercept_wagtail_user_bulk_actions(request, action_type, objects, bulk_action):
    """
    拦截 Wagtail 用户批量操作：
    1. 批量删除直接阻断（必须逐个确认页面/媒体接管人）；
    2. 批量启停接管到 AccountLifecycleService，确保原子递增 session_epoch 与记录审计日志。
    """
    if not issubclass(bulk_action.model, User):
        return None
    if request.method != "POST":
        return None

    if action_type == "delete":
        wagtail_messages.error(
            request,
            "禁止批量直接物理删除账号！请进入单个用户的“账号治理”页面完成资产过户确认后再删除。",
        )
        return redirect(bulk_action.next_url)

    if action_type == "set_active_state":
        if not request.user.is_superuser:
            raise PermissionDenied("仅超级管理员可批量启停账号。")
        mark_active = str(request.POST.get("mark_as_active", "")).lower() == "true"
        updated = 0
        for target in objects:
            if target.pk == request.user.pk:
                continue
            try:
                if mark_active:
                    AccountLifecycleService.reactivate_user(actor=request.user, target_user_id=target.pk)
                else:
                    AccountLifecycleService.deactivate_user(
                        actor=request.user,
                        target_user_id=target.pk,
                        reason="Wagtail 后台批量停用",
                    )
                updated += 1
            except Exception as exc:
                wagtail_messages.error(request, f"处理账号 {target.username} 失败: {exc}")
        if updated:
            wagtail_messages.success(request, f"已通过受控账号服务更新 {updated} 个账号状态并同步失效旧会话。")
        return redirect(bulk_action.next_url)

    return None


def admin_user_manage_view(request, user_id: int):
    """超级管理员专属：账号核密线索查看、停用/恢复、重置临时密码与带资产过户彻底删除。"""
    if not request.user.is_authenticated or not request.user.is_superuser:
        raise PermissionDenied("仅超级管理员可访问账号治理工作台。")

    target = get_object_or_404(User, pk=user_id)
    profile = MemberProfile.get_or_create_for_user(target)

    from content_lifecycle.models import RecycleBatch
    from media_library.models import CustomDocument, CustomImage
    from wagtail.models import Page

    owned_stats = {
        "pages": Page.objects.filter(owner_id=target.id).count(),
        "images": CustomImage.objects.filter(uploaded_by_user_id=target.id).count(),
        "documents": CustomDocument.objects.filter(uploaded_by_user_id=target.id).count(),
        "batches": RecycleBatch.objects.filter(actor_id=target.id).count(),
    }
    transfer_candidates = (
        User.objects.filter(is_active=True)
        .filter(Q(is_superuser=True) | Q(is_staff=True))
        .exclude(pk=target.id)
        .order_by("username")
    )

    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "deactivate":
                reason = (request.POST.get("reason") or "").strip()
                AccountLifecycleService.deactivate_user(
                    actor=request.user,
                    target_user_id=target.id,
                    reason=reason,
                )
                wagtail_messages.success(request, f"已停用账号 '{target.username}' 并使旧会话失效，头像文件已保留。")
            elif action == "reactivate":
                AccountLifecycleService.reactivate_user(
                    actor=request.user,
                    target_user_id=target.id,
                )
                wagtail_messages.success(request, f"已恢复账号 '{target.username}'。")
            elif action == "reset_password":
                temp_pw = request.POST.get("temp_password", "")
                AccountLifecycleService.admin_reset_password(
                    actor=request.user,
                    target_user_id=target.id,
                    new_password=temp_pw,
                )
                wagtail_messages.success(
                    request,
                    f"已重置 '{target.username}' 密码并踢下线全部旧会话，对方下次登录必须先修改密码。",
                )
            elif action == "hard_delete":
                confirm_username = (request.POST.get("confirm_username") or "").strip()
                if confirm_username != target.username:
                    raise ValidationError("请准确输入目标用户名以确认不可逆删除操作。")
                transfer_raw = (request.POST.get("transfer_to_user_id") or "").strip()
                transfer_id = int(transfer_raw) if transfer_raw else None
                res = AccountLifecycleService.hard_delete(
                    actor=request.user,
                    target_user_id=target.id,
                    transfer_to_user_id=transfer_id,
                    confirmed=True,
                )
                wagtail_messages.success(
                    request,
                    f"账号 '{res['target_username']}' 删除已提交（过户页面 {res['transferred_pages']} 篇、已入队头像清理任务 {len(res['queued_avatar_keys'])} 个）。",
                )
                return redirect("wagtailusers_users:index")
        except (PermissionDenied, ValidationError) as exc:
            wagtail_messages.error(request, str(exc))
        except Exception as exc:
            wagtail_messages.error(request, f"执行操作失败: {exc}")
        return redirect("identity_admin_user_manage", user_id=target.id)

    recent_logs = AccountAuditLog.objects.filter(target_user_id_snapshot=target.id)[:20]
    avatar_tasks = AvatarPurgeTask.objects.filter(target_user_id_snapshot=target.id)[:10]
    return render(
        request,
        "identity/admin/user_manage.html",
        {
            "target_user": target,
            "member_profile": profile,
            "owned_stats": owned_stats,
            "has_assets": any(owned_stats.values()),
            "transfer_candidates": transfer_candidates,
            "recent_logs": recent_logs,
            "avatar_tasks": avatar_tasks,
        },
    )


@hooks.register("register_admin_urls")
def register_identity_admin_urls():
    return [
        path(
            "identity/users/<int:user_id>/manage/",
            admin_user_manage_view,
            name="identity_admin_user_manage",
        ),
    ]
