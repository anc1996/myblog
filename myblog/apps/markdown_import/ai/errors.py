"""
大模型调用统一内部异常定义。
业务层仅捕获本模块异常，不直接依赖 openai / httpx 第三方异常树，以便 SDK 随时独立升级。
"""

from dataclasses import dataclass


@dataclass
class LLMProviderError(Exception):
    """与具体第三方大模型 SDK 解耦的统一错误对象（不包含 API Key 或文章正文）。"""
    category: str  # "context_limit" | "payload_limit" | "rate_limit" | "timeout" | "unavailable" | "invalid_output" | "privacy_unsupported"
    message: str
    status_code: int | None = None
    retry_after: float | None = None
    is_retriable: bool = False

    def __str__(self) -> str:
        return f"[{self.category}] {self.message}"
