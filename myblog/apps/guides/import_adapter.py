"""
GuidePage Markdown 导入适配器。
支持 summary/topic/difficulty/estimated_minutes 与显式 :::step 步骤块转换；
缺 topic 或 step 时允许存草稿，但返回明确的 publish_blockers 提醒。
"""

from typing import Any

from markdown_import.contracts import (
    DraftSpec,
    ImportFieldDef,
    ImportIssue,
    ImportSchema,
)
from markdown_import.parser import parse_explicit_steps, rewrite_asset_links, validate_relative_asset_path
from tools.markdown_import.templates import render_front_matter_template


class GuidePageImportAdapter:
    type_key = "guide_page"
    model_label = "guides.GuidePage"
    schema_version = 1

    def describe(self, actor: Any = None, parent: Any = None) -> ImportSchema:
        from guides.models import DIFFICULTY_CHOICES, GuideTopic
        topics = tuple((t.slug, t.name) for t in GuideTopic.objects.order_by("name")[:100])
        return ImportSchema(
            type_key=self.type_key,
            label="专题指南 (GuidePage)",
            model_label=self.model_label,
            schema_version=self.schema_version,
            allowed_parent_models=("guides.GuideIndexPage",),
            fields=(
                ImportFieldDef("title", "指南标题", "string", required=True),
                ImportFieldDef("authored_on", "成文日期", "date", required=False),
                ImportFieldDef("topic", "专栏主题", "select", required=False, choices=topics, help_text="发布前必选；导入草稿可暂留空"),
                ImportFieldDef("difficulty", "难度等级", "select", required=False, default="intermediate", choices=tuple(DIFFICULTY_CHOICES)),
                ImportFieldDef("estimated_minutes", "预计耗时(分钟)", "integer", required=False, default=15),
                ImportFieldDef("summary", "摘要导语", "text", required=False),
                ImportFieldDef("tags", "技术标签", "tags", required=False),
                ImportFieldDef("cover", "封面/架构图", "image_ref", required=False),
            ),
            template_markdown=render_front_matter_template("guide_page"),
        )

    def normalize(self, parsed_doc: dict[str, Any], edits: dict[str, Any], context: dict[str, Any]) -> DraftSpec:
        from guides.models import DIFFICULTY_CHOICES, GuideTopic

        fm = parsed_doc.get("front_matter") or {}
        body_md = str(parsed_doc.get("body_markdown") or "")
        replacements = parsed_doc.get("link_replacements") or {}
        if replacements:
            body_md = rewrite_asset_links(body_md, replacements)

        steps, rem_md, step_issues = parse_explicit_steps(body_md)
        issues = list(parsed_doc.get("pre_issues") or []) + step_issues

        summary_text = str(edits.get("summary") if "summary" in edits and edits.get("summary") is not None else (fm.get("summary") or fm.get("intro") or "")).strip()
        raw_topic = str(edits.get("topic") if "topic" in edits and edits.get("topic") is not None else (fm.get("topic") or "")).strip()
        topic_id = None
        if raw_topic:
            topic_obj = GuideTopic.objects.filter(slug=raw_topic).first() or GuideTopic.objects.filter(name=raw_topic).first()
            if topic_obj:
                topic_id = topic_obj.pk
            else:
                issues.append(
                    ImportIssue(
                        code="topic_not_found",
                        message=f"未找到匹配的专栏主题 '{raw_topic}'，已暂留空；发布前请在后台选择。",
                        field="topic",
                        severity="warning",
                    )
                )

        valid_diffs = {k for k, _ in DIFFICULTY_CHOICES}
        difficulty = str(edits.get("difficulty") or fm.get("difficulty") or "intermediate").strip()
        if difficulty not in valid_diffs:
            difficulty = "intermediate"

        raw_mins = edits.get("estimated_minutes") if "estimated_minutes" in edits and edits.get("estimated_minutes") is not None else fm.get("estimated_minutes", 15)
        try:
            estimated_minutes = max(1, min(600, int(raw_mins)))
        except (TypeError, ValueError):
            estimated_minutes = 15

        cover_key = ""
        raw_cover = str(edits.get("cover") if "cover" in edits and edits.get("cover") is not None else (fm.get("cover") or "")).strip()
        if raw_cover:
            try:
                cover_key = validate_relative_asset_path(raw_cover)
            except ValueError:
                cover_key = ""

        stream_blocks: list[dict[str, Any]] = []
        if rem_md.strip():
            stream_blocks.append({"type": "markdown", "value": rem_md})
        for st in steps:
            stream_blocks.append(
                {
                    "type": "step",
                    "value": {
                        "step_number": st["step_number"],
                        "title": st["title"],
                        "content": f"<p>{st['content']}</p>" if st["content"] else "<p></p>",
                        "code_language": "python",
                        "code_snippet": "",
                        "image": None,
                        "image_caption": "",
                    },
                }
            )

        return DraftSpec(
            type_key=self.type_key,
            title=parsed_doc["title"],
            slug_hint=str(fm.get("slug") or parsed_doc["title"]),
            authored_on=parsed_doc["authored_on"],
            summary_text=summary_text,
            tags=tuple(parsed_doc.get("tags") or ()),
            extra_fields={
                "topic_id": topic_id,
                "difficulty": difficulty,
                "estimated_minutes": estimated_minutes,
                "cover_key": cover_key,
                "step_count": len(steps),
            },
            stream_blocks=stream_blocks,
            media_refs=tuple(parsed_doc.get("media_refs") or ()),
            issues=tuple(issues),
            title_source=parsed_doc.get("title_source", "unknown"),
        )

    def build(self, draft_spec: DraftSpec, resolved_assets: dict[str, Any]) -> Any:
        from guides.blocks import GuideStreamBlock
        from guides.models import GuidePage

        cover_key = draft_spec.extra_fields.get("cover_key") or ""
        featured_image = resolved_assets.get(cover_key) if cover_key else None
        body_val = GuideStreamBlock().to_python(draft_spec.stream_blocks) if draft_spec.stream_blocks else []

        return GuidePage(
            title=draft_spec.title[:255],
            authored_on=draft_spec.authored_on,
            topic_id=draft_spec.extra_fields.get("topic_id"),
            difficulty=draft_spec.extra_fields.get("difficulty", "intermediate"),
            estimated_minutes=draft_spec.extra_fields.get("estimated_minutes", 15),
            summary=draft_spec.summary_text,
            featured_image=featured_image,
            body=body_val,
        )

    def publish_warnings(self, draft_spec: DraftSpec) -> list[ImportIssue]:
        blockers: list[ImportIssue] = []
        if not draft_spec.extra_fields.get("topic_id"):
            blockers.append(
                ImportIssue(
                    code="missing_guide_topic",
                    message="专题指南草稿未绑定专栏主题 (Topic)，可先存草稿，但正式发布前必须补选。",
                    field="topic",
                    severity="publish_blocker",
                )
            )
        if not draft_spec.extra_fields.get("step_count"):
            blockers.append(
                ImportIssue(
                    code="missing_guide_step",
                    message="专题指南未检测到 :::step 步骤块，可先存草稿，但正式发布前必须添加至少 1 个操作步骤。",
                    field="body",
                    severity="publish_blocker",
                )
            )
        return blockers
