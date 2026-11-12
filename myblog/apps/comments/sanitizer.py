"""
comments.sanitizer
评论专用安全净化器 (C-08 契约)。

严格防御 XSS 攻击与外部追踪像素注入：
1. 严禁 <img>, <video>, <audio>, <table>, <div> 等富媒体标签，防止追踪像素与跨站伪造；
2. 仅允许基础文本排版标签：p, br, strong, b, em, i, code, pre, blockquote, ul, ol, li, a；
3. 链接强制注入 rel="nofollow ugc noopener noreferrer"，仅允许 http/https 协议；
4. 正文限制最大 5,000 字符。
"""

import re
from urllib.parse import urlsplit
from django.core.exceptions import ValidationError
import nh3
import markdown

ALLOWED_COMMENT_TAGS = {
    "p", "br", "strong", "b", "em", "i", "s", "del",
    "code", "pre", "blockquote",
    "ul", "ol", "li",
    "a",
}

ALLOWED_COMMENT_ATTRIBUTES = {
    "a": {"href", "title"},
    "code": {"class"},
}

ALLOWED_COMMENT_SCHEMES = {"http", "https"}
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_HTML_ANCHOR_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)
_HREF_ATTR_RE = re.compile(r'href\s*=\s*["\'][^"\']*["\']', re.IGNORECASE)
_BARE_HTTP_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+", re.IGNORECASE)
_SUSPICIOUS_PROTOCOL_RE = re.compile(r"https?://", re.IGNORECASE)


def get_tier_sanitizer_limits(tier: str) -> tuple[int, int, int]:
    """
    返回指定身份档位的内容安全约束：
    (max_chars, max_external_links, force_pending_links_threshold)
    """
    if tier == "guest":
        return 500, 0, 1
    if tier == "new_member":
        return 1000, 1, 1
    return 2000, 2, 2


def validate_comment_length(raw_text: str, max_chars: int = 2000) -> None:
    """校验评论字数上限 (最大 2000 字符，拒绝静默截断，B-05)"""
    if not raw_text or not raw_text.strip():
        raise ValidationError("评论正文不能为空。")
    if len(raw_text) > max_chars:
        raise ValidationError(f"评论长度超出限制（当前 {len(raw_text)} 字符，最多允许 {max_chars} 字符）。")


def sanitize_comment_html(raw_markdown_or_html: str, max_chars: int = 2000) -> str:
    """对评论内容进行 Markdown 解析与严格白名单安全净化"""
    if not raw_markdown_or_html:
        return ""

    # 1. 校验长度
    validate_comment_length(raw_markdown_or_html, max_chars)

    # 2. Markdown 转换为 HTML
    raw_html = markdown.markdown(
        raw_markdown_or_html,
        extensions=["fenced_code", "tables"],
        output_format="html5",
    )

    # 3. nh3 严格白名单过滤与链接安全加固
    return nh3.clean(
        raw_html,
        tags=ALLOWED_COMMENT_TAGS,
        attributes=ALLOWED_COMMENT_ATTRIBUTES,
        url_schemes=ALLOWED_COMMENT_SCHEMES,
        link_rel="nofollow ugc noopener noreferrer",
    )


def inspect_links_and_sanitize(
    raw_markdown_or_html: str,
    internal_hosts: set[str] | None = None,
    *,
    max_chars: int = 2000,
) -> tuple[str, int]:
    """
    安全净化评论 HTML 并统计外链数量 (B-05 / 反垃圾门禁)。
    返回: (safe_html, external_link_count)
    """
    safe_html = sanitize_comment_html(raw_markdown_or_html, max_chars=max_chars)

    href_pattern = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
    external_links = 0
    suspicious_links = 0
    clean_hosts = internal_hosts or {"localhost", "127.0.0.1", "192.168.20.5"}

    for match in href_pattern.finditer(safe_html):
        href = match.group(1).strip()
        # 防御反斜杠与 userinfo 绕过 (B-05)
        if "\\" in href or href.startswith("//"):
            safe_html = safe_html.replace(match.group(0), 'href="#"')
            suspicious_links += 1
            continue
        try:
            parts = urlsplit(href)
            if parts.username or parts.password:
                safe_html = safe_html.replace(match.group(0), 'href="#"')
                suspicious_links += 1
                continue
            if parts.scheme in ("http", "https"):
                hostname = (parts.hostname or "").lower()
                if hostname not in clean_hosts:
                    external_links += 1
        except Exception:
            safe_html = safe_html.replace(match.group(0), 'href="#"')
            suspicious_links += 1

    # 额外检测未包裹成 Markdown/HTML <a> 的裸外部 URL，同时避免与已解析链接重复计数
    scrubbed_raw = _MARKDOWN_LINK_RE.sub(" ", raw_markdown_or_html or "")
    scrubbed_raw = _HTML_ANCHOR_RE.sub(" ", scrubbed_raw)
    scrubbed_raw = _HREF_ATTR_RE.sub(" ", scrubbed_raw)
    for bare_match in _BARE_HTTP_URL_RE.finditer(scrubbed_raw):
        try:
            parts = urlsplit(bare_match.group(0))
            hostname = (parts.hostname or "").lower()
            if hostname and hostname not in clean_hosts:
                external_links += 1
        except Exception:
            suspicious_links += 1

    return safe_html, external_links + suspicious_links


def has_external_or_http_links(raw_markdown_or_html: str) -> bool:
    """
    统一检测文本是否包含外部或可疑链接（供会员评论触发验证码与防绕过共用，覆盖 HTML 实体编码绕过）。
    """
    if not raw_markdown_or_html or not raw_markdown_or_html.strip():
        return False
    if _SUSPICIOUS_PROTOCOL_RE.search(raw_markdown_or_html):
        return True
    try:
        safe_html, external_links = inspect_links_and_sanitize(raw_markdown_or_html)
        return external_links > 0 or 'href="' in safe_html
    except ValidationError:
        return True


def sanitize_comment_for_tier(
    raw_markdown_or_html: str,
    *,
    tier: str = "trusted_member",
    internal_hosts: set[str] | None = None,
) -> tuple[str, int, list[str]]:
    """
    按评论发起人身份档位执行长度、链接白名单净化与风控判定。
    返回: (safe_html, external_link_count, risk_reasons)
    """
    max_chars, max_external_links, force_pending_threshold = get_tier_sanitizer_limits(tier)
    safe_html, external_links = inspect_links_and_sanitize(
        raw_markdown_or_html,
        internal_hosts=internal_hosts,
        max_chars=max_chars,
    )

    if tier == "guest":
        # 未登录访客严格禁止任何 HTTP/HTTPS 链接（含站内拼接 URL 与裸链接）
        if external_links > 0 or _SUSPICIOUS_PROTOCOL_RE.search(raw_markdown_or_html or "") or 'href="' in safe_html:
            raise ValidationError("未登录访客留言禁止包含任何链接或网址，如需分享链接请先登录会员。")

    if external_links > max_external_links:
        if tier == "new_member":
            raise ValidationError(f"新注册会员单条评论最多允许包含 {max_external_links} 个外部链接。")
        raise ValidationError(f"单条评论最多允许包含 {max_external_links} 个外部链接。")

    risk_reasons: list[str] = []
    if external_links >= force_pending_threshold:
        risk_reasons.append("external_links_threshold")

    return safe_html, external_links, risk_reasons
