"""
analytics.collector
公开上下文签发（只读核验）与 Redis Lua 无状态原子事件采集。

核心边界：
1. 上下文签发仅读 PostgreSQL 主库（绝不写 SQL），复核 Page 上线、未回收、非别名、未受阻断及 live_revision_id；
2. 事件采集通过单次 Redis Lua 脚本原子完成：30s PV 去重、单调 seq 防重放、阅读深度增量、活动增量 Hash 与 48h 固定过期 UV 集合；
3. Redis 不可用时优雅降级返回 {"accepted": False, "degraded": True}，绝不抛 500 或回退写 PostgreSQL。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from redis.exceptions import RedisError

from analytics.models import AnalyticsSettings
from analytics.tokens import (
    ANALYTICS_COOKIE_NAME,
    READING_PROOF_MAX_AGE,
    VISIT_TOKEN_MAX_AGE,
    is_valid_browser_id,
    match_safe_search_term,
    parse_referrer_url,
    sanitize_referrer,
    sign_reading_proof,
    sign_visit_token,
    verify_descriptor_token,
    verify_visit_token,
    visitor_hmac,
)

logger = logging.getLogger(__name__)

VISIT_AND_UV_TTL_SECONDS = 48 * 3600
ACTIVE_HASH_TTL_SECONDS = 7 * 24 * 3600
PV_DEDUP_TTL_SECONDS = 30
HEARTBEAT_INTERVAL_MS = 10_000
MAX_ACTIVE_SECONDS = 1800

_BOT_UA_RE = re.compile(
    r"(?:bot|crawl|spider|slurp|bingpreview|facebookexternalhit|whatsapp|telegrambot|"
    r"twitterbot|linkedinbot|discordbot|applebot|googlebot|baiduspider|yandexbot|"
    r"duckduckbot|semrushbot|ahrefsbot|mj12bot|dotbot|bytespider|petalbot|gptbot|"
    r"claudebot|anthropic-ai|ccbot|headlesschrome|phantomjs|python-requests|"
    r"curl/|wget/|httpx|aiohttp|scrapy)",
    re.IGNORECASE,
)
_SAFE_CONVERSION_KEY_RE = re.compile(r"^[a-z0-9_]{1,48}$")
_SAFE_CONVERSION_TARGET_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,64}$")
_SERVER_ONLY_CONVERSIONS = frozenset({"doc_download"})

# 单次原子 Redis Lua 脚本：完成防重放、30s PV 去重、阅读深度状态机、活动增量 Hash 与固定 48h TTL UV 集合写入
_RECORD_ANALYTICS_EVENT_LUA = """
local dedup_key = KEYS[1]
local visit_key = KEYS[2]
local active_pv_key = KEYS[3]
local active_scope_key = KEYS[4]
local uv_site_key = KEYS[5]
local uv_pages_idx_key = KEYS[6]
local uv_scopes_idx_key = KEYS[7]
local uv_page_key = KEYS[8]
local uv_type_key = KEYS[9]
local uv_entry_key = KEYS[10]
local uv_column_key = KEYS[11]

local event_kind = ARGV[1]
local seq = tonumber(ARGV[2])
local visitor_id = ARGV[3]
local visit_ttl = tonumber(ARGV[4])
local active_ttl = tonumber(ARGV[5])
local dedup_ttl = tonumber(ARGV[6])
local is_detail = ARGV[7]
local has_markers = ARGV[8]
local is_organic = ARGV[9]
local active_seconds = tonumber(ARGV[10])
local scroll_pct = tonumber(ARGV[11])
local reached_end = ARGV[12]
local short_article = ARGV[13]
local page_field_prefix = ARGV[14]
local page_idx_member = ARGV[15]
local type_key = ARGV[16]
local entry_kind = ARGV[17]
local column_id_str = ARGV[18]
local safe_search_term = ARGV[19]
local search_zero_result = ARGV[20]
local conversion_key = ARGV[21]
local use_uv_page = ARGV[22]
local use_uv_type = ARGV[23]
local use_uv_entry = ARGV[24]
local use_uv_column = ARGV[25]
local conversion_target = ARGV[27] or ''

local function set_fixed_ttl(k, ttl)
    if redis.call('TTL', k) == -1 then
        redis.call('EXPIRE', k, ttl)
    end
end

local function sadd_fixed_ttl(k, member, ttl)
    if member ~= '' then
        redis.call('SADD', k, member)
        set_fixed_ttl(k, ttl)
    end
end

local function hincr_fixed_ttl(k, field, inc, ttl)
    if inc > 0 then
        redis.call('HINCRBY', k, field, inc)
        set_fixed_ttl(k, ttl)
    end
end

local prev_seq = tonumber(redis.call('HGET', visit_key, 'last_seq'))
if prev_seq ~= nil and seq <= prev_seq then
    return {0, 'duplicate_seq'}
end
redis.call('HSET', visit_key, 'last_seq', seq)
set_fixed_ttl(visit_key, visit_ttl)

if event_kind == 'entry_click' then
    if entry_kind ~= '' then
        hincr_fixed_ttl(active_scope_key, 'entry|entry_clicks|' .. entry_kind, 1, active_ttl)
    end
    hincr_fixed_ttl(active_scope_key, 'site|entry_clicks|all', 1, active_ttl)
    return {1, 'entry_click'}
end

if event_kind == 'card_exposure' then
    return {1, 'card_exposure'}
end

if event_kind == 'conversion' then
    if is_detail == '1' and conversion_key ~= '' and page_field_prefix ~= '' then
        local tgt = conversion_target
        if tgt == '' then
            tgt = 'default'
        end
        local conv_seen_field = 'conv_seen:' .. conversion_key .. ':' .. tgt
        if redis.call('HEXISTS', visit_key, conv_seen_field) == 0 then
            redis.call('HSET', visit_key, conv_seen_field, 1)
            set_fixed_ttl(visit_key, visit_ttl)
            hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|conv:' .. conversion_key, 1, active_ttl)
            return {1, 'conversion'}
        end
        return {0, 'duplicate_conversion'}
    end
    return {0, 'ignored_conversion'}
end

local pv_counted = tonumber(redis.call('HGET', visit_key, 'pv_counted') or '0')
if pv_counted == 0 then
    redis.call('HSET', visit_key, 'pv_counted', 1)
    local dedup_ok = redis.call('SET', dedup_key, '1', 'EX', dedup_ttl, 'NX')
    if dedup_ok then
        hincr_fixed_ttl(active_scope_key, 'site|pv|all', 1, active_ttl)
        if is_detail == '1' and page_field_prefix ~= '' then
            hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|pv', 1, active_ttl)
            if is_organic == '1' then
                hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|organic_pv', 1, active_ttl)
            end
            if ARGV[26] ~= '' then
                hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|conv:src_' .. ARGV[26], 1, active_ttl)
            end
            if type_key ~= '' then
                hincr_fixed_ttl(active_scope_key, 'type|pv|' .. type_key, 1, active_ttl)
            end
            if column_id_str ~= '' then
                hincr_fixed_ttl(active_scope_key, 'column|pv|' .. column_id_str, 1, active_ttl)
            end
        else
            if entry_kind ~= '' then
                hincr_fixed_ttl(active_scope_key, 'entry|pv|' .. entry_kind, 1, active_ttl)
            end
            if safe_search_term ~= '' then
                if search_zero_result == '1' and safe_search_term ~= 'unknown' then
                    hincr_fixed_ttl(active_scope_key, 'safe_search_term|pv|zero:' .. safe_search_term, 1, active_ttl)
                else
                    hincr_fixed_ttl(active_scope_key, 'safe_search_term|pv|' .. safe_search_term, 1, active_ttl)
                end
            end
            if search_zero_result == '1' then
                hincr_fixed_ttl(active_scope_key, 'entry|pv|search_zero_result', 1, active_ttl)
            end
        end
    end

    sadd_fixed_ttl(uv_site_key, visitor_id, visit_ttl)
    sadd_fixed_ttl(uv_scopes_idx_key, 'site|all', visit_ttl)
    if use_uv_page == '1' then
        sadd_fixed_ttl(uv_page_key, visitor_id, visit_ttl)
        sadd_fixed_ttl(uv_pages_idx_key, page_idx_member, visit_ttl)
    end
    if use_uv_type == '1' and type_key ~= '' then
        sadd_fixed_ttl(uv_type_key, visitor_id, visit_ttl)
        sadd_fixed_ttl(uv_scopes_idx_key, 'type|' .. type_key, visit_ttl)
    end
    if use_uv_entry == '1' and entry_kind ~= '' then
        sadd_fixed_ttl(uv_entry_key, visitor_id, visit_ttl)
        sadd_fixed_ttl(uv_scopes_idx_key, 'entry|' .. entry_kind, visit_ttl)
    end
    if use_uv_column == '1' and column_id_str ~= '' then
        sadd_fixed_ttl(uv_column_key, visitor_id, visit_ttl)
        sadd_fixed_ttl(uv_scopes_idx_key, 'column|' .. column_id_str, visit_ttl)
    end
end

if is_detail == '1' and page_field_prefix ~= '' then
    local prev_active = tonumber(redis.call('HGET', visit_key, 'active_seconds') or '0')
    local new_active = math.max(prev_active, active_seconds)
    local delta_active = new_active - prev_active
    if delta_active > 0 then
        redis.call('HSET', visit_key, 'active_seconds', new_active)
        hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|total_active_seconds', delta_active, active_ttl)
    end

    local prev_scroll = tonumber(redis.call('HGET', visit_key, 'max_scroll_pct') or '0')
    local new_scroll = math.max(prev_scroll, scroll_pct)
    if new_scroll > prev_scroll then
        redis.call('HSET', visit_key, 'max_scroll_pct', new_scroll)
    end

    local prev_end = tonumber(redis.call('HGET', visit_key, 'reached_end') or '0')
    local new_end = (prev_end == 1 or reached_end == '1') and 1 or 0
    if new_end == 1 and prev_end == 0 then
        redis.call('HSET', visit_key, 'reached_end', 1)
    end

    if has_markers == '1' then
        local meas = tonumber(redis.call('HGET', visit_key, 'measurable_counted') or '0')
        if meas == 0 then
            redis.call('HSET', visit_key, 'measurable_counted', 1)
            hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|measurable_views', 1, active_ttl)
            if short_article == '1' then
                hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|conv:short_body', 1, active_ttl)
            end
        end

        if new_scroll >= 25 and tonumber(redis.call('HGET', visit_key, 's25_c') or '0') == 0 then
            redis.call('HSET', visit_key, 's25_c', 1)
            hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|conv:scroll_25', 1, active_ttl)
        end
        if new_scroll >= 50 and tonumber(redis.call('HGET', visit_key, 's50_c') or '0') == 0 then
            redis.call('HSET', visit_key, 's50_c', 1)
            hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|conv:scroll_50', 1, active_ttl)
        end
        if new_scroll >= 90 and tonumber(redis.call('HGET', visit_key, 's90_c') or '0') == 0 then
            redis.call('HSET', visit_key, 's90_c', 1)
            hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|conv:scroll_90', 1, active_ttl)
        end
        if new_end == 1 and tonumber(redis.call('HGET', visit_key, 'end_c') or '0') == 0 then
            redis.call('HSET', visit_key, 'end_c', 1)
            hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|conv:end_reached', 1, active_ttl)
        end

        local val_c = tonumber(redis.call('HGET', visit_key, 'valid_counted') or '0')
        if val_c == 0 and new_active >= 15 and (new_scroll >= 50 or new_end == 1 or short_article == '1') then
            redis.call('HSET', visit_key, 'valid_counted', 1)
            hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|valid_views', 1, active_ttl)
            val_c = 1
        end

        local comp_c = tonumber(redis.call('HGET', visit_key, 'completed_counted') or '0')
        if comp_c == 0 and new_end == 1 and new_active >= 15 then
            redis.call('HSET', visit_key, 'completed_counted', 1)
            hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|completed_views', 1, active_ttl)
        end

        local bnc_c = tonumber(redis.call('HGET', visit_key, 'bounce_counted') or '0')
        if bnc_c == 0 and event_kind == 'leave' and val_c == 0 and new_active < 10 and new_end == 0 and new_scroll < 30 then
            redis.call('HSET', visit_key, 'bounce_counted', 1)
            hincr_fixed_ttl(active_pv_key, page_field_prefix .. '|short_bounce_views', 1, active_ttl)
        end
    end
end

return {1, 'ok'}
"""


def _get_redis_client(*, write: bool = True):
    backend = getattr(cache, "_cache", None)
    if backend is not None and hasattr(backend, "get_client"):
        return backend.get_client(write=write)
    if backend is not None and hasattr(backend, "client"):
        return backend.client
    return None


def _resolve_prefix() -> str:
    prefix = getattr(settings, "REDIS_KEY_PREFIX", "") or settings.CACHES.get("default", {}).get("KEY_PREFIX", "myblog")
    return str(prefix or "myblog").strip().rstrip(":") or "myblog"


def is_bot_or_prefetch_request(request) -> bool:
    sec_purpose = (request.headers.get("Sec-Purpose") or request.META.get("HTTP_SEC_PURPOSE") or "").lower()
    purpose = (request.headers.get("Purpose") or request.META.get("HTTP_PURPOSE") or "").lower()
    x_purpose = (request.headers.get("X-Purpose") or request.META.get("HTTP_X_PURPOSE") or "").lower()
    x_moz = (request.headers.get("X-Moz") or request.META.get("HTTP_X_MOZ") or "").lower()

    if any(tok in val for val in (sec_purpose, purpose, x_moz) for tok in ("prefetch", "prerender")):
        return True
    if any(tok in x_purpose for tok in ("preview", "instant", "prefetch")):
        return True

    ua = request.META.get("HTTP_USER_AGENT", "") or ""
    if ua and _BOT_UA_RE.search(ua):
        return True
    return False


def resolve_page_analytics_spec(specific_page: Any):
    """从 discovery.registry 查找页面对应的 (AnalyticsCapabilities, type_key)。"""
    from discovery.registry import list_entry_analytics, list_type_adapters

    page_label = getattr(getattr(specific_page, "_meta", None), "label", "")
    if not page_label:
        return None, ""

    for adapter in list_type_adapters():
        if getattr(adapter, "model_label", "") == page_label:
            caps = getattr(adapter, "analytics_capabilities", None)
            return caps, getattr(adapter, "type_key", "")

    for spec in list_entry_analytics():
        if getattr(spec, "model_label", "") == page_label:
            return spec.analytics_capabilities, ""

    return None, ""


def resolve_column_page_id(specific_page: Any) -> int | None:
    """按注册表查找最近的上级栏目页面 ID（不硬编码具体模型类）。"""
    from discovery.registry import list_type_adapters

    entry_model_labels = {
        getattr(adapter, "model_label", "")
        for adapter in list_type_adapters()
        if getattr(getattr(adapter, "analytics_capabilities", None), "kind", "") == "entry"
    }
    if not entry_model_labels:
        return None

    try:
        ancestors = (
            specific_page.get_ancestors(inclusive=False)
            .select_related("content_type")
            .order_by("-depth")
        )
        for anc in ancestors:
            spec_cls = anc.specific_class
            label = getattr(getattr(spec_cls, "_meta", None), "label", "") if spec_cls else ""
            if label in entry_model_labels:
                return int(anc.id)
    except Exception as exc:
        logger.debug("Failed to resolve column_page_id for page %s: %s", getattr(specific_page, "id", None), exc)
    return None


def issue_analytics_context(request, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """
    核验公开上下文并签发 30 分钟有效 visit_token（只读操作，绝不写 SQL）。
    返回 (json_body, new_mb_avid_cookie_or_none)。
    """
    from wagtail.models import Page, Site
    from content_lifecycle.public_access import get_public_blocked_page_ids
    from discovery.registry import get_entry_analytics

    if is_bot_or_prefetch_request(request):
        return {"enabled": False, "reason": "bot_or_prefetch"}, None

    descriptor_token = payload.get("descriptor_token") or payload.get("descriptor") or ""
    claims = verify_descriptor_token(descriptor_token)
    if not claims:
        return {"enabled": False, "reason": "invalid_descriptor"}, None

    desc_site_id = claims.get("site_id")
    if not desc_site_id:
        return {"enabled": False, "reason": "invalid_site"}, None

    req_site = Site.find_for_request(request)
    site = Site.objects.filter(id=desc_site_id).first()
    if site is None:
        return {"enabled": False, "reason": "site_not_found"}, None
    if req_site is not None and req_site.id != site.id:
        return {"enabled": False, "reason": "site_mismatch"}, None

    ana_settings = AnalyticsSettings.get_for_site_readonly(site)
    if not ana_settings.collection_enabled:
        return {"enabled": False, "reason": "collection_disabled"}, None

    desc_kind = claims.get("kind")
    page_id = claims.get("page_id")
    live_revision_id = 0
    type_key = ""
    column_page_id = None
    entry_kind = str(claims.get("entry_kind") or "")[:64]
    entry_key = str(claims.get("entry_key") or "")[:128]
    has_body_markers = False
    allowed_conversions: list[str] = []
    safe_search_term = ""
    search_matched = False
    search_zero_result = False

    if page_id is not None:
        page = Page.objects.filter(id=page_id).select_related("live_revision", "content_type").first()
        if page is None:
            return {"enabled": False, "reason": "page_not_found"}, None
        if not page.live or page.alias_of_id is not None:
            return {"enabled": False, "reason": "not_live_or_alias"}, None
        specific_page = page.specific
        if getattr(specific_page, "is_recycled", False):
            return {"enabled": False, "reason": "recycled"}, None
        if page.id in get_public_blocked_page_ids([(page.id, page.path)]):
            return {"enabled": False, "reason": "blocked"}, None

        page_site = page.get_site() if hasattr(page, "get_site") else None
        if page_site is not None and page_site.id != site.id:
            return {"enabled": False, "reason": "site_mismatch"}, None

        caps, resolved_type_key = resolve_page_analytics_spec(specific_page)
        if caps is None or caps.kind != desc_kind:
            return {"enabled": False, "reason": "unsupported_type"}, None

        if caps.kind == "detail":
            current_rev = page.live_revision_id
            desc_rev = int(claims.get("live_revision_id") or 0)
            if not current_rev or current_rev != desc_rev:
                return {"enabled": False, "reason": "stale_revision"}, None
            live_revision_id = int(current_rev)
            type_key = resolved_type_key
            column_page_id = resolve_column_page_id(specific_page)
            markers = tuple(getattr(caps, "body_markers", ()) or ())
            has_body_markers = ("article-start" in markers and "article-end" in markers)
            allowed_conversions = list(getattr(caps, "conversion_keys", ()) or ()) + [
                k for k in (getattr(caps, "metric_keys", ()) or ())
                if k in ("code_copy", "mermaid_view")
            ]
            if "step_complete" in allowed_conversions and "step_reach" not in allowed_conversions:
                allowed_conversions.append("step_reach")
            if "doc_download" in allowed_conversions and "doc_attempt" not in allowed_conversions:
                allowed_conversions.append("doc_attempt")
        else:
            entry_kind = caps.entry_kind or entry_kind
    else:
        if desc_kind != "entry":
            return {"enabled": False, "reason": "invalid_kind"}, None
        spec = get_entry_analytics(entry_kind)
        if spec is None or spec.analytics_capabilities.kind != "entry":
            return {"enabled": False, "reason": "unsupported_entry"}, None
        if entry_kind == "search" and claims.get("search_query"):
            safe_search_term, search_matched = match_safe_search_term(claims["search_query"])
            search_total = claims.get("search_total")
            search_scoped = bool(claims.get("search_scoped", False))
            search_zero_result = bool(search_total == 0 and not search_scoped)

    existing_cookie = request.COOKIES.get(ANALYTICS_COOKIE_NAME, "")
    new_cookie_val = None
    if is_valid_browser_id(existing_cookie):
        browser_id = existing_cookie.strip()
    else:
        browser_id = uuid.uuid4().hex
        new_cookie_val = browser_id

    stat_date = timezone.localdate()
    v_hmac = visitor_hmac(site.id, stat_date, browser_id)

    current_host = ""
    try:
        current_host = request.get_host()
    except Exception:
        current_host = site.hostname or ""

    raw_ref = payload.get("referrer")
    if isinstance(raw_ref, str) and raw_ref.strip():
        ref_type, ref_source = parse_referrer_url(raw_ref, current_host)
    elif payload.get("referrer_host"):
        ref_type, ref_source = sanitize_referrer(
            str(payload.get("referrer_host") or ""),
            str(payload.get("referrer_path") or ""),
            current_host,
        )
    else:
        ref_type, ref_source = ("direct", "direct")

    source_placement = str(payload.get("source_placement") or "").strip().lower()
    is_organic = not bool(
        payload.get("from_curation")
        or payload.get("from_ranking")
        or source_placement in {"curated", "ranking", "carousel", "home_carousel", "home_guides", "home_projects"}
    )

    visit_claims = {
        "v": 1,
        "visit_id": uuid.uuid4().hex,
        "visitor_hmac": v_hmac,
        "site_id": int(site.id),
        "stat_date": stat_date.isoformat(),
        "kind": desc_kind,
        "page_id": int(page_id) if page_id is not None else None,
        "live_revision_id": int(live_revision_id),
        "type_key": type_key,
        "column_page_id": int(column_page_id) if column_page_id is not None else None,
        "entry_kind": entry_kind,
        "entry_key": entry_key,
        "referrer_type": ref_type,
        "referrer_source": ref_source,
        "is_organic": is_organic,
        "safe_search_term": safe_search_term,
        "search_matched": search_matched,
        "search_zero_result": search_zero_result,
        "has_body_markers": has_body_markers,
        "allowed_conversions": allowed_conversions[:16],
        "issued_at": int(time.time()),
    }
    visit_token = sign_visit_token(visit_claims)

    return {
        "enabled": True,
        "visit_token": visit_token,
        "kind": desc_kind,
        "heartbeat_interval_ms": HEARTBEAT_INTERVAL_MS,
        "max_age_seconds": VISIT_TOKEN_MAX_AGE,
    }, new_cookie_val


def _clamp_int(value: Any, low: int, high: int, default: int = 0) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


def _normalize_event_kind(raw_kind: Any) -> str:
    kind = str(raw_kind or "pageview").strip().lower()
    mapping = {
        "start": "pageview",
        "pageview": "pageview",
        "page_view": "pageview",
        "view": "pageview",
        "heartbeat": "heartbeat",
        "reading": "heartbeat",
        "progress": "heartbeat",
        "end": "leave",
        "leave": "leave",
        "page_leave": "leave",
        "unload": "leave",
        "finish": "leave",
        "card_exposure": "card_exposure",
        "entry_click": "entry_click",
        "conversion": "conversion",
    }
    return mapping.get(kind, "")


def _resolve_source_channel_key(visit_claims: dict[str, Any]) -> str:
    ref_type = str(visit_claims.get("referrer_type") or "direct")
    ref_src = str(visit_claims.get("referrer_source") or "direct")
    if ref_type == "internal":
        if ref_src in ("home", "search"):
            return ref_src
        if ref_src == "tag_index":
            return "tag"
        if ref_src == "author_detail":
            return "author"
        return "listing"
    if ref_type == "external":
        lowered = ref_src.lower()
        if any(s in lowered for s in ("google.", "bing.", "baidu.", "duckduckgo.", "sogou.", "so.com")):
            return "external_search"
        if any(s in lowered for s in ("github.com", "zhihu.com", "weibo.com", "x.com", "twitter.com", "v2ex.com", "reddit.com")):
            return "external_social"
        return "external_other"
    return "direct"


def record_analytics_events(
    visit_claims: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    redis_client: Any = None,
) -> dict[str, Any]:
    """
    执行单次原子 Redis Lua 脚本写入采集数据。
    Redis 不可用时优雅返回 {"accepted": False, "degraded": True}。
    """
    client = redis_client if redis_client is not None else _get_redis_client(write=True)
    if client is None:
        return {"accepted": False, "degraded": True}

    prefix = _resolve_prefix()
    site_id = int(visit_claims["site_id"])
    stat_date = str(visit_claims["stat_date"])
    visit_id = str(visit_claims["visit_id"])
    v_hmac = str(visit_claims["visitor_hmac"])
    kind = str(visit_claims.get("kind") or "entry")
    is_detail = "1" if kind == "detail" and visit_claims.get("page_id") is not None else "0"
    page_id = visit_claims.get("page_id")
    rev_id = int(visit_claims.get("live_revision_id") or 0)
    type_key = str(visit_claims.get("type_key") or "")
    column_page_id = visit_claims.get("column_page_id")
    column_id_str = str(int(column_page_id)) if column_page_id else ""
    entry_kind = str(visit_claims.get("entry_kind") or "")
    entry_key = str(visit_claims.get("entry_key") or "")
    is_organic = "1" if visit_claims.get("is_organic", True) else "0"
    safe_search_term = str(visit_claims.get("safe_search_term") or "")
    search_zero_result = "1" if visit_claims.get("search_zero_result") else "0"
    token_has_markers = bool(visit_claims.get("has_body_markers", False))
    allowed_conversions = set(visit_claims.get("allowed_conversions") or [])

    if is_detail == "1":
        target_dedup = f"p:{int(page_id)}"
        col_or_0 = column_id_str or "0"
        page_field_prefix = f"{int(page_id)}|{rev_id}|{type_key}|{col_or_0}"
        page_idx_member = page_field_prefix
        use_uv_page = "1"
        use_uv_type = "1" if type_key else "0"
        use_uv_entry = "0"
        use_uv_column = "1" if column_id_str else "0"
    else:
        if entry_kind == "search" and safe_search_term:
            target_dedup = f"e:search:{safe_search_term}:{search_zero_result}"
        else:
            target_dedup = f"e:{entry_kind}:{entry_key}"
        page_field_prefix = ""
        page_idx_member = ""
        use_uv_page = "0"
        use_uv_type = "0"
        use_uv_entry = "1" if entry_kind else "0"
        use_uv_column = "0"

    dedup_key = f"{prefix}:analytics:pv_dedup:{site_id}:{target_dedup}:{v_hmac}"
    visit_key = f"{prefix}:analytics:visit:{visit_id}"
    active_pv_key = f"{prefix}:analytics:active:pv:{site_id}:{stat_date}"
    active_scope_key = f"{prefix}:analytics:active:scope:{site_id}:{stat_date}"
    uv_site_key = f"{prefix}:analytics:uv:site:{site_id}:{stat_date}"
    uv_pages_idx_key = f"{prefix}:analytics:uv:pages_idx:{site_id}:{stat_date}"
    uv_scopes_idx_key = f"{prefix}:analytics:uv:scopes_idx:{site_id}:{stat_date}"
    uv_page_key = (
        f"{prefix}:analytics:uv:page:{site_id}:{stat_date}:{int(page_id)}:{rev_id}"
        if is_detail == "1"
        else f"{prefix}:analytics:uv:unused:page"
    )
    uv_type_key = (
        f"{prefix}:analytics:uv:type:{site_id}:{stat_date}:{type_key}"
        if use_uv_type == "1"
        else f"{prefix}:analytics:uv:unused:type"
    )
    uv_entry_key = (
        f"{prefix}:analytics:uv:entry:{site_id}:{stat_date}:{entry_kind}"
        if use_uv_entry == "1"
        else f"{prefix}:analytics:uv:unused:entry"
    )
    uv_column_key = (
        f"{prefix}:analytics:uv:column:{site_id}:{stat_date}:{column_id_str}"
        if use_uv_column == "1"
        else f"{prefix}:analytics:uv:unused:column"
    )

    keys = [
        dedup_key,
        visit_key,
        active_pv_key,
        active_scope_key,
        uv_site_key,
        uv_pages_idx_key,
        uv_scopes_idx_key,
        uv_page_key,
        uv_type_key,
        uv_entry_key,
        uv_column_key,
    ]

    processed = 0
    duplicates = 0
    try:
        script = client.register_script(_RECORD_ANALYTICS_EVENT_LUA)
        for idx, ev in enumerate(events):
            ev_kind = _normalize_event_kind(ev.get("event") or ev.get("event_type") or ev.get("type"))
            if not ev_kind:
                continue

            seq = _clamp_int(ev.get("seq", idx), 0, 10_000_000, default=idx)
            active_seconds = _clamp_int(
                ev.get("active_seconds", ev.get("active_sec", 0)),
                0,
                MAX_ACTIVE_SECONDS,
                default=0,
            )
            scroll_pct = _clamp_int(
                ev.get("max_scroll_pct", ev.get("scroll_pct", 0)),
                0,
                100,
                default=0,
            )
            reached_end = "1" if bool(ev.get("reached_end", False)) else "0"
            short_article = "1" if bool(ev.get("short_article", ev.get("is_short_body", False))) else "0"
            markers_missing = bool(ev.get("markers_missing", False)) or (ev.get("has_markers") is False)
            has_markers = "1" if (token_has_markers and not markers_missing) else "0"

            conv_key = ""
            conv_target = "default"
            if ev_kind == "conversion":
                raw_conv = str(ev.get("conversion_key") or "").strip().lower()
                if (
                    raw_conv not in _SERVER_ONLY_CONVERSIONS
                    and _SAFE_CONVERSION_KEY_RE.match(raw_conv)
                    and (not allowed_conversions or raw_conv in allowed_conversions)
                ):
                    conv_key = raw_conv
                    raw_tgt = str(ev.get("conversion_target") or ev.get("target_key") or "default").strip()
                    if raw_tgt and _SAFE_CONVERSION_TARGET_RE.match(raw_tgt):
                        conv_target = raw_tgt
                else:
                    continue

            args = [
                ev_kind,
                seq,
                v_hmac,
                VISIT_AND_UV_TTL_SECONDS,
                ACTIVE_HASH_TTL_SECONDS,
                PV_DEDUP_TTL_SECONDS,
                is_detail,
                has_markers,
                is_organic,
                active_seconds,
                scroll_pct,
                reached_end,
                short_article,
                page_field_prefix,
                page_idx_member,
                type_key,
                entry_kind,
                column_id_str,
                safe_search_term,
                search_zero_result,
                conv_key,
                use_uv_page,
                use_uv_type,
                use_uv_entry,
                use_uv_column,
                _resolve_source_channel_key(visit_claims),
                conv_target,
            ]
            res = script(keys=keys, args=args)
            status_flag = int(res[0]) if res else 0
            if status_flag == 1:
                processed += 1
            else:
                duplicates += 1
    except (RedisError, OSError, TimeoutError, ConnectionError) as exc:
        logger.warning("Analytics Redis write degraded: %s", exc)
        return {"accepted": False, "degraded": True}

    return {"accepted": True, "processed": processed, "duplicates": duplicates}


_SERVER_CONVERSION_LUA = """
local visit_key = KEYS[1]
local active_pv_key = KEYS[2]
local visit_ttl = tonumber(ARGV[1])
local active_ttl = tonumber(ARGV[2])
local conv_field = ARGV[3]
local seen_field = ARGV[4]

if redis.call('HEXISTS', visit_key, seen_field) == 0 then
    redis.call('HSET', visit_key, seen_field, 1)
    if redis.call('TTL', visit_key) == -1 then
        redis.call('EXPIRE', visit_key, visit_ttl)
    end
    redis.call('HINCRBY', active_pv_key, conv_field, 1)
    if redis.call('TTL', active_pv_key) == -1 then
        redis.call('EXPIRE', active_pv_key, active_ttl)
    end
    return 1
end
return 0
"""


def record_server_conversion(
    visit_claims: dict[str, Any],
    conversion_key: str,
    *,
    target_id: str = "default",
    redis_client: Any = None,
) -> bool:
    """
    服务端权威转化记账（用于 /media-gateway/ 文档 200/206 成功交付合并 Range 分段去重）。
    按 (visit_id, conversion_key, target_id) 原子去重；Redis 不可用时静默返回 False，绝不阻断交付。
    """
    if not isinstance(visit_claims, dict) or visit_claims.get("kind") != "detail":
        return False
    raw_conv = str(conversion_key or "").strip().lower()
    if not _SAFE_CONVERSION_KEY_RE.match(raw_conv):
        return False
    allowed = set(visit_claims.get("allowed_conversions") or [])
    if allowed and raw_conv not in allowed:
        return False
    page_id = visit_claims.get("page_id")
    rev_id = int(visit_claims.get("live_revision_id") or 0)
    if page_id is None or rev_id <= 0:
        return False
    raw_tgt = str(target_id or "default").strip()
    clean_tgt = raw_tgt if _SAFE_CONVERSION_TARGET_RE.match(raw_tgt) else "default"

    client = redis_client if redis_client is not None else _get_redis_client(write=True)
    if client is None:
        return False

    prefix = _resolve_prefix()
    site_id = int(visit_claims["site_id"])
    stat_date = str(visit_claims["stat_date"])
    visit_id = str(visit_claims["visit_id"])
    type_key = str(visit_claims.get("type_key") or "")
    col_id = visit_claims.get("column_page_id")
    col_or_0 = str(int(col_id)) if col_id else "0"
    page_field_prefix = f"{int(page_id)}|{rev_id}|{type_key}|{col_or_0}"
    conv_field = f"{page_field_prefix}|conv:{raw_conv}"
    seen_field = f"conv_seen:{raw_conv}:{clean_tgt}"
    visit_key = f"{prefix}:analytics:visit:{visit_id}"
    active_pv_key = f"{prefix}:analytics:active:pv:{site_id}:{stat_date}"
    try:
        script = client.register_script(_SERVER_CONVERSION_LUA)
        res = script(
            keys=[visit_key, active_pv_key],
            args=[VISIT_AND_UV_TTL_SECONDS, ACTIVE_HASH_TTL_SECONDS, conv_field, seen_field],
        )
        return bool(int(res or 0) == 1)
    except (RedisError, OSError, TimeoutError, ConnectionError) as exc:
        logger.warning("Server conversion write degraded: %s", exc)
        return False


def _decode_redis_hash(raw_map: dict[Any, Any] | None) -> dict[str, str]:
    decoded: dict[str, str] = {}
    if not raw_map:
        return decoded
    for k, v in raw_map.items():
        k_str = k.decode("utf-8", errors="ignore") if isinstance(k, (bytes, bytearray)) else str(k)
        v_str = v.decode("utf-8", errors="ignore") if isinstance(v, (bytes, bytearray)) else str(v)
        decoded[k_str] = v_str
    return decoded


def issue_reading_proof_for_visit(
    user,
    page_id: int,
    visit_token: str,
    *,
    site_id: int | None = None,
    is_short_body: bool = False,
    redis_client: Any = None,
) -> dict[str, Any]:
    """
    校验本次登录用户的阅读令牌与 Redis 阅读深度状态，满足门槛且非作者自评时签发 10 分钟有效的 reading_proof。
    门槛：active_seconds >= 15 且 (max_scroll_pct >= 50 或 reached_end == 1 或 valid_counted == 1 或 is_short_body)。
    """
    from wagtail.models import Page, Site
    from analytics.rating_service import is_user_page_author
    from content_lifecycle.public_access import get_public_blocked_page_ids

    if not user or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", True):
        return {"eligible": False, "reason": "authentication_required"}

    visit_claims = verify_visit_token(visit_token)
    if not visit_claims:
        return {"eligible": False, "reason": "invalid_visit_token"}

    if visit_claims.get("kind") != "detail" or visit_claims.get("page_id") is None:
        return {"eligible": False, "reason": "invalid_visit_kind"}

    try:
        target_page_id = int(page_id)
        claim_page_id = int(visit_claims["page_id"])
        claim_site_id = int(visit_claims["site_id"])
        claim_rev_id = int(visit_claims.get("live_revision_id") or 0)
    except (KeyError, TypeError, ValueError):
        return {"eligible": False, "reason": "invalid_visit_claims"}

    if claim_page_id != target_page_id:
        return {"eligible": False, "reason": "page_mismatch"}
    if site_id is not None and int(site_id) != claim_site_id:
        return {"eligible": False, "reason": "site_mismatch"}
    if not visit_claims.get("has_body_markers"):
        return {"eligible": False, "reason": "missing_body_markers"}

    site = Site.objects.filter(id=claim_site_id).first()
    if site is None:
        return {"eligible": False, "reason": "site_not_found"}

    page = Page.objects.filter(id=target_page_id).select_related("live_revision", "content_type").first()
    if page is None or not page.live or page.alias_of_id is not None:
        return {"eligible": False, "reason": "page_not_public"}

    specific_page = page.specific
    if getattr(specific_page, "is_recycled", False):
        return {"eligible": False, "reason": "recycled"}
    if page.id in get_public_blocked_page_ids([(page.id, page.path)]):
        return {"eligible": False, "reason": "blocked"}

    page_site = page.get_site() if hasattr(page, "get_site") else None
    if page_site is not None and int(page_site.id) != claim_site_id:
        return {"eligible": False, "reason": "site_mismatch"}

    if not page.live_revision_id or int(page.live_revision_id) != claim_rev_id:
        return {"eligible": False, "reason": "stale_revision"}

    ana_settings = AnalyticsSettings.get_for_site_readonly(site)
    if not ana_settings.rating_enabled:
        return {"eligible": False, "reason": "rating_disabled"}

    caps, _ = resolve_page_analytics_spec(specific_page)
    if caps is None or caps.kind != "detail" or not caps.rating_enabled:
        return {"eligible": False, "reason": "rating_unsupported"}

    if is_user_page_author(user, page):
        return {"eligible": False, "reason": "author_cannot_rate"}

    visit_id = str(visit_claims.get("visit_id") or "").strip()
    if not visit_id:
        return {"eligible": False, "reason": "invalid_visit_claims"}

    client = redis_client if redis_client is not None else _get_redis_client(write=False)
    if client is None:
        return {"eligible": False, "reason": "analytics_unavailable", "degraded": True}

    prefix = _resolve_prefix()
    visit_key = f"{prefix}:analytics:visit:{visit_id}"
    try:
        visit_state = _decode_redis_hash(client.hgetall(visit_key))
    except (RedisError, OSError, TimeoutError, ConnectionError) as exc:
        logger.warning("Reading proof Redis lookup error: %s", exc)
        return {"eligible": False, "reason": "analytics_unavailable", "degraded": True}

    active_seconds = _clamp_int(visit_state.get("active_seconds", 0), 0, MAX_ACTIVE_SECONDS, default=0)
    max_scroll_pct = _clamp_int(visit_state.get("max_scroll_pct", 0), 0, 100, default=0)
    reached_end = _clamp_int(visit_state.get("reached_end", 0), 0, 1, default=0)
    valid_counted = _clamp_int(visit_state.get("valid_counted", 0), 0, 1, default=0)

    threshold_met = active_seconds >= 15 and (
        max_scroll_pct >= 50 or reached_end == 1 or valid_counted == 1 or bool(is_short_body)
    )
    if not threshold_met:
        return {
            "eligible": False,
            "reason": "threshold_not_met",
            "active_seconds": active_seconds,
            "max_scroll_pct": max_scroll_pct,
        }

    evidence_cohort = "organic" if visit_claims.get("is_organic", True) else "promoted"
    proof_claims = {
        "v": 1,
        "user_id": int(user.id),
        "site_id": claim_site_id,
        "page_id": target_page_id,
        "live_revision_id": int(page.live_revision_id),
        "visit_id": visit_id,
        "evidence_cohort": evidence_cohort,
        "issued_at": int(time.time()),
    }
    token = sign_reading_proof(proof_claims)
    return {
        "eligible": True,
        "reading_proof": token,
        "expires_in": READING_PROOF_MAX_AGE,
    }
