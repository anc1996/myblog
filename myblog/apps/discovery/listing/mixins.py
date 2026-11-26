"""
全站层级列表与多维检索页面 Mixin (HierarchicalListingMixin)。

落实 RFC 规范与 B-01 闭环：
1. 声明式接入：子类仅需配置 listing_policy 即可自动获得全套层级检索与分页能力；
2. 双模同构交付 (serve)：首屏 SSR 返回完整 HTML，AJAX 请求直接短路返回 JSON 片段载荷，杜绝冗余构造全页 TemplateResponse；
3. 深度兼容 Wagtail 8.1 后台预览机制 (serve_preview)：后台草稿实时预览安全渲染；
4. 自动注入 Vary 响应头 (Accept, X-Listing-Response, X-Requested-With)。
"""

import html as html_lib
import logging
import re
from typing import Any
from django.db import models
from django.http import Http404, JsonResponse
from django.template.loader import render_to_string
from django.utils.html import linebreaks, strip_tags
from django.utils.safestring import mark_safe
from wagtail.models import Site

from discovery.detail_navigation import resolve_site_readonly
from discovery.listing.contracts import ListingPolicy, ListingViewModel
from discovery.listing.service import IndexListingService

logger = logging.getLogger(__name__)


_BLOCK_INTRO_RE = re.compile(r"<(?:ul|ol|h2|h3|h4)\b", re.IGNORECASE)
_PARAGRAPH_OPEN_RE = re.compile(r"<p\b", re.IGNORECASE)


class HierarchicalListingMixin(models.Model):
    """
    Wagtail 页面通用层级列表与检索 Mixin。
    继承顺序须位于 CustomBasePage 之前以正确协作 MRO。
    """
    HEADER_LAYOUT_EDITORIAL_SPLIT = "editorial_split"
    HEADER_LAYOUT_COMPACT_DOC = "compact_doc"
    HEADER_LAYOUT_VISUAL_BANNER = "visual_banner"
    HEADER_LAYOUT_CHOICES = [
        (HEADER_LAYOUT_EDITORIAL_SPLIT, "左文右图（默认，适合带配图的栏目）"),
        (HEADER_LAYOUT_COMPACT_DOC, "精简文档头（适合文字长、公文或无需横幅大图的栏目）"),
        (HEADER_LAYOUT_VISUAL_BANNER, "宽幅画报横幅（适合风景/建筑/宽图专题）"),
    ]
    header_layout = models.CharField(
        max_length=24,
        choices=HEADER_LAYOUT_CHOICES,
        default=HEADER_LAYOUT_EDITORIAL_SPLIT,
        verbose_name="页面头部排版样式",
        help_text="控制本栏目页顶部排版：默认「左文右图」不切头；文字较多或公文资料推荐「精简文档头」；宽幅风景图可选「宽幅画报横幅」。",
    )

    listing_policy: ListingPolicy = ListingPolicy(type_keys=("blog_page",))
    # 聚合大厅允许受限作者在其下创建声明了 author_creatable=True 的子内容页
    author_allow_child_creation = True

    class Meta:
        abstract = True

    def get_header_intro_html(self, request: Any = None) -> str:
        """获取用于栏目头部的安全 HTML 导读；富文本子类可重写以注入净化结果。"""
        raw_intro = (getattr(self, "intro", "") or "").strip()
        if not raw_intro:
            return ""
        return mark_safe(linebreaks(raw_intro, autoescape=True))

    def get_header_presentation(
        self,
        request: Any = None,
        *,
        intro_html: str | None = None,
    ) -> dict[str, Any]:
        """构建统一的栏目页头部呈现契约，供共享模板 `_listing_header.html` 消费。"""
        allowed_layouts = {
            self.HEADER_LAYOUT_EDITORIAL_SPLIT,
            self.HEADER_LAYOUT_COMPACT_DOC,
            self.HEADER_LAYOUT_VISUAL_BANNER,
        }
        raw_layout = getattr(self, "header_layout", self.HEADER_LAYOUT_EDITORIAL_SPLIT)
        layout = raw_layout if raw_layout in allowed_layouts else self.HEADER_LAYOUT_EDITORIAL_SPLIT

        img = getattr(self, "featured_image", None)
        cover_url = ""
        cover_alt = getattr(self, "title", "") or ""
        # 未设置焦点框时默认上偏（center 28%），避免左文右图/横幅裁切掉人物头部
        focal_class = "is-focal-upper"
        if img is not None and getattr(img, "public_id", None):
            cover_url = f"/media-gateway/image/{img.public_id}/"
            cover_alt = getattr(img, "title", None) or cover_alt
            fp = img.get_focal_point() if hasattr(img, "get_focal_point") else None
            img_height = getattr(img, "height", 0) or 0
            if fp is not None and img_height > 0:
                y_ratio = float(fp.y) / float(img_height)
                if y_ratio < 0.28:
                    focal_class = "is-focal-top"
                elif y_ratio < 0.44:
                    focal_class = "is-focal-upper"
                elif y_ratio < 0.62:
                    focal_class = "is-focal-center"
                elif y_ratio < 0.78:
                    focal_class = "is-focal-lower"
                else:
                    focal_class = "is-focal-bottom"

        resolved_intro_html = intro_html if intro_html is not None else self.get_header_intro_html(request)
        resolved_intro_html = (resolved_intro_html or "").strip()
        plain = html_lib.unescape(strip_tags(resolved_intro_html)).strip() if resolved_intro_html else ""
        is_long_intro = bool(
            resolved_intro_html
            and (
                len(plain) > 180
                or bool(_BLOCK_INTRO_RE.search(resolved_intro_html))
                or len(_PARAGRAPH_OPEN_RE.findall(resolved_intro_html)) > 1
            )
        )

        return {
            "layout": layout,
            "cover_url": cover_url,
            "cover_alt": cover_alt,
            "focal_class": focal_class,
            "intro_html": mark_safe(resolved_intro_html) if resolved_intro_html else "",
            "is_long_intro": is_long_intro,
            "has_visible_cover": bool(cover_url and layout != self.HEADER_LAYOUT_COMPACT_DOC),
        }

    def get_listing_policy(self, request: Any) -> ListingPolicy:
        """获取当前列表页面的能力策略定义，支持子类根据请求动态扩展"""
        return self.listing_policy

    def get_vary_headers(self, request: Any) -> list[str]:
        """自动合并 Wagtail 缓存协商头部 (RFC 7.1)"""
        base_vary = super().get_vary_headers(request) if hasattr(super(), "get_vary_headers") else []
        custom_vary = ["Accept", "X-Listing-Response", "X-Requested-With"]
        return list(dict.fromkeys(base_vary + custom_vary))

    def get_listing_breadcrumbs(self, request: Any) -> list[dict[str, Any]]:
        """
        生成经过公开与回收核验的面包屑导航结构：
        中间已发布的祖先栏目 -> 当前栏目（剔除站点根页，避免与模板头部「首页」重复）
        """
        breadcrumbs: list[dict[str, Any]] = []
        try:
            site = resolve_site_readonly(request, self)
            root_page = getattr(site, "root_page", None) if site is not None else None
            root_page_id = getattr(site, "root_page_id", None) if site is not None else None
            min_depth = int(getattr(root_page, "depth", 2) or 2) if root_page is not None else 2
            ancestors = [
                a for a in self.get_ancestors()
                if not a.is_root()
                and not getattr(a, "is_recycled", False)
                and int(getattr(a, "depth", 0) or 0) > min_depth
                and (root_page_id is None or a.id != root_page_id)
            ]
            for anc in ancestors:
                breadcrumbs.append({
                    "id": anc.id,
                    "title": getattr(anc, "title", ""),
                    "url": anc.get_url(request) if hasattr(anc, "get_url") else getattr(anc, "url_path", ""),
                })
        except Exception as exc:
            logger.debug("Failed to calculate listing breadcrumbs: %s", exc)
        return breadcrumbs

    def serve_preview(self, request: Any, mode_name: str) -> Any:
        """
        Wagtail 8.1 后台预览入口防崩溃守卫 (B-01)：
        1. 显式打上预览标记，允许草稿与未发布栏目安全预览；
        2. 栏目头部导语与标题直接取自当前内存实例，下属卡片走搜索副本。
        """
        if getattr(self, "is_recycled", False):
            raise Http404("已回收页面禁止在后台预览！")
        request._is_listing_preview = True
        return super().serve_preview(request, mode_name)

    def serve(self, request: Any, *args, **kwargs) -> Any:
        """
        双模同构服务总线：
        1. 拦截未发布与回收状态；
        2. 执行 IndexListingService；
        3. AJAX 请求直接返回 JSON 载荷（包含 _listing_results.html 片段），不走 super().serve()；
        4. 普通 SSR 请求走标准模板渲染。
        """
        is_preview = bool(
            getattr(request, "_is_listing_preview", False)
            or getattr(request, "is_preview", False)
            or getattr(request, "in_preview_panel", False)
        )
        if (not getattr(self, "live", True)) and (not is_preview):
            raise Http404(f"页面 '{getattr(self, 'title', '')}' 尚未发布或已被下架")
        if getattr(self, "is_recycled", False):
            raise Http404(f"页面 '{getattr(self, 'title', '')}' 当前处于回收站中")

        # 统一构建视图模型
        view_model: ListingViewModel = IndexListingService.build(self, request)
        request._listing_view_model = view_model

        # 检查是否为 AJAX 局部更新请求 (D3-A)
        is_ajax = bool(
            request.headers.get("X-Listing-Response") == "fragment-v1"
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.GET.get("format") == "partial"
        )

        if is_ajax:
            partial_template = "discovery/listing/_listing_results.html"
            html_fragment = render_to_string(
                partial_template,
                {"view_model": view_model, "page": self, "request": request},
                request=request,
            )
            return JsonResponse(
                {
                    "html": html_fragment,
                    "canonical_url": view_model.canonical_url,
                    "total_count": view_model.total_count,
                    "cursor_reset": view_model.cursor_reset,
                    "facets": view_model.facets,
                    "next_cursor": view_model.next_cursor,
                    "previous_cursor": view_model.previous_cursor,
                    "error_message": view_model.error_message,
                },
                status=view_model.http_status,
            )

        # 普通 SSR 请求走标准模板响应
        response = super().serve(request, *args, **kwargs)
        if hasattr(response, "status_code") and view_model.http_status != 200:
            response.status_code = view_model.http_status
        return response

    def get_context(self, request: Any, *args, **kwargs) -> dict[str, Any]:
        """注入统一的视图模型与面包屑，零额外二次 SQL 查询"""
        context = super().get_context(request, *args, **kwargs)
        view_model = getattr(request, "_listing_view_model", None)
        if not view_model:
            view_model = IndexListingService.build(self, request)
            request._listing_view_model = view_model

        context["view_model"] = view_model
        context["breadcrumbs"] = self.get_listing_breadcrumbs(request)
        context["header_presentation"] = self.get_header_presentation(
            request,
            intro_html=context.get("intro_html"),
        )
        return context
