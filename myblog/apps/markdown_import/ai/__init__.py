"""
10 万字超长文 AI 元数据引擎与可升级大模型薄封装层。
"""

from .contracts import LLMClientProtocol, LLMRequest, LLMResult, MetadataSuggestion
from .digest import DigestOutput, StratifiedDigestBuilder
from .errors import LLMProviderError
from .service import generate_metadata_with_fallback

__all__ = [
    "DigestOutput",
    "LLMClientProtocol",
    "LLMProviderError",
    "LLMRequest",
    "LLMResult",
    "MetadataSuggestion",
    "StratifiedDigestBuilder",
    "generate_metadata_with_fallback",
]
