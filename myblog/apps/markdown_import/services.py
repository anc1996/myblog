"""
通用多页面 Markdown 导入核心编排服务：
1. preflight: 纯只读预检，返回规范化元数据、发布阻断提醒、资产清单与签名 plan_token；
2. create_or_get_job: 以 (actor, site, idempotency_key) 保证整单作业建档幂等；
3. upload_asset: 边读边验 SHA-256，创建归属当前作者个人工作区集合的受限媒体资产；
4. commit_job: 锁外写 MongoDB 不可变版本 -> 统一锁序内幂等建未发布草稿并登记媒体引用。
"""

from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
import re
import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from taggit.models import Tag
from wagtail.documents.forms import get_document_form
from wagtail.images import get_image_model
from wagtail.images.forms import get_image_form
from wagtail.models import Page, Site
from wagtail.permissions import policy_registry

from content_lifecycle.guard import content_mutation_guard
from content_lifecycle.models import bind_linked_person_for_page_owner
from external_content.models import OperationResult
from external_content.store import MongoContentStore
from media_library.forms import ProtectedDocumentForm
from media_library.models import CustomDocument, CustomImage, VisibilityChoices

from .auth import sign_plan_token, verify_plan_token
from .contracts import DraftSpec, ImportIssue
from .models import ImportAsset, ImportJob, ImportJobStatus
from .parser import (
    align_tags_with_vocabulary,
    extract_local_media_refs,
    extract_title_and_strip_h1,
    fallback_title_from_filename,
    normalize_tags_input,
    parse_authored_date,
    parse_front_matter,
    strip_matching_leading_h1,
)
from .permissions import (
    resolve_model_from_label,
    resolve_upload_collection_for_actor,
    validate_import_target_parent,
)
from .registry import get_import_adapter

MAX_MARKDOWN_BYTES = 4 * 1024 * 1024
MAX_ASSETS_PER_JOB = 50
MAX_TOTAL_ASSETS_BYTES = 300 * 1024 * 1024
JOB_TTL_HOURS = 24
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_declared_assets(declared_assets: list[dict[str, Any]]) -> int:
    """强校验声明的资源清单：每项必须为正整数大小与合法 64 位小写 SHA-256，防止 0/-1 或空哈希绕过限额。"""
    total_bytes = 0
    seen_keys: set[str] = set()
    for item in declared_assets:
        if not isinstance(item, dict):
            raise ImportServiceError("invalid_asset_manifest", "资源清单条目格式无效。", status=422)
        key = str(item.get("asset_key") or "").strip()
        if not key or key in seen_keys:
            raise ImportServiceError("invalid_asset_manifest", f"资源键无效或重复: {key!r}", status=422)
        seen_keys.add(key)
        raw_size = item.get("size")
        if isinstance(raw_size, bool):
            raise ImportServiceError("invalid_asset_size", f"资源大小无效: {key}", status=422)
        try:
            size_val = int(raw_size)
        except (TypeError, ValueError):
            raise ImportServiceError("invalid_asset_size", f"资源大小必须为正整数: {key}", status=422)
        if size_val <= 0:
            raise ImportServiceError("invalid_asset_size", f"资源大小必须大于 0: {key}", status=422)
        sha = str(item.get("sha256") or "").strip().lower()
        if not _SHA256_HEX_RE.match(sha):
            raise ImportServiceError("invalid_asset_sha256", f"资源必须提供 64 位十六进制 SHA-256 摘要: {key}", status=422)
        total_bytes += size_val
    if total_bytes > MAX_TOTAL_ASSETS_BYTES:
        raise ImportServiceError("assets_too_large", "所有附件总大小超过 300 MiB 上限。", status=413)
    return total_bytes


def _stabilize_stream_block_ids(page: Any, job_id: uuid.UUID) -> None:
    """
    为外置 StreamField 积木注入基于 job_id 与序号的确定性 UUID，
    消除 Wagtail 随机生成 block id 导致的 MongoDB 重试哈希冲突！
    """
    for field_name in ("intro", "body"):
        stream_val = getattr(page, field_name, None)
        if not stream_val:
            continue
        for idx, block_child in enumerate(stream_val):
            stable_id = str(uuid.uuid5(job_id, f"{field_name}:{idx}"))
            try:
                block_child.id = stable_id
            except Exception:
                pass


class ImportServiceError(Exception):
    """携带稳定 HTTP 状态码与错误编码的服务异常。"""

    def __init__(self, code: str, message: str, status: int = 422, fields: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.status = status
        self.fields = fields or {}
        super().__init__(message)


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _compute_input_digest(
    *,
    type_key: str,
    parent_id: int,
    source_name: str,
    markdown_text: str,
    declared_assets: list[dict[str, Any]],
    edits: dict[str, Any],
    schema_version: int,
) -> str:
    canonical = json.dumps(
        {
            "type_key": type_key,
            "parent_id": parent_id,
            "source_name": source_name,
            "markdown_sha256": _sha256_text(markdown_text),
            "assets": sorted(
                [
                    {
                        "asset_key": str(a.get("asset_key") or ""),
                        "media_type": str(a.get("media_type") or ""),
                        "size": int(a.get("size") or 0),
                        "sha256": str(a.get("sha256") or "").lower(),
                    }
                    for a in declared_assets
                ],
                key=lambda x: x["asset_key"],
            ),
            "edits": edits,
            "schema_version": schema_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _sha256_text(canonical)


def _parse_and_normalize_spec(
    *,
    adapter,
    raw_markdown: str,
    source_name: str,
    edits: dict[str, Any],
    frozen_today_iso: str | None = None,
    link_replacements: dict[str, tuple[str, str]] | None = None,
) -> tuple[DraftSpec, str]:
    if len((raw_markdown or "").encode("utf-8")) > MAX_MARKDOWN_BYTES:
        raise ImportServiceError(
            "markdown_too_large",
            f"Markdown 正文超过单篇上限（最大 {MAX_MARKDOWN_BYTES // (1024 * 1024)} MiB）。",
            status=413,
        )

    front_matter, body_text, fm_issues = parse_front_matter(raw_markdown or "")
    title, title_source, stripped_body = extract_title_and_strip_h1(front_matter, body_text, source_name)

    edited_title = str(edits.get("title") or "").strip()
    if edited_title:
        title = edited_title[:255]
        title_source = str(edits.get("title_source") or "user")
        stripped_body = strip_matching_leading_h1(body_text, title)
    elif not title:
        title = fallback_title_from_filename(source_name, body_text)[:255]
        title_source = "filename"

    default_date = (
        timezone.datetime.fromisoformat(frozen_today_iso).date()
        if frozen_today_iso
        else timezone.localdate()
    )
    raw_date = edits.get("authored_on") if "authored_on" in edits and edits.get("authored_on") else front_matter.get("date")
    authored_on, date_issues = parse_authored_date(raw_date, default_date)

    raw_tags = edits.get("tags") if "tags" in edits and edits.get("tags") is not None else front_matter.get("tags")
    normalized_tags = normalize_tags_input(raw_tags)
    vocab_names = list(Tag.objects.order_by("name").values_list("name", flat=True)[:2000])
    aligned_tags, unmatched_tags = align_tags_with_vocabulary(normalized_tags, vocab_names)
    allow_new_tags = bool(edits.get("allow_new_tags", False))
    final_tags = (aligned_tags + unmatched_tags)[:10] if allow_new_tags else aligned_tags[:10]

    media_refs, ref_issues = extract_local_media_refs(stripped_body, front_matter)

    parsed_doc = {
        "front_matter": front_matter,
        "body_markdown": stripped_body,
        "source_name": source_name,
        "title": title,
        "title_source": title_source,
        "authored_on": authored_on,
        "tags": final_tags,
        "unmatched_tags": unmatched_tags,
        "media_refs": media_refs,
        "pre_issues": fm_issues + date_issues + ref_issues,
        "link_replacements": link_replacements or {},
    }
    if not allow_new_tags and unmatched_tags:
        parsed_doc["pre_issues"].append(
            ImportIssue(
                code="unmatched_tags_ignored",
                message=f"以下标签不在站内词库中，未勾选“允许新建标签”时将略过：{', '.join(unmatched_tags)}",
                field="tags",
                severity="warning",
            )
        )

    draft_spec = adapter.normalize(parsed_doc, edits, {"default_date": default_date})
    return draft_spec, default_date.isoformat()


def build_preflight_payload(*, actor, site: Site, data: dict[str, Any]) -> dict[str, Any]:
    type_key = str(data.get("type_key") or "").strip()
    adapter = get_import_adapter(type_key)
    if not adapter:
        raise ImportServiceError("unknown_type_key", f"未注册的页面类型: {type_key}", status=422)

    try:
        parent_id = int(data.get("parent_id") or 0)
    except (TypeError, ValueError):
        raise ImportServiceError("invalid_parent_id", "目标父栏目 ID 无效。", status=422)

    target_model = resolve_model_from_label(adapter.model_label)
    parent_page = validate_import_target_parent(actor, parent_id, target_model, site)

    source_name = str(data.get("source_name") or "untitled.md").strip() or "untitled.md"
    raw_markdown = str(data.get("markdown") or "")
    edits = data.get("edits") if isinstance(data.get("edits"), dict) else {}
    declared_assets = data.get("assets") if isinstance(data.get("assets"), list) else []

    if len(declared_assets) > MAX_ASSETS_PER_JOB:
        raise ImportServiceError("too_many_assets", f"单篇最多允许携带 {MAX_ASSETS_PER_JOB} 个本地资源。", status=413)

    _validate_declared_assets(declared_assets)

    draft_spec, frozen_today = _parse_and_normalize_spec(
        adapter=adapter,
        raw_markdown=raw_markdown,
        source_name=source_name,
        edits=edits,
    )
    publish_blockers = adapter.publish_warnings(draft_spec)

    # 核对客户端上传清单是否覆盖了 Markdown 引用的所有本地资源
    declared_map = {str(a.get("asset_key") or ""): a for a in declared_assets}
    missing_assets = [
        ref.asset_key for ref in draft_spec.media_refs if ref.asset_key not in declared_map
    ]
    issues = list(draft_spec.issues)
    for missing_key in missing_assets:
        issues.append(
            ImportIssue(
                code="missing_local_asset",
                message=f"文中引用的本地文件未在上传清单中：{missing_key}",
                field="assets",
                severity="error",
            )
        )

    errors = [asdict(i) for i in issues if i.severity == "error"]
    warnings = [asdict(i) for i in issues if i.severity == "warning"]
    blockers = [asdict(i) for i in publish_blockers]

    # 同父节点同名标题提示（不阻断，仅提醒将自动生成唯一 slug）
    dup_exists = Page.objects.filter(
        path__startswith=parent_page.path,
        depth=parent_page.depth + 1,
        title=draft_spec.title,
    ).exists()
    if dup_exists:
        warnings.append(
            asdict(
                ImportIssue(
                    code="duplicate_title_warning",
                    message=f"栏目“{parent_page.title}”下已存在同名页面《{draft_spec.title}》，导入草稿会自动分配新网址后缀，绝不覆盖旧文章。",
                    field="title",
                    severity="warning",
                )
            )
        )

    input_digest = _compute_input_digest(
        type_key=type_key,
        parent_id=parent_page.id,
        source_name=source_name,
        markdown_text=raw_markdown,
        declared_assets=declared_assets,
        edits=edits,
        schema_version=adapter.schema_version,
    )

    plan_token = ""
    if not errors:
        plan_token = sign_plan_token(
            {
                "actor_id": actor.pk,
                "site_id": site.pk,
                "type_key": type_key,
                "parent_id": parent_page.id,
                "schema_version": adapter.schema_version,
                "input_digest": input_digest,
                "frozen_today": frozen_today,
            }
        )

    return {
        "normalized_metadata": {
            "title": draft_spec.title,
            "title_source": draft_spec.title_source,
            "authored_on": draft_spec.authored_on.isoformat(),
            "summary_text": draft_spec.summary_text,
            "tags": list(draft_spec.tags),
            "extra_fields": draft_spec.extra_fields,
        },
        "block_summary": {
            "total_blocks": len(draft_spec.stream_blocks),
            "block_types": [b.get("type") for b in draft_spec.stream_blocks],
            "media_count": len(draft_spec.media_refs),
        },
        "errors": errors,
        "warnings": warnings,
        "publish_blockers": blockers,
        "plan_token": plan_token,
    }


def create_or_get_import_job(*, actor, site: Site, data: dict[str, Any], idempotency_key: str) -> tuple[ImportJob, bool]:
    idem = (idempotency_key or "").strip()
    if not idem:
        raise ImportServiceError("missing_idempotency_key", "必须提供 Idempotency-Key。", status=422)

    plan_token = str(data.get("plan_token") or "").strip()
    try:
        plan = verify_plan_token(plan_token)
    except ValueError as exc:
        raise ImportServiceError("invalid_plan_token", str(exc), status=422)

    if int(plan.get("actor_id", 0)) != actor.pk or int(plan.get("site_id", 0)) != site.pk:
        raise ImportServiceError("plan_actor_mismatch", "预检令牌与当前账号或站点不匹配。", status=403)

    type_key = str(data.get("type_key") or "").strip()
    adapter = get_import_adapter(type_key)
    if not adapter:
        raise ImportServiceError("unknown_type_key", f"未知页面类型: {type_key}", status=422)

    parent_id = int(data.get("parent_id") or 0)
    source_name = str(data.get("source_name") or "untitled.md").strip() or "untitled.md"
    raw_markdown = str(data.get("markdown") or "")
    edits = data.get("edits") if isinstance(data.get("edits"), dict) else {}
    declared_assets = data.get("assets") if isinstance(data.get("assets"), list) else []

    target_model = resolve_model_from_label(adapter.model_label)
    validate_import_target_parent(actor, parent_id, target_model, site)
    if len(declared_assets) > MAX_ASSETS_PER_JOB:
        raise ImportServiceError("too_many_assets", f"单篇最多允许携带 {MAX_ASSETS_PER_JOB} 个本地资源。", status=413)
    _validate_declared_assets(declared_assets)

    input_digest = _compute_input_digest(
        type_key=type_key,
        parent_id=parent_id,
        source_name=source_name,
        markdown_text=raw_markdown,
        declared_assets=declared_assets,
        edits=edits,
        schema_version=adapter.schema_version,
    )
    if input_digest != plan.get("input_digest") or type_key != plan.get("type_key") or parent_id != int(plan.get("parent_id", 0)):
        raise ImportServiceError("plan_input_changed", "文件内容或目标选项自预检后发生变化，请重新预检。", status=409)

    frozen_payload = {
        "source_name": source_name,
        "markdown": raw_markdown,
        "edits": edits,
        "frozen_today": plan.get("frozen_today"),
        "declared_assets": declared_assets,
    }

    with transaction.atomic():
        existing = ImportJob.objects.select_for_update().filter(
            actor=actor, site=site, idempotency_key=idem
        ).first()
        if existing:
            if existing.input_digest != input_digest:
                raise ImportServiceError(
                    "idempotency_conflict",
                    "该 Idempotency-Key 已绑定其他内容，请使用新的幂等键。",
                    status=409,
                )
            return existing, False

        initial_status = ImportJobStatus.UPLOADING if declared_assets else ImportJobStatus.READY
        job = ImportJob.objects.create(
            actor=actor,
            site=site,
            idempotency_key=idem,
            input_digest=input_digest,
            type_key=type_key,
            parent_page_id=parent_id,
            source_name=source_name[:255],
            payload_json=frozen_payload,
            status=initial_status,
            expires_at=timezone.now() + timedelta(hours=JOB_TTL_HOURS),
        )
        for item in declared_assets:
            ImportAsset.objects.create(
                job=job,
                asset_key=str(item.get("asset_key") or "")[:255],
                media_type="image" if item.get("media_type") == "image" else "document",
                filename=str(item.get("filename") or item.get("asset_key") or "file")[:255],
                expected_size=int(item.get("size") or 0),
                expected_sha256=str(item.get("sha256") or "").lower(),
                is_staging=True,
            )
        return job, True


def upload_import_asset(*, actor, job_id: uuid.UUID, asset_key: str, uploaded_file) -> ImportAsset:
    if not uploaded_file:
        raise ImportServiceError("missing_file", "未收到上传文件。", status=422)

    # 锁外流式读取并计算实际字节与 SHA-256
    hasher = hashlib.sha256()
    actual_size = 0
    for chunk in uploaded_file.chunks():
        actual_size += len(chunk)
        hasher.update(chunk)
    actual_sha256 = hasher.hexdigest()
    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(0)

    with transaction.atomic():
        job = ImportJob.objects.select_for_update().filter(job_id=job_id).first()
        if not job or job.actor_id != actor.pk:
            raise ImportServiceError("job_not_found", "导入作业不存在。", status=404)
        if job.status == ImportJobStatus.DRAFT_CREATED:
            raise ImportServiceError("job_already_committed", "作业已完成提交，无需重复上传。", status=409)
        if job.expires_at <= timezone.now():
            raise ImportServiceError("job_expired", "导入作业已过期，请重新预检创建。", status=409)

        asset_row = ImportAsset.objects.select_for_update().filter(job=job, asset_key=asset_key).first()
        if not asset_row:
            raise ImportServiceError("asset_not_in_manifest", f"资源不在已确认清单中: {asset_key}", status=422)

        if actual_size <= 0 or actual_size != asset_row.expected_size:
            raise ImportServiceError("asset_size_mismatch", f"文件大小与预检不一致: {asset_key}", status=422)
        if not _SHA256_HEX_RE.match(asset_row.expected_sha256 or "") or actual_sha256 != asset_row.expected_sha256:
            raise ImportServiceError("asset_hash_mismatch", f"文件 SHA-256 与预检不一致: {asset_key}", status=422)

        uploaded_so_far = sum(
            int(sz)
            for sz in ImportAsset.objects.filter(job=job, actual_size__isnull=False)
            .exclude(pk=asset_row.pk)
            .values_list("actual_size", flat=True)
        )
        if uploaded_so_far + actual_size > MAX_TOTAL_ASSETS_BYTES:
            raise ImportServiceError("assets_too_large", "作业累计上传资源总大小超过 300 MiB 上限。", status=413)

        if asset_row.target_public_id and asset_row.actual_sha256 == actual_sha256:
            return asset_row

        if asset_row.media_type == "image":
            ImageModel = get_image_model()
            collection = resolve_upload_collection_for_actor(actor, ImageModel)
            ImageForm = get_image_form(ImageModel)
            form = ImageForm(
                data={"title": asset_row.filename[:120], "collection": str(collection.pk)},
                files={"file": uploaded_file},
                user=actor,
            )
            if not form.is_valid():
                raise ImportServiceError("invalid_image", f"图片校验失败: {form.errors.as_json()}", status=422)
            media_obj = form.save(commit=False)
            media_obj.uploaded_by_user = actor
            media_obj.visibility = VisibilityChoices.RESTRICTED
            media_obj.save()
            object_key = getattr(media_obj.file, "name", "") or ""
        else:
            collection = resolve_upload_collection_for_actor(actor, CustomDocument)
            DocumentForm = get_document_form(CustomDocument)
            form = DocumentForm(
                data={"title": asset_row.filename[:120], "collection": str(collection.pk)},
                files={"file": uploaded_file},
                user=actor,
            )
            if not form.is_valid():
                raise ImportServiceError("invalid_document", f"文档校验失败: {form.errors.as_json()}", status=422)
            media_obj = form.save(commit=False)
            media_obj.uploaded_by_user = actor
            media_obj.visibility = VisibilityChoices.RESTRICTED
            media_obj.save()
            object_key = getattr(media_obj.file, "name", "") or ""

        asset_row.actual_size = actual_size
        asset_row.actual_sha256 = actual_sha256
        asset_row.object_key = object_key[:512]
        asset_row.target_public_id = media_obj.public_id
        asset_row.uploaded_at = timezone.now()
        asset_row.save()

        pending = ImportAsset.objects.filter(job=job, target_public_id__isnull=True).exists()
        if not pending:
            job.status = ImportJobStatus.READY
            job.save(update_fields=["status", "updated_at"])
        return asset_row


def _generate_unique_child_slug(parent_page: Page, title: str, slug_hint: str) -> str:
    base = slugify(slug_hint or title, allow_unicode=True)[:220].strip("-")
    if not base:
        base = f"draft-{uuid.uuid4().hex[:8]}"
    existing_slugs = set(
        Page.objects.filter(
            path__startswith=parent_page.path,
            depth=parent_page.depth + 1,
        ).values_list("slug", flat=True)
    )
    if base not in existing_slugs:
        return base
    idx = 2
    while f"{base}-{idx}" in existing_slugs:
        idx += 1
    return f"{base}-{idx}"[:255]


def _resolve_job_media_objects(actor, job: ImportJob) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
    assets = list(job.assets.all().order_by("id"))
    missing = [a.asset_key for a in assets if not a.target_public_id]
    if missing:
        raise ImportServiceError(
            "assets_incomplete",
            f"尚有未上传完成的媒体资源：{', '.join(missing)}",
            status=422,
        )
    img_ids = [a.target_public_id for a in assets if a.media_type == "image"]
    doc_ids = [a.target_public_id for a in assets if a.media_type == "document"]
    img_map = {img.public_id: img for img in CustomImage.objects.filter(public_id__in=img_ids)}
    doc_map = {doc.public_id: doc for doc in CustomDocument.objects.filter(public_id__in=doc_ids)}

    img_policy = policy_registry.get_by_type(CustomImage)
    doc_policy = policy_registry.get_by_type(CustomDocument)

    resolved_objs: dict[str, Any] = {}
    link_replacements: dict[str, tuple[str, str]] = {}
    for a in assets:
        if a.media_type == "image":
            obj = img_map.get(a.target_public_id)
            if not obj or not img_policy.user_has_permission_for_instance(actor, "choose", obj):
                raise ImportServiceError("asset_permission_denied", f"无权选用图片资源: {a.asset_key}", status=403)
            resolved_objs[a.asset_key] = obj
            link_replacements[a.asset_key] = ("image", str(obj.public_id))
        else:
            obj = doc_map.get(a.target_public_id)
            if not obj or not doc_policy.user_has_permission_for_instance(actor, "choose", obj):
                raise ImportServiceError("asset_permission_denied", f"无权选用文档资源: {a.asset_key}", status=403)
            resolved_objs[a.asset_key] = obj
            link_replacements[a.asset_key] = ("document", str(obj.public_id))
    return resolved_objs, link_replacements


def commit_import_job(*, actor, site: Site, job_id: uuid.UUID) -> dict[str, Any]:
    """
    提交导入作业创建未发布草稿：
    1. 锁外准备内容并写入 MongoDB 不可变版本（使用稳定幂等 operation_id）；
    2. 在 ContentMutationGuard -> ImportJob 行锁 -> 页面树锁序内原子建草稿、登记引用并签发回执。
    """
    job = ImportJob.objects.filter(job_id=job_id).first()
    if not job or job.actor_id != actor.pk or job.site_id != site.pk:
        raise ImportServiceError("job_not_found", "导入作业不存在。", status=404)

    if job.status == ImportJobStatus.DRAFT_CREATED and job.result_page_id:
        page_obj = Page.objects.filter(id=job.result_page_id).first()
        if page_obj and page_obj.specific.permissions_for_user(actor).can_edit():
            return {
                "job_id": str(job.job_id),
                "page_id": job.result_page_id,
                "revision_id": job.result_revision_id,
                "edit_url": reverse("wagtailadmin_pages:edit", args=[job.result_page_id]),
                "status": "draft_created",
            }
        raise ImportServiceError("permission_denied", "当前账号已失去该草稿编辑权限。", status=403)

    if job.expires_at <= timezone.now():
        raise ImportServiceError("job_expired", "导入作业已过期，请重新预检创建。", status=409)

    adapter = get_import_adapter(job.type_key)
    if not adapter:
        raise ImportServiceError("unknown_type_key", f"页面类型不可用: {job.type_key}", status=422)

    # 锁外预备资产替换与 MongoDB 写入
    resolved_objs, link_replacements = _resolve_job_media_objects(actor, job)
    payload = job.payload_json or {}
    draft_spec, _ = _parse_and_normalize_spec(
        adapter=adapter,
        raw_markdown=payload.get("markdown", ""),
        source_name=job.source_name,
        edits=payload.get("edits") or {},
        frozen_today_iso=payload.get("frozen_today"),
        link_replacements=link_replacements,
    )

    temp_page = adapter.build(draft_spec, resolved_objs)
    _stabilize_stream_block_ids(temp_page, job.job_id)
    mongo_pointers = []
    store = None
    try:
        store = MongoContentStore()
        if hasattr(temp_page, "intro") and getattr(temp_page, "intro", None):
            intro_val = temp_page.intro.get_prep_value() if hasattr(temp_page.intro, "get_prep_value") else list(temp_page.intro)
            if intro_val:
                mongo_pointers.append(
                    store.save_field_version(
                        document_uuid=job.document_uuid,
                        field_name="intro",
                        payload=intro_val,
                        model_label=adapter.model_label,
                        operation_id=f"import:{job.job_id}:intro",
                    )
                )
        if hasattr(temp_page, "body") and getattr(temp_page, "body", None):
            body_val = temp_page.body.get_prep_value() if hasattr(temp_page.body, "get_prep_value") else list(temp_page.body)
            if body_val:
                mongo_pointers.append(
                    store.save_field_version(
                        document_uuid=job.document_uuid,
                        field_name="body",
                        payload=body_val,
                        model_label=adapter.model_label,
                        operation_id=f"import:{job.job_id}:body",
                    )
                )
    finally:
        if store:
            store.close()

    target_model = resolve_model_from_label(adapter.model_label)

    # 统一锁序：ContentMutationGuard -> ImportJob 行锁 -> 父页树与资产
    with content_mutation_guard(timeout_seconds=5):
        with transaction.atomic():
            locked_job = ImportJob.objects.select_for_update().get(pk=job.pk)
            if locked_job.status == ImportJobStatus.DRAFT_CREATED and locked_job.result_page_id:
                return {
                    "job_id": str(locked_job.job_id),
                    "page_id": locked_job.result_page_id,
                    "revision_id": locked_job.result_revision_id,
                    "edit_url": reverse("wagtailadmin_pages:edit", args=[locked_job.result_page_id]),
                    "status": "draft_created",
                }

            parent_page = validate_import_target_parent(actor, locked_job.parent_page_id, target_model, site)
            resolved_objs_in_lock, _ = _resolve_job_media_objects(actor, locked_job)
            page = adapter.build(draft_spec, resolved_objs_in_lock)
            _stabilize_stream_block_ids(page, locked_job.job_id)
            page.document_uuid = locked_job.document_uuid
            page.owner = actor
            page.live = False
            page.has_unpublished_changes = True
            page.slug = _generate_unique_child_slug(parent_page, page.title, draft_spec.slug_hint)

            parent_page.add_child(instance=page)
            bind_linked_person_for_page_owner(page)
            if draft_spec.tags and hasattr(page, "tags"):
                page.tags.set(list(draft_spec.tags))
                page.save(clean=False)

            page._prepared_external_pointers = mongo_pointers
            try:
                revision = page.save_revision(user=actor, log_action=True)
            finally:
                page._prepared_external_pointers = None

            ImportAsset.objects.filter(job=locked_job).update(is_staging=False)
            locked_job.status = ImportJobStatus.DRAFT_CREATED
            locked_job.result_page_id = page.id
            locked_job.result_revision_id = revision.id
            locked_job.save(update_fields=["status", "result_page_id", "result_revision_id", "updated_at"])

            result_payload = {
                "job_id": str(locked_job.job_id),
                "page_id": page.id,
                "revision_id": revision.id,
                "edit_url": reverse("wagtailadmin_pages:edit", args=[page.id]),
                "status": "draft_created",
            }
            OperationResult.objects.get_or_create(
                operation_id=f"markdown_import:{locked_job.job_id}",
                defaults={
                    "actor_id": actor.pk,
                    "action": "markdown_import.commit",
                    "page_id": page.id,
                    "input_hash": locked_job.input_digest,
                    "result_json": result_payload,
                },
            )
            return result_payload
