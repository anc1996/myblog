"""
comments.templatetags.comment_tags
全站通用评论模板标签。
"""

from django import template
from comments.models import PageCommentSettings

register = template.Library()


@register.simple_tag(takes_context=True)
def get_page_comment_policy(context, page):
    """
    解析当前页面的评论准入与运行期开关策略。
    避免详情页模板只读取静态类属性 page.allow_comments 而忽略单篇 PageCommentSettings 或预览状态。
    """
    if not page:
        return {
            "supported": False,
            "has_id": False,
            "is_preview": False,
            "allow_comments": False,
            "close_reason": "",
        }

    supports = getattr(page, "supports_comments", None)
    if supports is not None:
        supported = bool(supports)
    else:
        supported = bool(getattr(page, "allow_comments", False))

    page_id = getattr(page, "id", None)
    if not supported:
        return {
            "supported": False,
            "has_id": bool(page_id),
            "is_preview": False,
            "allow_comments": False,
            "close_reason": "",
        }

    request = context.get("request")
    is_req_preview = bool(
        request and (getattr(request, "is_preview", False) or getattr(request, "in_preview_panel", False))
    )
    is_live = bool(getattr(page, "live", False)) and bool(getattr(page, "live_revision_id", None))
    is_recycled = bool(getattr(page, "is_recycled", False))
    is_preview = is_req_preview or (not is_live) or is_recycled

    target_page_id = page.alias_of_id if getattr(page, "alias_of_id", None) else page_id
    allow_comments = True
    close_reason = ""
    if target_page_id:
        settings_obj = PageCommentSettings.objects.filter(page_id=target_page_id).first()
        if settings_obj:
            allow_comments = settings_obj.allow_comments
            close_reason = settings_obj.close_reason

    return {
        "supported": True,
        "has_id": bool(page_id),
        "is_preview": is_preview,
        "allow_comments": allow_comments,
        "close_reason": close_reason,
    }
