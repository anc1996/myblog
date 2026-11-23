"""
myblog 本地 Markdown 导入 HTTP 客户端（CLI 与桌面 GUI 共用）。

特点：
1. 支持一次性配对码换取短期导入 Bearer Token（或直接配置 Bearer Token）；
2. 支持拉取服务器动态注册的所有页面类型 (BlogPage / GuidePage / ProjectPage / 未来 XxPage)；
3. 支持扫描单个 .md 或资料文件夹、预览与 AI 自动补全、断线续传（脱敏保存，不落明文 Token）；
4. 提交成功后返回后台草稿编辑网址，绝不自动发布或覆盖现有页面。
"""

import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
import uuid

import requests

from .scanner import ScannedMarkdownSource


def _session_checkpoint_path(md_path: Path) -> Path:
    return md_path.with_name(f".{md_path.name}.markdown-import-session.json")


def load_session_checkpoint(md_path: Path) -> dict[str, Any] | None:
    cp = _session_checkpoint_path(md_path)
    if not cp.exists():
        return None
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_session_checkpoint(md_path: Path, data: dict[str, Any]) -> None:
    """只保存脱敏后的作业 ID、幂等键与输入摘要，绝不将 Token 或源码写入断点文件。"""
    cp = _session_checkpoint_path(md_path)
    safe_payload = {
        "job_id": data.get("job_id"),
        "idempotency_key": data.get("idempotency_key"),
        "type_key": data.get("type_key"),
        "parent_id": data.get("parent_id"),
        "status": data.get("status"),
        "page_id": data.get("page_id"),
    }
    cp.write_text(json.dumps(safe_payload, ensure_ascii=False, indent=2), encoding="utf-8")


class MarkdownImportClient:
    """与 myblog /api/markdown-import/ 对接的统一客户端。"""

    def __init__(
        self,
        site_url: str,
        token: str = "",
        timeout: float = 30.0,
        *,
        allow_insecure_http: bool = False,
    ) -> None:
        normalized = site_url.strip().rstrip("/")
        parsed = urlsplit(normalized)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        is_loopback = host in {"127.0.0.1", "localhost", "::1"}
        if scheme == "http" and not is_loopback and not allow_insecure_http:
            raise ValueError(
                "非本机回环站点默认强制使用 HTTPS，防止配对码、导入令牌和草稿正文以明文传输；"
                "若确认在可信内网测试环境使用，请显式传入 allow_insecure_http=True。"
            )
        if scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"站点地址格式无效: {site_url}")
        self.site_url = normalized
        self.token = token.strip()
        self.timeout = timeout

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def _api(self, suffix: str) -> str:
        return f"{self.site_url}/api/markdown-import/{suffix.lstrip('/')}"

    @staticmethod
    def _raise_for_api_error(resp: requests.Response) -> dict[str, Any]:
        try:
            payload = resp.json()
        except Exception:
            payload = {"code": f"http_{resp.status_code}", "message": resp.text[:300]}
        if resp.status_code >= 400:
            code = payload.get("code", f"http_{resp.status_code}")
            msg = payload.get("message", resp.reason or "请求失败")
            raise RuntimeError(f"[{resp.status_code} {code}] {msg}")
        return payload

    def exchange_pair_code(self, pair_code: str, client_name: str = "桌面导入向导") -> dict[str, Any]:
        resp = requests.post(
            self._api("pair/exchange/"),
            json={"pair_code": pair_code, "client_name": client_name},
            timeout=self.timeout,
        )
        data = self._raise_for_api_error(resp)
        self.token = data["token"]
        return data

    def fetch_catalog(
        self,
        type_key: str = "",
        *,
        q: str = "",
        containers_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "containers_only": "1" if containers_only else "0",
        }
        if q.strip():
            params["q"] = q.strip()
        if type_key:
            params["type_key"] = type_key
        resp = requests.get(
            self._api("catalog/"),
            headers=self._headers(),
            params=params,
            timeout=self.timeout,
        )
        return self._raise_for_api_error(resp)

    def ai_suggest(
        self,
        *,
        type_key: str,
        parent_id: int,
        source_name: str,
        markdown_text: str,
        existing_title: str = "",
        existing_tags: list[str] | None = None,
        allow_external_processing: bool = True,
        allow_new_tags: bool = False,
    ) -> dict[str, Any]:
        resp = requests.post(
            self._api("ai/suggest/"),
            headers=self._headers(),
            json={
                "type_key": type_key,
                "parent_id": parent_id,
                "source_name": source_name,
                "markdown": markdown_text,
                "existing_title": existing_title,
                "existing_tags": existing_tags or [],
                "allow_external_processing": allow_external_processing,
                "allow_new_tags": allow_new_tags,
            },
            timeout=max(self.timeout, 30.0),
        )
        return self._raise_for_api_error(resp)

    @staticmethod
    def build_assets_manifest(scanned: ScannedMarkdownSource) -> list[dict[str, Any]]:
        return [
            {
                "asset_key": asset.asset_key,
                "media_type": asset.media_type,
                "filename": asset.local_path.name,
                "size": asset.size_bytes,
                "sha256": asset.sha256,
            }
            for asset in scanned.assets
        ]

    def preflight(
        self,
        *,
        type_key: str,
        parent_id: int,
        scanned: ScannedMarkdownSource,
        edits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resp = requests.post(
            self._api("preflight/"),
            headers=self._headers(),
            json={
                "type_key": type_key,
                "parent_id": parent_id,
                "source_name": scanned.source_name,
                "markdown": scanned.raw_text,
                "assets": self.build_assets_manifest(scanned),
                "edits": edits or {},
            },
            timeout=self.timeout,
        )
        return self._raise_for_api_error(resp)

    def import_source(
        self,
        *,
        type_key: str,
        parent_id: int,
        scanned: ScannedMarkdownSource,
        edits: dict[str, Any] | None = None,
        idempotency_key: str = "",
        progress_cb: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        if scanned.missing_assets:
            raise RuntimeError(f"存在缺失本地文件，无法导入: {', '.join(scanned.missing_assets)}")
        if scanned.blocked_paths:
            raise RuntimeError(f"存在不安全的资源路径，已拦截: {'; '.join(scanned.blocked_paths)}")

        pre = self.preflight(type_key=type_key, parent_id=parent_id, scanned=scanned, edits=edits)
        if pre.get("errors"):
            msgs = [e.get("message", "") for e in pre["errors"]]
            raise RuntimeError("预检未通过: " + "；".join(msgs))
        plan_token = pre["plan_token"]

        idem = idempotency_key.strip() or f"mdimp-{uuid.uuid4().hex}"
        manifest_assets = self.build_assets_manifest(scanned)
        job_resp = requests.post(
            self._api("jobs/"),
            headers=self._headers({"Idempotency-Key": idem}),
            json={
                "plan_token": plan_token,
                "type_key": type_key,
                "parent_id": parent_id,
                "source_name": scanned.source_name,
                "markdown": scanned.raw_text,
                "assets": manifest_assets,
                "edits": edits or {},
            },
            timeout=self.timeout,
        )
        job_data = self._raise_for_api_error(job_resp)
        job_id = job_data["job_id"]
        save_session_checkpoint(
            scanned.markdown_path,
            {
                "job_id": job_id,
                "idempotency_key": idem,
                "type_key": type_key,
                "parent_id": parent_id,
                "status": job_data.get("status"),
            },
        )

        uploaded_keys = {a["asset_key"] for a in job_data.get("assets", []) if a.get("uploaded")}
        total = len(scanned.assets)
        for idx, asset in enumerate(scanned.assets, start=1):
            if asset.asset_key in uploaded_keys:
                if progress_cb:
                    progress_cb("skipped", idx, total)
                continue
            with asset.local_path.open("rb") as fh:
                up_resp = requests.post(
                    self._api(f"jobs/{job_id}/assets/"),
                    headers=self._headers(),
                    data={"asset_key": asset.asset_key},
                    files={"file": (asset.local_path.name, fh)},
                    timeout=max(self.timeout, 120.0),
                )
            self._raise_for_api_error(up_resp)
            if progress_cb:
                progress_cb("uploaded", idx, total)

        commit_resp = requests.post(
            self._api(f"jobs/{job_id}/commit/"),
            headers=self._headers(),
            timeout=max(self.timeout, 60.0),
        )
        res = self._raise_for_api_error(commit_resp)
        save_session_checkpoint(
            scanned.markdown_path,
            {
                "job_id": job_id,
                "idempotency_key": idem,
                "type_key": type_key,
                "parent_id": parent_id,
                "status": res.get("status"),
                "page_id": res.get("page_id"),
            },
        )
        res["publish_blockers"] = pre.get("publish_blockers", [])
        res["edit_full_url"] = f"{self.site_url}{res['edit_url']}"
        return res
