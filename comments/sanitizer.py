"""
comments.sanitizer
评论专用安全净化器 (C-08 契约)。

严格防御 XSS 攻击与外部追踪像素注入：
1. 严禁 <img>, <video>, <audio>, <table>, <div> 等富媒体标签，防止追踪像素与跨站伪造；
2. 仅允许基础文本排版标签：p, br, strong, b, em, i, code, pre, blockquote, ul, ol, li, a；
3. 链接强制注入 rel="nofollow ugc noopener noreferrer"，仅允许 http/https 协议；
4. 正文限制最大 5,000 字符。
"""

import markdown
import nh3

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


def sanitize_comment_html(raw_markdown_or_html: str) -> str:
    """对评论内容进行 Markdown 解析与严格白名单安全净化"""
    if not raw_markdown_or_html:
        return ""

    # 1. 限制输入长度防 DoS
    truncated = raw_markdown_or_html[:5000]

    # 2. Markdown 转换为 HTML
    raw_html = markdown.markdown(
        truncated,
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
