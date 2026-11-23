"""
OpenAI 官方 SDK 驱动封装 (OpenAISDKProvider)。

隔离与升级要点：
- 惰性导入 `openai`（未安装 SDK 的环境下绝不影响 Django 启动）；
- 强制关闭 SDK 内置重试 (`max_retries=0`)，防止多级降级叠加超时卡死 WSGI worker；
- 强制所有请求携带 `store=False`，若上游或配置拒绝则直接返回 `privacy_unsupported`；
- 未来升级 `openai` 主版本只需修改本文件，业务层零改动。
"""

import json
from typing import Any

from .contracts import LLMRequest, LLMResult
from .errors import LLMProviderError


class OpenAISDKProvider:
    """基于 openai>=2.x 官方 SDK 的驱动实现，支持 Responses API 与 Chat Completions。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        mode: str = "responses",
        allow_store_false: bool = True,
    ) -> None:
        if not allow_store_false:
            raise LLMProviderError(
                category="privacy_unsupported",
                message="当前供应商未确认支持 store=False 隐私策略，已禁止外发。",
            )
        if not api_key or not base_url or not model:
            raise LLMProviderError(
                category="unavailable",
                message="AI 服务端配置不完整（仅允许从服务端环境变量配置）。",
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.mode = mode  # "responses" | "chat_completions" | "chat_json_object"
        self._client = None

    def _get_client(self, timeout_seconds: float):
        try:
            import openai  # 惰性导入
        except ImportError as exc:
            raise LLMProviderError(
                category="unavailable",
                message="当前环境未安装 openai SDK，已自动切换本地兜底。",
            ) from exc
        if self._client is None:
            self._client = openai.OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                max_retries=0,
                timeout=timeout_seconds,
            )
        return openai, self._client

    def generate(self, request: LLMRequest) -> LLMResult:
        openai, client = self._get_client(request.timeout_seconds)
        target_model = request.model or self.model

        try:
            if self.mode == "responses":
                resp = client.responses.create(
                    model=target_model,
                    instructions=request.instructions,
                    input=request.digest_text,
                    store=False,
                    max_output_tokens=request.max_output_tokens,
                    timeout=request.timeout_seconds,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "markdown_metadata",
                            "strict": True,
                            "schema": request.json_schema,
                        }
                    },
                )
                raw_text = getattr(resp, "output_text", "") or ""
            elif self.mode == "chat_completions":
                resp = client.chat.completions.create(
                    model=target_model,
                    store=False,
                    max_tokens=request.max_output_tokens,
                    timeout=request.timeout_seconds,
                    messages=[
                        {"role": "system", "content": request.instructions},
                        {"role": "user", "content": request.digest_text},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "markdown_metadata",
                            "strict": True,
                            "schema": request.json_schema,
                        },
                    },
                )
                raw_text = (resp.choices[0].message.content or "") if resp.choices else ""
            else:
                resp = client.chat.completions.create(
                    model=target_model,
                    store=False,
                    max_tokens=request.max_output_tokens,
                    timeout=request.timeout_seconds,
                    messages=[
                        {"role": "system", "content": request.instructions + "\n请严格只输出 JSON 对象。"},
                        {"role": "user", "content": request.digest_text},
                    ],
                    response_format={"type": "json_object"},
                )
                raw_text = (resp.choices[0].message.content or "") if resp.choices else ""
        except openai.APITimeoutError as exc:
            raise LLMProviderError(category="timeout", message="模型请求超时。", is_retriable=True) from exc
        except openai.RateLimitError as exc:
            raise LLMProviderError(category="rate_limit", message="模型触发限流。", status_code=429, is_retriable=False) from exc
        except openai.BadRequestError as exc:
            msg = str(exc).lower()
            if "store" in msg and ("unsupported" in msg or "unknown" in msg or "not allowed" in msg):
                raise LLMProviderError(category="privacy_unsupported", message="上游拒绝 store=False 参数，已停止外发。", status_code=400) from exc
            if "context" in msg or "token" in msg or "length" in msg:
                raise LLMProviderError(category="context_limit", message="输入摘要超过模型上下文上限。", status_code=400, is_retriable=True) from exc
            raise LLMProviderError(category="invalid_output", message="模型参数校验未通过。", status_code=400) from exc
        except openai.APIStatusError as exc:
            code = getattr(exc, "status_code", None)
            if code == 413:
                raise LLMProviderError(category="payload_limit", message="请求体大小超过网关上限。", status_code=413, is_retriable=True) from exc
            raise LLMProviderError(category="unavailable", message="模型网关暂时不可用。", status_code=code) from exc
        except Exception as exc:
            raise LLMProviderError(category="unavailable", message="模型连接失败。") from exc

        if not raw_text.strip():
            raise LLMProviderError(category="invalid_output", message="模型返回空响应。")
        try:
            parsed = json.loads(raw_text)
        except Exception as exc:
            raise LLMProviderError(category="invalid_output", message="模型未返回合法 JSON。") from exc
        if not isinstance(parsed, dict):
            raise LLMProviderError(category="invalid_output", message="模型返回 JSON 不是对象。")
        return LLMResult(raw_text=raw_text, parsed_json=parsed, usage={})
