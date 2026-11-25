"""
comments.sanitizer
评论专用 Markdown 解析、富媒体预算与安全净化器 (C-08 契约)。

严格防御 XSS 攻击与外部追踪像素注入：
1. 严禁 <img>, <video>, <audio>, <iframe>, <script>, <h1..h6> 等高危/破版标签；
2. 允许基础文本排版、安全表格（table/thead/tbody/tr/th/td）、围栏代码块与 Unicode Emoji；
3. 注册会员额外支持 KaTeX 数学公式（复用 content_blocks.rendering.math）与受限预算 Mermaid 图表；
4. 未登录访客严禁外链，且不解析公式与 Mermaid 图表；
5. 链接强制注入 rel="nofollow ugc noopener noreferrer"，仅允许 http/https 协议；
6. 注册会员统一 3,000 字 / 最多 5 外链（人人平等），访客限制 500 字 / 0 外链。
"""

import re
import textwrap
from uuid import uuid4
from urllib.parse import urlsplit
from django.core.exceptions import ValidationError
import nh3

from content_blocks.rendering.markdown_engine import (
    extract_fenced_mermaid_blocks,
    render_markdown_to_html,
)
from content_blocks.rendering.math import protect_math_in_markdown, restore_math_in_html
from content_blocks.rendering.mermaid import render_mermaid_dom
from content_blocks.rendering.table_content import enhance_markdown_tables_html

ALLOWED_COMMENT_TAGS = {
    "p", "br", "strong", "b", "em", "i", "s", "del",
    "code", "pre", "blockquote",
    "ul", "ol", "li",
    "a",
    "hr",
    "span",
    "table", "thead", "tbody", "tr", "th", "td",
}

ALLOWED_COMMENT_ATTRIBUTES = {
    "a": {"href", "title"},
    "code": {"class"},
    "span": {"class"},
    "table": {"class"},
    "th": {"align", "rowspan", "colspan", "scope"},
    "td": {"align", "rowspan", "colspan"},
}

ALLOWED_COMMENT_SCHEMES = {"http", "https"}
MAX_MEMBER_COMMENT_CHARS = 3000
MAX_MEMBER_EXTERNAL_LINKS = 5
MAX_GUEST_COMMENT_CHARS = 500
MAX_COMMENT_CODE_BLOCKS = 3
MAX_COMMENT_CODE_BLOCK_CHARS = 1500
MAX_COMMENT_MATH_COUNT = 10
MAX_COMMENT_MATH_CHARS = 500
MAX_COMMENT_MERMAID_COUNT = 1
MAX_COMMENT_MERMAID_CHARS = 800
MAX_COMMENT_MERMAID_EDGES = 30

_SAFE_CODE_LANG_CLASS_RE = re.compile(r"^language-[a-zA-Z0-9_+-]+$")
_FENCED_MERMAID_RE = re.compile(r"```mermaid[ \t]*\n([\s\S]*?)```", re.IGNORECASE)
_FENCED_CODE_RE = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\n[\s\S]*?```")
_HTML_TABLE_RE = re.compile(r"<table\b[^>]*>[\s\S]*?</table>", re.IGNORECASE)
_MERMAID_EDGE_RE = re.compile(r"(?:-->|---|-\.->|==>|-\.-|===|--x|--o|->>|-->>)")
_MERMAID_DIRECTIVE_RE = re.compile(r"%%\{[\s\S]*?\}%%")
_MERMAID_CLICK_RE = re.compile(r"^\s*click\s+.*$", re.IGNORECASE | re.MULTILINE)
_ALLOWED_MERMAID_HEADERS = (
    "flowchart",
    "graph",
    "sequencediagram",
    "statediagram",
    "statediagram-v2",
    "classdiagram",
    "erdiagram",
    "pie",
)
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_HTML_ANCHOR_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)
_HREF_ATTR_RE = re.compile(r'href\s*=\s*["\'][^"\']*["\']', re.IGNORECASE)
_BARE_HTTP_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+", re.IGNORECASE)
_SUSPICIOUS_PROTOCOL_RE = re.compile(r"https?://", re.IGNORECASE)


def _comment_attribute_filter(tag: str, attr: str, value: str) -> str | None:
    if tag == "code" and attr == "class":
        parts = [p for p in str(value or "").split() if _SAFE_CODE_LANG_CLASS_RE.match(p)]
        return parts[0] if parts else None
    if tag == "span" and attr == "class":
        allowed = {"md-task-box", "is-checked"}
        kept = [p for p in str(value or "").split() if p in allowed]
        return " ".join(kept) if "md-task-box" in kept else None
    if tag == "table" and attr == "class":
        # 剥离用户手写的任意 table class，交由后置 enhance_markdown_tables_html 统一注入 custom-table-block
        return None
    if tag in ("th", "td") and attr in ("rowspan", "colspan"):
        raw_val = str(value or "").strip()
        if not raw_val.isdigit():
            return None
        span_num = int(raw_val)
        return str(span_num) if 1 <= span_num <= 20 else None
    if tag == "th" and attr == "scope":
        norm = str(value or "").strip().lower()
        return norm if norm in ("row", "col", "rowgroup", "colgroup") else None
    if tag in ("th", "td") and attr == "align":
        norm = str(value or "").strip().lower()
        return norm if norm in ("left", "center", "right") else None
    return value


def get_tier_sanitizer_limits(tier: str) -> tuple[int, int, int]:
    """
    返回指定身份档位的内容安全约束：
    (max_chars, max_external_links, force_pending_links_threshold)
    """
    if tier == "guest":
        return MAX_GUEST_COMMENT_CHARS, 0, 1
    return MAX_MEMBER_COMMENT_CHARS, MAX_MEMBER_EXTERNAL_LINKS, 2


def validate_comment_length(raw_text: str, max_chars: int = MAX_MEMBER_COMMENT_CHARS) -> None:
    """校验评论字数上限（拒绝静默截断，B-05）"""
    if not raw_text or not raw_text.strip():
        raise ValidationError("评论正文不能为空。")
    if len(raw_text) > max_chars:
        raise ValidationError(f"评论长度超出限制（当前 {len(raw_text)} 字符，最多允许 {max_chars} 字符）。")


def _validate_and_extract_mermaid(text: str, nonce: str, *, allow_rich_embeds: bool) -> tuple[str, list[dict[str, str]]]:
    mermaid_items: list[dict[str, str]] = []

    def _handle_mermaid(raw_block: str, _idx: int) -> str:
        if not allow_rich_embeds:
            return f"```plaintext\n{raw_block}```\n"
        if len(mermaid_items) >= MAX_COMMENT_MERMAID_COUNT:
            raise ValidationError(f"单条评论最多支持 {MAX_COMMENT_MERMAID_COUNT} 张 Mermaid 图表。")
        raw_code = textwrap.dedent((raw_block or "").replace("\r\n", "\n").replace("\r", "\n")).strip()
        if not raw_code:
            raise ValidationError("Mermaid 代码块内容不能为空。")
        cleaned_code = _MERMAID_DIRECTIVE_RE.sub("", raw_code)
        cleaned_code = _MERMAID_CLICK_RE.sub("", cleaned_code).strip()
        if len(cleaned_code) > MAX_COMMENT_MERMAID_CHARS:
            raise ValidationError(
                f"评论内 Mermaid 源码超出限制（当前 {len(cleaned_code)} 字符，最多 {MAX_COMMENT_MERMAID_CHARS} 字符）。"
            )
        first_line = ""
        for line in cleaned_code.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("%%"):
                continue
            first_line = stripped.split()[0].lower()
            break
        if not first_line or not any(first_line.startswith(prefix) for prefix in _ALLOWED_MERMAID_HEADERS):
            raise ValidationError("评论区暂支持 flowchart / graph / sequenceDiagram / stateDiagram / classDiagram / erDiagram / pie 图表。")
        edge_count = len(_MERMAID_EDGE_RE.findall(cleaned_code))
        if edge_count > MAX_COMMENT_MERMAID_EDGES:
            raise ValidationError(f"评论内 Mermaid 连线数过多（{edge_count} 条，最多允许 {MAX_COMMENT_MERMAID_EDGES} 条）。")

        token = f"CBMERMAID_{nonce}_{len(mermaid_items)}_X"
        mermaid_items.append({"token": token, "code": cleaned_code})
        return f"\n\n{token}\n\n"

    replaced = extract_fenced_mermaid_blocks(text, on_mermaid=_handle_mermaid)
    return replaced, mermaid_items


def _validate_code_block_budget(text: str) -> None:
    blocks = _FENCED_CODE_RE.findall(text)
    if len(blocks) > MAX_COMMENT_CODE_BLOCKS:
        raise ValidationError(f"单条评论最多允许包含 {MAX_COMMENT_CODE_BLOCKS} 个代码块。")
    for blk in blocks:
        if len(blk) > MAX_COMMENT_CODE_BLOCK_CHARS:
            raise ValidationError(
                f"单个代码块超出长度限制（最多允许 {MAX_COMMENT_CODE_BLOCK_CHARS} 字符）。"
            )


def _extract_html_tables(text: str, nonce: str) -> tuple[str, list[dict[str, str]]]:
    """
    在关闭通用 raw HTML 的前提下，仅提取代码块之外的 <table>...</table> 复合表格，
    避免 <img>/<script> 等标签触发 CommonMark HTML-block 吞并紧随其后的 Markdown 链接/列表。
    提取出的表格仍完整经过 nh3 白名单清洗。
    """
    if not text or "<table" not in text.lower():
        return text, []

    code_stash: list[str] = []

    def _stash_code(m: re.Match) -> str:
        idx = len(code_stash)
        code_stash.append(m.group(0))
        return f"CBTBLCODE_{nonce}_{idx}_X"

    tmp = re.sub(r"```[\s\S]*?```", _stash_code, text)
    tmp = re.sub(r"`[^`\n]+`", _stash_code, tmp)

    table_items: list[dict[str, str]] = []

    def _stash_table(m: re.Match) -> str:
        token = f"CBTABLE_{nonce}_{len(table_items)}_X"
        table_items.append({"token": token, "html": m.group(0)})
        return f"\n\n{token}\n\n"

    tmp = _HTML_TABLE_RE.sub(_stash_table, tmp)

    for idx, raw_code in enumerate(code_stash):
        tmp = tmp.replace(f"CBTBLCODE_{nonce}_{idx}_X", raw_code)
    return tmp, table_items


def _restore_mermaid_in_html(html_str: str, mermaid_items: list[dict[str, str]]) -> str:
    if not html_str or not mermaid_items:
        return html_str
    res = html_str
    for item in mermaid_items:
        token = item["token"]
        dom = render_mermaid_dom(item["code"], title="评论图表", theme="follow-site")
        p_pattern = re.compile(r"<p>\s*" + re.escape(token) + r"\s*</p>")
        if p_pattern.search(res):
            res = p_pattern.sub(lambda _: dom, res)
        else:
            res = res.replace(token, dom)
    return res


def sanitize_comment_html(
    raw_markdown_or_html: str,
    max_chars: int = MAX_MEMBER_COMMENT_CHARS,
    *,
    allow_rich_embeds: bool = True,
) -> str:
    """对评论内容进行 Markdown 解析与严格白名单安全净化"""
    if not raw_markdown_or_html:
        return ""

    # 1. 校验长度
    validate_comment_length(raw_markdown_or_html, max_chars)

    normalized = raw_markdown_or_html.replace("\r\n", "\n").replace("\r", "\n")
    nonce = uuid4().hex[:12].upper()

    # 2. Mermaid 预算提取（在公式与 Markdown 扩展之前）
    prepared, mermaid_items = _validate_and_extract_mermaid(
        normalized,
        nonce,
        allow_rich_embeds=allow_rich_embeds,
    )

    # 3. 校验普通代码块数量与单块长度上限
    _validate_code_block_budget(prepared)

    # 4. 数学公式前置保护（仅登录会员开放）
    math_items = []
    table_items: list[dict[str, str]] = []
    if allow_rich_embeds:
        prepared, math_items = protect_math_in_markdown(prepared, nonce)
        if len(math_items) > MAX_COMMENT_MATH_COUNT:
            raise ValidationError(f"单条评论最多支持 {MAX_COMMENT_MATH_COUNT} 个数学公式。")
        for m_item in math_items:
            if len(m_item.get("tex") or "") > MAX_COMMENT_MATH_CHARS:
                raise ValidationError(f"单个数学公式长度不能超过 {MAX_COMMENT_MATH_CHARS} 字符。")
        prepared, table_items = _extract_html_tables(prepared, nonce)

    # 5. 使用全站统一 CommonMark/Typora 兼容引擎转换 Markdown 为 HTML（普通文本关闭原始 HTML）
    raw_html = render_markdown_to_html(prepared, allow_raw_html=False)

    # 5b. 将提取的复合 HTML 表格回填并交由下方 nh3 白名单严格净化
    for t_item in table_items:
        tok = t_item["token"]
        p_pat = re.compile(r"<p>\s*" + re.escape(tok) + r"\s*</p>")
        if p_pat.search(raw_html):
            raw_html = p_pat.sub(lambda _, h=t_item["html"]: h, raw_html)
        else:
            raw_html = raw_html.replace(tok, t_item["html"])

    # 6. nh3 严格白名单过滤与链接安全加固
    cleaned_html = nh3.clean(
        raw_html,
        tags=ALLOWED_COMMENT_TAGS,
        attributes=ALLOWED_COMMENT_ATTRIBUTES,
        attribute_filter=_comment_attribute_filter,
        url_schemes=ALLOWED_COMMENT_SCHEMES,
        link_rel="nofollow ugc noopener noreferrer",
    )

    # 7. 表格自适应包裹与安全样式注入（严禁评论区单元格渲染图片或附件下载）
    cleaned_html = enhance_markdown_tables_html(
        cleaned_html,
        allow_media=False,
        table_class="custom-table-block",
    )

    # 8. 在 nh3 与表格增强之后安全还原服务端转义后的公式与 Mermaid 占位容器
    if allow_rich_embeds:
        cleaned_html = restore_math_in_html(cleaned_html, math_items)
        cleaned_html = _restore_mermaid_in_html(cleaned_html, mermaid_items)
    return cleaned_html


def inspect_links_and_sanitize(
    raw_markdown_or_html: str,
    internal_hosts: set[str] | None = None,
    *,
    max_chars: int = MAX_MEMBER_COMMENT_CHARS,
    allow_rich_embeds: bool = True,
) -> tuple[str, int]:
    """
    安全净化评论 HTML 并统计外链数量 (B-05 / 反垃圾门禁)。
    返回: (safe_html, external_link_count)
    """
    safe_html = sanitize_comment_html(
        raw_markdown_or_html,
        max_chars=max_chars,
        allow_rich_embeds=allow_rich_embeds,
    )

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
    allow_rich_embeds = tier != "guest"
    safe_html, external_links = inspect_links_and_sanitize(
        raw_markdown_or_html,
        internal_hosts=internal_hosts,
        max_chars=max_chars,
        allow_rich_embeds=allow_rich_embeds,
    )

    if tier == "guest":
        # 未登录访客严格禁止任何 HTTP/HTTPS 链接（含站内拼接 URL 与裸链接）
        if external_links > 0 or _SUSPICIOUS_PROTOCOL_RE.search(raw_markdown_or_html or "") or 'href="' in safe_html:
            raise ValidationError("未登录访客留言禁止包含任何链接或网址，如需分享链接请先登录会员。")

    if external_links > max_external_links:
        raise ValidationError(f"单条评论最多允许包含 {max_external_links} 个外部链接。")

    risk_reasons: list[str] = []
    if external_links >= force_pending_threshold:
        risk_reasons.append("external_links_threshold")

    return safe_html, external_links, risk_reasons
