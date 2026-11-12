"""
前台账号凭证校验、安全重定向与 Redis Lua 原子防刷限流。
"""

import logging
import re
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils.http import url_has_allowed_host_and_scheme

from identity.challenges import ChallengeServiceUnavailable

logger = logging.getLogger(__name__)

SAFE_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,32}$")

# 单次原子检查并扣减双重计数（小时/日），避免并发穿透或误扣
_REGISTER_QUOTA_LUA = """
local hr_key = KEYS[1]
local day_key = KEYS[2]
local hr_limit = tonumber(ARGV[1])
local day_limit = tonumber(ARGV[2])
local hr_ttl = tonumber(ARGV[3])
local day_ttl = tonumber(ARGV[4])

local hr_cnt = tonumber(redis.call('GET', hr_key) or '0')
local day_cnt = tonumber(redis.call('GET', day_key) or '0')
if hr_cnt >= hr_limit then
    local ttl = redis.call('TTL', hr_key)
    return {0, ttl > 0 and ttl or hr_ttl}
end
if day_cnt >= day_limit then
    local ttl = redis.call('TTL', day_key)
    return {0, ttl > 0 and ttl or day_ttl}
end

local new_hr = redis.call('INCR', hr_key)
if new_hr == 1 then redis.call('EXPIRE', hr_key, hr_ttl) end
local new_day = redis.call('INCR', day_key)
if new_day == 1 then redis.call('EXPIRE', day_key, day_ttl) end
return {1, 0}
"""

# 原子递增登录失败次数，到达阈值设置冷却期
_LOGIN_FAIL_LUA = """
local ip_key = KEYS[1]
local user_key = KEYS[2]
local ttl = tonumber(ARGV[1])
local ip_cnt = redis.call('INCR', ip_key)
if ip_cnt == 1 then redis.call('EXPIRE', ip_key, ttl) end
local user_cnt = 0
if user_key ~= '' then
    user_cnt = redis.call('INCR', user_key)
    if user_cnt == 1 then redis.call('EXPIRE', user_key, ttl) end
end
return {ip_cnt, user_cnt}
"""


class RateLimitExceeded(Exception):
    def __init__(self, message: str, retry_after: int = 60):
        super().__init__(message)
        self.message = message
        self.retry_after = int(retry_after)


def validate_safe_username(username: str) -> str:
    clean = (username or "").strip()
    if not SAFE_USERNAME_RE.match(clean):
        raise ValidationError("用户名需为 3~32 位字母、数字或下划线组合。")
    return clean


def get_client_ip(request) -> str:
    trusted_proxies = getattr(settings, "TRUSTED_PROXIES", ["127.0.0.1", "192.168.20.5"])
    remote_addr = request.META.get("REMOTE_ADDR", "127.0.0.1")
    if remote_addr in trusted_proxies:
        x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded:
            return x_forwarded.split(",")[0].strip()
    return remote_addr


def get_safe_next_url(request, default: str = "/") -> str:
    candidate = (request.POST.get("next") or request.GET.get("next") or "").strip()
    if not candidate or "\\" in candidate:
        return default
    if url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return default


def _get_redis_client(*, write: bool = True):
    backend = getattr(cache, "_cache", None)
    if backend is not None and hasattr(backend, "get_client"):
        return backend.get_client(write=write)
    return None


def check_and_consume_register_quota(ip_address: str) -> None:
    """同 IP 注册防刷限制：每小时最多 5 个、每日最多 10 个。Redis 不可用时 Fail-Closed。"""
    hr_limit = int(getattr(settings, "IDENTITY_REGISTER_HOURLY_IP_LIMIT", 5))
    day_limit = int(getattr(settings, "IDENTITY_REGISTER_DAILY_IP_LIMIT", 10))
    try:
        r = _get_redis_client(write=True)
        if r is None:
            raise ChallengeServiceUnavailable("限流存储未就绪，请稍后再试。")
        res = r.eval(
            _REGISTER_QUOTA_LUA,
            2,
            f"myblog:rl:reg:hr:{ip_address}",
            f"myblog:rl:reg:day:{ip_address}",
            hr_limit,
            day_limit,
            3600,
            86400,
        )
        allowed, retry_after = int(res[0]), int(res[1])
        if not allowed:
            raise RateLimitExceeded(
                f"当前网络注册过于频繁，请在 {retry_after} 秒后再试。",
                retry_after=retry_after,
            )
    except (RateLimitExceeded, ChallengeServiceUnavailable):
        raise
    except Exception as exc:
        logger.error("Register quota Redis error: %s", exc)
        raise ChallengeServiceUnavailable("安全校验服务暂时不可用，请稍后再试。") from exc


# 原子检查并预占登录尝试槽位：消除并发下绕过失败阈值尝试密码的窗口
_RESERVE_LOGIN_ATTEMPT_LUA = """
local ip_fail_k = KEYS[1]
local u_fail_k = KEYS[2]
local ip_try_k = KEYS[3]
local u_try_k = KEYS[4]

local captcha_th = tonumber(ARGV[1])
local lock_th = tonumber(ARGV[2])
local window_ttl = tonumber(ARGV[3])
local has_captcha = tonumber(ARGV[4])

local ip_fail = tonumber(redis.call('GET', ip_fail_k) or '0')
local u_fail = u_fail_k ~= '' and tonumber(redis.call('GET', u_fail_k) or '0') or 0
local ip_try = tonumber(redis.call('GET', ip_try_k) or '0')
local u_try = u_try_k ~= '' and tonumber(redis.call('GET', u_try_k) or '0') or 0

local cur = math.max(ip_fail, u_fail, ip_try, u_try)
if cur >= lock_th then
    local t1 = redis.call('TTL', ip_fail_k)
    local t2 = u_fail_k ~= '' and redis.call('TTL', u_fail_k) or 0
    local t3 = redis.call('TTL', ip_try_k)
    local t4 = u_try_k ~= '' and redis.call('TTL', u_try_k) or 0
    local m = math.max(t1, t2, t3, t4)
    return {'locked', m > 0 and m or window_ttl}
end
if has_captcha == 0 and cur >= captcha_th then
    return {'captcha_required', 0}
end

local n_ip = redis.call('INCR', ip_try_k)
if n_ip == 1 then redis.call('EXPIRE', ip_try_k, window_ttl) end
if u_try_k ~= '' then
    local n_u = redis.call('INCR', u_try_k)
    if n_u == 1 then redis.call('EXPIRE', u_try_k, window_ttl) end
end
return {'ok', 0}
"""


def _login_keys(username: str, ip_address: str) -> tuple[str, str]:
    norm_user = (username or "").strip().lower()
    return (
        f"myblog:loginfail:ip:{ip_address}",
        f"myblog:loginfail:user:{norm_user}" if norm_user else "",
    )


def _login_try_keys(username: str, ip_address: str) -> tuple[str, str]:
    norm_user = (username or "").strip().lower()
    return (
        f"myblog:logintry:ip:{ip_address}",
        f"myblog:logintry:user:{norm_user}" if norm_user else "",
    )


def get_login_failure_state(username: str, ip_address: str) -> tuple[int, int]:
    """返回 (max_fail_count, max_ttl_seconds)。Redis 不可用时 Fail-Closed 抛出 ChallengeServiceUnavailable。"""
    ip_key, user_key = _login_keys(username, ip_address)
    try:
        r = _get_redis_client(write=False)
        if r is None:
            raise ChallengeServiceUnavailable("安全校验存储未就绪，请稍后再试。")
        pipe = r.pipeline()
        pipe.get(ip_key)
        pipe.ttl(ip_key)
        if user_key:
            pipe.get(user_key)
            pipe.ttl(user_key)
        vals = pipe.execute()
        ip_cnt = int(vals[0] or 0)
        ip_ttl = max(0, int(vals[1] or 0))
        u_cnt = int(vals[2] or 0) if user_key else 0
        u_ttl = max(0, int(vals[3] or 0)) if user_key else 0
        return max(ip_cnt, u_cnt), max(ip_ttl, u_ttl)
    except ChallengeServiceUnavailable:
        raise
    except Exception as exc:
        logger.error("Read login failure state Redis error: %s", exc)
        raise ChallengeServiceUnavailable("安全校验服务暂时不可用，请稍后再试。") from exc


def is_login_locked(username: str, ip_address: str) -> tuple[bool, int]:
    """连续失败达 10 次触发 15 分钟冷却锁定。"""
    lock_threshold = int(getattr(settings, "IDENTITY_LOGIN_LOCK_THRESHOLD", 10))
    cnt, ttl = get_login_failure_state(username, ip_address)
    if cnt >= lock_threshold:
        return True, max(ttl, 60)
    return False, 0


def is_login_captcha_required(username: str, ip_address: str) -> bool:
    """连续失败达 3 次要求图片验证码。"""
    captcha_threshold = int(getattr(settings, "IDENTITY_LOGIN_CAPTCHA_THRESHOLD", 3))
    cnt, _ = get_login_failure_state(username, ip_address)
    return cnt >= captcha_threshold


def record_login_failure(username: str, ip_address: str) -> tuple[int, int]:
    ip_key, user_key = _login_keys(username, ip_address)
    window_ttl = int(getattr(settings, "IDENTITY_LOGIN_LOCK_WINDOW_SECONDS", 900))
    try:
        r = _get_redis_client(write=True)
        if r is None:
            raise ChallengeServiceUnavailable("安全校验存储未就绪，请稍后再试。")
        res = r.eval(_LOGIN_FAIL_LUA, 2, ip_key, user_key, window_ttl)
        return int(res[0]), int(res[1])
    except ChallengeServiceUnavailable:
        raise
    except Exception as exc:
        logger.error("Record login failure Redis error: %s", exc)
        raise ChallengeServiceUnavailable("安全校验服务暂时不可用，请稍后再试。") from exc


def clear_login_failures(username: str, ip_address: str) -> None:
    ip_key, user_key = _login_keys(username, ip_address)
    ip_try_key, user_try_key = _login_try_keys(username, ip_address)
    try:
        r = _get_redis_client(write=True)
        if r is None:
            return
        keys = [k for k in (ip_key, user_key, ip_try_key, user_try_key) if k]
        if keys:
            r.delete(*keys)
    except Exception as exc:
        logger.warning("Clear login failures Redis error: %s", exc)


def reserve_login_attempt(username: str, ip_address: str, *, has_captcha: bool) -> tuple[str, int]:
    """
    在调用 authenticate() 之前原子检查并预占尝试名额：
    返回 ("ok" | "captcha_required" | "locked", retry_after_seconds)。
    """
    ip_fail_k, user_fail_k = _login_keys(username, ip_address)
    ip_try_k, user_try_k = _login_try_keys(username, ip_address)
    captcha_threshold = int(getattr(settings, "IDENTITY_LOGIN_CAPTCHA_THRESHOLD", 3))
    lock_threshold = int(getattr(settings, "IDENTITY_LOGIN_LOCK_THRESHOLD", 10))
    window_ttl = int(getattr(settings, "IDENTITY_LOGIN_LOCK_WINDOW_SECONDS", 900))
    try:
        r = _get_redis_client(write=True)
        if r is None:
            raise ChallengeServiceUnavailable("安全校验存储未就绪，请稍后再试。")
        res = r.eval(
            _RESERVE_LOGIN_ATTEMPT_LUA,
            4,
            ip_fail_k,
            user_fail_k,
            ip_try_k,
            user_try_k,
            captcha_threshold,
            lock_threshold,
            window_ttl,
            1 if has_captcha else 0,
        )
        status = res[0].decode("utf-8") if isinstance(res[0], bytes) else str(res[0])
        retry_after = int(res[1])
        return status, retry_after
    except ChallengeServiceUnavailable:
        raise
    except Exception as exc:
        logger.error("Reserve login attempt Redis error: %s", exc)
        raise ChallengeServiceUnavailable("安全校验服务暂时不可用，请稍后再试。") from exc
