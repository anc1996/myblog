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


def validate_comment_length(raw_text: str, max_chars: int = 2000) -> None:
    """校验评论字数上限 (最大 2000 字符，拒绝静默截断，B-05)"""
    if not raw_text or not raw_text.strip():
        raise ValidationError("评论正文不能为空。")
    if len(raw_text) > max_chars:
        raise ValidationError(f"评论长度超出限制（当前 {len(raw_text)} 字符，最多允许 {max_chars} 字符）。")


def sanitize_comment_html(raw_markdown_or_html: str) -> str:
    """对评论内容进行 Markdown 解析与严格白名单安全净化"""
    if not raw_markdown_or_html:
        return ""

    # 1. 校验长度 (最大 2000 字符)
    validate_comment_length(raw_markdown_or_html, 2000)

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


def inspect_links_and_sanitize(raw_markdown_or_html: str, internal_hosts: set[str] | None = None) -> tuple[str, int]:
    """
    安全净化评论 HTML 并统计外链数量 (B-05 / 反垃圾门禁)。
    返回: (safe_html, external_link_count)
    若外链数 >= 2，调用方将自动标记进入人工待审队列。
    """
    safe_html = sanitize_comment_html(raw_markdown_or_html)

    href_pattern = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
    external_links = 0
    clean_hosts = internal_hosts or {"localhost", "127.0.0.1", "192.168.20.5"}

    for match in href_pattern.finditer(safe_html):
        href = match.group(1).strip()
        # 防御反斜杠与 userinfo 绕过 (B-05)
        if "\\" in href or href.startswith("//"):
            safe_html = safe_html.replace(match.group(0), 'href="#"')
            continue
        try:
            parts = urlsplit(href)
            if parts.username or parts.password:
                safe_html = safe_html.replace(match.group(0), 'href="#"')
                continue
            if parts.scheme in ("http", "https"):
                hostname = (parts.hostname or "").lower()
                if hostname not in clean_hosts:
                    external_links += 1
        except Exception:
            safe_html = safe_html.replace(match.group(0), 'href="#"')

    return safe_html, external_links
