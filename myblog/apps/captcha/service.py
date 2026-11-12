"""
公共图片验证码核心服务（Redis Lua 原子签发、刷新作废与单次核销）。

安全契约：
1. 答案绝不以明文存入 Redis 或返回给客户端，Redis 中仅保存 HMAC-SHA256 摘要；
2. 挑战通过 HttpOnly 签名 Cookie (myblog_captcha_bid) 绑定浏览器会话，不强绑原始客户端 IP，
   避免 4G/5G 或 IPv4/IPv6 双栈切换误杀正常用户；IP 仅用于限制挑战签发频率；
3. 核销采用单次 Redis Lua 脚本：无论输入对错、scope 是否一致，只要命中 captcha_id 立即 DEL，
   并发下至多 1 个请求可取出有效记录，彻底封堵重放与并发爆破；
4. Redis 不可用时统一 Fail-Closed 抛出 ChallengeServiceUnavailable (HTTP 503)。
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import time
import uuid

from django.conf import settings
from django.core.cache import cache
from django.core.signing import BadSignature, Signer

from captcha.renderer import generate_captcha_code, render_captcha_png
from identity.challenges import ChallengeServiceUnavailable, ChallengeVerificationError
from identity.credentials import RateLimitExceeded, get_client_ip

logger = logging.getLogger(__name__)

CAPTCHA_TTL_SECONDS = 180
CAPTCHA_BINDING_COOKIE_NAME = "myblog_captcha_bid"
ALLOWED_CAPTCHA_SCOPES = frozenset({"register", "login", "guest_comment", "member_comment"})
_VALID_CAPTCHA_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_BINDING_SIGNER = Signer(salt="myblog.captcha.binding")

# 第一步原子预留脚本：先在渲染 PNG 之前检查 IP 频次与活跃数上限并扣减配额，防止超限请求空耗 Pillow CPU
_RESERVE_CAPTCHA_LUA = """
local rl_key = KEYS[1]
local active_key = KEYS[2]

local ip_limit = tonumber(ARGV[1])
local ip_window = tonumber(ARGV[2])
local active_limit = tonumber(ARGV[3])
local now_ts = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local captcha_id = ARGV[6]
local prev_id = ARGV[7]
local prev_ch_key = ARGV[8]
local prev_img_key = ARGV[9]
local expected_binding = ARGV[10]

local cur_ip = tonumber(redis.call('GET', rl_key) or '0')
if cur_ip >= ip_limit then
    local retry = redis.call('TTL', rl_key)
    return {0, 'rate_limited', retry > 0 and retry or ip_window}
end

-- 若刷新携带旧 captcha_id，仅当确实属于同一绑定指纹时才作废旧挑战
if prev_id ~= '' and prev_ch_key ~= '' then
    local prev_raw = redis.call('GET', prev_ch_key)
    if prev_raw then
        local ok, prev_obj = pcall(cjson.decode, prev_raw)
        if ok and prev_obj and prev_obj['binding_hash'] == expected_binding then
            redis.call('DEL', prev_ch_key, prev_img_key)
            redis.call('ZREM', active_key, prev_id)
        end
    end
end

redis.call('ZREMRANGEBYSCORE', active_key, '-inf', now_ts)
local active_cnt = tonumber(redis.call('ZCARD', active_key) or '0')
if active_cnt >= active_limit then
    return {0, 'too_many_active', 30}
end

local new_ip = redis.call('INCR', rl_key)
if new_ip == 1 then
    redis.call('EXPIRE', rl_key, ip_window)
end

redis.call('ZADD', active_key, now_ts + ttl, captcha_id)
redis.call('EXPIRE', active_key, ttl + 5)
return {1, 'ok', 0}
"""

# 原子单次核销脚本：先比对归属绑定指纹，一致时无论输入对错立即删除挑战与图像并清理所属活跃集合；
# 若绑定指纹不匹配则绝不销毁他人挑战或污染活跃计数。
_CONSUME_CAPTCHA_LUA = """
local ch_key = KEYS[1]
local img_key = KEYS[2]
local active_key = KEYS[3]
local captcha_id = ARGV[1]
local caller_binding = ARGV[2]

local payload = redis.call('GET', ch_key)
if not payload then
    return {0, 'missing'}
end

local ok, obj = pcall(cjson.decode, payload)
if not ok or not obj or obj['binding_hash'] ~= caller_binding then
    return {0, 'binding_mismatch'}
end

redis.call('DEL', ch_key, img_key)
if active_key ~= '' and captcha_id ~= '' then
    redis.call('ZREM', active_key, captcha_id)
end
return {1, payload}
"""


def _get_redis_client(*, write: bool = True):
    backend = getattr(cache, "_cache", None)
    if backend is not None and hasattr(backend, "get_client"):
        return backend.get_client(write=write)
    return None


def _ch_key(captcha_id: str) -> str:
    return f"myblog:captcha:ch:{captcha_id}"


def _img_key(captcha_id: str) -> str:
    return f"myblog:captcha:img:{captcha_id}"


def _active_key(binding_hash: str) -> str:
    return f"myblog:captcha:active:{binding_hash}"


def _compute_binding_hash(binding_token: str) -> str:
    return hashlib.sha256(binding_token.encode("utf-8")).hexdigest()[:32]


def _compute_answer_hmac(captcha_id: str, code: str) -> str:
    norm = (code or "").strip().upper()
    secret = str(settings.SECRET_KEY).encode("utf-8")
    msg = f"{captcha_id}:{norm}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def resolve_or_create_binding(request) -> tuple[str, str | None]:
    """
    读取并验签浏览器中的 HttpOnly 绑定 Cookie；
    若缺失或被篡改则签发全新高熵标识，并返回待写入响应的签名字符串。
    """
    raw_cookie = request.COOKIES.get(CAPTCHA_BINDING_COOKIE_NAME) if hasattr(request, "COOKIES") else None
    if raw_cookie:
        try:
            unsigned = _BINDING_SIGNER.unsign(raw_cookie)
            if unsigned:
                return unsigned, None
        except BadSignature:
            pass
    fresh = uuid.uuid4().hex
    signed = _BINDING_SIGNER.sign(fresh)
    return fresh, signed


def read_existing_binding(request) -> str | None:
    """仅读取已有并验签通过的绑定标识（用于消费校验与图片读取，不重新签发）。"""
    raw_cookie = request.COOKIES.get(CAPTCHA_BINDING_COOKIE_NAME) if hasattr(request, "COOKIES") else None
    if not raw_cookie:
        return None
    try:
        unsigned = _BINDING_SIGNER.unsign(raw_cookie)
        return unsigned or None
    except BadSignature:
        return None


def apply_captcha_binding_cookie(response, request, signed_binding: str | None) -> None:
    """将新签发的 HttpOnly 绑定 Cookie 写入 HTTP 响应，并持久化回 request.COOKIES 供同请求复用。"""
    if not signed_binding:
        return
    response.set_cookie(
        CAPTCHA_BINDING_COOKIE_NAME,
        signed_binding,
        max_age=7 * 86400,
        httponly=True,
        samesite="Lax",
        secure=bool(request.is_secure()),
    )


def issue_captcha_challenge(
    *,
    request,
    scope: str,
    previous_captcha_id: str = "",
) -> tuple[dict, str | None]:
    """
    签发新的图片验证码挑战：
    返回 (payload_dict, signed_cookie_to_set)。
    """
    clean_scope = (scope or "").strip()
    if clean_scope not in ALLOWED_CAPTCHA_SCOPES:
        raise ChallengeVerificationError("不支持的验证码业务场景。")

    binding_token, signed_cookie = resolve_or_create_binding(request)
    binding_hash = _compute_binding_hash(binding_token)
    ip_address = get_client_ip(request)

    captcha_id = uuid.uuid4().hex
    ttl = int(getattr(settings, "CAPTCHA_TTL_SECONDS", CAPTCHA_TTL_SECONDS))
    ip_limit = int(getattr(settings, "CAPTCHA_ISSUE_IP_MINUTE_LIMIT", 30))
    active_limit = int(getattr(settings, "CAPTCHA_ACTIVE_BINDING_LIMIT", 10))

    prev_clean = (previous_captcha_id or "").strip().lower()
    if not _VALID_CAPTCHA_ID_RE.match(prev_clean):
        prev_clean = ""

    # 1. 先执行 Redis 原子配额与活跃数预留，超限时绝不触发 Pillow 图像渲染
    try:
        r = _get_redis_client(write=True)
        if r is None:
            raise ChallengeServiceUnavailable("安全校验存储未就绪，请稍后再试。")
        res = r.eval(
            _RESERVE_CAPTCHA_LUA,
            2,
            f"myblog:captcha:rl:ip:{ip_address}",
            _active_key(binding_hash),
            ip_limit,
            60,
            active_limit,
            int(time.time()),
            ttl,
            captcha_id,
            prev_clean,
            _ch_key(prev_clean) if prev_clean else "",
            _img_key(prev_clean) if prev_clean else "",
            binding_hash,
        )
    except ChallengeServiceUnavailable:
        raise
    except Exception as exc:
        logger.error("Issue captcha Redis error: %s", exc)
        raise ChallengeServiceUnavailable("安全校验服务暂时不可用，请稍后再试。") from exc

    ok = int(res[0])
    reason = res[1].decode("utf-8") if isinstance(res[1], bytes) else str(res[1])
    retry_after = int(res[2])
    if ok != 1:
        if reason == "too_many_active":
            raise RateLimitExceeded("安全验证码生成过于频繁，请先使用或稍后再试。", retry_after=retry_after)
        raise RateLimitExceeded(f"验证码请求过于频繁，请在 {retry_after} 秒后再试。", retry_after=retry_after)

    # 2. 预留成功后才执行 Pillow 绘图与写入挑战载荷
    try:
        code = generate_captcha_code(4)
        png_bytes = render_captcha_png(code)
        png_b64 = base64.b64encode(png_bytes).decode("ascii")
        payload_json = json.dumps(
            {
                "scope": clean_scope,
                "binding_hash": binding_hash,
                "answer_hmac": _compute_answer_hmac(captcha_id, code),
            },
            separators=(",", ":"),
        )
        pipe = r.pipeline()
        pipe.set(_ch_key(captcha_id), payload_json, ex=ttl)
        pipe.set(_img_key(captcha_id), png_b64, ex=ttl)
        pipe.execute()
    except Exception as exc:
        try:
            r.zrem(_active_key(binding_hash), captcha_id)
        except Exception:
            pass
        logger.error("Render or store captcha failed: %s", exc)
        raise ChallengeServiceUnavailable("安全校验服务暂时不可用，请稍后再试。") from exc

    # 让同一请求周期后续逻辑可立即识别该绑定 Cookie
    if signed_cookie and hasattr(request, "COOKIES"):
        try:
            request.COOKIES[CAPTCHA_BINDING_COOKIE_NAME] = signed_cookie
        except Exception:
            pass

    return (
        {
            "captcha_id": captcha_id,
            "image_data_url": f"data:image/png;base64,{png_b64}",
            "image_url": f"/api/v1/captcha/image/{captcha_id}/",
            "expires_in": ttl,
        },
        signed_cookie,
    )


def get_challenge_image_bytes(*, request, captcha_id: str) -> bytes | None:
    """按 captcha_id 与浏览器绑定指纹校验后返回验证码 PNG 字节流。"""
    clean_id = (captcha_id or "").strip().lower()
    if not _VALID_CAPTCHA_ID_RE.match(clean_id):
        return None
    binding_token = read_existing_binding(request)
    if not binding_token:
        return None
    binding_hash = _compute_binding_hash(binding_token)

    try:
        r = _get_redis_client(write=False)
        if r is None:
            raise ChallengeServiceUnavailable("安全校验存储未就绪，请稍后再试。")
        pipe = r.pipeline()
        pipe.get(_ch_key(clean_id))
        pipe.get(_img_key(clean_id))
        ch_raw, img_raw = pipe.execute()
    except ChallengeServiceUnavailable:
        raise
    except Exception as exc:
        logger.error("Read captcha image Redis error: %s", exc)
        raise ChallengeServiceUnavailable("安全校验服务暂时不可用，请稍后再试。") from exc

    if not ch_raw or not img_raw:
        return None
    try:
        ch_obj = json.loads(ch_raw.decode("utf-8") if isinstance(ch_raw, bytes) else ch_raw)
    except Exception:
        return None
    if ch_obj.get("binding_hash") != binding_hash:
        return None
    try:
        b64_str = img_raw.decode("ascii") if isinstance(img_raw, bytes) else img_raw
        return base64.b64decode(b64_str)
    except Exception:
        return None


def verify_and_consume_captcha(
    *,
    request,
    expected_scope: str,
    captcha_id: str,
    code: str,
) -> None:
    """
    原子核销验证码挑战：
    在持有匹配浏览器绑定 Cookie 时，Lua 脚本无论答案是否为空或正确均原子销毁该次挑战，
    彻底杜绝暴力试错与并发重放；若绑定不匹配则拒绝且不销毁他人挑战。
    """
    clean_id = (captcha_id or "").strip().lower()
    clean_code = (code or "").strip()
    if not _VALID_CAPTCHA_ID_RE.match(clean_id):
        raise ChallengeVerificationError("请输入有效的人机安全验证码。")

    binding_token = read_existing_binding(request)
    if not binding_token:
        raise ChallengeVerificationError("安全验证会话缺失或已过期，请刷新验证码后重试。")
    caller_binding_hash = _compute_binding_hash(binding_token)

    try:
        r = _get_redis_client(write=True)
        if r is None:
            raise ChallengeServiceUnavailable("安全校验存储未就绪，请稍后再试。")
        lua_res = r.eval(
            _CONSUME_CAPTCHA_LUA,
            3,
            _ch_key(clean_id),
            _img_key(clean_id),
            _active_key(caller_binding_hash),
            clean_id,
            caller_binding_hash,
        )
    except ChallengeServiceUnavailable:
        raise
    except Exception as exc:
        logger.error("Consume captcha Redis error: %s", exc)
        raise ChallengeServiceUnavailable("安全校验服务暂时不可用，请稍后再试。") from exc

    status_flag = int(lua_res[0])
    raw_or_reason = lua_res[1]
    if status_flag != 1:
        reason_str = raw_or_reason.decode("utf-8") if isinstance(raw_or_reason, bytes) else str(raw_or_reason)
        if reason_str == "binding_mismatch":
            raise ChallengeVerificationError("安全验证码会话归属校验失败，请刷新后重试。")
        raise ChallengeVerificationError("安全验证码已过期或已被使用，请刷新后重试。")

    if not clean_code:
        raise ChallengeVerificationError("请输入有效的人机安全验证码。")

    try:
        obj = json.loads(raw_or_reason.decode("utf-8") if isinstance(raw_or_reason, bytes) else raw_or_reason)
    except Exception as exc:
        raise ChallengeVerificationError("安全验证码状态异常，请刷新后重试。") from exc

    if obj.get("scope") != expected_scope:
        raise ChallengeVerificationError("安全验证码业务场景不匹配，请刷新后重试。")

    expected_hmac = str(obj.get("answer_hmac", ""))
    candidate_hmac = _compute_answer_hmac(clean_id, clean_code)
    if not expected_hmac or not hmac.compare_digest(expected_hmac, candidate_hmac):
        raise ChallengeVerificationError("安全验证码输入错误，请重新获取并输入。")
