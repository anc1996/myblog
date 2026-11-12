"""
comments.templatetags.comment_tags
全站通用评论模板标签。
"""

from django import template
from comments.models import CommunitySettings, PageCommentSettings
from comments.rate_limit import is_member_comment_captcha_required
from identity.challenges import ChallengeServiceUnavailable

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
            "allow_guest_comments": False,
            "effective_allow_guests": False,
            "comment_mode": "members_only",
            "can_submit": False,
            "requires_login": False,
            "captcha_required": False,
            "captcha_scope": "",
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
            "allow_guest_comments": False,
            "effective_allow_guests": False,
            "comment_mode": "members_only",
            "can_submit": False,
            "requires_login": False,
            "captcha_required": False,
            "captcha_scope": "",
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
    community = CommunitySettings.get_solo()
    comment_mode = community.comment_mode
    page_allow_comments = True
    allow_guest_comments = False
    close_reason = ""
    if target_page_id:
        settings_obj = PageCommentSettings.objects.filter(page_id=target_page_id).first()
        if settings_obj:
            page_allow_comments = settings_obj.allow_comments
            allow_guest_comments = bool(settings_obj.allow_guest_comments)
            close_reason = settings_obj.close_reason

    effective_allow_comments = (comment_mode != "closed") and page_allow_comments
    if comment_mode == "closed" and not close_reason:
        close_reason = "全站评论功能当前已暂停维护"

    effective_allow_guests = (
        effective_allow_comments
        and comment_mode == "controlled_guests"
        and allow_guest_comments
    )

    user = getattr(request, "user", None) if request else None
    is_authenticated = bool(user and getattr(user, "is_authenticated", False))
    can_submit = effective_allow_comments and (is_authenticated or effective_allow_guests)
    requires_login = effective_allow_comments and (not is_authenticated) and (not effective_allow_guests)

    captcha_scope = ""
    captcha_required = False
    if can_submit:
        if is_authenticated:
            captcha_scope = "member_comment"
            try:
                captcha_required = is_member_comment_captcha_required(request, user)
            except ChallengeServiceUnavailable:
                captcha_required = False
        else:
            captcha_scope = "guest_comment"
            captcha_required = True

    return {
        "supported": True,
        "has_id": bool(page_id),
        "is_preview": is_preview,
        "allow_comments": effective_allow_comments,
        "allow_guest_comments": allow_guest_comments,
        "effective_allow_guests": effective_allow_guests,
        "comment_mode": comment_mode,
        "can_submit": can_submit,
        "requires_login": requires_login,
        "captcha_required": captcha_required,
        "captcha_scope": captcha_scope,
        "close_reason": close_reason,
    }
