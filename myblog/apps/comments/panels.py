"""
comments.panels
Wagtail 8.1 页面编辑器评论与互动面板 (B-03 隔离契约)。

落实契约：
1. 100% 异步加载与独立提交，严禁在 HTML 控件中使用 name="..." 属性 (B-03)；
2. 隔离 Wagtail UnsavedController，防止未保存的文章草稿被自动保存或标记为已修改；
3. 支持就地开关评论 (allow_comments)、编辑关评公告 (close_reason)、单篇评论状态快速审核。
"""

from wagtail.admin.panels import Panel


class PageCommentGovernancePanel(Panel):
    """
    页面编辑器专属 [💬 评论与互动] 面板。
    绑定当前 Page 实例，提供纯异步的评论配置与就地审核工作台。
    """

    def __init__(self, template="comments/admin/page_comment_governance_panel.html", **kwargs):
        kwargs.setdefault("heading", "💬 评论与互动")
        super().__init__(**kwargs)
        self.template = template or "comments/admin/page_comment_governance_panel.html"

    def clone_kwargs(self):
        kwargs = super().clone_kwargs()
        kwargs.update(template=self.template)
        return kwargs

    @property
    def clean_name(self):
        return "comment_governance"

    class BoundPanel(Panel.BoundPanel):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.template_name = self.panel.template

        def get_context_data(self, parent_context=None):
            context = super().get_context_data(parent_context)
            page = self.instance
            request = getattr(self, "request", None) or (
                parent_context.get("request") if isinstance(parent_context, dict) else None
            )
            user = getattr(request, "user", None)

            from comments.models import CommunitySettings
            community_settings = CommunitySettings.get_solo()
            context["community_comment_mode"] = community_settings.comment_mode
            context["community_comment_mode_display"] = community_settings.get_comment_mode_display()

            if page and getattr(page, "pk", None):
                from comments.access import can_user_manage_guest_comments
                from comments.models import PageCommentSettings
                settings_obj = PageCommentSettings.objects.filter(page_id=page.pk).first()
                context["comment_settings"] = settings_obj
                context["page_id"] = page.pk
                context["page_title"] = getattr(page, "title", "")
                context["supports_comments"] = getattr(page, "supports_comments", True)
                context["can_manage_guest_comments"] = can_user_manage_guest_comments(user, page)
                context["can_moderate_comments"] = bool(
                    user and getattr(user, "is_authenticated", False)
                    and (user.is_superuser or user.has_perm("comments.moderate_comment"))
                )
            else:
                context["comment_settings"] = None
                context["page_id"] = None
                context["page_title"] = ""
                context["supports_comments"] = getattr(self.panel.model, "supports_comments", True)
                context["can_manage_guest_comments"] = False
                context["can_moderate_comments"] = False
            return context
