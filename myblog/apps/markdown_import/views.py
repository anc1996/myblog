"""
通用多页面 Markdown 导入 REST API 视图层。

安全规范：
- 携带 Authorization: Bearer <mdimp_...> 时走专用短期令牌鉴权（仅认令牌，不回退 Session）；
- 未携带 Authorization 头时走 Wagtail Session 鉴权并强制执行 Django CSRF 校验；
- 所有错误返回统一结构 {code, message, fields, retryable}，不泄漏内部堆栈或凭据。
"""

from dataclasses import asdict
import json
import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from taggit.models import Tag
from wagtail.models import Site

from .ai.httpx_provider import HttpxProvider
from .ai.openai_provider import OpenAISDKProvider
from .ai.service import generate_metadata_with_fallback
from .auth import (
    authenticate_import_request,
    exchange_pair_code,
    resolve_request_site,
)
from .models import ImportAsset, ImportJob
from .permissions import list_allowed_parents, resolve_model_from_label, validate_import_target_parent
from .registry import get_import_adapter, list_import_adapters
from .services import (
    MAX_ASSETS_PER_JOB,
    MAX_MARKDOWN_BYTES,
    MAX_TOTAL_ASSETS_BYTES,
    ImportServiceError,
    build_preflight_payload,
    commit_import_job,
    create_or_get_import_job,
    upload_import_asset,
)


def _error_response(code: str, message: str, *, status: int = 400, fields: dict[str, Any] | None = None, retryable: bool = False) -> JsonResponse:
    return JsonResponse(
        {
            "code": code,
            "message": message,
            "fields": fields or {},
            "retryable": retryable,
        },
        status=status,
    )


def _enforce_auth_and_csrf(request, *, require_csrf_for_session: bool = True):
    has_auth_header = bool((request.META.get("HTTP_AUTHORIZATION") or "").strip())
    if not has_auth_header and require_csrf_for_session and request.method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
        csrf_check = CsrfViewMiddleware(lambda r: None).process_view(request, None, (), {})
        if csrf_check is not None:
            raise PermissionDenied("CSRF 校验未通过。")
    return authenticate_import_request(request)


def _parse_json_body(request) -> dict[str, Any]:
    try:
        raw = (request.body or b"{}").decode("utf-8")
        data = json.loads(raw)
    except Exception:
        raise ImportServiceError("invalid_json", "请求体必须是合法 UTF-8 JSON。", status=422)
    if not isinstance(data, dict):
        raise ImportServiceError("invalid_json", "请求 JSON 根节点必须是对象。", status=422)
    return data


@never_cache
@require_GET
def catalog_view(request):
    """返回已注册页面类型、字段 Schema、可选目标父栏目与上传限制。"""
    try:
        actor, site, _ = _enforce_auth_and_csrf(request, require_csrf_for_session=False)
    except PermissionDenied as exc:
        return _error_response("permission_denied", str(exc), status=403)

    requested_type = (request.GET.get("type_key") or "").strip()
    search_q = (request.GET.get("q") or "").strip()
    raw_containers_only = (request.GET.get("containers_only") or "1").strip().lower()
    containers_only = raw_containers_only not in {"0", "false", "no"}
    try:
        limit = max(1, min(200, int(request.GET.get("limit", "100"))))
        offset = max(0, int(request.GET.get("offset", "0")))
    except ValueError:
        return _error_response("invalid_pagination", "分页参数必须为整数。", status=422)

    types_out = []
    selected_parents = []
    has_more_parents = False
    total_parents = 0
    all_adapters = list_import_adapters()

    for adapter in all_adapters:
        schema = adapter.describe(actor=actor)
        types_out.append(
            {
                "type_key": schema.type_key,
                "label": schema.label,
                "model_label": schema.model_label,
                "schema_version": schema.schema_version,
                "allowed_parent_models": list(schema.allowed_parent_models),
                "fields": [asdict(f) for f in schema.fields],
                "template_markdown": schema.template_markdown,
            }
        )
        if requested_type == adapter.type_key:
            target_model = resolve_model_from_label(adapter.model_label)
            selected_parents, has_more_parents, total_parents = list_allowed_parents(
                actor,
                target_model,
                site,
                limit=limit,
                offset=offset,
                query=search_q,
                containers_only=containers_only,
                all_adapters=all_adapters,
            )

    ai_enabled = bool(
        getattr(settings, "AI_METADATA_API_KEY", "")
        and getattr(settings, "AI_METADATA_BASE_URL", "")
        and getattr(settings, "AI_METADATA_MODEL", "")
    )

    return JsonResponse(
        {
            "api_version": 1,
            "types": types_out,
            "parents": selected_parents,
            "parents_has_more": has_more_parents,
            "parents_total": total_parents,
            "ai_enabled": ai_enabled,
            "limits": {
                "max_markdown_bytes": MAX_MARKDOWN_BYTES,
                "max_assets_per_job": MAX_ASSETS_PER_JOB,
                "max_total_assets_bytes": MAX_TOTAL_ASSETS_BYTES,
                "max_document_bytes": int(getattr(settings, "WAGTAILDOCS_MAX_UPLOAD_SIZE", 50 * 1024 * 1024)),
                "max_image_bytes": int(getattr(settings, "WAGTAILIMAGES_MAX_UPLOAD_SIZE", 10 * 1024 * 1024)),
            },
        }
    )


@csrf_exempt
@never_cache
@require_POST
def pair_exchange_view(request):
    """一次性短期配对码兑换专用导入 Bearer Token（仅此一次返回明文）。"""
    try:
        site = resolve_request_site(request)
        data = _parse_json_body(request)
        raw_code = str(data.get("pair_code") or "")
        client_name = str(data.get("client_name") or "桌面导入向导")
        token_obj, plaintext = exchange_pair_code(raw_code=raw_code, site=site, client_name=client_name)
        return JsonResponse(
            {
                "token": plaintext,
                "prefix": token_obj.prefix,
                "expires_at": token_obj.expires_at.isoformat(),
                "site_id": site.pk,
            }
        )
    except PermissionDenied as exc:
        return _error_response("pair_exchange_denied", str(exc), status=403)
    except ImportServiceError as exc:
        return _error_response(exc.code, exc.message, status=exc.status, fields=exc.fields)


@csrf_exempt
@never_cache
@require_POST
def preflight_view(request):
    try:
        actor, site, _ = _enforce_auth_and_csrf(request)
        data = _parse_json_body(request)
        result = build_preflight_payload(actor=actor, site=site, data=data)
        return JsonResponse(result)
    except PermissionDenied as exc:
        return _error_response("permission_denied", str(exc), status=403)
    except ImportServiceError as exc:
        return _error_response(exc.code, exc.message, status=exc.status, fields=exc.fields)


@csrf_exempt
@never_cache
@require_POST
def jobs_create_view(request):
    try:
        actor, site, _ = _enforce_auth_and_csrf(request)
        data = _parse_json_body(request)
        idem_key = request.META.get("HTTP_IDEMPOTENCY_KEY") or str(data.get("idempotency_key") or "")
        job, created = create_or_get_import_job(actor=actor, site=site, data=data, idempotency_key=idem_key)
        assets = [
            {
                "asset_key": a.asset_key,
                "media_type": a.media_type,
                "uploaded": bool(a.target_public_id),
                "public_id": str(a.target_public_id) if a.target_public_id else None,
            }
            for a in job.assets.all().order_by("id")
        ]
        return JsonResponse(
            {
                "job_id": str(job.job_id),
                "status": job.status,
                "created": created,
                "assets": assets,
                "page_id": job.result_page_id,
            },
            status=201 if created else 200,
        )
    except PermissionDenied as exc:
        return _error_response("permission_denied", str(exc), status=403)
    except ImportServiceError as exc:
        return _error_response(exc.code, exc.message, status=exc.status, fields=exc.fields)


@csrf_exempt
@never_cache
@require_GET
def job_detail_view(request, job_id: uuid.UUID):
    try:
        actor, site, _ = _enforce_auth_and_csrf(request, require_csrf_for_session=False)
        job = ImportJob.objects.filter(job_id=job_id, actor=actor, site=site).first()
        if not job:
            return _error_response("job_not_found", "导入作业不存在。", status=404)
        assets = [
            {
                "asset_key": a.asset_key,
                "media_type": a.media_type,
                "uploaded": bool(a.target_public_id),
                "public_id": str(a.target_public_id) if a.target_public_id else None,
            }
            for a in job.assets.all().order_by("id")
        ]
        return JsonResponse(
            {
                "job_id": str(job.job_id),
                "status": job.status,
                "page_id": job.result_page_id,
                "revision_id": job.result_revision_id,
                "assets": assets,
            }
        )
    except PermissionDenied as exc:
        return _error_response("permission_denied", str(exc), status=403)


@csrf_exempt
@never_cache
@require_http_methods(["PUT", "POST"])
def job_asset_upload_view(request, job_id: uuid.UUID):
    try:
        actor, _, _ = _enforce_auth_and_csrf(request)
        asset_key = (request.POST.get("asset_key") or request.GET.get("asset_key") or "").strip()
        if not asset_key:
            return _error_response("missing_asset_key", "必须指定 asset_key。", status=422)
        uploaded_file = request.FILES.get("file")
        asset_row = upload_import_asset(
            actor=actor,
            job_id=job_id,
            asset_key=asset_key,
            uploaded_file=uploaded_file,
        )
        return JsonResponse(
            {
                "asset_key": asset_row.asset_key,
                "media_type": asset_row.media_type,
                "public_id": str(asset_row.target_public_id),
                "actual_size": asset_row.actual_size,
                "actual_sha256": asset_row.actual_sha256,
            }
        )
    except PermissionDenied as exc:
        return _error_response("permission_denied", str(exc), status=403)
    except ImportServiceError as exc:
        return _error_response(exc.code, exc.message, status=exc.status, fields=exc.fields)


@csrf_exempt
@never_cache
@require_POST
def job_commit_view(request, job_id: uuid.UUID):
    try:
        actor, site, _ = _enforce_auth_and_csrf(request)
        res = commit_import_job(actor=actor, site=site, job_id=job_id)
        return JsonResponse(res)
    except PermissionDenied as exc:
        return _error_response("permission_denied", str(exc), status=403)
    except ImportServiceError as exc:
        return _error_response(exc.code, exc.message, status=exc.status, fields=exc.fields)


@never_cache
@require_GET
def ai_templates_view(request):
    try:
        _enforce_auth_and_csrf(request, require_csrf_for_session=False)
    except PermissionDenied as exc:
        return _error_response("permission_denied", str(exc), status=403)

    ai_enabled = bool(
        getattr(settings, "AI_METADATA_API_KEY", "")
        and getattr(settings, "AI_METADATA_BASE_URL", "")
        and getattr(settings, "AI_METADATA_MODEL", "")
    )
    return JsonResponse(
        {
            "provider_enabled": ai_enabled,
            "templates": [
                {
                    "id": "tech_guide",
                    "version": 1,
                    "label": "技术文章核心讲解与导读（推荐）",
                    "supported_fields": ["title", "summary", "tags"],
                },
                {
                    "id": "beginner_friendly",
                    "version": 1,
                    "label": "通俗易懂新手导读风",
                    "supported_fields": ["title", "summary", "tags"],
                },
            ],
            "limits": {
                "max_digest_chars": 16_000,
                "max_markdown_bytes": MAX_MARKDOWN_BYTES,
            },
        }
    )


def _build_configured_llm_client():
    api_key = getattr(settings, "AI_METADATA_API_KEY", "")
    base_url = getattr(settings, "AI_METADATA_BASE_URL", "")
    model = getattr(settings, "AI_METADATA_MODEL", "")
    if not (api_key and base_url and model):
        return None
    driver = str(getattr(settings, "AI_METADATA_DRIVER", "openai")).lower()
    mode = str(getattr(settings, "AI_METADATA_MODE", "responses")).lower()
    allow_store_false = bool(getattr(settings, "AI_METADATA_ALLOW_STORE_FALSE", True))
    if driver == "httpx":
        return HttpxProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
            mode=mode,
            allow_store_false=allow_store_false,
        )
    return OpenAISDKProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
        mode=mode,
        allow_store_false=allow_store_false,
    )


@csrf_exempt
@never_cache
@require_POST
def ai_suggest_view(request):
    """
    为无头/无简介/缺标签的超长文提供 AI（或自动降级本地）元数据建议。
    模型 API Key 与 Base URL 仅从服务端配置读取，绝不接收客户端传入的 base_url。
    """
    try:
        actor, site, _ = _enforce_auth_and_csrf(request)
        data = _parse_json_body(request)
        type_key = str(data.get("type_key") or "blog_page").strip()
        adapter = get_import_adapter(type_key)
        if not adapter:
            return _error_response("unknown_type_key", f"未知页面类型: {type_key}", status=422)
        parent_id = int(data.get("parent_id") or 0)
        if parent_id > 0:
            target_model = resolve_model_from_label(adapter.model_label)
            validate_import_target_parent(actor, parent_id, target_model, site)

        raw_markdown = str(data.get("markdown") or "")
        if len(raw_markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
            return _error_response("markdown_too_large", "待分析正文超过 4 MiB 上限。", status=413)

        source_name = str(data.get("source_name") or "untitled.md")
        existing_title = str(data.get("existing_title") or "")
        existing_tags = data.get("existing_tags") if isinstance(data.get("existing_tags"), list) else []
        allow_external = bool(data.get("allow_external_processing", False))
        allow_new_tags = bool(data.get("allow_new_tags", False))
        vocab_names = list(Tag.objects.order_by("name").values_list("name", flat=True)[:2000])

        client = _build_configured_llm_client() if allow_external else None
        sug = generate_metadata_with_fallback(
            raw_markdown,
            source_name=source_name,
            existing_title=existing_title,
            existing_tags=existing_tags,
            allow_external_ai=allow_external,
            llm_client=client,
            vocabulary_tags=vocab_names,
            allow_new_tags=allow_new_tags,
        )
        return JsonResponse(
            {
                "suggestions": {
                    "title": sug.title,
                    "summary": sug.summary,
                    "tags": sug.tags,
                },
                "source": sug.source,
                "warnings": sug.warnings,
                "digest_stats": sug.digest_stats,
            }
        )
    except PermissionDenied as exc:
        return _error_response("permission_denied", str(exc), status=403)
    except ImportServiceError as exc:
        return _error_response(exc.code, exc.message, status=exc.status, fields=exc.fields)
