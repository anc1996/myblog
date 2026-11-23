"""
analytics.rating_service
第 2 期四档文章评价核心服务层（防刷限流、作者自评拦截、并发幂等事务与销户扣票联动）。

安全与并发边界：
1. 评价写限流采用独立 Redis Lua 原子滑动窗口（user_id:10/60s、user_id+page_id:4/60s、network_hmac:60/60s），
   Redis 不可用时严格 Fail-Closed 返回 rate_limit_unavailable（HTTP 503），不回退放行；
2. 评价真值表 ArticleRating 唯一约束为 (page, user)，并发首票在内层 savepoint 捕获 IntegrityError 后
   回滚保存点并重读返回 409，绝不把失败的首票误转为改票；
3. 跨版改投按 (page_id, site_id, live_revision_id) 升序锁定新旧两行 ArticleRatingAggregate，防止 AB-BA 死锁；
4. 撤销投票置 grade=None 保留原行，保持 first_eligible_day 与 trust_tier 不变，防撤票重投刷日期或刷层级；
5. 销户联动按 page_id 升序锁该用户全部 ArticleRating 及对应聚合行，精准扣减后删除，配合 PROTECT 外键守死一致性。
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import ipaddress
import json
import logging
import time
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from wagtail.models import Page, Revision, Site

from analytics.collector import resolve_page_analytics_spec
from analytics.models import (
    AnalyticsSettings,
    ArticleRating,
    ArticleRatingAggregate,
    ArticleRatingReceipt,
)
from analytics.tokens import network_hmac, verify_reading_proof
from content_lifecycle.public_access import get_public_blocked_page_ids
from people.attribution import (
    extract_raw_author_refs_from_page,
    extract_raw_author_refs_from_revision_content,
    resolve_author_attributions,
)
from people.models import Person

logger = logging.getLogger(__name__)

VALID_RATING_GRADES = frozenset({"excellent", "good", "pass", "poor"})
_GRADE_TO_COUNT_FIELD = {
    "excellent": "excellent_count",
    "good": "good_count",
    "pass": "pass_count",
    "poor": "poor_count",
}

RATING_RATE_LIMIT_WINDOW = 60
RATING_USER_LIMIT_60S = 10
RATING_USER_PAGE_LIMIT_60S = 4
RATING_NETWORK_LIMIT_60S = 60
RATING_RECEIPT_TTL_HOURS = 24

_RATING_RATE_LIMIT_LUA = """
local now_ts = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local member_id = ARGV[3]
local cutoff = now_ts - window

for i = 1, #KEYS do
    local k = KEYS[i]
    local limit = tonumber(ARGV[3 + i])
    redis.call('ZREMRANGEBYSCORE', k, '-inf', cutoff)
    local cnt = tonumber(redis.call('ZCARD', k) or '0')
    if cnt >= limit then
        local oldest = redis.call('ZRANGE', k, 0, 0, 'WITHSCORES')
        local retry = window
        if oldest and #oldest >= 2 then
            local oldest_ts = tonumber(oldest[2])
            if oldest_ts then
                local rem = math.ceil((oldest_ts + window) - now_ts)
                if rem > 0 and rem <= window then
                    retry = rem
                end
            end
        end
        return {0, retry, i}
    end
end

for i = 1, #KEYS do
    local k = KEYS[i]
    redis.call('ZADD', k, now_ts, member_id)
    redis.call('EXPIRE', k, window + 5)
end
return {1, 0, 0}
"""


class RatingError(ValidationError):
    """四档评价参数或业务规则校验失败。"""

    http_status = 400
    error_code = "invalid_rating_request"

    def __init__(self, message: str, *, error_code: str | None = None, http_status: int | None = None):
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code
        if http_status is not None:
            self.http_status = http_status


class RatingConflictError(RatingError):
    """幂等键冲突、乐观版本冲突或并发首票冲突（HTTP 409）。"""

    http_status = 409
    error_code = "rating_conflict"


class RatingPermissionError(PermissionDenied):
    """未登录、停用、未达阅读门槛、作者自评或功能未开启（HTTP 401/403）。"""

    http_status = 403
    error_code = "rating_forbidden"

    def __init__(self, message: str, *, error_code: str | None = None, http_status: int | None = None):
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code
        if http_status is not None:
            self.http_status = http_status


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


def _get_client_ip(request) -> str:
    """仅在直接来源属于受信代理时从右向左解析 X-Forwarded-For，防止伪造源 IP。"""
    trusted_proxies = set(getattr(settings, "TRUSTED_PROXIES", ["127.0.0.1", "192.168.20.5"]))
    remote_addr = (request.META.get("REMOTE_ADDR") or "127.0.0.1").strip()
    if remote_addr in trusted_proxies:
        x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded:
            candidates = [part.strip() for part in x_forwarded.split(",") if part.strip()]
            for candidate in reversed(candidates):
                if candidate in trusted_proxies:
                    continue
                try:
                    return str(ipaddress.ip_address(candidate))
                except ValueError:
                    continue
    return remote_addr


def check_rating_rate_limit(
    request,
    user,
    page_id: int,
    *,
    redis_client: Any = None,
) -> tuple[bool, int, str]:
    """
    四档评价独立 Redis Lua 原子滑动窗口限流（60s）：
    - user_id: 10 次
    - user_id + page_id: 4 次
    - network_hmac: 60 次（绝不以明文 IP 作为 Redis 键）
    Redis 不可用时严格 Fail-Closed 返回 (False, 30, "rate_limit_unavailable")。
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False, 0, "authentication_required"

    client_ip = _get_client_ip(request)
    net_digest = network_hmac(client_ip)
    prefix = _resolve_prefix()
    uid = int(user.id)
    pid = int(page_id)

    keys = [
        f"{prefix}:analytics:ratelimit:rating:user:{uid}",
        f"{prefix}:analytics:ratelimit:rating:user_page:{uid}:{pid}",
        f"{prefix}:analytics:ratelimit:rating:net:{net_digest}",
    ]
    limits = [
        RATING_USER_LIMIT_60S,
        RATING_USER_PAGE_LIMIT_60S,
        RATING_NETWORK_LIMIT_60S,
    ]

    now = time.time()
    member_id = f"{now}:{uuid4().hex[:8]}"

    try:
        r = redis_client if redis_client is not None else _get_redis_client(write=True)
        if r is None:
            logger.error("Redis unavailable for rating rate limit; failing closed.")
            return False, 30, "rate_limit_unavailable"
        res = r.eval(
            _RATING_RATE_LIMIT_LUA,
            len(keys),
            *keys,
            now,
            RATING_RATE_LIMIT_WINDOW,
            member_id,
            *limits,
        )
        allowed = int(res[0]) == 1
        if not allowed:
            retry_after = max(1, int(res[1])) if len(res) > 1 else RATING_RATE_LIMIT_WINDOW
            return False, retry_after, "rate_limited"
        return True, 0, ""
    except Exception as exc:
        logger.exception("Rating rate limit Redis exception (Fail-Closed): %s", exc)
        return False, 30, "rate_limit_unavailable"


def is_user_page_author(user, page: Page | Any) -> bool:
    """
    判断用户是否为该页面的作者/归属人（防作者自评）：
    1. Page.owner_id == user.id；
    2. 当前 live_revision 解析出的 PublishedAuthorRef 对应 Person.linked_user_id == user.id。
    """
    if not user or not getattr(user, "is_authenticated", False) or page is None:
        return False
    if getattr(page, "owner_id", None) is not None and int(page.owner_id) == int(user.id):
        return True

    live_rev = getattr(page, "live_revision", None)
    if live_rev is None and getattr(page, "live_revision_id", None):
        live_rev = Revision.objects.filter(id=page.live_revision_id).first()
    if live_rev is None:
        return False

    raw_refs = extract_raw_author_refs_from_revision_content(getattr(live_rev, "content", None))
    if not raw_refs:
        try:
            snapshot = live_rev.as_object()
            raw_refs = extract_raw_author_refs_from_page(snapshot)
        except Exception:
            raw_refs = []

    resolved_authors = resolve_author_attributions(
        raw_refs,
        revision_id=live_rev.id,
        allow_anonymous=False,
    )
    person_ids = [a.person_id for a in resolved_authors if not a.is_anonymous]
    if not person_ids:
        return False

    return Person.objects.filter(
        id__in=person_ids,
        linked_user_id=user.id,
        is_system_anonymous=False,
    ).exists()


def _resolve_site_for_page(page: Page, explicit_site: Site | None = None) -> Site | None:
    if explicit_site is not None:
        return explicit_site
    if page is not None and hasattr(page, "get_site"):
        try:
            site = page.get_site()
            if site is not None:
                return site
        except Exception:
            pass
    return Site.objects.filter(is_default_site=True).order_by("id").first() or Site.objects.order_by("id").first()


def _is_page_public_for_analytics(page: Page) -> bool:
    if page is None or not page.live or page.alias_of_id is not None:
        return False
    specific_page = page.specific
    if getattr(specific_page, "is_recycled", False):
        return False
    if page.id in get_public_blocked_page_ids([(page.id, page.path)]):
        return False
    return True


def get_rating_state(user, page: Page, site: Site | None = None) -> dict[str, Any]:
    """
    只读返回当前访问者在该页面的评价状态，绝不写 SQL，也绝不泄露总票数与他人评价。
    """
    resolved_site = _resolve_site_for_page(page, site)
    ana_settings = AnalyticsSettings.get_for_site_readonly(resolved_site)
    specific_page = page.specific if hasattr(page, "specific") else page
    caps, _ = resolve_page_analytics_spec(specific_page)
    caps_enabled = bool(caps is not None and caps.kind == "detail" and caps.rating_enabled)
    rating_enabled = bool(ana_settings.rating_enabled and caps_enabled)
    is_public = _is_page_public_for_analytics(page)
    live_revision_id = int(getattr(page, "live_revision_id", 0) or 0) if is_public else 0

    authenticated = bool(user and getattr(user, "is_authenticated", False) and getattr(user, "is_active", True))
    if not authenticated:
        return {
            "authenticated": False,
            "can_rate": False,
            "is_author": False,
            "rating_enabled": rating_enabled,
            "grade": None,
            "version": 0,
            "live_revision_id": live_revision_id,
        }

    is_author = is_user_page_author(user, page)
    existing = (
        ArticleRating.objects.filter(page_id=page.id, user_id=user.id)
        .values("grade", "version")
        .first()
    )
    grade = existing["grade"] if existing else None
    version = int(existing["version"]) if existing else 0
    can_rate = bool(rating_enabled and is_public and live_revision_id > 0 and not is_author)

    return {
        "authenticated": True,
        "can_rate": can_rate,
        "is_author": is_author,
        "rating_enabled": rating_enabled,
        "grade": grade,
        "version": version,
        "live_revision_id": live_revision_id,
    }


def _compute_rating_request_hash(
    *,
    site_id: int,
    page_id: int,
    user_id: int,
    target_grade: str | None,
    expected_revision_id: int,
    expected_version: int,
) -> str:
    canonical = json.dumps(
        {
            "site_id": int(site_id),
            "page_id": int(page_id),
            "user_id": int(user_id),
            "target_grade": target_grade,
            "expected_revision_id": int(expected_revision_id),
            "expected_version": int(expected_version),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _lock_or_create_aggregates(
    keys: list[tuple[int, int, int]],
) -> dict[tuple[int, int, int], ArticleRatingAggregate]:
    """
    按 (page_id, site_id, live_revision_id) 严格升序锁定（必要时 savepoint 创建）聚合行，
    彻底消除跨版改投或并发首票的死锁与唯一键冲突。
    """
    sorted_keys = sorted(
        {(int(s_id), int(p_id), int(rev_id)) for (s_id, p_id, rev_id) in keys},
        key=lambda item: (item[1], item[0], item[2]),
    )
    locked: dict[tuple[int, int, int], ArticleRatingAggregate] = {}
    for s_id, p_id, rev_id in sorted_keys:
        agg = (
            ArticleRatingAggregate.objects.select_for_update()
            .filter(site_id=s_id, page_id=p_id, live_revision_id=rev_id)
            .first()
        )
        if agg is None:
            try:
                with transaction.atomic():
                    agg = ArticleRatingAggregate.objects.create(
                        site_id=s_id,
                        page_id=p_id,
                        live_revision_id=rev_id,
                        excellent_count=0,
                        good_count=0,
                        pass_count=0,
                        poor_count=0,
                        rating_evidence_version=1,
                    )
            except IntegrityError:
                agg = ArticleRatingAggregate.objects.select_for_update().get(
                    site_id=s_id,
                    page_id=p_id,
                    live_revision_id=rev_id,
                )
        locked[(s_id, p_id, rev_id)] = agg
    return locked


def _apply_aggregate_delta(
    agg: ArticleRatingAggregate,
    *,
    dec_grade: str | None = None,
    inc_grade: str | None = None,
) -> None:
    if dec_grade == inc_grade:
        return
    changed_fields = ["rating_evidence_version", "updated_at"]
    if dec_grade and dec_grade in _GRADE_TO_COUNT_FIELD:
        dec_field = _GRADE_TO_COUNT_FIELD[dec_grade]
        setattr(agg, dec_field, max(0, int(getattr(agg, dec_field)) - 1))
        changed_fields.append(dec_field)
    if inc_grade and inc_grade in _GRADE_TO_COUNT_FIELD:
        inc_field = _GRADE_TO_COUNT_FIELD[inc_grade]
        setattr(agg, inc_field, int(getattr(agg, inc_field)) + 1)
        changed_fields.append(inc_field)
    agg.rating_evidence_version = int(agg.rating_evidence_version) + 1
    agg.save(update_fields=changed_fields)


def _resolve_initial_trust_tier(user) -> str:
    joined_at = getattr(user, "date_joined", None)
    if joined_at is None:
        return "member"
    if timezone.is_naive(joined_at):
        joined_at = timezone.make_aware(joined_at, timezone.get_current_timezone())
    if (timezone.now() - joined_at) < timedelta(days=7):
        return "new_member"
    return "member"


def submit_rating(
    user,
    page: Page | int,
    *,
    target_grade: str | None,
    expected_revision_id: int,
    expected_version: int,
    request_id: str,
    reading_proof: str = "",
    site: Site | None = None,
) -> dict[str, Any]:
    """
    提交、切换或撤销四档文章评价（强一致局部行锁 + 请求幂等 + 跨版联动）。
    """
    if not user or not getattr(user, "is_authenticated", False):
        raise RatingPermissionError(
            "请先登录后再参与文章评价。",
            error_code="authentication_required",
            http_status=401,
        )
    if not getattr(user, "is_active", True):
        raise RatingPermissionError("当前账号已停用，无法操作评价。", error_code="account_inactive", http_status=403)

    page_id = page.id if isinstance(page, Page) else int(page)
    db_page = Page.objects.filter(id=page_id).select_related("live_revision", "content_type").first()
    if db_page is None:
        raise RatingError("页面不存在或已被彻底删除。", error_code="page_not_found", http_status=404)

    if target_grade == "":
        target_grade = None
    if target_grade is not None and target_grade not in VALID_RATING_GRADES:
        raise RatingError("无效的评价档位。", error_code="invalid_grade", http_status=400)

    clean_request_id = str(request_id or "").strip()
    if not clean_request_id or len(clean_request_id) > 64:
        raise RatingError("缺少或非法的幂等标识 request_id。", error_code="invalid_request_id", http_status=400)

    try:
        exp_ver = int(expected_version)
    except (TypeError, ValueError):
        raise RatingError("expected_version 必须为非负整数。", error_code="invalid_expected_version", http_status=400)
    if exp_ver < 0:
        raise RatingError("expected_version 不能为负数。", error_code="invalid_expected_version", http_status=400)

    try:
        exp_rev_id = int(expected_revision_id or 0)
    except (TypeError, ValueError):
        raise RatingError("expected_revision_id 必须为整数。", error_code="invalid_expected_revision", http_status=400)

    resolved_site = _resolve_site_for_page(db_page, site)
    if resolved_site is None:
        raise RatingError("系统尚未配置默认站点。", error_code="site_not_found", http_status=400)

    req_hash = _compute_rating_request_hash(
        site_id=resolved_site.id,
        page_id=db_page.id,
        user_id=user.id,
        target_grade=target_grade,
        expected_revision_id=exp_rev_id,
        expected_version=exp_ver,
    )

    cohort = "organic"
    current_rev_id = int(db_page.live_revision_id or 0)

    if target_grade is not None:
        # 非空投票必须核验页面公开资格、站点评价开关、版本一致性、作者身份与阅读凭证
        if not _is_page_public_for_analytics(db_page):
            raise RatingPermissionError("当前页面未公开发布，暂不可评价。", error_code="page_not_public", http_status=403)

        page_site = db_page.get_site() if hasattr(db_page, "get_site") else None
        if page_site is not None and int(page_site.id) != int(resolved_site.id):
            raise RatingPermissionError("站点上下文与页面所属站点不一致。", error_code="site_mismatch", http_status=403)

        ana_settings = AnalyticsSettings.get_for_site_readonly(resolved_site)
        if not ana_settings.rating_enabled:
            raise RatingPermissionError("当前站点尚未开启文章评价功能。", error_code="rating_disabled", http_status=403)

        specific_page = db_page.specific
        caps, _ = resolve_page_analytics_spec(specific_page)
        if caps is None or caps.kind != "detail" or not caps.rating_enabled:
            raise RatingPermissionError("当前内容类型未启用文章评价。", error_code="rating_unsupported", http_status=403)

        if current_rev_id <= 0 or exp_rev_id != current_rev_id:
            raise RatingConflictError("文章已发布新版本，请刷新页面后再评价。", error_code="stale_revision")

        if is_user_page_author(user, db_page):
            raise RatingPermissionError("作者本人不可对自己的文章进行评价。", error_code="author_cannot_rate", http_status=403)

        proof_claims = verify_reading_proof(reading_proof or "")
        if (
            not proof_claims
            or int(proof_claims.get("user_id") or 0) != int(user.id)
            or int(proof_claims.get("site_id") or 0) != int(resolved_site.id)
            or int(proof_claims.get("page_id") or 0) != int(db_page.id)
            or int(proof_claims.get("live_revision_id") or 0) != current_rev_id
        ):
            raise RatingPermissionError(
                "阅读时长或正文进度未达标，请完成阅读后再评价。",
                error_code="invalid_reading_proof",
                http_status=403,
            )
        raw_cohort = str(proof_claims.get("evidence_cohort") or "organic").strip()
        cohort = raw_cohort[:16] if raw_cohort else "organic"

    receipt_expires_at = timezone.now() + timedelta(hours=RATING_RECEIPT_TTL_HOURS)

    with transaction.atomic():
        # 1. 先检查请求幂等收据
        receipt = ArticleRatingReceipt.objects.filter(
            page_id=db_page.id,
            user_id=user.id,
            request_id=clean_request_id,
        ).first()
        if receipt is not None:
            if receipt.request_hash != req_hash:
                raise RatingConflictError(
                    "幂等键冲突：相同 request_id 不允许提交不同的评价参数。",
                    error_code="idempotency_conflict",
                )
            current_rating = ArticleRating.objects.filter(page_id=db_page.id, user_id=user.id).first()
            return {
                "ok": True,
                "idempotent": True,
                "grade": current_rating.grade if current_rating else None,
                "version": int(receipt.result_version),
                "live_revision_id": int(current_rating.live_revision_id) if current_rating else exp_rev_id,
            }

        # 2. 锁定当前用户在当前页面的评价行
        rating = (
            ArticleRating.objects.select_for_update()
            .filter(page_id=db_page.id, user_id=user.id)
            .first()
        )

        if rating is None:
            if target_grade is None:
                if exp_ver != 0:
                    raise RatingConflictError("评价状态已变更，请刷新后重试。", error_code="version_conflict")
                raise RatingError("当前尚未提交过评价，无需撤销。", error_code="no_existing_rating", http_status=400)

            if exp_ver != 0:
                raise RatingConflictError("评价版本不匹配，请刷新页面后重试。", error_code="version_conflict")

            trust_tier = _resolve_initial_trust_tier(user)
            first_day = timezone.localdate()

            # 在内层保存点尝试插入首票，捕获并发首票 IntegrityError 回滚保存点并重读返回 409
            try:
                with transaction.atomic():
                    rating = ArticleRating.objects.create(
                        site_id=resolved_site.id,
                        page_id=db_page.id,
                        user_id=user.id,
                        live_revision_id=current_rev_id,
                        grade=target_grade,
                        trust_tier=trust_tier,
                        first_eligible_day=first_day,
                        evidence_cohort=cohort,
                        version=1,
                    )
            except IntegrityError:
                concurrent_receipt = ArticleRatingReceipt.objects.filter(
                    page_id=db_page.id,
                    user_id=user.id,
                    request_id=clean_request_id,
                ).first()
                if concurrent_receipt is not None:
                    if concurrent_receipt.request_hash != req_hash:
                        raise RatingConflictError(
                            "幂等键冲突：相同 request_id 不允许提交不同的评价参数。",
                            error_code="idempotency_conflict",
                        )
                    current_rating = ArticleRating.objects.filter(page_id=db_page.id, user_id=user.id).first()
                    return {
                        "ok": True,
                        "idempotent": True,
                        "grade": current_rating.grade if current_rating else None,
                        "version": int(concurrent_receipt.result_version),
                        "live_revision_id": int(current_rating.live_revision_id) if current_rating else current_rev_id,
                    }
                _ = (
                    ArticleRating.objects.select_for_update()
                    .filter(page_id=db_page.id, user_id=user.id)
                    .first()
                )
                raise RatingConflictError("检测到并发评价提交，请刷新后重试。", error_code="concurrent_first_rating")

            agg_key = (resolved_site.id, db_page.id, current_rev_id)
            aggs = _lock_or_create_aggregates([agg_key])
            _apply_aggregate_delta(aggs[agg_key], dec_grade=None, inc_grade=target_grade)

            ArticleRatingReceipt.objects.create(
                page_id=db_page.id,
                user_id=user.id,
                request_id=clean_request_id,
                request_hash=req_hash,
                result_version=int(rating.version),
                expires_at=receipt_expires_at,
            )
            return {
                "ok": True,
                "idempotent": False,
                "grade": rating.grade,
                "version": int(rating.version),
                "live_revision_id": int(rating.live_revision_id),
                "trust_tier": rating.trust_tier,
                "first_eligible_day": rating.first_eligible_day.isoformat(),
            }

        # 已有记录：核对乐观锁版本
        if int(rating.version) != exp_ver:
            concurrent_receipt = ArticleRatingReceipt.objects.filter(
                page_id=db_page.id,
                user_id=user.id,
                request_id=clean_request_id,
            ).first()
            if concurrent_receipt is not None:
                if concurrent_receipt.request_hash != req_hash:
                    raise RatingConflictError(
                        "幂等键冲突：相同 request_id 不允许提交不同的评价参数。",
                        error_code="idempotency_conflict",
                    )
                return {
                    "ok": True,
                    "idempotent": True,
                    "grade": rating.grade,
                    "version": int(concurrent_receipt.result_version),
                    "live_revision_id": int(rating.live_revision_id),
                }
            raise RatingConflictError("评价状态已被更新，请刷新后重试。", error_code="version_conflict")

        old_grade = rating.grade
        old_site_id = int(rating.site_id)
        old_rev_id = int(rating.live_revision_id)

        if target_grade is None:
            # 撤销评价：保留行、保持 first_eligible_day 与 trust_tier 不变，仅置 grade=None 并递增 version
            if old_grade is not None:
                old_key = (old_site_id, db_page.id, old_rev_id)
                aggs = _lock_or_create_aggregates([old_key])
                _apply_aggregate_delta(aggs[old_key], dec_grade=old_grade, inc_grade=None)
                rating.grade = None
                rating.version = int(rating.version) + 1
                rating.save(update_fields=["grade", "version", "updated_at"])
        else:
            new_site_id = int(resolved_site.id)
            new_rev_id = current_rev_id
            if old_grade == target_grade and old_site_id == new_site_id and old_rev_id == new_rev_id:
                # 同版同档无真实变更，不递增版本号
                pass
            elif old_grade is None:
                # 撤销后重新投票：锁定新版本聚合行并累加，仍保持首次投票时的 trust_tier 与 first_eligible_day
                new_key = (new_site_id, db_page.id, new_rev_id)
                aggs = _lock_or_create_aggregates([new_key])
                _apply_aggregate_delta(aggs[new_key], dec_grade=None, inc_grade=target_grade)
                rating.site_id = new_site_id
                rating.live_revision_id = new_rev_id
                rating.grade = target_grade
                rating.evidence_cohort = cohort
                rating.version = int(rating.version) + 1
                rating.save(update_fields=["site", "live_revision_id", "grade", "evidence_cohort", "version", "updated_at"])
            elif (old_site_id, old_rev_id) == (new_site_id, new_rev_id):
                # 同版切换档位：旧 -1、新 +1
                same_key = (new_site_id, db_page.id, new_rev_id)
                aggs = _lock_or_create_aggregates([same_key])
                _apply_aggregate_delta(aggs[same_key], dec_grade=old_grade, inc_grade=target_grade)
                rating.grade = target_grade
                rating.evidence_cohort = cohort
                rating.version = int(rating.version) + 1
                rating.save(update_fields=["grade", "evidence_cohort", "version", "updated_at"])
            else:
                # 跨版改投：按 (page_id, site_id, live_revision_id) 升序同时锁新旧两行聚合，扣旧加新
                old_key = (old_site_id, db_page.id, old_rev_id)
                new_key = (new_site_id, db_page.id, new_rev_id)
                aggs = _lock_or_create_aggregates([old_key, new_key])
                _apply_aggregate_delta(aggs[old_key], dec_grade=old_grade, inc_grade=None)
                _apply_aggregate_delta(aggs[new_key], dec_grade=None, inc_grade=target_grade)
                rating.site_id = new_site_id
                rating.live_revision_id = new_rev_id
                rating.grade = target_grade
                rating.evidence_cohort = cohort
                rating.version = int(rating.version) + 1
                rating.save(update_fields=["site", "live_revision_id", "grade", "evidence_cohort", "version", "updated_at"])

        ArticleRatingReceipt.objects.create(
            page_id=db_page.id,
            user_id=user.id,
            request_id=clean_request_id,
            request_hash=req_hash,
            result_version=int(rating.version),
            expires_at=receipt_expires_at,
        )
        return {
            "ok": True,
            "idempotent": False,
            "grade": rating.grade,
            "version": int(rating.version),
            "live_revision_id": int(rating.live_revision_id),
            "trust_tier": rating.trust_tier,
            "first_eligible_day": rating.first_eligible_day.isoformat(),
        }


def deduct_user_ratings_for_account_lifecycle(user_id: int) -> dict[str, int]:
    """
    账号彻底删除联动：
    必须在 AccountLifecycleService.hard_delete 外层事务内调用。
    按 page_id 升序锁定该用户全部 ArticleRating 与对应 ArticleRatingAggregate，
    精准扣减四档计数并递增 rating_evidence_version，随后删除其 ArticleRating 与 ArticleRatingReceipt。
    """
    uid = int(user_id)
    with transaction.atomic():
        ratings = list(
            ArticleRating.objects.select_for_update()
            .filter(user_id=uid)
            .order_by("page_id", "id")
        )
        deltas: dict[tuple[int, int, int], dict[str, int]] = {}
        for r in ratings:
            col = _GRADE_TO_COUNT_FIELD.get(r.grade or "")
            if not col:
                continue
            key = (int(r.page_id), int(r.site_id), int(r.live_revision_id))
            bucket = deltas.setdefault(
                key,
                {"excellent_count": 0, "good_count": 0, "pass_count": 0, "poor_count": 0},
            )
            bucket[col] += 1

        recounted_aggregates = 0
        for p_id, s_id, rev_id in sorted(deltas.keys()):
            agg = (
                ArticleRatingAggregate.objects.select_for_update()
                .filter(site_id=s_id, page_id=p_id, live_revision_id=rev_id)
                .first()
            )
            if agg is None:
                continue
            d = deltas[(p_id, s_id, rev_id)]
            agg.excellent_count = max(0, int(agg.excellent_count) - d["excellent_count"])
            agg.good_count = max(0, int(agg.good_count) - d["good_count"])
            agg.pass_count = max(0, int(agg.pass_count) - d["pass_count"])
            agg.poor_count = max(0, int(agg.poor_count) - d["poor_count"])
            agg.rating_evidence_version = int(agg.rating_evidence_version) + 1
            agg.save(
                update_fields=[
                    "excellent_count",
                    "good_count",
                    "pass_count",
                    "poor_count",
                    "rating_evidence_version",
                    "updated_at",
                ]
            )
            recounted_aggregates += 1

        deleted_ratings, _ = ArticleRating.objects.filter(user_id=uid).delete()
        deleted_receipts, _ = ArticleRatingReceipt.objects.filter(user_id=uid).delete()

    return {
        "deleted_ratings": int(deleted_ratings),
        "deleted_receipts": int(deleted_receipts),
        "recounted_aggregates": int(recounted_aggregates),
    }
