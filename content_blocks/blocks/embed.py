"""
受控嵌入内容块 (CustomEmbedBlock)。

提供外部第三方富媒体（如 Bilibili、YouTube、Vimeo、CodePen）的安全嵌入能力：
1. 强制白名单域名校验，杜绝恶意网站伪造 iframe；
2. 严格防御 SSRF：拒绝内网回环地址 (127.0.0.1, localhost)、内网私有 IP (10.x, 192.168.x, 172.16-31.x)
   与云服务元数据探测地址 (169.254.169.254)；
3. 仅允许 http/https 协议；
4. 满足 AC-14 安全验收要求。
"""

import ipaddress
from urllib.parse import urlparse
from typing import Any
from wagtail.embeds.blocks import EmbedBlock, EmbedValue

from content_blocks.contracts import (
    BlockContract,
    BlockValidationResult,
    BlockRenderResult,
    BlockTextExtraction,
    MediaReference,
)
from content_blocks.sanitizer import sanitize_html


# 允许嵌入的第三方平台域名白名单
ALLOWED_EMBED_DOMAINS = {
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "vimeo.com",
    "player.vimeo.com",
    "bilibili.com",
    "www.bilibili.com",
    "player.bilibili.com",
    "codepen.io",
}


def is_safe_embed_url(url: str) -> tuple[bool, str]:
    """校验外部嵌入 URL 是否安全合规且防 SSRF"""
    if not url:
        return False, "URL 不能为空"

    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False, "非法 URL 格式"

    # 协议检查
    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的协议 {parsed.scheme}，仅允许 http/https"

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False, "无法解析的主机名"

    # 防 SSRF：拒绝环回、私有 IP 及链路本地地址
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return False, "禁止嵌入本地回环地址 (SSRF 防御)"

    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False, "禁止嵌入私有/内网 IP 地址 (SSRF 防御)"
    except ValueError:
        # 不是 IP，是普通域名
        pass

    # 域名白名单匹配（支持二级域）
    domain_matched = False
    for allowed in ALLOWED_EMBED_DOMAINS:
        if hostname == allowed or hostname.endswith(f".{allowed}"):
            domain_matched = True
            break

    if not domain_matched:
        return False, f"域名 {hostname} 不在受信任的嵌入白名单中"

    return True, ""


class CustomEmbedBlock(EmbedBlock):
    type_name = "embed"
    schema_version = 1

    def clean(self, value):
        url = value.url if hasattr(value, "url") else str(value or "")
        if url:
            is_safe, reason = is_safe_embed_url(url)
            if not is_safe:
                from django.core.exceptions import ValidationError
                raise ValidationError(reason)
        if isinstance(value, str):
            value = EmbedValue(value)
        value = super().clean(value)
        return value

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value:
            return BlockValidationResult(is_valid=True)
        url = getattr(value, "url", str(value))
        is_safe, reason = is_safe_embed_url(url)
        if not is_safe:
            return BlockValidationResult(is_valid=False, errors=[reason])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)
        url = getattr(value, "url", str(value))
        is_safe, _ = is_safe_embed_url(url)
        if not is_safe:
            return BlockRenderResult(html="", is_safe=True)
        # 渲染受限 iframe 容器
        raw_html = f'<div class="media-embed-wrapper" data-embed-url="{url}"></div>'
        return BlockRenderResult(html=sanitize_html(raw_html), is_safe=True)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        url = getattr(value, "url", str(value))
        return BlockTextExtraction(plain_text=url, logical_segments=[url])

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        return []
