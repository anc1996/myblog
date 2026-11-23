"""
analytics.settlement
随时可补跑的幂等批结算、48 小时窗口日 UV 封账、缺测检测、过期清理与 Purge 联动。

核心工程契约：
1. settle_pv_and_reading_batches(): Redis Lua 原子 RENAME 冻结活动桶 -> 批量查 Page 与
   get_public_blocked_page_ids() 防复活 -> 单短事务插 AnalyticsBatchReceipt 并按 page_id 升序更新 ->
   SQL 提交成功后才删除 Redis 冻结键；
2. seal_daily_uv(): 扫描 < today 且仍在 48h Redis 窗口内的未封账日期，用 SUNION 合并同页同日多版本 UV
   （绝不把 V1+V2 相加），用 SCARD 取各维度 UV，按 new_uv - old_uv 差值更新 PageAnalyticsTotal.total_visitor_days；
   若 Redis 键已过期缺失，则标 health_status="missing"（不写 0 伪装完整）；
3. on_pages_purged(): 彻底销毁页面提交后清理 Redis 中的对应键与展示缓存。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json
import logging
from typing import Any, Iterable
import uuid

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from redis.exceptions import RedisError

from analytics.collector import _get_redis_client, _resolve_prefix
from analytics.models import (
    AnalyticsBatchReceipt,
    PageAnalyticsDaily,
    PageAnalyticsTotal,
    ScopeAnalyticsDaily,
)

logger = logging.getLogger(__name__)

_SEALED_TTL_SECONDS = 7 * 24 * 3600
_RECEIPT_TTL_DAYS = 7
_DEFAULT_RETENTION_DAYS = 395  # 约 13 个月

_SEAL_ACTIVE_HASH_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    redis.call('RENAME', KEYS[1], KEYS[2])
    redis.call('EXPIRE', KEYS[2], tonumber(ARGV[1]))
    return 1
end
return 0
"""

_SCALAR_PAGE_METRICS = frozenset({
    "pv",
    "organic_pv",
    "measurable_views",
    "valid_views",
    "completed_views",
    "short_bounce_views",
    "total_active_seconds",
})
_ALLOWED_SCOPE_KINDS = frozenset({"site", "type", "entry", "column", "safe_search_term"})


def _decode_redis_str(val: Any) -> str:
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def _decode_redis_hash(raw: dict[Any, Any]) -> dict[str, int]:
    decoded: dict[str, int] = {}
    for k, v in (raw or {}).items():
        ks = _decode_redis_str(k)
        try:
            decoded[ks] = int(_decode_redis_str(v))
        except (TypeError, ValueError):
            continue
    return decoded


def acquire_settle_cooldown(timeout_seconds: int = 60) -> bool:
    """获取短冷却锁，防止多进程/定时任务在短时间内并发重复结算。"""
    try:
        return bool(cache.add("analytics:settle_cooldown", "1", timeout=max(1, int(timeout_seconds))))
    except Exception:
        return True


def invalidate_analytics_display_cache(
    *,
    page_ids: Iterable[int] | None = None,
    site_id: int | None = None,
) -> None:
    """失效受影响页面的展示缓存键，并递增全局展示缓存代次。"""
    try:
        keys_to_delete: list[str] = []
        if page_ids:
            for pid in page_ids:
                if pid is not None:
                    keys_to_delete.append(f"analytics:display:page:{int(pid)}")
        if site_id is not None:
            keys_to_delete.append(f"analytics:display:site:{int(site_id)}")
        if keys_to_delete:
            cache.delete_many(keys_to_delete)
        cache.set("analytics:display:epoch", uuid.uuid4().hex, timeout=None)
    except Exception as exc:
        logger.debug("Failed to invalidate analytics display cache: %s", exc)


def _parse_active_key(full_key: str, prefix: str) -> tuple[str, int, date] | None:
    head = f"{prefix}:analytics:active:"
    if not full_key.startswith(head):
        return None
    tail = full_key[len(head):]
    parts = tail.split(":")
    if len(parts) != 3:
        return None
    kind, site_str, date_str = parts
    if kind not in ("pv", "scope") or not site_str.isdigit():
        return None
    try:
        stat_date = date.fromisoformat(date_str)
    except ValueError:
        return None
    return kind, int(site_str), stat_date


def _parse_batch_id(batch_id: str) -> tuple[str, int, date] | None:
    parts = batch_id.split(":")
    if len(parts) != 4:
        return None
    kind, site_str, date_str, _rand = parts
    if kind not in ("pv", "scope") or not site_str.isdigit():
        return None
    try:
        stat_date = date.fromisoformat(date_str)
    except ValueError:
        return None
    return kind, int(site_str), stat_date


def _settle_pv_batch(
    *,
    batch_id: str,
    payload_hash: str,
    site_id: int,
    stat_date: date,
    data: dict[str, int],
) -> bool:
    from wagtail.models import Page, Site
    from content_lifecycle.public_access import get_public_blocked_page_ids

    if not Site.objects.filter(id=site_id).exists():
        return True

    grouped: dict[tuple[int, int], dict[str, Any]] = {}
    for field, val in data.items():
        if val <= 0:
            continue
        parts = field.split("|", 4)
        if len(parts) != 5:
            continue
        pid_str, rev_str, type_key, col_str, metric = parts
        if not pid_str.isdigit() or not rev_str.isdigit():
            continue
        pid = int(pid_str)
        rev_id = int(rev_str)
        col_id = int(col_str) if col_str.isdigit() and int(col_str) > 0 else None
        bucket = grouped.setdefault(
            (pid, rev_id),
            {
                "type_key": type_key[:64],
                "column_page_id": col_id,
                "metrics": defaultdict(int),
                "conversions": defaultdict(int),
            },
        )
        if type_key and not bucket["type_key"]:
            bucket["type_key"] = type_key[:64]
        if col_id and not bucket["column_page_id"]:
            bucket["column_page_id"] = col_id
        if metric in _SCALAR_PAGE_METRICS:
            bucket["metrics"][metric] += val
        elif metric.startswith("conv:"):
            conv_key = metric[5:53].strip()
            if conv_key:
                bucket["conversions"][conv_key] += val

    all_page_ids = sorted({pid for pid, _ in grouped.keys()})
    eligible_ids: set[int] = set()
    if all_page_ids:
        page_rows = list(
            Page.objects.filter(id__in=all_page_ids).values_list("id", "path", "alias_of_id", "live")
        )
        blocked_ids = get_public_blocked_page_ids([(pid, path) for pid, path, _, _ in page_rows])
        eligible_ids = {
            pid
            for pid, _, alias_of_id, live in page_rows
            if alias_of_id is None and live and pid not in blocked_ids
        }

    revs_by_page: dict[int, list[int]] = defaultdict(list)
    for pid, rev_id in grouped.keys():
        if pid in eligible_ids:
            revs_by_page[pid].append(rev_id)

    try:
        with transaction.atomic():
            AnalyticsBatchReceipt.objects.create(
                batch_id=batch_id,
                payload_hash=payload_hash,
                batch_kind="pv_reading",
                expires_at=timezone.now() + timedelta(days=_RECEIPT_TTL_DAYS),
            )

            for pid in sorted(revs_by_page.keys()):
                try:
                    with transaction.atomic():
                        if not Page.objects.filter(id=pid).exists():
                            continue
                        tot_pv_inc = 0
                        tot_valid_inc = 0
                        tot_completed_inc = 0

                        for rev_id in sorted(revs_by_page[pid]):
                            bucket = grouped[(pid, rev_id)]
                            m = bucket["metrics"]
                            convs = bucket["conversions"]

                            daily_obj = (
                                PageAnalyticsDaily.objects.select_for_update()
                                .filter(
                                    site_id=site_id,
                                    page_id=pid,
                                    stat_date=stat_date,
                                    live_revision_id=rev_id,
                                )
                                .first()
                            )
                            if daily_obj is None:
                                daily_obj = PageAnalyticsDaily(
                                    site_id=site_id,
                                    page_id=pid,
                                    stat_date=stat_date,
                                    live_revision_id=rev_id,
                                    type_key=bucket["type_key"] or "unknown",
                                    column_page_id=bucket["column_page_id"],
                                )
                            if bucket["type_key"] and (not daily_obj.type_key or daily_obj.type_key == "unknown"):
                                daily_obj.type_key = bucket["type_key"]
                            if bucket["column_page_id"] and not daily_obj.column_page_id:
                                daily_obj.column_page_id = bucket["column_page_id"]

                            daily_obj.pv += m.get("pv", 0)
                            daily_obj.organic_pv += m.get("organic_pv", 0)
                            daily_obj.measurable_views += m.get("measurable_views", 0)
                            daily_obj.valid_views += m.get("valid_views", 0)
                            daily_obj.completed_views += m.get("completed_views", 0)
                            daily_obj.short_bounce_views += m.get("short_bounce_views", 0)
                            daily_obj.total_active_seconds += m.get("total_active_seconds", 0)
                            if convs:
                                merged_convs = dict(daily_obj.conversions_json or {})
                                for ck, cv in convs.items():
                                    merged_convs[ck] = int(merged_convs.get(ck, 0)) + cv
                                daily_obj.conversions_json = merged_convs
                            daily_obj.save()

                            tot_pv_inc += m.get("pv", 0)
                            tot_valid_inc += m.get("valid_views", 0)
                            tot_completed_inc += m.get("completed_views", 0)

                        if tot_pv_inc or tot_valid_inc or tot_completed_inc:
                            tot_obj = (
                                PageAnalyticsTotal.objects.select_for_update()
                                .filter(site_id=site_id, page_id=pid)
                                .first()
                            )
                            if tot_obj is None:
                                tot_obj = PageAnalyticsTotal(site_id=site_id, page_id=pid)
                            tot_obj.total_pv += tot_pv_inc
                            tot_obj.total_valid_views += tot_valid_inc
                            tot_obj.total_completed_views += tot_completed_inc
                            if tot_obj.last_settled_date is None or stat_date > tot_obj.last_settled_date:
                                tot_obj.last_settled_date = stat_date
                            tot_obj.save()
                except IntegrityError as exc:
                    # 若页面在结算瞬间被并发 Purge，丢弃该页面而不破坏整批事务
                    logger.info("Skipped page %s during PV settlement due to concurrent integrity change: %s", pid, exc)
        return True
    except IntegrityError:
        # batch_id 已被并发 worker 写入收据，幂等跳过
        return False


def _settle_scope_batch(
    *,
    batch_id: str,
    payload_hash: str,
    site_id: int,
    stat_date: date,
    data: dict[str, int],
) -> bool:
    from wagtail.models import Site

    if not Site.objects.filter(id=site_id).exists():
        return True

    grouped: dict[tuple[str, str], dict[str, int]] = {}
    for field, val in data.items():
        if val <= 0:
            continue
        parts = field.split("|", 2)
        if len(parts) != 3:
            continue
        scope_kind, metric, scope_key = parts
        if scope_kind not in _ALLOWED_SCOPE_KINDS or metric not in ("pv", "entry_clicks"):
            continue
        clean_key = scope_key[:128]
        if not clean_key:
            continue
        bucket = grouped.setdefault((scope_kind, clean_key), {"pv": 0, "entry_clicks": 0})
        bucket[metric] += val

    try:
        with transaction.atomic():
            AnalyticsBatchReceipt.objects.create(
                batch_id=batch_id,
                payload_hash=payload_hash,
                batch_kind="pv_reading",
                expires_at=timezone.now() + timedelta(days=_RECEIPT_TTL_DAYS),
            )

            for scope_kind, scope_key in sorted(grouped.keys()):
                inc = grouped[(scope_kind, scope_key)]
                obj = (
                    ScopeAnalyticsDaily.objects.select_for_update()
                    .filter(
                        site_id=site_id,
                        stat_date=stat_date,
                        scope_kind=scope_kind,
                        scope_key=scope_key,
                    )
                    .first()
                )
                if obj is None:
                    obj = ScopeAnalyticsDaily(
                        site_id=site_id,
                        stat_date=stat_date,
                        scope_kind=scope_kind,
                        scope_key=scope_key,
                    )
                obj.pv += inc["pv"]
                obj.entry_clicks += inc["entry_clicks"]
                obj.save()
        return True
    except IntegrityError:
        return False


def settle_pv_and_reading_batches(*, redis_client: Any = None) -> dict[str, Any]:
    """
    原子冻结 Redis 活动增量 Hash 并批量幂等结算至 PostgreSQL。
    仅在 SQL 事务成功提交后删除 Redis 冻结键。
    """
    client = redis_client if redis_client is not None else _get_redis_client(write=True)
    if client is None:
        return {"degraded": True, "settled_batches": 0, "skipped_batches": 0}

    prefix = _resolve_prefix()
    settled_batches = 0
    skipped_batches = 0

    try:
        seal_script = client.register_script(_SEAL_ACTIVE_HASH_LUA)
        active_keys = [_decode_redis_str(k) for k in client.scan_iter(match=f"{prefix}:analytics:active:*")]
        for active_key in active_keys:
            parsed = _parse_active_key(active_key, prefix)
            if parsed is None:
                continue
            kind, site_id, stat_date = parsed
            batch_id = f"{kind}:{site_id}:{stat_date.isoformat()}:{uuid.uuid4().hex[:12]}"
            sealed_key = f"{prefix}:analytics:sealed:{batch_id}"
            seal_script(keys=[active_key, sealed_key], args=[_SEALED_TTL_SECONDS])

        sealed_keys = sorted(
            _decode_redis_str(k) for k in client.scan_iter(match=f"{prefix}:analytics:sealed:*")
        )
    except (RedisError, OSError, TimeoutError, ConnectionError) as exc:
        logger.warning("Analytics PV settlement Redis scan/seal failed: %s", exc)
        return {"degraded": True, "settled_batches": settled_batches, "skipped_batches": skipped_batches}

    sealed_prefix = f"{prefix}:analytics:sealed:"
    for sealed_key in sealed_keys:
        batch_id = sealed_key[len(sealed_prefix):] if sealed_key.startswith(sealed_prefix) else ""
        parsed = _parse_batch_id(batch_id)
        if parsed is None:
            continue
        kind, site_id, stat_date = parsed

        if AnalyticsBatchReceipt.objects.filter(batch_id=batch_id).exists():
            try:
                client.delete(sealed_key)
            except (RedisError, OSError, TimeoutError, ConnectionError):
                pass
            skipped_batches += 1
            continue

        try:
            raw_hash = client.hgetall(sealed_key)
        except (RedisError, OSError, TimeoutError, ConnectionError) as exc:
            logger.warning("Failed reading sealed key %s: %s", sealed_key, exc)
            continue

        data = _decode_redis_hash(raw_hash)
        payload_hash = hashlib.sha256(
            json.dumps(sorted(data.items()), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        if kind == "pv":
            written = _settle_pv_batch(
                batch_id=batch_id,
                payload_hash=payload_hash,
                site_id=site_id,
                stat_date=stat_date,
                data=data,
            )
        else:
            written = _settle_scope_batch(
                batch_id=batch_id,
                payload_hash=payload_hash,
                site_id=site_id,
                stat_date=stat_date,
                data=data,
            )

        # SQL 提交成功（或发现已有收据幂等跳过）后才删 Redis 冻结键
        try:
            client.delete(sealed_key)
        except (RedisError, OSError, TimeoutError, ConnectionError) as exc:
            logger.warning("SQL committed for batch %s, Redis delete failed (safe to retry): %s", batch_id, exc)

        if written:
            settled_batches += 1
        else:
            skipped_batches += 1

    return {
        "degraded": False,
        "settled_batches": settled_batches,
        "skipped_batches": skipped_batches,
    }


def _mark_day_missing(site_id: int, stat_date: date) -> None:
    with transaction.atomic():
        PageAnalyticsDaily.objects.filter(
            site_id=site_id,
            stat_date=stat_date,
        ).exclude(health_status="normal").update(health_status="missing")

        ScopeAnalyticsDaily.objects.filter(
            site_id=site_id,
            stat_date=stat_date,
        ).exclude(health_status="normal").update(health_status="missing")

        site_scope = (
            ScopeAnalyticsDaily.objects.select_for_update()
            .filter(
                site_id=site_id,
                stat_date=stat_date,
                scope_kind="site",
                scope_key="all",
            )
            .first()
        )
        if site_scope is None:
            ScopeAnalyticsDaily.objects.create(
                site_id=site_id,
                stat_date=stat_date,
                scope_kind="site",
                scope_key="all",
                health_status="missing",
            )
        elif site_scope.health_status != "normal":
            site_scope.health_status = "missing"
            site_scope.save(update_fields=["health_status"])


def seal_daily_uv(
    target_date: date | str | None = None,
    *,
    days_back: int = 2,
    force: bool = False,
    redis_client: Any = None,
) -> dict[str, Any]:
    """
    补跑或封账已结束自然日 (< today) 的日 UV。
    - 页面多版本同日 UV 使用 SUNION 合并；
    - 维度 UV 使用 SCARD；
    - 按 new_uv - old_uv 差值同步更新 PageAnalyticsTotal.total_visitor_days；
    - Redis 键缺失时标 health_status="missing"，绝不写 0 伪装完整。
    """
    from wagtail.models import Page, Site
    from content_lifecycle.public_access import get_public_blocked_page_ids

    client = redis_client if redis_client is not None else _get_redis_client(write=True)
    if client is None:
        return {"degraded": True, "sealed_dates": [], "missing_dates": [], "skipped_dates": []}

    today = timezone.localdate()
    if target_date is not None:
        parsed_target = date.fromisoformat(target_date) if isinstance(target_date, str) else target_date
        if not force and parsed_target >= today:
            return {"degraded": False, "sealed_dates": [], "missing_dates": [], "skipped_dates": [parsed_target.isoformat()]}
        dates_to_check = [parsed_target]
    else:
        dates_to_check = [today - timedelta(days=d) for d in range(1, max(1, int(days_back)) + 1)]

    prefix = _resolve_prefix()
    sites = list(Site.objects.all().order_by("id"))
    sealed_dates: list[str] = []
    missing_dates: list[str] = []
    skipped_dates: list[str] = []

    for site in sites:
        for stat_date in dates_to_check:
            date_str = stat_date.isoformat()
            receipt_id = f"daily_uv:{site.id}:{date_str}"
            already_sealed = AnalyticsBatchReceipt.objects.filter(batch_id=receipt_id).exists()
            if already_sealed and not force:
                skipped_dates.append(f"{site.id}:{date_str}")
                continue

            uv_site_key = f"{prefix}:analytics:uv:site:{site.id}:{date_str}"
            uv_pages_idx_key = f"{prefix}:analytics:uv:pages_idx:{site.id}:{date_str}"
            uv_scopes_idx_key = f"{prefix}:analytics:uv:scopes_idx:{site.id}:{date_str}"

            try:
                site_exists = bool(client.exists(uv_site_key))
                pages_idx_exists = bool(client.exists(uv_pages_idx_key))
            except (RedisError, OSError, TimeoutError, ConnectionError) as exc:
                logger.warning("UV seal Redis check failed for site=%s date=%s: %s", site.id, date_str, exc)
                return {
                    "degraded": True,
                    "sealed_dates": sealed_dates,
                    "missing_dates": missing_dates,
                    "skipped_dates": skipped_dates,
                }

            if not site_exists and not pages_idx_exists:
                _mark_day_missing(site.id, stat_date)
                missing_dates.append(f"{site.id}:{date_str}")
                continue

            try:
                page_revs: dict[int, dict[int, tuple[str, int | None]]] = defaultdict(dict)
                if pages_idx_exists:
                    for raw_member in client.smembers(uv_pages_idx_key):
                        member = _decode_redis_str(raw_member)
                        parts = member.split("|", 3)
                        if len(parts) != 4:
                            continue
                        pid_s, rev_s, tkey, col_s = parts
                        if not pid_s.isdigit() or not rev_s.isdigit():
                            continue
                        col_id = int(col_s) if col_s.isdigit() and int(col_s) > 0 else None
                        page_revs[int(pid_s)][int(rev_s)] = (tkey[:64], col_id)

                for row in PageAnalyticsDaily.objects.filter(site=site, stat_date=stat_date).values(
                    "page_id", "live_revision_id", "type_key", "column_page_id"
                ):
                    page_revs[row["page_id"]].setdefault(
                        row["live_revision_id"],
                        (row["type_key"], row["column_page_id"]),
                    )

                all_pids = sorted(page_revs.keys())
                eligible_pids: list[int] = []
                if all_pids:
                    page_rows = list(
                        Page.objects.filter(id__in=all_pids).values_list("id", "path", "alias_of_id", "live")
                    )
                    blocked_ids = get_public_blocked_page_ids([(pid, path) for pid, path, _, _ in page_rows])
                    eligible_pids = sorted(
                        pid
                        for pid, _, alias_of_id, live in page_rows
                        if alias_of_id is None and live and pid not in blocked_ids
                    )

                page_uv_plan: dict[int, dict[str, Any]] = {}
                for pid in eligible_pids:
                    rev_map = page_revs[pid]
                    rev_keys: list[tuple[int, str]] = []
                    existing_keys: list[str] = []
                    rev_uvs: dict[int, int] = {}
                    for rev_id in sorted(rev_map.keys()):
                        rk = f"{prefix}:analytics:uv:page:{site.id}:{date_str}:{pid}:{rev_id}"
                        rev_keys.append((rev_id, rk))
                        if client.exists(rk):
                            existing_keys.append(rk)
                            rev_uvs[rev_id] = int(client.scard(rk))
                        else:
                            rev_uvs[rev_id] = 0

                    if existing_keys:
                        if len(existing_keys) == 1:
                            union_uv = int(client.scard(existing_keys[0]))
                        else:
                            union_uv = len(client.sunion(*existing_keys))
                        is_partial = len(existing_keys) < len(rev_keys)
                        health = "partial" if is_partial else "normal"
                    else:
                        union_uv = None
                        health = "missing"

                    page_uv_plan[pid] = {
                        "rev_meta": rev_map,
                        "rev_uvs": rev_uvs,
                        "union_uv": union_uv,
                        "health": health,
                    }

                scope_uv_plan: dict[tuple[str, str], tuple[int | None, str]] = {}
                scope_targets: set[tuple[str, str]] = {("site", "all")}
                if client.exists(uv_scopes_idx_key):
                    for raw_m in client.smembers(uv_scopes_idx_key):
                        ms = _decode_redis_str(raw_m)
                        if "|" in ms:
                            sk, sv = ms.split("|", 1)
                            if sk in _ALLOWED_SCOPE_KINDS and sv:
                                scope_targets.add((sk, sv[:128]))
                for row in ScopeAnalyticsDaily.objects.filter(site=site, stat_date=stat_date).values(
                    "scope_kind", "scope_key"
                ):
                    if row["scope_kind"] != "safe_search_term":
                        scope_targets.add((row["scope_kind"], row["scope_key"]))

                for sk, sv in sorted(scope_targets):
                    if sk == "site":
                        uk = uv_site_key
                    elif sk in ("type", "entry", "column"):
                        uk = f"{prefix}:analytics:uv:{sk}:{site.id}:{date_str}:{sv}"
                    else:
                        continue
                    if client.exists(uk):
                        scope_uv_plan[(sk, sv)] = (int(client.scard(uk)), "normal")
                    else:
                        scope_uv_plan[(sk, sv)] = (None, "missing")
            except (RedisError, OSError, TimeoutError, ConnectionError) as exc:
                logger.warning("UV seal Redis read failed for site=%s date=%s: %s", site.id, date_str, exc)
                return {
                    "degraded": True,
                    "sealed_dates": sealed_dates,
                    "missing_dates": missing_dates,
                    "skipped_dates": skipped_dates,
                }

            digest_payload = {
                "pages": {str(pid): plan["union_uv"] for pid, plan in page_uv_plan.items()},
                "scopes": {f"{k}|{v}": uv for (k, v), (uv, _) in scope_uv_plan.items()},
            }
            payload_hash = hashlib.sha256(
                json.dumps(digest_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()

            with transaction.atomic():
                receipt_obj = AnalyticsBatchReceipt.objects.filter(batch_id=receipt_id).first()
                expires_at = timezone.now() + timedelta(days=_RECEIPT_TTL_DAYS)
                if receipt_obj is None:
                    AnalyticsBatchReceipt.objects.create(
                        batch_id=receipt_id,
                        payload_hash=payload_hash,
                        batch_kind="daily_uv",
                        expires_at=expires_at,
                    )
                else:
                    receipt_obj.payload_hash = payload_hash
                    receipt_obj.expires_at = expires_at
                    receipt_obj.save(update_fields=["payload_hash", "expires_at"])

                for pid in sorted(page_uv_plan.keys()):
                    plan = page_uv_plan[pid]
                    try:
                        with transaction.atomic():
                            if not Page.objects.filter(id=pid).exists():
                                continue
                            existing_rows = list(
                                PageAnalyticsDaily.objects.select_for_update()
                                .filter(site=site, page_id=pid, stat_date=stat_date)
                                .order_by("live_revision_id")
                            )
                            existing_by_rev = {r.live_revision_id: r for r in existing_rows}

                            # 计算 old_uv，确保按 new_uv - old_uv 差值更新 total_visitor_days
                            sealed_union_cache_key = f"{prefix}:analytics:sealed_uv:{site.id}:{date_str}:{pid}"
                            if not already_sealed:
                                old_union_uv = 0
                            elif len(existing_rows) == 1:
                                old_union_uv = int(existing_rows[0].daily_uv)
                            else:
                                cached_prev = None
                                try:
                                    cached_prev = client.get(sealed_union_cache_key)
                                except Exception:
                                    cached_prev = None
                                if cached_prev is not None:
                                    old_union_uv = int(_decode_redis_str(cached_prev))
                                else:
                                    old_union_uv = max((int(r.daily_uv) for r in existing_rows), default=0)

                            for rev_id in sorted(plan["rev_meta"].keys()):
                                tkey, col_id = plan["rev_meta"][rev_id]
                                rev_uv = plan["rev_uvs"].get(rev_id, 0)
                                row = existing_by_rev.get(rev_id)
                                if row is None:
                                    row = PageAnalyticsDaily(
                                        site=site,
                                        page_id=pid,
                                        stat_date=stat_date,
                                        live_revision_id=rev_id,
                                        type_key=tkey or "unknown",
                                        column_page_id=col_id,
                                    )
                                if plan["union_uv"] is not None:
                                    row.daily_uv = rev_uv
                                row.health_status = plan["health"]
                                row.save()

                            new_union_uv = plan["union_uv"]
                            if new_union_uv is not None:
                                delta_uv = int(new_union_uv) - int(old_union_uv)
                                tot_obj = (
                                    PageAnalyticsTotal.objects.select_for_update()
                                    .filter(site=site, page_id=pid)
                                    .first()
                                )
                                if tot_obj is None:
                                    tot_obj = PageAnalyticsTotal(site=site, page_id=pid)
                                tot_obj.total_visitor_days = max(0, int(tot_obj.total_visitor_days) + delta_uv)
                                if tot_obj.last_settled_date is None or stat_date > tot_obj.last_settled_date:
                                    tot_obj.last_settled_date = stat_date
                                tot_obj.save()

                                try:
                                    client.set(sealed_union_cache_key, str(new_union_uv), ex=48 * 3600)
                                except Exception:
                                    pass
                    except IntegrityError as exc:
                        logger.info("Skipped page %s during daily UV seal: %s", pid, exc)

                for (sk, sv), (uv_cnt, health) in sorted(scope_uv_plan.items()):
                    row = (
                        ScopeAnalyticsDaily.objects.select_for_update()
                        .filter(site=site, stat_date=stat_date, scope_kind=sk, scope_key=sv)
                        .first()
                    )
                    if row is None:
                        row = ScopeAnalyticsDaily(
                            site=site,
                            stat_date=stat_date,
                            scope_kind=sk,
                            scope_key=sv,
                        )
                    if uv_cnt is not None:
                        row.daily_uv = uv_cnt
                    row.health_status = health
                    row.save()

            sealed_dates.append(f"{site.id}:{date_str}")

    return {
        "degraded": False,
        "sealed_dates": sealed_dates,
        "missing_dates": missing_dates,
        "skipped_dates": skipped_dates,
    }


def cleanup_expired_analytics(*, retention_days: int = _DEFAULT_RETENTION_DAYS) -> dict[str, int]:
    """清理超过 7 天的批次收据以及超过 13 个月（默认 395 天）的日聚合明细。"""
    now = timezone.now()
    cutoff_date = timezone.localdate() - timedelta(days=max(1, int(retention_days)))

    receipts_deleted, _ = AnalyticsBatchReceipt.objects.filter(expires_at__lte=now).delete()
    page_daily_deleted, _ = PageAnalyticsDaily.objects.filter(stat_date__lt=cutoff_date).delete()
    scope_daily_deleted, _ = ScopeAnalyticsDaily.objects.filter(stat_date__lt=cutoff_date).delete()

    return {
        "receipts_deleted": receipts_deleted,
        "page_daily_deleted": page_daily_deleted,
        "scope_daily_deleted": scope_daily_deleted,
    }


def on_pages_purged(page_ids: Iterable[int], *, redis_client: Any = None) -> dict[str, Any]:
    """
    页面彻底销毁 (Purge) 事务提交后清理 Redis 中的对应 UV/状态键与展示缓存。
    Redis 不可用时不抛异常，依赖 48h TTL 自动过期与结算前主库 Page 存在性校验兜底。
    """
    clean_ids = sorted({int(pid) for pid in (page_ids or ()) if pid is not None})
    if not clean_ids:
        return {"deleted_keys": 0}

    invalidate_analytics_display_cache(page_ids=clean_ids)

    client = redis_client if redis_client is not None else _get_redis_client(write=True)
    if client is None:
        return {"deleted_keys": 0, "degraded": True}

    prefix = _resolve_prefix()
    deleted_keys = 0
    try:
        keys_to_del: list[str] = []
        for pid in clean_ids:
            for pattern in (
                f"{prefix}:analytics:uv:page:*:*:{pid}:*",
                f"{prefix}:analytics:uv:column:*:*:{pid}",
                f"{prefix}:analytics:sealed_uv:*:*:{pid}",
            ):
                for k in client.scan_iter(match=pattern):
                    keys_to_del.append(_decode_redis_str(k))
        if keys_to_del:
            deleted_keys = int(client.delete(*keys_to_del))
    except (RedisError, OSError, TimeoutError, ConnectionError) as exc:
        logger.warning("Analytics Redis cleanup after page purge %s degraded: %s", clean_ids, exc)
        return {"deleted_keys": deleted_keys, "degraded": True}

    return {"deleted_keys": deleted_keys, "degraded": False}
