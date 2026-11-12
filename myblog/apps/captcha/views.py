"""
公共图片验证码 API 视图 (/api/v1/captcha/*)。
"""

import json
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from captcha.service import (
    apply_captcha_binding_cookie,
    get_challenge_image_bytes,
    issue_captcha_challenge,
)
from identity.challenges import ChallengeServiceUnavailable, ChallengeVerificationError
from identity.credentials import RateLimitExceeded


def _no_store_json(data: dict, status: int = 200, extra_headers: dict | None = None) -> JsonResponse:
    resp = JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
    resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp["Pragma"] = "no-cache"
    if extra_headers:
        for k, v in extra_headers.items():
            resp[k] = v
    return resp


@never_cache
@require_POST
def issue_challenge_api(request):
    """
    签发图片验证码挑战。
    支持 JSON 或表单提交：{scope, previous_captcha_id}；绝不返回明文答案。
    """
    scope = ""
    previous_id = ""
    if request.content_type and "application/json" in request.content_type:
        try:
            body = json.loads((request.body or b"{}").decode("utf-8"))
            if isinstance(body, dict):
                scope = body.get("scope", "")
                previous_id = body.get("previous_captcha_id", "")
        except Exception:
            return _no_store_json({"status": "error", "message": "请求体 JSON 格式无效"}, status=400)
    else:
        scope = request.POST.get("scope", "")
        previous_id = request.POST.get("previous_captcha_id", "")

    try:
        payload, signed_cookie = issue_captcha_challenge(
            request=request,
            scope=scope,
            previous_captcha_id=previous_id,
        )
    except ChallengeVerificationError as exc:
        return _no_store_json({"status": "error", "code": "invalid_scope", "message": str(exc)}, status=400)
    except RateLimitExceeded as exc:
        return _no_store_json(
            {"status": "error", "code": "rate_limited", "message": str(exc)},
            status=429,
            extra_headers={"Retry-After": str(max(1, exc.retry_after))},
        )
    except ChallengeServiceUnavailable as exc:
        return _no_store_json(
            {"status": "error", "code": "captcha_unavailable", "message": str(exc)},
            status=503,
        )

    resp = _no_store_json({"status": "success", **payload}, status=200)
    apply_captcha_binding_cookie(resp, request, signed_cookie)
    return resp


@never_cache
@require_GET
def challenge_image_api(request, captcha_id: str):
    """输出指定挑战 ID 的验证码 PNG 图像字节流（校验绑定 Cookie 与防缓存头）。"""
    try:
        png_bytes = get_challenge_image_bytes(request=request, captcha_id=captcha_id)
    except ChallengeServiceUnavailable as exc:
        return _no_store_json(
            {"status": "error", "code": "captcha_unavailable", "message": str(exc)},
            status=503,
        )

    if not png_bytes:
        resp = HttpResponseNotFound("404 Not Found: Captcha expired or unauthorized")
        resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    resp = HttpResponse(png_bytes, content_type="image/png", status=200)
    resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp["Pragma"] = "no-cache"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp
