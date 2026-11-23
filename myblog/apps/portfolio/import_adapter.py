"""
ProjectPage Markdown 导入适配器。
映射 project_status / repo_url / demo_url / primary_language / license_name / summary 与外置 body。
"""

from typing import Any
from urllib.parse import urlsplit

from markdown_import.contracts import (
    DraftSpec,
    ImportFieldDef,
    ImportIssue,
    ImportSchema,
)
from markdown_import.parser import rewrite_asset_links, validate_relative_asset_path
from tools.markdown_import.templates import render_front_matter_template


def _clean_http_url(raw_url: Any) -> tuple[str, bool]:
    val = str(raw_url or "").strip()
    if not val:
        return "", True
    parsed = urlsplit(val)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return val[:200], True
    return "", False


class ProjectPageImportAdapter:
    type_key = "project_page"
    model_label = "portfolio.ProjectPage"
    schema_version = 1

    def describe(self, actor: Any = None, parent: Any = None) -> ImportSchema:
        from portfolio.models import PROJECT_STATUS_CHOICES
        return ImportSchema(
            type_key=self.type_key,
            label="作品集项目 (ProjectPage)",
            model_label=self.model_label,
            schema_version=self.schema_version,
            allowed_parent_models=("portfolio.PortfolioIndexPage",),
            fields=(
                ImportFieldDef("title", "项目名称", "string", required=True),
                ImportFieldDef("authored_on", "展示成文日期", "date", required=False),
                ImportFieldDef("project_status", "项目状态", "select", required=False, default="maintained", choices=tuple(PROJECT_STATUS_CHOICES)),
                ImportFieldDef("primary_language", "主要语言", "string", required=False, default="Python"),
                ImportFieldDef("license_name", "开源协议", "string", required=False, default="MIT"),
                ImportFieldDef("repo_url", "仓库地址", "url", required=False),
                ImportFieldDef("demo_url", "演示地址", "url", required=False),
                ImportFieldDef("summary", "项目摘要", "text", required=False),
                ImportFieldDef("tags", "技术标签", "tags", required=False),
                ImportFieldDef("cover", "项目封面图", "image_ref", required=False),
            ),
            template_markdown=render_front_matter_template("project_page"),
        )

    def normalize(self, parsed_doc: dict[str, Any], edits: dict[str, Any], context: dict[str, Any]) -> DraftSpec:
        from portfolio.models import PROJECT_STATUS_CHOICES

        fm = parsed_doc.get("front_matter") or {}
        body_md = str(parsed_doc.get("body_markdown") or "")
        replacements = parsed_doc.get("link_replacements") or {}
        if replacements:
            body_md = rewrite_asset_links(body_md, replacements)

        issues = list(parsed_doc.get("pre_issues") or [])
        valid_statuses = {k for k, _ in PROJECT_STATUS_CHOICES}
        project_status = str(edits.get("project_status") or fm.get("project_status") or "maintained").strip()
        if project_status not in valid_statuses:
            project_status = "maintained"

        repo_url, repo_ok = _clean_http_url(edits.get("repo_url") if "repo_url" in edits else fm.get("repo_url"))
        if not repo_ok:
            issues.append(ImportIssue(code="invalid_repo_url", message="仓库地址必须是合法的 http/https URL。", field="repo_url"))

        demo_url, demo_ok = _clean_http_url(edits.get("demo_url") if "demo_url" in edits else fm.get("demo_url"))
        if not demo_ok:
            issues.append(ImportIssue(code="invalid_demo_url", message="演示地址必须是合法的 http/https URL。", field="demo_url"))

        summary_text = str(edits.get("summary") if "summary" in edits and edits.get("summary") is not None else (fm.get("summary") or fm.get("intro") or "")).strip()
        primary_language = str(edits.get("primary_language") or fm.get("primary_language") or "Python").strip()[:64]
        license_name = str(edits.get("license_name") or fm.get("license_name") or "MIT").strip()[:64]

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
            summary_text=summary_text,
            tags=tuple(parsed_doc.get("tags") or ()),
            extra_fields={
                "project_status": project_status,
                "repo_url": repo_url,
                "demo_url": demo_url,
                "primary_language": primary_language,
                "license_name": license_name,
                "cover_key": cover_key,
            },
            stream_blocks=stream_blocks,
            media_refs=tuple(parsed_doc.get("media_refs") or ()),
            issues=tuple(issues),
            title_source=parsed_doc.get("title_source", "unknown"),
        )

    def build(self, draft_spec: DraftSpec, resolved_assets: dict[str, Any]) -> Any:
        from portfolio.blocks import PortfolioStreamBlock
        from portfolio.models import ProjectPage

        cover_key = draft_spec.extra_fields.get("cover_key") or ""
        featured_image = resolved_assets.get(cover_key) if cover_key else None
        body_val = PortfolioStreamBlock().to_python(draft_spec.stream_blocks) if draft_spec.stream_blocks else []

        return ProjectPage(
            title=draft_spec.title[:255],
            authored_on=draft_spec.authored_on,
            project_status=draft_spec.extra_fields.get("project_status", "maintained"),
            summary=draft_spec.summary_text,
            repo_url=draft_spec.extra_fields.get("repo_url", ""),
            demo_url=draft_spec.extra_fields.get("demo_url", ""),
            primary_language=draft_spec.extra_fields.get("primary_language", "Python"),
            license_name=draft_spec.extra_fields.get("license_name", "MIT"),
            featured_image=featured_image,
            body=body_val,
        )

    def publish_warnings(self, draft_spec: DraftSpec) -> list[ImportIssue]:
        return []
