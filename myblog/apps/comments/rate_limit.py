"""
comments.rate_limit
评论专用写操作流控守卫 (C-08 契约)。

落实安全防御：
1. 双维度滑动窗口：单用户 60 秒限 5 次，单 IP 60 秒限 20 次；
2. 先查后写：只有未超限时才执行 ZADD，杜绝超限后重复请求自我锁死的 Bug；
3. Fail-Closed 熔断：写操作在 Redis 故障时直接返回 503，防止风控失效被刷爆。
"""

import time
import logging
from uuid import uuid4
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


def get_client_ip(request) -> str:
    """安全解析客户端 IP（防止 X-Forwarded-For 伪造绕过）"""
    trusted_proxies = getattr(settings, "TRUSTED_PROXIES", ["127.0.0.1", "192.168.20.5"])
    remote_addr = request.META.get("REMOTE_ADDR", "127.0.0.1")
    if remote_addr in trusted_proxies:
        x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded:
            return x_forwarded.split(",")[0].strip()
    return remote_addr


def check_comment_rate_limit(request, user, action: str = "post") -> tuple[bool, int, str]:
    """
    检查评论写操作限流。
    返回: (allowed: bool, retry_after: int, error_code: str)
    """
    ip = get_client_ip(request)
    now = time.time()
    window = 60
    max_user_reqs = 5 if action == "post" else 15
    max_ip_reqs = 20 if action == "post" else 40

    user_key = f"myblog:ratelimit:comment:user:{user.id}:{action}" if user and user.is_authenticated else None
    ip_key = f"myblog:ratelimit:comment:ip:{ip}:{action}"

    try:
        client = getattr(cache, "_cache", None)
        r = None
        if hasattr(client, "get_client"):
            r = client.get_client()
        elif hasattr(client, "client"):
            r = client.client

        if r is None:
            # Redis 不可用，写操作 Fail-Closed 阻断
            logger.error("Redis cache unavailable for comment rate limit; failing closed.")
            return False, 30, "risk_control_unavailable"

        pipe = r.pipeline()
        window_start = now - window

        # 1. 先统计 IP 窗口内请求数
        pipe.zremrangebyscore(ip_key, 0, window_start)
        pipe.zcard(ip_key)
        if user_key:
            pipe.zremrangebyscore(user_key, 0, window_start)
            pipe.zcard(user_key)

        res = pipe.execute()
        ip_count = res[1]
        user_count = res[3] if user_key else 0

        if ip_count >= max_ip_reqs or user_count >= max_user_reqs:
            return False, int(window), "rate_limited"

        # 2. 未超限才原子记录新请求
        pipe = r.pipeline()
        member_id = f"{now}:{uuid4().hex[:6]}"
        pipe.zadd(ip_key, {member_id: now})
        pipe.expire(ip_key, window + 5)
        if user_key:
            pipe.zadd(user_key, {member_id: now})
            pipe.expire(user_key, window + 5)
        pipe.execute()

        return True, 0, ""

    except Exception as exc:
        logger.exception("Comment rate limit Redis exception: %s", exc)
        return False, 30, "risk_control_unavailable"
