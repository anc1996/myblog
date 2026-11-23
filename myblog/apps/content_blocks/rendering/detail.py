"""
全站详情页公共展示与单次服务端大纲目录 (TOC) 提取器。

职责：
1. 单次渲染 StreamField 每个积木并包裹受控根容器 `[data-cb-block]`；
2. 同一遍解析安全 HTML 中的 h1~h4 标题与 Guide/Step 卡片，跳过代码块、工具栏和提示卡片中的无关标题；
3. 为每个目录项生成并注入唯一确定性锚点（保留 `#step-<编号>` 兼容），确保无 JS 也能精准跳转；
4. 统一整理面包屑、作者列表与 TagIndexPage 详情页标签路由（缺失时安全降级为纯文本徽章）。
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any

from django.utils.safestring import mark_safe

_HEADING_TAGS = {"h1", "h2", "h3", "h4"}
_RAW_SKIP_TAGS = {"pre", "code", "script", "style", "svg"}
_SKIP_CLASSES = {
    "cb-mermaid-header",
    "cb-mermaid-source-wrapper",
    "cb-mermaid-status",
    "cb-cell-code",
    "code-header",
    "code-body",
    "step-code-container",
    "step-header",
    "tutorial-tip-card",
    "block-tip",
    "tutorial-faq-card",
    "block-faq",
    "tutorial-lead-card",
    "block-lead",
    "tutorial-reference-card",
    "block-reference",
    "featured-posts-widget",
    "portfolio-repo-card",
    "portfolio-gallery-widget",
}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


def _parse_attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in attrs:
        lk = k.lower()
        if lk not in out:
            out[lk] = "" if v is None else v
    return out


def _format_start_tag(tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool = False) -> str:
    chunks: list[str] = []
    for k, v in attrs:
        if v is None:
            chunks.append(f" {k}")
        else:
            chunks.append(f' {k}="{html.escape(v, quote=True)}"')
    suffix = " />" if (self_closing and tag.lower() not in _VOID_TAGS) else ">"
    return f"<{tag}{''.join(chunks)}{suffix}"


def _unique_anchor(candidate: str, used_ids: set[str]) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]", "-", candidate).strip("-") or "section"
    if clean not in used_ids:
        used_ids.add(clean)
        return clean
    seq = 2
    while f"{clean}-{seq}" in used_ids:
        seq += 1
    final = f"{clean}-{seq}"
    used_ids.add(final)
    return final


class _SinglePassTocHTMLRewriter(HTMLParser):
    """单遍扫描单个积木渲染后的 HTML，收集目录项并注入唯一锚点 ID。"""

    def __init__(
        self,
        *,
        safe_block_id: str,
        used_ids: set[str],
        is_step_block: bool = False,
        step_anchor: str = "",
    ) -> None:
        super().__init__(convert_charrefs=False)
        self.safe_block_id = safe_block_id
        self.used_ids = used_ids
        self.is_step_block = is_step_block
        self.step_anchor = step_anchor
        self._step_root_handled = False

        self.parts: list[str] = []
        self.toc_items: list[dict[str, Any]] = []
        self._heading_seq = 0

        # 元素栈跟踪跳过区域: tuple[tag, skip_flag]
        self._stack: list[tuple[str, bool]] = []
        self._skip_depth = 0

        # 当前正在采集的标题状态
        self._active_heading: dict[str, Any] | None = None

    def _flush_heading(self) -> None:
        if not self._active_heading:
            return
        item = self._active_heading
        self._active_heading = None
        raw_text = html.unescape("".join(item["text_parts"]))
        clean_text = re.sub(r"\s+", " ", raw_text).strip()
        attrs: list[tuple[str, str | None]] = item["attrs"]

        if clean_text:
            existing_id = ""
            for k, v in attrs:
                if k.lower() == "id" and v:
                    existing_id = v.strip()
                    break
            if existing_id and existing_id not in self.used_ids:
                anchor_id = existing_id
                self.used_ids.add(anchor_id)
            else:
                self._heading_seq += 1
                anchor_id = _unique_anchor(f"toc-{self.safe_block_id}-{self._heading_seq}", self.used_ids)

            new_attrs: list[tuple[str, str | None]] = []
            replaced = False
            for k, v in attrs:
                if k.lower() == "id":
                    if not replaced:
                        new_attrs.append(("id", anchor_id))
                        replaced = True
                else:
                    new_attrs.append((k, v))
            if not replaced:
                new_attrs.insert(0, ("id", anchor_id))
            attrs = new_attrs

            self.toc_items.append({
                "id": anchor_id,
                "text": clean_text,
                "level": item["level"],
                "is_step": False,
            })

        self.parts.append(_format_start_tag(item["tag"], attrs))
        self.parts.extend(item["inner_parts"])
        self.parts.append(f"</{item['tag']}>")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ltag = tag.lower()
        attr_map = _parse_attrs_dict(attrs)
        classes = set(attr_map.get("class", "").split())

        # Step 卡片外层 div 同步唯一锚点 ID（保留 #step-<编号> 兼容性）
        if self.is_step_block and not self._step_root_handled and ltag == "div" and "block-step" in classes:
            self._step_root_handled = True
            new_attrs: list[tuple[str, str | None]] = []
            replaced = False
            for k, v in attrs:
                if k.lower() == "id":
                    if not replaced:
                        new_attrs.append(("id", self.step_anchor))
                        replaced = True
                else:
                    new_attrs.append((k, v))
            if not replaced:
                new_attrs.insert(0, ("id", self.step_anchor))
            attrs = new_attrs

        is_skip_node = (ltag in _RAW_SKIP_TAGS) or bool(classes & _SKIP_CLASSES)
        if ltag not in _VOID_TAGS:
            self._stack.append((ltag, is_skip_node))
            if is_skip_node:
                self._skip_depth += 1

        if self._active_heading is not None:
            self._active_heading["inner_parts"].append(_format_start_tag(tag, attrs))
            return

        if ltag in _HEADING_TAGS and self._skip_depth == 0:
            self._active_heading = {
                "tag": tag,
                "level": int(ltag[1]),
                "attrs": list(attrs),
                "inner_parts": [],
                "text_parts": [],
            }
            return

        self.parts.append(_format_start_tag(tag, attrs))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_str = _format_start_tag(tag, attrs, self_closing=True)
        if self._active_heading is not None:
            self._active_heading["inner_parts"].append(tag_str)
        else:
            self.parts.append(tag_str)

    def handle_endtag(self, tag: str) -> None:
        ltag = tag.lower()
        if self._active_heading is not None:
            if ltag == self._active_heading["tag"].lower():
                self._flush_heading()
            else:
                self._active_heading["inner_parts"].append(f"</{tag}>")
            # 仍需弹出对应栈元素
            for idx in range(len(self._stack) - 1, -1, -1):
                st_tag, st_skip = self._stack[idx]
                if st_tag == ltag:
                    self._stack.pop(idx)
                    if st_skip and self._skip_depth > 0:
                        self._skip_depth -= 1
                    break
            return

        for idx in range(len(self._stack) - 1, -1, -1):
            st_tag, st_skip = self._stack[idx]
            if st_tag == ltag:
                self._stack.pop(idx)
                if st_skip and self._skip_depth > 0:
                    self._skip_depth -= 1
                break

        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._active_heading is not None:
            self._active_heading["inner_parts"].append(data)
            self._active_heading["text_parts"].append(data)
        else:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        ref = f"&{name};"
        if self._active_heading is not None:
            self._active_heading["inner_parts"].append(ref)
            self._active_heading["text_parts"].append(ref)
        else:
            self.parts.append(ref)

    def handle_charref(self, name: str) -> None:
        ref = f"&#{name};"
        if self._active_heading is not None:
            self._active_heading["inner_parts"].append(ref)
            self._active_heading["text_parts"].append(ref)
        else:
            self.parts.append(ref)

    def handle_comment(self, data: str) -> None:
        if self._active_heading is None:
            self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        if self._active_heading is None:
            self.parts.append(f"<!{decl}>")

    def render(self) -> str:
        if self._active_heading is not None:
            self._flush_heading()
        return "".join(self.parts)


def _resolve_tag_index_page(request: Any = None):
    """查找当前站点下已发布且未回收的 TagIndexPage，避免硬编码 /tags/ 路径。"""
    try:
        from taxonomy.models import TagIndexPage
        qs = TagIndexPage.objects.live().filter(is_recycled=False)
        if request is not None:
            from wagtail.models import Site
            site = Site.find_for_request(request)
            if site and site.root_page:
                scoped = qs.descendant_of(site.root_page, inclusive=True).first()
                if scoped:
                    return scoped
        return qs.first()
    except Exception:
        return None


def resolve_tag_links(page: Any, request: Any = None) -> list[dict[str, Any]]:
    """将页面标签统一解析为 TagIndexPage 详情路由；未部署 TagIndexPage 时返回 url=None（纯文本标签）。"""
    tags = list(page.tags.all()) if hasattr(page, "tags") else []
    if not tags:
        return []

    tag_index = _resolve_tag_index_page(request)
    base_url = ""
    if tag_index is not None:
        try:
            base_url = tag_index.get_url(request) or tag_index.url or ""
        except Exception:
            base_url = tag_index.url or ""

    links: list[dict[str, Any]] = []
    for tag in tags:
        resolved_url = None
        if tag_index is not None and base_url:
            try:
                sub = tag_index.reverse_subpage("detail", args=[tag.slug])
                if sub is not None:
                    resolved_url = f"{base_url.rstrip('/')}/{str(sub).lstrip('/')}"
            except Exception:
                resolved_url = None
        links.append({
            "tag": tag,
            "id": tag.id,
            "name": tag.name,
            "slug": tag.slug,
            "url": resolved_url,
        })
    return links


def _resolve_author_badges(page: Any, request: Any, *, is_preview: bool) -> list[dict[str, Any]]:
    """
    生成文章详情页统一署名徽章 DTO（支持跳转至 /authors/<slug>/ 与未署名兜底「佚名」）：
    - 公开浏览优先读取与当前 live_revision_id 严格匹配的 PublishedSearchAuthor + 最新 Person 资料；
    - 副本暂缺时从当前 page.authors / 历史核准计算，不在 GET 中写库或水合正文；
    - 后台预览读取预览对象草稿作者，仅在对方已具备有效公开专栏目标时附链接。
    """
    if not hasattr(page, "authors"):
        return []

    from django.urls import NoReverseMatch, reverse
    from django.conf import settings
    from django.utils import timezone
    from wagtail.models import Site
    from media_library.models import VisibilityChoices
    from people.attribution import (
        extract_raw_author_refs_from_page,
        extract_raw_author_refs_from_revision_content,
        resolve_author_attributions,
    )
    from people.author_identity import normalize_author_role
    from people.models import Person

    author_discovery_enabled = getattr(settings, "AUTHOR_DISCOVERY_ENABLED", True)

    def _author_url(slug: str) -> str:
        if not author_discovery_enabled:
            return ""
        try:
            return reverse("people:author_detail", kwargs={"slug": slug})
        except NoReverseMatch:
            return f"/authors/{slug}/"

    def _avatar_url_for_person(p: Person | None) -> str:
        if p is None or not getattr(p, "avatar_id", None):
            return ""
        av = getattr(p, "avatar", None)
        if av is None or getattr(av, "visibility", None) != VisibilityChoices.PUBLIC:
            return ""
        return f"/media-gateway/image/{av.public_id}/"

    live_rev_id = getattr(page, "live_revision_id", None)
    page_id = getattr(page, "id", None)

    if not is_preview and getattr(page, "live", False) and page_id and live_rev_id:
        from discovery.models import PublishedSearchAuthor, PublishedSearchDocument

        doc = (
            PublishedSearchDocument.objects.filter(
                page_id=page_id,
                effective_revision_id=live_rev_id,
                index_generation=1,
                is_live=True,
                is_recycled=False,
            )
            .only("id")
            .first()
        )
        if doc is not None:
            rows = list(
                PublishedSearchAuthor.objects.filter(document=doc)
                .order_by("sort_order", "id")
                .values("person_id", "author_name", "author_slug", "role")
            )
            if rows:
                persons_map = {
                    p.pk: p
                    for p in Person.objects.select_related("avatar").filter(
                        pk__in=[r["person_id"] for r in rows]
                    )
                }
                badges: list[dict[str, Any]] = []
                for r in rows:
                    p = persons_map.get(r["person_id"])
                    slug = p.slug if p else r["author_slug"]
                    name = p.name if p else r["author_name"]
                    is_anon = bool(p.is_system_anonymous) if p else (slug == "anonymous")
                    _, role_label = normalize_author_role(r["role"])
                    badges.append({
                        "name": name,
                        "slug": slug,
                        "url": _author_url(slug),
                        "avatar_url": _avatar_url_for_person(p),
                        "role": r["role"],
                        "role_label": role_label,
                        "is_anonymous": is_anon,
                    })
                return badges

    if not is_preview and live_rev_id:
        live_rev = getattr(page, "live_revision", None)
        if live_rev is None or getattr(live_rev, "id", None) != live_rev_id:
            from wagtail.models import Revision
            live_rev = Revision.objects.filter(id=live_rev_id).first()
        raw_refs = extract_raw_author_refs_from_revision_content(
            getattr(live_rev, "content", None) if live_rev is not None else None
        )
    else:
        raw_refs = extract_raw_author_refs_from_page(page)
    try:
        resolved = resolve_author_attributions(
            raw_refs,
            revision_id=None if is_preview else live_rev_id,
            allow_anonymous=True,
        )
    except RuntimeError:
        resolved = []

    if not resolved:
        return []

    persons_map = {
        p.pk: p
        for p in Person.objects.select_related("avatar").filter(
            pk__in=[a.person_id for a in resolved]
        )
    }

    active_preview_pids: set[int] = set()
    if is_preview:
        from discovery.eligibility import apply_public_eligibility_filter
        from discovery.models import PublishedSearchAuthor, PublishedSearchDocument
        from discovery.registry import get_author_enabled_type_keys

        site = Site.find_for_request(request) if request is not None else None
        eligible = PublishedSearchDocument.objects.filter(
            is_live=True,
            is_recycled=False,
            type_key__in=get_author_enabled_type_keys(),
            authored_on__lte=timezone.localdate(),
        )
        if site is not None:
            eligible = eligible.filter(site_id=site.id)
        eligible = apply_public_eligibility_filter(eligible)
        active_preview_pids = set(
            PublishedSearchAuthor.objects.filter(
                person_id__in=[a.person_id for a in resolved],
                document__in=eligible,
            ).values_list("person_id", flat=True)
        )

    badges = []
    for a in resolved:
        p = persons_map.get(a.person_id)
        url = _author_url(a.slug) if (not is_preview or a.person_id in active_preview_pids) else ""
        badges.append({
            "name": p.name if p else a.name,
            "slug": a.slug,
            "url": url,
            "avatar_url": _avatar_url_for_person(p),
            "role": a.role,
            "role_label": a.role_label,
            "is_anonymous": a.is_anonymous,
        })
    return badges


def _build_toc_tree(flat_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    根据平铺标题列表构建真实多级嵌套树（支持最多 4 级：如 h2→h3→h4，兼容旧文含 h1）。
    规则：以最近一个级别更高的标题作父节点，跳级不补空层。
    """
    if not flat_items:
        return []

    min_level = min(item["level"] for item in flat_items)
    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []

    for item in flat_items:
        node = {
            "id": item["id"],
            "text": item["text"],
            "heading_level": item["level"],
            "depth": max(0, min(3, item["level"] - min_level)),
            "is_step": bool(item.get("is_step")),
            "children": [],
        }
        while stack and stack[-1]["heading_level"] >= node["heading_level"]:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            roots.append(node)
        stack.append(node)

    return roots


def build_detail_presentation(page: Any, request: Any = None, *, body_field: str = "body") -> dict[str, Any]:
    """
    单次遍历并渲染页面主体 StreamField，输出：
    - rendered_body_html: 带 [data-cb-block] 包裹与确定性锚点 ID 的安全 HTML
    - toc_items: 供桌面左侧折叠目录与手机/iPad <dialog> 抽屉复用的分级大纲
    - toc_tree: 嵌套树形目录节点列表（支持二级/三级/四级标题逐级折叠与定位）
    - has_toc: 是否存在至少 1 个有效目录项
    - breadcrumbs / authors / tag_links / is_preview
    """
    stream_val = getattr(page, body_field, None) or []
    # 预先保留详情页壳层自身 ID，防止正文标题锚点撞车
    used_ids: set[str] = {
        "page-top",
        "article-start",
        "article-end",
        "detail-toc-aside",
        "detail-toc-dialog",
        "comments-section",
    }
    rendered_blocks: list[str] = []
    toc_items: list[dict[str, Any]] = []

    for idx, bound_block in enumerate(stream_val):
        block_type = str(getattr(bound_block, "block_type", "") or "unknown")
        raw_id = str(getattr(bound_block, "id", "") or f"b{idx}")
        safe_block_id = re.sub(r"[^a-zA-Z0-9_-]", "", raw_id) or f"b{idx}"

        try:
            raw_block_html = str(bound_block.render())
        except Exception:
            raw_block_html = ""

        step_anchor = ""
        is_step = block_type == "step"
        if is_step:
            val = getattr(bound_block, "value", None) or {}
            step_num = str(val.get("step_number") or (idx + 1)).strip()
            step_title = str(val.get("title") or "").strip()
            step_anchor = _unique_anchor(f"step-{step_num}", used_ids)
            if step_title:
                toc_items.append({
                    "id": step_anchor,
                    "text": f"第 {step_num} 步: {step_title}" if step_num else step_title,
                    "level": 2,
                    "is_step": True,
                })

        rewriter = _SinglePassTocHTMLRewriter(
            safe_block_id=safe_block_id,
            used_ids=used_ids,
            is_step_block=is_step,
            step_anchor=step_anchor,
        )
        rewriter.feed(raw_block_html)
        rewriter.close()
        rewritten_html = rewriter.render()
        toc_items.extend(rewriter.toc_items)

        rendered_blocks.append(
            f'<div class="cb-block-root" data-cb-block="{html.escape(block_type, quote=True)}" '
            f'data-cb-block-id="{html.escape(safe_block_id, quote=True)}">{rewritten_html}</div>'
        )

    if toc_items:
        min_level = min(item["level"] for item in toc_items)
        for item in toc_items:
            item["indent_level"] = max(0, min(3, item["level"] - min_level))
    toc_tree = _build_toc_tree(toc_items)

    breadcrumbs = [
        ancestor
        for ancestor in page.get_ancestors()
        if not ancestor.is_root() and not getattr(ancestor, "is_recycled", False)
    ]
    tag_links = resolve_tag_links(page, request)
    is_preview = bool(
        request is not None
        and (getattr(request, "is_preview", False) or getattr(request, "in_preview_panel", False))
    )
    authors = _resolve_author_badges(page, request, is_preview=is_preview)

    return {
        "rendered_body_html": mark_safe("".join(rendered_blocks)),
        "toc_items": toc_items,
        "toc_tree": toc_tree,
        "has_toc": bool(toc_items),
        "breadcrumbs": breadcrumbs,
        "authors": authors,
        "tag_links": tag_links,
        "is_preview": is_preview,
    }
