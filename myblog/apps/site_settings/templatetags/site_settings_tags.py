"""全站导航安全过滤模板标签 (H-07 优雅降级)。"""

import json
from django import template
from django.conf import settings
from django.urls import NoReverseMatch, reverse
from wagtail.models import Page, Site

from content_lifecycle.public_access import get_public_blocked_page_ids
from discovery.navigation import (
    annotate_active_directory_tree,
    get_site_directory_tree,
    resolve_site_for_navigation,
    resolve_tag_index_page,
)
from site_settings.models import SiteThemeSettings, SocialMediaSettings

register = template.Library()


@register.simple_tag
def get_theme_settings() -> dict:
    """只读返回全站前台主题配置字典（不走 Wagtail {% get_settings %}，避免公开 GET 隐式写库）。"""
    return SiteThemeSettings.get_readonly_dict()


@register.simple_tag
def get_public_social_links() -> list[dict]:
    """只读返回带平台标识、安全 URL 与打开方式的页脚社交链接（空表不写库）。"""
    return SocialMediaSettings.get_public_links_readonly(fallback_to_defaults=True)


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


@register.simple_tag(takes_context=True)
def get_site_navigation_data(context, nav_setting) -> dict:
    """
    全站顶部扁平导航、左侧栏目目录树与页脚大厅统一数据提供器（N-01 / N-02）：
    1. 顶栏仅保留全局固定入口（首页/作者/标签/时间线）与 NavigationSettings 扁平项，不再堆放多级栏目；
    2. 全站所有公开层级列表栏目树（不依赖 show_in_menus）统一交付左侧抽屉 directory_tree；
    3. 页脚「核心大厅」复用 directory_tree 一级栏目，或回退到 NavigationSettings 有效项。
    """
    request = context.get("request")
    current_page = context.get("page")
    site = resolve_site_for_navigation(request, page=current_page)

    author_enabled = getattr(settings, "AUTHOR_DISCOVERY_ENABLED", True)
    raw_tree = get_site_directory_tree(request, site=site, page=current_page)
    directory_tree = annotate_active_directory_tree(raw_tree, current_page)

    tag_index_url = ""
    ti = resolve_tag_index_page(request, page=current_page, site=site)
    if ti is not None:
        try:
            tag_index_url = (ti.get_url(request) if request else ti.url) or ""
        except Exception:
            tag_index_url = getattr(ti, "url", "") or ""

    def _safe_reverse(name: str, fallback: str) -> str:
        try:
            return reverse(name)
        except NoReverseMatch:
            return fallback

    home_url = "/"
    try:
        home_url = reverse("wagtail_serve", args=("",))
    except NoReverseMatch:
        home_url = "/"

    authors_url = _safe_reverse("people:author_directory", "/authors/")
    timeline_url = _safe_reverse("timeline", "/timeline/")

    configured_items = get_public_nav_items(context, nav_setting)
    nav_items: list[dict] = [
        {"label": "首页", "badge": "", "url": home_url, "is_external": False}
    ]
    for it in configured_items:
        nav_items.append({
            "label": it["label"],
            "badge": it.get("badge", ""),
            "url": it["url"],
            "is_external": bool(it.get("is_external", False)),
        })

    if author_enabled and not nav_has_url(nav_items, authors_url):
        nav_items.append({"label": "作者专栏", "badge": "", "url": authors_url, "is_external": False})
    if tag_index_url and not nav_has_url(nav_items, tag_index_url):
        nav_items.append({"label": "全站标签", "badge": "", "url": tag_index_url, "is_external": False})
    if not nav_has_url(nav_items, timeline_url):
        nav_items.append({"label": "时间线", "badge": "", "url": timeline_url, "is_external": False})

    if directory_tree:
        footer_hall_items = [{"label": "网站首页", "url": home_url, "is_external": False}] + [
            {"label": n["title"], "url": n["url"], "is_external": False}
            for n in directory_tree[:6]
        ]
    elif configured_items:
        footer_hall_items = [{"label": "网站首页", "url": home_url, "is_external": False}] + [
            {"label": it["label"], "url": it["url"], "is_external": bool(it.get("is_external", False))}
            for it in configured_items[:5]
        ]
    else:
        footer_hall_items = [{"label": "网站首页", "url": home_url, "is_external": False}]

    return {
        "home_url": home_url,
        "nav_items": nav_items,
        "footer_hall_items": footer_hall_items,
        "tag_index_url": tag_index_url,
        "directory_tree": directory_tree,
    }


def _resolve_abs_url(path_or_url: str, site, request) -> str:
    raw = str(path_or_url or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if not raw.startswith("/"):
        raw = "/" + raw
    root_url = (getattr(site, "root_url", "") or "").rstrip("/") if site is not None else ""
    if root_url:
        return f"{root_url}{raw}"
    if request is not None:
        try:
            return request.build_absolute_uri(raw)
        except Exception:
            return raw
    return raw


@register.simple_tag(takes_context=True)
def get_seo_meta(context) -> dict:
    """
    只读生成全站 canonical / Open Graph / Article JSON-LD 元数据：
    - 严禁写库；
    - 严禁依赖具体业务 Page 模型；
    - canonical 优先使用批准站点 Site.root_url，防止不可信 Host 注入。
    """
    request = context.get("request")
    page = context.get("page")
    detail = context.get("detail")
    is_preview = bool(
        request is not None
        and (getattr(request, "is_preview", False) or getattr(request, "in_preview_panel", False))
    )

    site = getattr(request, "_wagtail_site", None) if request is not None else None
    if site is None and request is not None:
        try:
            site = Site.find_for_request(request)
        except Exception:
            site = None
    if site is None:
        try:
            site = Site.objects.filter(is_default_site=True).first()
        except Exception:
            site = None

    site_name = (getattr(site, "site_name", "") or "").strip() or "myblog"

    # 1. Canonical URL（页码/视图传入 canonical_url 时优先，其次 page.get_url，最后 request.path）
    raw_canonical = str(context.get("canonical_url") or "").strip()
    if not raw_canonical and page is not None and hasattr(page, "get_url"):
        try:
            raw_canonical = (page.get_url(request) if request is not None else page.url) or ""
        except Exception:
            raw_canonical = getattr(page, "url_path", "") or ""
    if not raw_canonical and request is not None:
        raw_canonical = getattr(request, "path", "") or "/"
    canonical_url = _resolve_abs_url(raw_canonical, site, request)

    # 2. Title & Description
    seo_title = (getattr(page, "seo_title", "") or "").strip() if page is not None else ""
    page_title = (getattr(page, "title", "") or "").strip() if page is not None else ""
    og_title = seo_title or page_title or site_name
    og_description = (getattr(page, "search_description", "") or "").strip() if page is not None else ""
    if not og_description and page is not None:
        raw_summary = getattr(page, "summary", "") or ""
        if isinstance(raw_summary, str):
            og_description = raw_summary.strip()[:180]

    is_detail = bool(page is not None and getattr(page, "supports_comments", False))
    og_type = "article" if is_detail else "website"

    # 3. 封面图 OG Image
    og_image = ""
    featured_img = getattr(page, "featured_image", None) if page is not None else None
    if featured_img is not None and getattr(featured_img, "public_id", None):
        og_image = _resolve_abs_url(f"/media-gateway/image/{featured_img.public_id}/", site, request)

    # 4. Article JSON-LD（仅公开已发布详情页输出，草稿预览不输出）
    json_ld_str = ""
    if is_detail and not is_preview and getattr(page, "live", False):
        ld: dict = {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": page_title or og_title,
        }
        if canonical_url:
            ld["url"] = canonical_url
            ld["mainEntityOfPage"] = {"@type": "WebPage", "@id": canonical_url}
        if og_description:
            ld["description"] = og_description
        if og_image:
            ld["image"] = [og_image]
        authored_on = getattr(page, "authored_on", None)
        if authored_on is not None:
            ld["datePublished"] = authored_on.isoformat()
        elif getattr(page, "first_published_at", None) is not None:
            ld["datePublished"] = page.first_published_at.isoformat()
        if getattr(page, "last_published_at", None) is not None:
            ld["dateModified"] = page.last_published_at.isoformat()
        if isinstance(detail, dict) and detail.get("authors"):
            ld["author"] = [
                {"@type": "Person", "name": str(a.get("name") or "")}
                for a in detail["authors"]
                if a.get("name")
            ]
        ld["publisher"] = {"@type": "Organization", "name": site_name}
        json_ld_str = json.dumps(ld, ensure_ascii=False).replace("</", "<\\/")

    return {
        "canonical_url": canonical_url,
        "og_type": og_type,
        "og_title": og_title,
        "og_description": og_description,
        "og_site_name": site_name,
        "og_image": og_image,
        "is_preview": is_preview,
        "json_ld_str": json_ld_str,
    }
