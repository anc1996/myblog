"""
人机挑战校验可插拔契约 (ChallengeVerifier)。

Phase 1 通过本抽象层与尚未实施的 Phase 2 (captcha 应用) 解耦：
- 未配置有效 Provider 时严格按 Fail-Closed 抛出 ChallengeServiceUnavailable (HTTP 503)；
- 绝不提供生产自动放行的空壳实现。
"""

from django.conf import settings
from django.utils.module_loading import import_string


class ChallengeServiceUnavailable(Exception):
    """验证码底层服务未配置或 Redis 不可用（返回 HTTP 503）。"""


class ChallengeVerificationError(Exception):
    """验证码缺失、错误或已过期（返回 HTTP 400 / 表单报错）。"""


class BaseChallengeVerifier:
    """验证码核销抽象基类。"""

    def verify_and_consume(self, *, request, expected_scope: str, captcha_id: str, code: str) -> None:
        raise NotImplementedError


class UnavailableChallengeVerifier(BaseChallengeVerifier):
    """默认安全兜底实现：未接入验证码 Provider 时拒绝绕过并返回 503。"""

    def verify_and_consume(self, *, request, expected_scope: str, captcha_id: str, code: str) -> None:
        raise ChallengeServiceUnavailable("安全校验服务暂未就绪，请稍后再试。")


def get_challenge_verifier() -> BaseChallengeVerifier:
    configured = getattr(settings, "IDENTITY_CHALLENGE_VERIFIER", None)
    if not configured:
        return UnavailableChallengeVerifier()
    if isinstance(configured, str):
        obj = import_string(configured)
        return obj() if isinstance(obj, type) else obj
    if isinstance(configured, type):
        return configured()
    return configured
