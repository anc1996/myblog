"""
大模型薄协议 (LLMClientProtocol) 与结构化返回契约。
本模块零第三方 HTTP/SDK 顶层导入。
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class LLMRequest:
    """业务层传递给底层大模型驱动的最小受控请求。"""
    digest_text: str
    instructions: str
    json_schema: dict[str, Any]
    max_output_tokens: int = 600
    timeout_seconds: float = 15.0
    model: str = ""
    template_version: int = 1


@dataclass(frozen=True)
class LLMResult:
    """底层驱动返回的纯文本与脱敏用量统计。"""
    raw_text: str
    parsed_json: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetadataSuggestion:
    """最终经过校验的元数据建议，可供预检页面与适配器使用。"""
    title: str
    summary: str
    tags: list[str]
    source: str  # "ai" | "local"
    warnings: list[str] = field(default_factory=list)
    digest_stats: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMClientProtocol(Protocol):
    """所有大模型后端（OpenAI SDK / HTTPX / Mock 测试替身）必须遵守的统一协议。"""

    def generate(self, request: LLMRequest) -> LLMResult: ...
