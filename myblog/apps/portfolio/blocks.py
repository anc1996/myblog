"""
portfolio.blocks
作品集与开源项目专属 StreamField 积木定义。

包含：
1. TechStackBlock: 技术栈徽标（分类、语言/框架、版本）；
2. RepositoryBlock: 开源仓库元数据（GitHub/Gitee 地址、Star 数、License、Demo 演示）；
3. ScreenshotGalleryBlock: 界面截图画廊（引用 CustomImage 并提取媒体引用）；
4. PortfolioStreamBlock: 聚合通用积木与作品集专属积木。
"""

import html
from typing import Any
from django.utils.safestring import mark_safe
from wagtail import blocks
from wagtail.images.blocks import ImageChooserBlock

from content_blocks.blocks import (
    CustomRichTextBlock,
    CustomMarkdownBlock,
    CustomCodeBlock,
    CustomTableBlock,
    CustomMermaidBlock,
    CustomImageBlock,
    CustomMediaTextBlock,
    CustomDocumentBlock,
    CustomAttachmentListBlock,
    CustomAudioBlock,
    CustomVideoBlock,
    CustomEmbedBlock,
    CustomLeadBlock,
    CustomStepBlock,
    CustomTipBlock,
    CustomFaqBlock,
    CustomReferenceBlock,
)
from content_blocks.blocks.media import _resolve_asset_id
from content_blocks.contracts import BlockRenderResult, MediaReference
from content_blocks.sanitizer import sanitize_html

TECH_CATEGORIES = [
    ("frontend", "前端技术 (Frontend)"),
    ("backend", "后端架构 (Backend)"),
    ("database", "数据库与存储 (Database)"),
    ("devops", "容器与运维 (DevOps)"),
    ("ai", "人工智能与算法 (AI/ML)"),
    ("tools", "开发工具与库 (Tools/Libs)"),
]

REPO_PLATFORMS = [
    ("github", "GitHub"),
    ("gitee", "Gitee"),
    ("gitlab", "GitLab"),
    ("custom", "自建 Git / 私有仓库"),
]


class TechStackBlock(blocks.StructBlock):
    """单个技术栈徽标组件"""
    category = blocks.ChoiceBlock(choices=TECH_CATEGORIES, default="backend", label="技术分类")
    name = blocks.CharBlock(max_length=64, label="技术/框架名称 (如 Django 6.1)")
    version = blocks.CharBlock(max_length=32, required=False, label="核心版本号")
    description = blocks.CharBlock(max_length=255, required=False, label="在项目中的角色与应用场景")

    class Meta:
        icon = "cogs"
        label = "技术栈组件"

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)
        cat_key = str(value.get("category") or "backend")
        cat_label = html.escape(dict(TECH_CATEGORIES).get(cat_key, cat_key))
        name = html.escape(str(value.get("name") or ""))
        version = html.escape(str(value.get("version") or "").strip())
        desc = html.escape(str(value.get("description") or "").strip())
        version_html = f'<span class="portfolio-tech-version">v{version}</span>' if version else ""
        desc_html = f'<p class="portfolio-tech-desc">{desc}</p>' if desc else ""
        html_out = (
            f'<div class="portfolio-tech-item">'
            f'<div class="portfolio-tech-top">'
            f'<span class="portfolio-tech-cat">{cat_label}</span>'
            f'{version_html}'
            f'</div>'
            f'<div class="portfolio-tech-name">{name}</div>'
            f'{desc_html}'
            f'</div>'
        )
        return BlockRenderResult(html=sanitize_html(html_out), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        return mark_safe(self.render_clean(value, context).html)


class RepositoryBlock(blocks.StructBlock):
    """开源仓库与演示链接组件"""
    platform = blocks.ChoiceBlock(choices=REPO_PLATFORMS, default="github", label="代码托管平台")
    repo_url = blocks.URLBlock(label="代码仓库地址 (如 https://github.com/...)")
    demo_url = blocks.URLBlock(required=False, label="在线演示体验地址")
    license_type = blocks.CharBlock(required=False, max_length=64, default="MIT", label="开源协议 (License)")
    star_count = blocks.IntegerBlock(required=False, default=0, min_value=0, label="Star 数量（展示用）")

    class Meta:
        icon = "site"
        label = "代码仓库与演示"

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)
        plat_key = str(value.get("platform") or "github")
        plat_label = html.escape(dict(REPO_PLATFORMS).get(plat_key, plat_key))
        repo_url = str(value.get("repo_url") or "").strip()
        demo_url = str(value.get("demo_url") or "").strip()
        license_type = html.escape(str(value.get("license_type") or "").strip())
        star_count = value.get("star_count")

        badges = [f'<span class="detail-badge">{plat_label}</span>']
        if license_type:
            badges.append(f'<span class="detail-badge">📄 {license_type}</span>')
        if star_count is not None and str(star_count) != "":
            badges.append(f'<span class="detail-badge">⭐ {int(star_count)}</span>')

        links: list[str] = []
        if repo_url:
            safe_repo = html.escape(repo_url, quote=True)
            links.append(
                f'<a href="{safe_repo}" target="_blank" rel="noopener noreferrer" data-analytics-conv="repo_click">🔗 访问代码仓库</a>'
            )
        if demo_url:
            safe_demo = html.escape(demo_url, quote=True)
            links.append(
                f'<a href="{safe_demo}" target="_blank" rel="noopener noreferrer" data-analytics-conv="demo_click">🚀 在线演示</a>'
            )

        html_out = (
            f'<div class="portfolio-repo-card">'
            f'<h4 class="portfolio-repo-title">开源仓库与构建信息</h4>'
            f'<div class="detail-badges">{"".join(badges)}</div>'
            f'<div class="portfolio-repo-links">{"".join(links)}</div>'
            f'</div>'
        )
        return BlockRenderResult(html=sanitize_html(html_out), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        return mark_safe(self.render_clean(value, context).html)


class ScreenshotItemBlock(blocks.StructBlock):
    """画廊单张截图项"""
    image = ImageChooserBlock(label="选择截图")
    caption = blocks.CharBlock(required=False, max_length=255, label="截图说明/功能描述")
    is_cover = blocks.BooleanBlock(required=False, default=False, label="设为本画廊主推荐图")

    class Meta:
        icon = "image"
        label = "截图项"


class ScreenshotGalleryBlock(blocks.StructBlock):
    """界面截图与功能演示多图画廊"""
    heading = blocks.CharBlock(required=False, max_length=120, default="系统界面与功能演示", label="画廊标题")
    screenshots = blocks.ListBlock(ScreenshotItemBlock(), label="截图列表")

    class Meta:
        icon = "image"
        label = "界面截图画廊"

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)
        heading = html.escape(str(value.get("heading") or "系统界面与功能演示"))
        cards: list[str] = []
        for item in value.get("screenshots") or []:
            img = item.get("image")
            asset_id = _resolve_asset_id(img)
            if not asset_id:
                continue
            src = f"/media-gateway/image/{asset_id}/"
            caption = html.escape(str(item.get("caption") or "").strip())
            alt_text = caption or html.escape(str(getattr(img, "title", "") or heading))
            is_cover = bool(item.get("is_cover"))
            cover_badge = '<span class="portfolio-cover-tag">主推截图</span>' if is_cover else ""
            cap_html = f'<div class="gallery-caption">{cover_badge}{caption}</div>' if (caption or is_cover) else ""
            cards.append(
                f'<div class="gallery-card" data-full-src="{src}" data-caption="{caption}">'
                f'<img src="{src}" alt="{alt_text}" class="gallery-thumb" loading="lazy" data-asset-id="{asset_id}" />'
                f'{cap_html}'
                f'</div>'
            )
        if not cards:
            return BlockRenderResult(html="", is_safe=True)
        html_out = (
            f'<div class="portfolio-gallery-widget">'
            f'<h4 class="portfolio-gallery-heading">{heading}</h4>'
            f'<div class="gallery-grid">{"".join(cards)}</div>'
            f'</div>'
        )
        return BlockRenderResult(html=sanitize_html(html_out), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        return mark_safe(self.render_clean(value, context).html)

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        if not value or not value.get("screenshots"):
            return []
        refs = []
        for idx, item in enumerate(value["screenshots"]):
            img = item.get("image")
            asset_id = _resolve_asset_id(img)
            if asset_id:
                refs.append(
                    MediaReference(
                        asset_id=asset_id,
                        asset_type="image",
                        block_path=f"{path}.screenshots.{idx}.image",
                    )
                )
        return refs


class FeaturedPostsBlock(blocks.StructBlock):
    """展示与该项目相关的精选/配套博客文章列表（支持 PageChooser 跨模型关联）"""
    heading = blocks.CharBlock(max_length=120, default="相关技术文章与架构演进", label="模块标题")
    description = blocks.TextBlock(required=False, rows=2, label="导语说明")
    posts = blocks.ListBlock(blocks.PageChooserBlock(page_type="blog.BlogPage"), label="选择关联博客文章")

    class Meta:
        icon = "folder-open-inverse"
        label = "关联博客文章推荐"

    def render_clean(self, value: Any, context: Any = None):
        from content_lifecycle.public_access import filter_public_pages, resolve_public_site_for_request

        ctx = context if isinstance(context, dict) else {}
        req = ctx.get("request")
        host_page = ctx.get("page")
        is_preview = bool(
            ctx.get("is_preview")
            or (req is not None and (getattr(req, "is_preview", False) or getattr(req, "in_preview_panel", False)))
        )
        site = resolve_public_site_for_request(req, page=host_page) if (req is not None or host_page is not None) else None
        if site is None:
            return BlockRenderResult(html="", is_safe=True)

        heading = html.escape(value.get("heading") or "相关技术文章与架构演进")
        desc = html.escape(value.get("description") or "")
        raw_posts = [p for p in (value.get("posts") or []) if p is not None]
        public_posts = filter_public_pages(raw_posts, site=site, request=req)
        filtered_out_count = len(raw_posts) - len(public_posts)

        if not public_posts and not (is_preview and filtered_out_count > 0):
            return BlockRenderResult(html="", is_safe=True)

        posts_html = []
        for post in public_posts:
            url = (post.get_url(request=req) if hasattr(post, "get_url") else post.url) or ""
            if not url:
                continue
            title = html.escape(post.title)
            safe_url = html.escape(url)
            date_str = post.authored_on.strftime("%Y-%m-%d") if getattr(post, "authored_on", None) else ""
            posts_html.append(
                f'<li class="featured-post-item">'
                f'<a href="{safe_url}" class="featured-post-link">{title}</a>'
                f'{f"<span class=\"featured-post-date\">{date_str}</span>" if date_str else ""}'
                f'</li>'
            )

        if is_preview and filtered_out_count > 0:
            posts_html.append(
                '<li class="featured-post-item is-preview-hidden">'
                '（存在未发布、受限或非当前站点关联文章，公开页面将自动隐藏）'
                '</li>'
            )

        if not posts_html:
            return BlockRenderResult(html="", is_safe=True)

        items_html = "".join(posts_html)
        html_out = (
            f'<div class="featured-posts-widget">'
            f'<h4 class="featured-posts-heading">{heading}</h4>'
            f'{f"<p class=\"featured-posts-desc\">{desc}</p>" if desc else ""}'
            f'<ul class="featured-posts-list">{items_html}</ul>'
            f'</div>'
        )
        return BlockRenderResult(html=sanitize_html(html_out), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        return mark_safe(self.render_clean(value, context).html)

    def extract_text(self, value: Any):
        from content_blocks.contracts import BlockTextExtraction
        heading = value.get("heading") or ""
        desc = value.get("description") or ""
        # 仅索引块自身的标题与导语，不把目标文章标题写入宿主页索引（防止目标转私密后从宿主页摘要泄漏）
        text = f"{heading} {desc}".strip()
        return BlockTextExtraction(plain_text=text, logical_segments=[text] if text else [])


class PortfolioStreamBlock(blocks.StreamBlock):
    """作品集与开源项目主积木流"""
    # 专属积木
    tech_stack = blocks.ListBlock(TechStackBlock(), label="核心技术栈列表", group="项目专属")
    repository = RepositoryBlock(label="代码仓库与在线演示", group="项目专属")
    gallery = ScreenshotGalleryBlock(label="界面截图画廊", group="项目专属")
    featured_posts = FeaturedPostsBlock(label="关联博客文章推荐", group="项目专属")

    # 通用内容积木 (从 content_blocks 引入)
    rich_text = CustomRichTextBlock(label="富文本段落", group="排版内容")
    markdown = CustomMarkdownBlock(label="Markdown 章节", group="排版内容")
    code = CustomCodeBlock(label="代码块高亮", group="排版内容")
    table = CustomTableBlock(label="结构化对比表格", group="排版内容")
    mermaid = CustomMermaidBlock(label="架构流程图 (Mermaid)", group="排版内容")
    image = CustomImageBlock(label="单张配图", group="多媒体")
    media_text = CustomMediaTextBlock(label="图文并排", group="多媒体")
    document = CustomDocumentBlock(label="文档附件", group="多媒体")
    attachment_list = CustomAttachmentListBlock(label="课件/附件下载列表", group="多媒体")
    audio = CustomAudioBlock(label="音频", group="多媒体")
    video = CustomVideoBlock(label="演示视频", group="多媒体")
    embed = CustomEmbedBlock(label="外部媒体", group="多媒体")

    # 教程积木
    lead = CustomLeadBlock(label="项目导读", group="结构化模块")
    step = CustomStepBlock(label="实施步骤", group="结构化模块")
    tip = CustomTipBlock(label="技术要点提示", group="结构化模块")
    faq = CustomFaqBlock(label="常见答疑 (FAQ)", group="结构化模块")
    reference = CustomReferenceBlock(label="参考资料", group="结构化模块")

    class Meta:
        label = "作品集内容积木流"
