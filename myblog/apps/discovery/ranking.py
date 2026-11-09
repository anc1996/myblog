"""
全站内容发现与列表排序与 Keyset 游标服务 (Ranking & Keyset Pagination)。

落实 RFC 规范与 D1-A / A-01 决策：
1. 同向 Keyset 排序健：将 match_tier 映射为全降序的 tier_rank (FILTER=3, A=3, B=2, C=1)；
   统一排序元组为 (tier_rank DESC, score_key DESC, authored_on DESC, page_id DESC)；
2. 签名 Keyset 游标 (v2:...)：通过 Django TimestampSigner 签名防篡改，绑定 Epoch、指纹与排序边界；
3. 双向翻页：上一页 (prev) 与下一页 (next) 边界精准定位，杜绝海量数据深分页扫表性能退化；
4. 严格校验与游标重置：指纹不匹配或跨版本自动重置第一页并置 cursor_reset=True。
"""

import hashlib
import json
import logging
from datetime import date
from typing import Any
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired

logger = logging.getLogger(__name__)

TIER_RANK_FILTER = 3
TIER_RANK_A = 3
TIER_RANK_B = 2
TIER_RANK_C = 1

MATCH_MODE_TO_TIER_RANK = {
    "FILTER": TIER_RANK_FILTER,
    "A": TIER_RANK_A,
    "B": TIER_RANK_B,
    "C": TIER_RANK_C,
}

KEYSET_SALT = "discovery.keyset_cursor.v2"
KEYSET_MAX_AGE = 1800  # 30 分钟有效期


def compute_query_fingerprint(
    q: str | None,
    search_fields: str,
    root_page_id: int | None,
    tree_scope: str,
    type_keys: list[str],
    date_from: date | None,
    date_to: date | None,
    sort: str,
    page_size: int,
    extra_filters: dict[str, Any] | None = None,
) -> str:
    """计算当前查询条件的唯一规范化指纹 (SHA-256 前16位)"""
    data = {
        "q": (q or "").strip().lower(),
        "search_fields": search_fields or "all",
        "root_page_id": root_page_id,
        "tree_scope": tree_scope or "descendants",
        "type_keys": sorted(type_keys or []),
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "sort": sort or "relevance",
        "page_size": page_size,
        "extra_filters": json.dumps(extra_filters or {}, sort_keys=True),
    }
    payload = json.dumps(data, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def encode_keyset_cursor(
    epoch: int,
    direction: str,
    tier_rank: int,
    score_key: int,
    authored_on: date,
    page_id: int,
    fingerprint: str,
) -> str:
    """生成带签名的 v2 Keyset 游标"""
    signer = TimestampSigner(salt=KEYSET_SALT)
    payload = {
        "v": 2,
        "ep": epoch,
        "dir": direction,
        "tr": int(tier_rank),
        "sk": int(score_key),
        "ao": authored_on.isoformat(),
        "pid": int(page_id),
        "fp": fingerprint,
    }
    serialized = json.dumps(payload, separators=(",", ":"))
    signed_token = signer.sign(serialized)
    return f"v2:{signed_token}"


def decode_keyset_cursor(
    cursor_str: str | None,
    current_epoch: int,
    expected_fingerprint: str,
    max_age: int = KEYSET_MAX_AGE,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    """
    解析并核验 v2 Keyset 游标。
    返回: (is_valid, payload, error_status)
    error_status: None | 'reset' (过期或Epoch/指纹失效，需轻提示刷新首页) | 'invalid' (签名篡改或格式错误，报400)
    """
    if not cursor_str:
        return False, None, None

    if not cursor_str.startswith("v2:"):
        return False, None, "unsupported_version"

    token = cursor_str[3:]
    signer = TimestampSigner(salt=KEYSET_SALT)
    try:
        unsigned_str = signer.unsign(token, max_age=max_age)
        payload = json.loads(unsigned_str)
    except SignatureExpired:
        logger.info("Keyset cursor expired (max_age=%ds)", max_age)
        return False, None, "reset"
    except (BadSignature, json.JSONDecodeError, Exception) as exc:
        logger.warning("Keyset cursor signature verification failed: %s", exc)
        return False, None, "invalid"

    # 校验版本、Epoch 与指纹
    if payload.get("v") != 2:
        return False, None, "invalid"

    if payload.get("ep") != current_epoch:
        logger.info("Keyset cursor epoch mismatch (cursor=%s, current=%s) -> trigger reset", payload.get("ep"), current_epoch)
        return False, None, "reset"

    if payload.get("fp") != expected_fingerprint:
        logger.info("Keyset cursor query fingerprint mismatch -> trigger reset")
        return False, None, "reset"

    return True, payload, None
