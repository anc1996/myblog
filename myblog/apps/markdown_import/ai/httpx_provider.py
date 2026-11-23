"""
纯 httpx 轻量备用大模型驱动 (HttpxProvider)。

不依赖 openai SDK，支持直接挂载 httpx.MockTransport 进行 100% 离线契约回归测试；
同样强制携带 `store=False` 并统一将 HTTP 错误映射为 LLMProviderError。
"""

import json
from typing import Any

from .contracts import LLMRequest, LLMResult
from .errors import LLMProviderError


class HttpxProvider:
    """直接通过 httpx 调用 OpenAI 兼容 HTTPS 接口的可切换驱动。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        mode: str = "responses",
        transport: Any = None,
        allow_store_false: bool = True,
    ) -> None:
        if not allow_store_false:
            raise LLMProviderError(
                category="privacy_unsupported",
                message="当前配置未允许 store=False，已禁止外发。",
            )
        if not api_key or not base_url or not model:
            raise LLMProviderError(
                category="unavailable",
                message="AI 服务端配置不完整。",
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.mode = mode
        self.transport = transport

    def generate(self, request: LLMRequest) -> LLMResult:
        try:
            import httpx  # 惰性导入
        except ImportError as exc:
            raise LLMProviderError(
                category="unavailable",
                message="当前环境未安装 httpx，已自动切换本地兜底。",
            ) from exc

        target_model = request.model or self.model
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.mode == "responses":
            endpoint = f"{self.base_url}/responses"
            body: dict[str, Any] = {
                "model": target_model,
                "instructions": request.instructions,
                "input": request.digest_text,
                "store": False,
                "max_output_tokens": request.max_output_tokens,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "markdown_metadata",
                        "strict": True,
                        "schema": request.json_schema,
                    }
                },
            }
        else:
            endpoint = f"{self.base_url}/chat/completions"
            fmt: dict[str, Any]
            if self.mode == "chat_completions":
                fmt = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "markdown_metadata",
                        "strict": True,
                        "schema": request.json_schema,
                    },
                }
            else:
                fmt = {"type": "json_object"}
            body = {
                "model": target_model,
                "store": False,
                "max_tokens": request.max_output_tokens,
                "messages": [
                    {"role": "system", "content": request.instructions},
                    {"role": "user", "content": request.digest_text},
                ],
                "response_format": fmt,
            }

        try:
            with httpx.Client(timeout=request.timeout_seconds, transport=self.transport) as client:
                resp = client.post(endpoint, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise LLMProviderError(category="timeout", message="模型请求超时。", is_retriable=True) from exc
        except Exception as exc:
            raise LLMProviderError(category="unavailable", message="模型网络请求失败。") from exc

        if resp.status_code == 413:
            raise LLMProviderError(category="payload_limit", message="请求体超过网关上限。", status_code=413, is_retriable=True)
        if resp.status_code == 429:
            retry_after = None
            raw_ra = resp.headers.get("Retry-After")
            if raw_ra:
                try:
                    retry_after = float(raw_ra)
                except ValueError:
                    retry_after = None
            raise LLMProviderError(category="rate_limit", message="模型限流。", status_code=429, retry_after=retry_after, is_retriable=False)
        if resp.status_code == 400:
            err_text = resp.text.lower()
            if "store" in err_text:
                raise LLMProviderError(category="privacy_unsupported", message="上游拒绝 store=False。", status_code=400)
            if "context" in err_text or "token" in err_text or "length" in err_text:
                raise LLMProviderError(category="context_limit", message="超过模型上下文上限。", status_code=400, is_retriable=True)
            raise LLMProviderError(category="invalid_output", message="模型参数报错。", status_code=400)
        if resp.status_code >= 400:
            raise LLMProviderError(category="unavailable", message=f"上游 HTTP {resp.status_code} 不可用。", status_code=resp.status_code)

        try:
            payload = resp.json()
        except Exception as exc:
            raise LLMProviderError(category="invalid_output", message="模型响应不是合法 JSON。") from exc

        raw_text = ""
        if self.mode == "responses":
            raw_text = payload.get("output_text") or ""
            if not raw_text and isinstance(payload.get("output"), list):
                chunks: list[str] = []
                for item in payload["output"]:
                    for content in item.get("content", []):
                        if content.get("type") in {"output_text", "text"} and content.get("text"):
                            chunks.append(content["text"])
                raw_text = "".join(chunks)
        else:
            choices = payload.get("choices") or []
            if choices and isinstance(choices[0], dict):
                raw_text = ((choices[0].get("message") or {}).get("content")) or ""

        if not str(raw_text).strip():
            raise LLMProviderError(category="invalid_output", message="模型返回空文本。")
        try:
            parsed = json.loads(raw_text)
        except Exception as exc:
            raise LLMProviderError(category="invalid_output", message="模型未返回合法结构化 JSON。") from exc
        if not isinstance(parsed, dict):
            raise LLMProviderError(category="invalid_output", message="模型返回值不是 JSON 对象。")

        return LLMResult(raw_text=str(raw_text), parsed_json=parsed, usage={})
