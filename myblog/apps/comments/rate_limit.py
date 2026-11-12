"""
comments.rate_limit
评论专用写操作原子流控守卫 (C-08 & RFC Phase 3 契约)。

落实安全防御：
1. 单次 Redis Lua 原子滑动窗口脚本：先清理过期窗口并核验全部维度未超限，再一次性写入 ZADD 与 EXPIRE，
   彻底消除双 pipeline 并发穿透；
2. 分级流控（60 秒滑动窗口）：
   - 游客：单浏览器 Cookie 最多 1 次、单 IP 最多 3 次；
   - 新注册会员（<7天）：单用户最多 2 次、单 IP 最多 20 次；
   - 普通可信会员：单用户最多 5 次、单 IP 最多 20 次；
   - 编辑 / 赞踩 / 删除接口同步接入限流；
3. 会员高频风控信号识别：60 秒内连续发评或携带外链时触发人机验证码；
4. Fail-Closed 熔断：写操作在 Redis 故障时直接返回 503，防止风控失效被刷爆。
"""

import ipaddress
import re
import time
import logging
from uuid import uuid4
from django.conf import settings
from django.core.cache import cache
from comments.access import get_comment_user_tier
from comments.sanitizer import has_external_or_http_links
from identity.challenges import ChallengeServiceUnavailable

logger = logging.getLogger(__name__)

# 单次原子滑动窗口脚本：全维度检查通过后才执行 ZADD，并返回精确 Retry-After
# ARGV[1]=now_ts, ARGV[2]=window, ARGV[3]=member_id, ARGV[4]=burst_key_index(1-based or 0), ARGV[5]=burst_limit(or 0)
# ARGV[5 + i]=limit for KEYS[i]
_COMMENT_RATE_LIMIT_LUA = """
local now_ts = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local member_id = ARGV[3]
local burst_idx = tonumber(ARGV[4])
local burst_limit = tonumber(ARGV[5])
local cutoff = now_ts - window

for i = 1, #KEYS do
    local k = KEYS[i]
    local limit = tonumber(ARGV[5 + i])
    redis.call('ZREMRANGEBYSCORE', k, '-inf', cutoff)
    local cnt = tonumber(redis.call('ZCARD', k) or '0')
    if burst_idx == i and burst_limit > 0 and cnt >= burst_limit then
        return {0, 0, -1}
    end
    if cnt >= limit then
        local oldest = redis.call('ZRANGE', k, 0, 0, 'WITHSCORES')
        local retry = window
        if oldest and #oldest >= 2 then
            local oldest_ts = tonumber(oldest[2])
            if oldest_ts then
                local rem = math.ceil((oldest_ts + window) - now_ts)
                if rem > 0 and rem <= window then
                    retry = rem
                end
            end
        end
        return {0, retry, i}
    end
end

for i = 1, #KEYS do
    local k = KEYS[i]
    redis.call('ZADD', k, now_ts, member_id)
    redis.call('EXPIRE', k, window + 5)
end
return {1, 0, 0}
"""


def _get_redis_client(*, write: bool = True):
    client = getattr(cache, "_cache", None)
    if client is not None and hasattr(client, "get_client"):
        return client.get_client(write=write)
    if client is not None and hasattr(client, "client"):
        return client.client
    return None


def get_client_ip(request) -> str:
    """
    安全解析客户端 IP（防止 X-Forwarded-For 左端伪造绕过）：
    仅当直接来源为受信反向代理时解析 X-Forwarded-For，且从右向左跳过受信代理 IP，
    取第一个合法的非代理客户端 IP。
    """
    trusted_proxies = set(getattr(settings, "TRUSTED_PROXIES", ["127.0.0.1", "192.168.20.5"]))
    remote_addr = (request.META.get("REMOTE_ADDR") or "127.0.0.1").strip()
    if remote_addr in trusted_proxies:
        x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded:
            candidates = [part.strip() for part in x_forwarded.split(",") if part.strip()]
            for candidate in reversed(candidates):
                if candidate in trusted_proxies:
                    continue
                try:
                    return str(ipaddress.ip_address(candidate))
                except ValueError:
                    continue
    return remote_addr


def _resolve_rate_limits(tier: str, action: str) -> tuple[int, int, int]:
    """
    返回 (browser_limit, user_limit, ip_limit)（均为 60 秒滑动窗口上限）。
    """
    if tier == "guest":
        if action == "post":
            return 1, 0, 3
        if action == "post_attempt":
            return 0, 0, 15
        return 1, 0, 3

    if tier == "new_member":
        if action == "post":
            return 0, 2, 20
        if action == "post_attempt":
            return 0, 10, 30
        if action == "edit":
            return 0, 5, 20
        if action == "delete":
            return 0, 10, 30
        return 0, 15, 40

    # trusted_member / superuser
    if action == "post":
        return 0, 5, 20
    if action == "post_attempt":
        return 0, 15, 40
    if action == "edit":
        return 0, 10, 30
    if action == "delete":
        return 0, 15, 40
    return 0, 15, 40


def check_comment_rate_limit(
    request,
    user,
    action: str = "post",
    *,
    guest_browser_key: str | None = None,
    has_captcha: bool = True,
    raw_content: str = "",
) -> tuple[bool, int, str]:
    """
    检查评论写操作限流。
    当 action='post' 且为非超管已登录会员且 has_captcha=False 时，原子检查是否包含外链或已触发高频阈值；
    若触发则返回 (False, 0, 'captcha_required') 且不消耗限流槽位，彻底消除并发穿透。
    返回: (allowed: bool, retry_after: int, error_code: str)
    """
    ip = get_client_ip(request)
    now = time.time()
    window = 60
    tier = get_comment_user_tier(user)
    browser_limit, user_limit, ip_limit = _resolve_rate_limits(tier, action)

    burst_idx = 0
    burst_limit = 0
    if (
        action == "post"
        and not has_captcha
        and tier in ("new_member", "trusted_member")
    ):
        if has_external_or_http_links(raw_content):
            return False, 0, "captcha_required"
        burst_limit = int(getattr(settings, "COMMENT_MEMBER_CAPTCHA_BURST_THRESHOLD", 1))
        if burst_limit <= 0:
            return False, 0, "captcha_required"

    keys: list[str] = []
    limits: list[int] = []
    if tier == "guest" and guest_browser_key and browser_limit > 0:
        keys.append(f"myblog:ratelimit:comment:browser:{guest_browser_key}:{action}")
        limits.append(browser_limit)
    if user and getattr(user, "is_authenticated", False) and user_limit > 0:
        keys.append(f"myblog:ratelimit:comment:user:{user.id}:{action}")
        limits.append(user_limit)
        if burst_limit > 0:
            burst_idx = len(keys)
    keys.append(f"myblog:ratelimit:comment:ip:{ip}:{action}")
    limits.append(ip_limit)

    try:
        r = _get_redis_client(write=True)
        if r is None:
            # Redis 不可用，写操作 Fail-Closed 阻断
            logger.error("Redis cache unavailable for comment rate limit; failing closed.")
            return False, 30, "risk_control_unavailable"

        member_id = f"{now}:{uuid4().hex[:8]}"
        res = r.eval(
            _COMMENT_RATE_LIMIT_LUA,
            len(keys),
            *keys,
            now,
            window,
            member_id,
            burst_idx,
            burst_limit,
            *limits,
        )
        allowed = int(res[0]) == 1
        if not allowed:
            if int(res[2]) == -1:
                return False, 0, "captcha_required"
            retry_after = max(1, int(res[1]))
            return False, retry_after, "rate_limited"

        return True, 0, ""

    except Exception as exc:
        logger.exception("Comment rate limit Redis exception: %s", exc)
        return False, 30, "risk_control_unavailable"


def is_member_comment_captcha_required(request, user, raw_content: str = "") -> bool:
    """
    判断已登录会员当前发表评论是否触发高频或外链风控信号，需要弹出人机验证码。
    Redis 不可用时严格 Fail-Closed 抛出 ChallengeServiceUnavailable。
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return False

    if raw_content and has_external_or_http_links(raw_content):
        return True

    burst_threshold = int(getattr(settings, "COMMENT_MEMBER_CAPTCHA_BURST_THRESHOLD", 1))
    if burst_threshold <= 0:
        return True

    user_key = f"myblog:ratelimit:comment:user:{user.id}:post"
    now = time.time()
    cutoff = now - 60
    try:
        r = _get_redis_client(write=False)
        if r is None:
            raise ChallengeServiceUnavailable("安全校验存储未就绪，请稍后再试。")
        recent_count = int(r.zcount(user_key, cutoff, "+inf") or 0)
        return recent_count >= burst_threshold
    except ChallengeServiceUnavailable:
        raise
    except Exception as exc:
        logger.error("Member captcha signal Redis error: %s", exc)
        raise ChallengeServiceUnavailable("安全校验服务暂时不可用，请稍后再试。") from exc
