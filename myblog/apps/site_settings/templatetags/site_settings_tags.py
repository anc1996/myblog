"""全站导航安全过滤模板标签 (H-07 优雅降级)。"""

from django import template
from wagtail.models import Page

from content_lifecycle.public_access import get_public_blocked_page_ids
from site_settings.models import SiteThemeSettings

register = template.Library()


@register.simple_tag
def get_theme_settings() -> dict:
    """只读返回全站前台主题配置字典（不走 Wagtail {% get_settings %}，避免公开 GET 隐式写库）。"""
    return SiteThemeSettings.get_readonly_dict()


@register.simple_tag(takes_context=True)
def get_public_nav_items(context, nav_setting):
    """
    过滤 NavigationSettings.nav_items：
    - 外链保留 external_url；
    - 内链核验 live、live_revision、未进回收站且无访问限制；
    - 自动丢弃 target_page=None 且无 external_url 的失效占位项。
    """
    if not nav_setting or not getattr(nav_setting, "nav_items", None):
        return []

    request = context.get("request")
    raw_items = []
    target_ids = set()
    for block in nav_setting.nav_items:
        val = getattr(block, "value", None) or {}
        target_page = val.get("target_page")
        p_id = getattr(target_page, "id", None) if target_page else None
        if p_id:
            target_ids.add(p_id)
        raw_items.append({
            "label": val.get("label", ""),
            "badge": val.get("badge", ""),
            "target_page_id": p_id,
            "external_url": (val.get("external_url") or "").strip(),
            "is_external": bool(val.get("is_external", False)),
        })

    allowed_urls = {}
    if target_ids:
        pages_qs = Page.objects.filter(id__in=target_ids, live=True).exclude(live_revision=None)
        candidates = list(pages_qs.values_list("id", "path"))
        blocked_ids = get_public_blocked_page_ids(candidates)
        for p in pages_qs:
            if p.id not in blocked_ids and getattr(p, "alias_of_id", None) is None:
                url = p.get_url(request) if request else p.url
                if url:
                    allowed_urls[p.id] = url

    result = []
    for item in raw_items:
        ext_url = item["external_url"]
        p_id = item["target_page_id"]
        if ext_url:
            url = ext_url
        elif p_id and p_id in allowed_urls:
            url = allowed_urls[p_id]
        else:
            continue
        result.append({
            "label": item["label"],
            "badge": item["badge"],
            "url": url,
            "is_external": item["is_external"],
        })
    return result


@register.simple_tag
def nav_has_url(nav_items, target_path: str) -> bool:
    """检查动态导航配置中是否已包含指定相对路径（防止重复渲染固定栏目入口）。"""
    if not nav_items:
        return False
    norm_target = "/" + str(target_path or "").strip("/") + "/"
    for item in nav_items:
        raw_url = str(item.get("url") or "").strip()
        if not raw_url:
            continue
        if raw_url.rstrip("/") + "/" == norm_target or raw_url.endswith(norm_target):
            return True
    return False
