"""
人机挑战 Provider 抽象与 Pillow+Redis 默认实现。
"""

from captcha.service import issue_captcha_challenge, verify_and_consume_captcha
from identity.challenges import BaseChallengeVerifier


class CaptchaProvider(BaseChallengeVerifier):
    """全站可替换人机验证 Provider 契约（后续可平滑替换 Turnstile 等实现）。"""

    def issue(self, *, request, scope: str, previous_captcha_id: str = ""):
        raise NotImplementedError


class PillowRedisCaptchaVerifier(CaptchaProvider):
    """基于 Pillow 13.0.0 与 Redis Lua 原子核销的公共图片验证码实现。"""

    def issue(self, *, request, scope: str, previous_captcha_id: str = ""):
        return issue_captcha_challenge(
            request=request,
            scope=scope,
            previous_captcha_id=previous_captcha_id,
        )

    def verify_and_consume(self, *, request, expected_scope: str, captcha_id: str, code: str) -> None:
        verify_and_consume_captcha(
            request=request,
            expected_scope=expected_scope,
            captcha_id=captcha_id,
            code=code,
        )
