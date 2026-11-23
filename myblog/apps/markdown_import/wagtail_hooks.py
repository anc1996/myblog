"""
Wagtail 后台“Markdown 导入工具与令牌”管理入口。
创作者可在此生成一次性桌面配对码、创建短期导入 Token 或一键吊销旧令牌。
"""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone
from wagtail import hooks
from wagtail.admin.auth import require_admin_access
from wagtail.admin.menu import MenuItem

from .auth import issue_import_token, issue_pair_code, resolve_request_site
from .models import MarkdownImportToken


@require_admin_access
def admin_import_tokens_view(request):
    site = resolve_request_site(request)
    issued_pair_code = None
    issued_token_plaintext = None

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "create_pair_code":
            _, issued_pair_code = issue_pair_code(user=request.user, site=site)
            messages.success(request, "已生成 10 分钟一次性桌面配对码，请在导入客户端粘贴使用。")
        elif action == "create_token":
            name = (request.POST.get("name") or "本地导入客户端").strip()
            _, issued_token_plaintext = issue_import_token(user=request.user, site=site, name=name)
            messages.success(request, "已签发导入令牌（仅本次展示明文，请立即复制保存）。")
        elif action == "revoke_token":
            token_id = request.POST.get("token_id")
            updated = MarkdownImportToken.objects.filter(
                pk=token_id,
                user=request.user,
                revoked_at__isnull=True,
            ).update(is_active=False, revoked_at=timezone.now())
            if updated:
                messages.success(request, "已吊销选定导入令牌。")
            return redirect("markdown_import_admin_tokens")

    tokens = MarkdownImportToken.objects.filter(user=request.user, site=site).order_by("-created_at")[:30]
    return render(
        request,
        "markdown_import/admin_tokens.html",
        {
            "site": site,
            "tokens": tokens,
            "issued_pair_code": issued_pair_code,
            "issued_token_plaintext": issued_token_plaintext,
        },
    )


@hooks.register("register_admin_urls")
def register_markdown_import_admin_urls():
    return [
        path(
            "markdown-import/tokens/",
            admin_import_tokens_view,
            name="markdown_import_admin_tokens",
        ),
    ]


@hooks.register("register_admin_menu_item")
def register_markdown_import_menu_item():
    return MenuItem(
        "Markdown 导入令牌",
        reverse("markdown_import_admin_tokens"),
        icon_name="upload",
        order=650,
    )
