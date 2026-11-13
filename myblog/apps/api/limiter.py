"""
api.limiter
基于 Redis 的高精度滑动窗口限流守卫 (Rate Limiter)。

落实防刷与流控契约：
1. 采用 Redis Sorted Set (ZSET) 维护滑动时间窗口；
2. 窗口内时间精确到微秒，自动驱逐超期记录，原子性统计窗口频次；
3. 达到阈值返回 429 Too Many Requests 与 Retry-After 头；
4. 若 Redis 故障，Fail-Open 放行并打日志，绝不卡死正常用户请求。
"""

import time
import logging
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

logger = logging.getLogger(__name__)


class RateLimiter:
    """基于 Redis 的滑动窗口限流器"""

    @staticmethod
    def build_redis_key(scope: str, ip: str) -> str:
        prefix = getattr(settings, "REDIS_KEY_PREFIX", "") or settings.CACHES.get("default", {}).get("KEY_PREFIX", "myblog")
        clean_prefix = str(prefix or "myblog").strip().rstrip(":") or "myblog"
        return f"{clean_prefix}:ratelimit:{scope}:{ip}"

    @classmethod
    def get_client_ip(cls, request) -> str:
        """提取真实客户端 IP（优先 X-Forwarded-For）"""
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip = x_forwarded_for.split(",")[0].strip()
        else:
            ip = request.META.get("REMOTE_ADDR", "127.0.0.1")
        return ip

    @classmethod
    def check_rate_limit(
        cls,
        request,
        scope: str = "default",
        max_requests: int = 60,
        window_seconds: int = 60,
    ) -> tuple[bool, int, int]:
        """
        检查当前请求是否触发限流。
        返回: (is_limited: bool, remaining_requests: int, retry_after: int)
        """
        ip = cls.get_client_ip(request)
        now = time.time()
        window_start = now - window_seconds
        redis_key = cls.build_redis_key(scope, ip)

        try:
            client = getattr(cache, "_cache", None)
            r = None
            if hasattr(client, "get_client"):
                r = client.get_client()
            elif hasattr(client, "client"):
                r = client.client

            if r is not None:
                pipe = r.pipeline()
                pipe.zremrangebyscore(redis_key, 0, window_start)
                pipe.zcard(redis_key)
                pipe.zadd(redis_key, {str(now): now})
                pipe.expire(redis_key, window_seconds + 5)
                res = pipe.execute()

                current_requests = res[1]
                if current_requests >= max_requests:
                    return True, 0, int(window_seconds)
                remaining = max(0, max_requests - current_requests - 1)
                return False, remaining, 0

        except Exception as exc:
            logger.warning("RateLimiter Redis failure, failing open: %s", exc)

        return False, max_requests, 0


def ratelimit_api(scope: str = "api", max_requests: int = 60, window_seconds: int = 60):
    """API 视图限流装饰器"""
    def decorator(view_func):
        def wrapper(request, *args, **kwargs):
            is_limited, remaining, retry_after = RateLimiter.check_rate_limit(
                request,
                scope=scope,
                max_requests=max_requests,
                window_seconds=window_seconds,
            )
            if is_limited:
                resp = JsonResponse(
                    {
                        "status": "error",
                        "code": 429,
                        "message": "请求过于频繁，请稍后再试。",
                        "retry_after": retry_after,
                    },
                    status=429,
                )
                resp["Retry-After"] = str(retry_after)
                resp["Access-Control-Allow-Origin"] = "*"
                return resp

            response = view_func(request, *args, **kwargs)
            response["X-RateLimit-Limit"] = str(max_requests)
            response["X-RateLimit-Remaining"] = str(remaining)
            response["Access-Control-Allow-Origin"] = "*"
            return response
        return wrapper
    return decorator
