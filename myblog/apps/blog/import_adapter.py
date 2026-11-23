"""
BlogPage Markdown 导入适配器。
映射到 BlogPage 的外置 intro (BlogIntroBlocks) 与外置 body (BlogBodyBlocks)。
"""

from typing import Any

from markdown_import.contracts import (
    DraftSpec,
    ImportFieldDef,
    ImportIssue,
    ImportSchema,
)
from markdown_import.parser import rewrite_asset_links, validate_relative_asset_path
from tools.markdown_import.templates import render_front_matter_template


class BlogPageImportAdapter:
    type_key = "blog_page"
    model_label = "blog.BlogPage"
    schema_version = 1

    def describe(self, actor: Any = None, parent: Any = None) -> ImportSchema:
        return ImportSchema(
            type_key=self.type_key,
            label="博客文章 (BlogPage)",
            model_label=self.model_label,
            schema_version=self.schema_version,
            allowed_parent_models=("home.HomePage", "blog.BlogIndexPage", "blog.BlogPage"),
            fields=(
                ImportFieldDef("title", "文章标题", "string", required=True, help_text="留空自动取首个 # 标题或 AI 生成"),
                ImportFieldDef("authored_on", "成文日期", "date", required=False, help_text="YYYY-MM-DD，留空默认为当天"),
                ImportFieldDef("intro", "文章简介", "text", required=False, help_text="映射为外置 intro Markdown 积木，可由 AI 自动导读"),
                ImportFieldDef("tags", "技术标签", "tags", required=False, help_text="逗号或顿号分隔"),
                ImportFieldDef("cover", "封面配图", "image_ref", required=False, help_text="本地相对路径"),
            ),
            template_markdown=render_front_matter_template("blog_page"),
        )

    def normalize(self, parsed_doc: dict[str, Any], edits: dict[str, Any], context: dict[str, Any]) -> DraftSpec:
        fm = parsed_doc.get("front_matter") or {}
        body_md = str(parsed_doc.get("body_markdown") or "")
        replacements = parsed_doc.get("link_replacements") or {}
        if replacements:
            body_md = rewrite_asset_links(body_md, replacements)

        intro_text = str(edits.get("intro") if "intro" in edits and edits.get("intro") is not None else (fm.get("intro") or fm.get("summary") or "")).strip()
        cover_key = ""
        raw_cover = str(edits.get("cover") if "cover" in edits and edits.get("cover") is not None else (fm.get("cover") or "")).strip()
        if raw_cover:
            try:
                cover_key = validate_relative_asset_path(raw_cover)
            except ValueError:
                cover_key = ""

        stream_blocks = []
        if body_md.strip():
            stream_blocks.append({"type": "markdown", "value": body_md})

        return DraftSpec(
            type_key=self.type_key,
            title=parsed_doc["title"],
            slug_hint=str(fm.get("slug") or parsed_doc["title"]),
            authored_on=parsed_doc["authored_on"],
            summary_text=intro_text,
            tags=tuple(parsed_doc.get("tags") or ()),
            extra_fields={"cover_key": cover_key},
            stream_blocks=stream_blocks,
            media_refs=tuple(parsed_doc.get("media_refs") or ()),
            issues=tuple(parsed_doc.get("pre_issues") or ()),
            title_source=parsed_doc.get("title_source", "unknown"),
        )

    def build(self, draft_spec: DraftSpec, resolved_assets: dict[str, Any]) -> Any:
        from blog.blocks import BlogBodyBlocks, BlogIntroBlocks
        from blog.models import BlogPage

        cover_key = draft_spec.extra_fields.get("cover_key") or ""
        featured_image = resolved_assets.get(cover_key) if cover_key else None
        intro_val = BlogIntroBlocks().to_python([{"type": "markdown", "value": draft_spec.summary_text}]) if draft_spec.summary_text else []
        body_val = BlogBodyBlocks().to_python(draft_spec.stream_blocks) if draft_spec.stream_blocks else []

        return BlogPage(
            title=draft_spec.title[:255],
            authored_on=draft_spec.authored_on,
            featured_image=featured_image,
            intro=intro_val,
            body=body_val,
        )

    def publish_warnings(self, draft_spec: DraftSpec) -> list[ImportIssue]:
        return []
