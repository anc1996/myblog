"""
作者身份标识、拼音/拉丁规范 Slug 生成、A–Z 分组与角色归一化工具。

纯函数模块：导入时不触发任何数据库或网络 IO。
"""

from __future__ import annotations

import re
import unicodedata
import uuid

import anyascii


PERSON_SLUG_MAX_LENGTH = 96
RESERVED_AUTHOR_SLUGS = frozenset({
    "anonymous",
    "lookup",
    "all",
    "page",
    "search",
    "new",
    "edit",
    "delete",
})

_UUID_32_RE = re.compile(r"^[0-9a-f]{32}$")
_UUID_HYPHEN_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VALID_PERSON_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

CANONICAL_AUTHOR_ROLES = (
    "primary_author",
    "co_author",
    "reviewer",
    "translator",
    "editor",
    "core_dev",
    "other",
    "anonymous",
)

MAJOR_AUTHOR_ROLES = frozenset({"primary_author", "core_dev"})
OTHER_CONTRIB_ROLES = frozenset({"reviewer", "translator", "editor", "other"})

_ROLE_MAP: dict[str, tuple[str, str]] = {
    "primary_author": ("primary_author", "主要作者"),
    "主要作者": ("primary_author", "主要作者"),
    "主笔作者": ("primary_author", "主笔作者"),
    "主笔": ("primary_author", "主笔"),
    "co_author": ("co_author", "共同作者"),
    "共同作者": ("co_author", "共同作者"),
    "reviewer": ("reviewer", "技术审校"),
    "审校人员": ("reviewer", "审校人员"),
    "技术审校": ("reviewer", "技术审校"),
    "translator": ("translator", "翻译"),
    "翻译贡献": ("translator", "翻译贡献"),
    "翻译": ("translator", "翻译"),
    "editor": ("editor", "责任编辑"),
    "责任编辑": ("editor", "责任编辑"),
    "core_dev": ("core_dev", "核心研发"),
    "架构设计与主研发": ("core_dev", "核心研发"),
    "核心研发": ("core_dev", "核心研发"),
    "负责人": ("core_dev", "核心研发"),
    "other": ("other", "其他贡献"),
    "其他贡献": ("other", "其他贡献"),
    "anonymous": ("anonymous", "佚名"),
    "佚名": ("anonymous", "佚名"),
}

ROLE_FILTER_GROUPS: dict[str, tuple[str, ...]] = {
    "major": ("primary_author", "core_dev"),
    "co_author": ("co_author",),
    "other_contrib": ("reviewer", "translator", "editor", "other"),
    "primary_author": ("primary_author",),
    "core_dev": ("core_dev",),
    "reviewer": ("reviewer",),
    "translator": ("translator",),
    "editor": ("editor",),
    "other": ("other",),
    "anonymous": ("anonymous",),
}


def normalize_author_role(raw_role: str | None) -> tuple[str, str]:
    """
    将各业务模型中的作者角色（代码或中文自由文本）归一化为：
    (canonical_role_code, display_label)。
    """
    cleaned = unicodedata.normalize("NFKC", str(raw_role or "")).strip()
    if not cleaned:
        return ("other", "其他贡献")
    key = cleaned.lower()
    if key in _ROLE_MAP:
        return _ROLE_MAP[key]
    if cleaned in _ROLE_MAP:
        return _ROLE_MAP[cleaned]
    return ("other", cleaned[:64])


def roles_for_filter_key(role_key: str | None) -> tuple[str, ...]:
    """将前端筛选键或规范角色代码解析为 SQL `role__in` 元组。"""
    if not role_key:
        return ()
    key = unicodedata.normalize("NFKC", str(role_key)).strip().lower()
    if not key or key == "all":
        return ()
    return ROLE_FILTER_GROUPS.get(key, ())


def _transliterate_ascii(text: str) -> str:
    """对 Unicode 文本执行 NFKC 归一化并离线转为 ASCII（含汉字拼音与拉丁重音折叠）。"""
    normalized = unicodedata.normalize("NFKC", text or "").strip()
    if not normalized:
        return ""
    return anyascii.anyascii(normalized)


def compute_initial_and_sort_key(name: str) -> tuple[str, int, str]:
    """
    计算作者大厅分组首字母、分组桶序与字典排序键：
    - A–Z 返回 (letter, 0, sort_key)
    - 符号/数字/未知返回 ('#', 1, sort_key)
    """
    normalized = unicodedata.normalize("NFKC", name or "").strip()
    ascii_raw = _transliterate_ascii(normalized)
    # 按字母清洗后提取首字母与排序键
    ascii_spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", ascii_raw)
    ascii_spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", ascii_spaced)
    sort_key = re.sub(r"\s+", " ", ascii_spaced.lower()).strip()[:255]
    if not sort_key:
        sort_key = normalized.lower()[:255]

    initial = "#"
    for ch in ascii_raw:
        if "a" <= ch <= "z" or "A" <= ch <= "Z":
            initial = ch.upper()
            break
    rank = 0 if "A" <= initial <= "Z" else 1
    return initial, rank, sort_key


def is_disallowed_ordinary_slug(slug: str) -> bool:
    """判断 slug 是否为保留词、纯数字、UUID 外形或非法格式。"""
    if not slug or len(slug) > PERSON_SLUG_MAX_LENGTH:
        return True
    if slug in RESERVED_AUTHOR_SLUGS:
        return True
    if slug.isdigit():
        return True
    if _UUID_32_RE.fullmatch(slug) or _UUID_HYPHEN_RE.fullmatch(slug):
        return True
    if not _VALID_PERSON_SLUG_RE.fullmatch(slug):
        return True
    return False


def generate_base_slug(name: str) -> str:
    """
    根据显示名称生成基础 ASCII slug（仅小写字母、数字和单连字符）：
    - 张三 -> zhang-san
    - Ada Lovelace -> ada-lovelace
    - 不可作为普通作者使用的保留词/纯数字/UUID/纯符号返回空串，由调用方拼接 UUID 后缀。
    """
    ascii_raw = _transliterate_ascii(name)
    if not ascii_raw:
        return ""
    # 分拆汉字拼音 CamelCase 边界（如 ZhangSan -> Zhang San, PostgreSQLZhuanJia -> PostgreSQL Zhuan Jia）
    segmented = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", ascii_raw)
    segmented = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", segmented)
    lowered = segmented.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        return ""
    slug = slug[:80].strip("-")
    if is_disallowed_ordinary_slug(slug):
        return ""
    return slug


def build_unique_slug_with_suffix(base_slug: str, public_id: uuid.UUID | str | None, attempt: int = 0) -> str:
    """
    在基础 slug 为空或发生唯一约束碰撞时，使用稳定 public_id 构造合法唯一 slug。
    attempt=0 时：若 base_slug 非空直接返回 base_slug，否则返回 person-<12位hex>；
    attempt>=1 时：在保留后缀空间后截断 base_slug 并追加 -<hex片段>。
    """
    if isinstance(public_id, uuid.UUID):
        uid_hex = public_id.hex
    else:
        raw_hex = re.sub(r"[^0-9a-f]", "", str(public_id or "").lower())
        uid_hex = raw_hex if len(raw_hex) >= 12 else uuid.uuid4().hex

    if attempt == 0 and base_slug and not is_disallowed_ordinary_slug(base_slug):
        return base_slug

    suffix_len = min(32, 6 + max(0, attempt) * 4)
    suffix = uid_hex[:suffix_len]
    if not base_slug:
        candidate = f"person-{uid_hex[:12]}" if attempt == 0 else f"person-{suffix}"
    else:
        max_base_len = PERSON_SLUG_MAX_LENGTH - 1 - len(suffix)
        trimmed = base_slug[:max_base_len].strip("-")
        candidate = f"{trimmed}-{suffix}" if trimmed else f"person-{suffix}"

    if is_disallowed_ordinary_slug(candidate):
        candidate = f"person-{uid_hex[:16]}"
    return candidate[:PERSON_SLUG_MAX_LENGTH]
