"""
analytics.tokens
数据分析隐私保护、HMAC 签名令牌、来源脱敏与安全搜索词白名单匹配。

安全与隐私边界：
1. descriptor_token 与 visit_token 使用完全独立的盐值签名，互不混用；
2. visitor_hmac 使用每日轮换盐值（SECRET_KEY + site_id + stat_date），不保存原始 IP/UA/session_key/user_id；
3. sanitize_referrer 站外仅保留干净主域名（丢弃路径、查询串与片段），站内归类为入口大类；
4. match_safe_search_term 先拦截 PII/敏感正则，再比对白名单与公开标签/主题，未命中一律返回 ("unknown", False)。
"""

from __future__ import annotations

from datetime import date
import hashlib
import hmac
import ipaddress
import logging
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.db import DatabaseError
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

ANALYTICS_COOKIE_NAME = "mb_avid"
ANALYTICS_COOKIE_MAX_AGE = 48 * 3600
DESCRIPTOR_TOKEN_MAX_AGE = 12 * 3600
VISIT_TOKEN_MAX_AGE = 1800  # 30 分钟
READING_PROOF_MAX_AGE = 600  # 10 分钟

_DESCRIPTOR_SALT = "myblog.analytics.descriptor.v1"
_VISIT_SALT = "myblog.analytics.visit.v1"
_READING_PROOF_SALT = "myblog.analytics.reading_proof.v1"

_VALID_BROWSER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")

# D5-A 敏感信息与 PII 过滤正则（命中即拒绝，绝不进入白名单匹配或存储）
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"(?:https?://|ftp://|www\.)", re.IGNORECASE)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"(?:::|(?:[0-9a-fA-F]{1,4}:){2,}[0-9a-fA-F]{0,4})")
_PHONE_RE = re.compile(r"(?:\b1[3-9]\d{9}\b|(?:\+?\d[\d\s\-().]{6,}\d))")
_CN_ID_RE = re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b")
_SECRET_TOKEN_RE = re.compile(
    r"(?:sk-|pk_|ghp_|gho_|glpat-|xox[baprs]-|bearer\s+|token=|password=|passwd=|secret=|api[_-]?key=)",
    re.IGNORECASE,
)
_LONG_OPAQUE_RE = re.compile(r"[A-Za-z0-9_+/=-]{28,}")

# 常见双后缀公共后缀，用于外部来源主域名归并
_SECOND_LEVEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
    "com.hk", "com.tw", "co.jp", "ne.jp", "or.jp",
    "com.au", "net.au", "com.sg", "com.br",
})


def is_valid_browser_id(value: str | None) -> bool:
    return bool(value and _VALID_BROWSER_ID_RE.match(value.strip()))


def sign_descriptor_token(payload: dict) -> str:
    return signing.dumps(payload, salt=_DESCRIPTOR_SALT, compress=True)


def verify_descriptor_token(token: str, max_age: int = DESCRIPTOR_TOKEN_MAX_AGE) -> dict | None:
    if not token or not isinstance(token, str) or len(token) > 4096:
        return None
    try:
        data = signing.loads(token, salt=_DESCRIPTOR_SALT, max_age=max_age)
        return data if isinstance(data, dict) else None
    except signing.BadSignature:
        return None


def sign_visit_token(payload: dict) -> str:
    return signing.dumps(payload, salt=_VISIT_SALT, compress=True)


def verify_visit_token(token: str, max_age: int = VISIT_TOKEN_MAX_AGE) -> dict | None:
    if not token or not isinstance(token, str) or len(token) > 4096:
        return None
    try:
        data = signing.loads(token, salt=_VISIT_SALT, max_age=max_age)
        return data if isinstance(data, dict) else None
    except signing.BadSignature:
        return None


def sign_reading_proof(payload: dict) -> str:
    """使用独立盐值签发短期阅读完成凭证（默认 10 分钟有效）。"""
    return signing.dumps(payload, salt=_READING_PROOF_SALT, compress=True)


def verify_reading_proof(token: str, max_age: int = READING_PROOF_MAX_AGE) -> dict | None:
    if not token or not isinstance(token, str) or len(token) > 4096:
        return None
    try:
        data = signing.loads(token, salt=_READING_PROOF_SALT, max_age=max_age)
        return data if isinstance(data, dict) else None
    except signing.BadSignature:
        return None


def network_hmac(ip_str: str, stat_date: date | str | None = None) -> str:
    """
    使用每日轮换密钥生成不可逆网络摘要（HMAC-SHA256 截断 32 位十六进制）。
    严禁将明文 IP 写入 Redis 键或 PostgreSQL 业务表。
    """
    if stat_date is None:
        stat_date = timezone.localdate()
    date_str = stat_date.isoformat() if isinstance(stat_date, date) else str(stat_date)
    secret = str(getattr(settings, "SECRET_KEY", ""))
    daily_key = hashlib.sha256(
        f"{secret}:analytics-network-v1:{date_str}".encode("utf-8")
    ).digest()
    normalized_ip = (ip_str or "").strip()
    if normalized_ip:
        try:
            normalized_ip = str(ipaddress.ip_address(normalized_ip))
        except ValueError:
            pass
    return hmac.new(daily_key, normalized_ip.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def visitor_hmac(site_id: int, stat_date: date | str, browser_id: str) -> str:
    """
    使用每日轮换盐值生成不可跨日追踪的短期访客 HMAC 标识。
    结合 SECRET_KEY + site_id + stat_date，杜绝长期跨日画像与原始 ID 落库。
    """
    date_str = stat_date.isoformat() if isinstance(stat_date, date) else str(stat_date)
    secret = str(getattr(settings, "SECRET_KEY", ""))
    daily_key = hashlib.sha256(
        f"{secret}:analytics-visitor-v1:{int(site_id)}:{date_str}".encode("utf-8")
    ).digest()
    return hmac.new(daily_key, str(browser_id).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _normalize_hostname(raw_host: str) -> str:
    if not raw_host:
        return ""
    host = raw_host.strip().lower()
    # 去除可能附带的 userinfo 与端口
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if host.startswith("["):
        end = host.find("]")
        host = host[1:end] if end != -1 else host
    elif ":" in host:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def _extract_main_domain(hostname: str) -> str:
    if not hostname or not _SAFE_HOST_RE.match(hostname):
        return ""
    parts = hostname.split(".")
    if len(parts) <= 2:
        return hostname[:128]
    last_two = ".".join(parts[-2:])
    if last_two in _SECOND_LEVEL_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])[:128]
    return last_two[:128]


def sanitize_referrer(
    referrer_host: str,
    referrer_path: str,
    current_host: str,
) -> tuple[str, str]:
    """
    脱敏来源信息：
    - 空来源 -> ("direct", "direct")
    - 站内来源 -> ("internal", entry_kind_or_path_category)
    - 站外来源 -> ("external", main_domain)（严格丢弃路径、查询串与片段）
    """
    ref_host = _normalize_hostname(referrer_host or "")
    cur_host = _normalize_hostname(current_host or "")
    if not ref_host:
        return ("direct", "direct")

    if cur_host and ref_host == cur_host:
        path = (referrer_path or "/").split("?", 1)[0].split("#", 1)[0].strip().lower()
        if path in ("", "/", "/home/"):
            return ("internal", "home")
        if path.startswith("/search"):
            return ("internal", "search")
        if path.startswith("/tags"):
            return ("internal", "tag_index")
        if path.startswith("/authors"):
            return ("internal", "author_detail")
        if path.startswith("/timeline"):
            return ("internal", "timeline")
        if path.startswith("/guides"):
            return ("internal", "guide_index")
        if path.startswith("/projects") or path.startswith("/portfolio"):
            return ("internal", "portfolio_index")
        if path.startswith("/blog"):
            return ("internal", "blog_index")
        return ("internal", "internal_page")

    domain = _extract_main_domain(ref_host)
    if not domain:
        return ("external", "unknown")
    return ("external", domain)


def parse_referrer_url(referrer_url: str, current_host: str) -> tuple[str, str]:
    if not referrer_url or not isinstance(referrer_url, str):
        return ("direct", "direct")
    raw = referrer_url.strip()
    if not raw:
        return ("direct", "direct")
    try:
        parsed = urlsplit(raw)
    except Exception:
        return ("direct", "direct")
    if not parsed.netloc and raw.startswith("/"):
        return sanitize_referrer(current_host, parsed.path, current_host)
    return sanitize_referrer(parsed.netloc, parsed.path, current_host)


def _contains_sensitive_or_pii(term: str) -> bool:
    if _EMAIL_RE.search(term):
        return True
    if _URL_RE.search(term):
        return True
    if _IPV4_RE.search(term):
        return True
    if _IPV6_RE.search(term):
        return True
    if _CN_ID_RE.search(term):
        return True
    if _PHONE_RE.search(term):
        return True
    if _SECRET_TOKEN_RE.search(term):
        return True
    if _LONG_OPAQUE_RE.search(term):
        return True
    return False


def match_safe_search_term(raw_query: str) -> tuple[str, bool]:
    """
    D5-A 安全搜索词匹配：
    1. 归一化空白与长度检查（1..64），拦截邮箱/电话/IP/URL/Token 等 PII；
    2. 比对可配置白名单 ANALYTICS_SAFE_SEARCH_TERMS；
    3. 比对公开已发布的 taggit.Tag 规范词与已发布 GuideTopic；
    4. 未命中统一返回 ("unknown", False)，绝不把原词写入 Redis 或 SQL。
    """
    if not raw_query or not isinstance(raw_query, str):
        return ("unknown", False)

    normalized = " ".join(raw_query.strip().split())
    if not normalized or len(normalized) > 64:
        return ("unknown", False)

    if _contains_sensitive_or_pii(normalized):
        return ("unknown", False)

    norm_lower = normalized.lower()

    # 1. 静态配置白名单（无 SQL）
    configured_terms = getattr(settings, "ANALYTICS_SAFE_SEARCH_TERMS", ()) or ()
    for candidate in configured_terms:
        cand_str = " ".join(str(candidate).strip().split())
        if cand_str and cand_str.lower() == norm_lower:
            return (cand_str[:128], True)

    # 2. 公开已发布页面使用的标签词（严格排除未上线、回收站及祖先受限页）
    try:
        from content_lifecycle.public_access import get_public_blocked_page_ids
        from discovery.models import PublishedSearchTag

        tag_rows = list(
            PublishedSearchTag.objects.filter(
                document__is_live=True,
                document__is_recycled=False,
            )
            .filter(Q(tag_name__iexact=normalized) | Q(tag_slug__iexact=normalized))
            .select_related("document")
            .values_list("document__page_id", "document__root_path", "tag_name")[:20]
        )
        if tag_rows:
            blocked = get_public_blocked_page_ids([(pid, rpath) for pid, rpath, _ in tag_rows])
            for pid, _, tag_name in tag_rows:
                if pid not in blocked:
                    return (str(tag_name)[:128], True)
    except DatabaseError:
        pass
    except Exception as exc:
        logger.debug("Safe search tag lookup error: %s", exc)

    # 3. 公开已发布内容关联的主题词（extra_data.topic_name / topic_slug）与技术栈主语言（extra_data.primary_language）
    try:
        from content_lifecycle.public_access import get_public_blocked_page_ids
        from discovery.models import PublishedSearchDocument

        topic_rows = list(
            PublishedSearchDocument.objects.filter(
                is_live=True,
                is_recycled=False,
                index_generation=1,
            )
            .filter(
                Q(extra_data__topic_name__iexact=normalized)
                | Q(extra_data__topic_slug__iexact=normalized)
            )
            .values_list("page_id", "root_path", "extra_data")[:20]
        )
        if topic_rows:
            blocked = get_public_blocked_page_ids([(pid, rpath) for pid, rpath, _ in topic_rows])
            for pid, _, extra in topic_rows:
                topic_name = (extra or {}).get("topic_name")
                if pid not in blocked and topic_name:
                    return (str(topic_name)[:128], True)
    except DatabaseError:
        pass
    except Exception as exc:
        logger.debug("Safe search topic lookup error: %s", exc)

    # 4. 公开已发布项目的技术栈语言（通过 PublishedSearchDocument 统一查询，不直接导入具体 Page 模型）
    try:
        from content_lifecycle.public_access import get_public_blocked_page_ids
        from discovery.models import PublishedSearchDocument

        proj_rows = list(
            PublishedSearchDocument.objects.filter(
                is_live=True,
                is_recycled=False,
                index_generation=1,
                extra_data__primary_language__iexact=normalized,
            ).values_list("page_id", "root_path", "extra_data")[:20]
        )
        if proj_rows:
            blocked = get_public_blocked_page_ids([(pid, rpath) for pid, rpath, _ in proj_rows])
            for pid, _, extra in proj_rows:
                lang = (extra or {}).get("primary_language")
                if pid not in blocked and lang:
                    return (str(lang).strip()[:128], True)
    except DatabaseError:
        pass
    except Exception as exc:
        logger.debug("Safe search project language lookup error: %s", exc)

    return ("unknown", False)
