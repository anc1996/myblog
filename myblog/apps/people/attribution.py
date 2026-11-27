"""
全站统一作者署名解析服务 (people.attribution)。

落实三级署名契约：
1. 显式署名：读取正式 Revision 或页面实例的 authors 关系，按序去重并归一化角色；
2. 历史版本核准：仅当显式署名为空且存在指定 revision_id 的 LegacyAuthorAttribution 时采用；
3. 内置「佚名」兜底：以上皆空时，归入内置受保护名片 slug='anonymous'，不修改原页面表单或 Revision。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from people.author_identity import normalize_author_role
from people.models import LegacyAuthorAttribution, Person, extract_person_bio_text


AUTHOR_PROJECTION_VERSION = 1


@dataclass(frozen=True)
class ResolvedAuthor:
    person_id: int
    public_id: str
    name: str
    slug: str
    role: str
    role_label: str
    sort_order: int
    avatar_id: int | None = None
    bio: str = ""
    email: str = ""
    is_anonymous: bool = False

    def to_extra_data_dict(self) -> dict[str, Any]:
        return {
            "id": self.person_id,
            "public_id": self.public_id,
            "name": self.name,
            "slug": self.slug,
            "role": self.role,
            "role_label": self.role_label,
            "sort_order": self.sort_order,
        }


def extract_raw_author_refs_from_page(page: Any) -> list[dict[str, Any]]:
    """从页面实例或 live_revision.as_object() 提取原始 authors 关系行。"""
    if not page or not hasattr(page, "authors"):
        return []
    refs: list[dict[str, Any]] = []
    for idx, rel in enumerate(page.authors.all()):
        person_id = getattr(rel, "person_id", None)
        if person_id is None and getattr(rel, "person", None) is not None:
            person_id = rel.person.pk
        if person_id is None:
            continue
        refs.append({
            "person_id": int(person_id),
            "role": getattr(rel, "role", ""),
            "sort_order": getattr(rel, "sort_order", None),
            "_idx": idx,
        })
    return refs


def extract_raw_author_refs_from_revision_content(content: dict[str, Any] | None) -> list[dict[str, Any]]:
    """无需实例化或水合正文，直接从 Revision.content JSON 中提取 authors 列表。"""
    if not isinstance(content, dict):
        return []
    raw_list = content.get("authors")
    if not isinstance(raw_list, list):
        return []
    refs: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        pid = item.get("person") if item.get("person") is not None else item.get("person_id")
        if pid is None:
            continue
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            continue
        refs.append({
            "person_id": pid_int,
            "role": item.get("role", ""),
            "sort_order": item.get("sort_order"),
            "_idx": idx,
        })
    return refs


def resolve_author_attributions(
    raw_refs: Iterable[Any],
    *,
    revision_id: int | None = None,
    allow_anonymous: bool = True,
    using: str = "default",
) -> list[ResolvedAuthor]:
    """
    执行三级署名解析：
    - 过滤不存在或误选的系统「佚名」行；
    - 按 (sort_order, original_index, person_id) 排序并每人只保留第一条有效角色；
    - 若结果为空，先查 LegacyAuthorAttribution(revision_id)，仍为空且 allow_anonymous=True 时归入系统「佚名」。
    """
    normalized_inputs: list[tuple[int, int, int, str]] = []
    for idx, item in enumerate(raw_refs or ()):
        if isinstance(item, dict):
            pid = item.get("person_id")
            role = item.get("role", "")
            sort_order = item.get("sort_order")
            orig_idx = item.get("_idx", idx)
        else:
            pid = getattr(item, "person_id", None)
            role = getattr(item, "role", "")
            sort_order = getattr(item, "sort_order", None)
            orig_idx = idx
        if pid is None:
            continue
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            continue
        sort_val = int(sort_order) if sort_order is not None else 10**9
        normalized_inputs.append((sort_val, int(orig_idx), pid_int, str(role or "")))

    normalized_inputs.sort(key=lambda x: (x[0], x[1], x[2]))

    person_ids = sorted({row[2] for row in normalized_inputs})
    persons_by_id: dict[int, Person] = {}
    if person_ids:
        persons_by_id = {
            p.pk: p
            for p in Person.objects.using(using).filter(pk__in=person_ids, is_system_anonymous=False)
        }

    resolved: list[ResolvedAuthor] = []
    seen_person_ids: set[int] = set()
    for _, _, pid_int, raw_role in normalized_inputs:
        if pid_int in seen_person_ids:
            continue
        person = persons_by_id.get(pid_int)
        if person is None:
            continue
        seen_person_ids.add(pid_int)
        canonical_role, role_label = normalize_author_role(raw_role)
        if canonical_role == "anonymous":
            canonical_role = "other"
            role_label = "其他贡献"
        resolved.append(
            ResolvedAuthor(
                person_id=person.pk,
                public_id=str(person.public_id),
                name=person.name,
                slug=person.slug,
                role=canonical_role,
                role_label=role_label,
                sort_order=len(resolved),
                avatar_id=person.avatar_id,
                bio=extract_person_bio_text(person.bio),
                email=person.email or "",
                is_anonymous=False,
            )
        )

    if resolved:
        return resolved

    if revision_id is not None:
        legacy = (
            LegacyAuthorAttribution.objects.using(using)
            .select_related("person")
            .filter(revision_id=int(revision_id))
            .first()
        )
        if legacy is not None and legacy.person and not legacy.person.is_system_anonymous:
            p = legacy.person
            return [
                ResolvedAuthor(
                    person_id=p.pk,
                    public_id=str(p.public_id),
                    name=p.name,
                    slug=p.slug,
                    role="primary_author",
                    role_label="主要作者",
                    sort_order=0,
                    avatar_id=p.avatar_id,
                    bio=extract_person_bio_text(p.bio),
                    email=p.email or "",
                    is_anonymous=False,
                )
            ]

    if not allow_anonymous:
        return []

    anon = Person.objects.using(using).filter(slug="anonymous", is_system_anonymous=True).first()
    if anon is None:
        raise RuntimeError(
            "System anonymous Person (slug='anonymous', is_system_anonymous=True) is missing; run people migrations."
        )
    return [
        ResolvedAuthor(
            person_id=anon.pk,
            public_id=str(anon.public_id),
            name=anon.name,
            slug="anonymous",
            role="anonymous",
            role_label="佚名",
            sort_order=0,
            avatar_id=anon.avatar_id,
            bio=extract_person_bio_text(anon.bio),
            email="",
            is_anonymous=True,
        )
    ]


def build_projection_author_payload(resolved_authors: list[ResolvedAuthor]) -> tuple[list[dict[str, Any]], list[str]]:
    """生成严格对齐 PublishedSearchAuthor 关系行的 extra_data['authors'] 与 extra_data['author_slugs']。"""
    authors_json = [a.to_extra_data_dict() for a in resolved_authors]
    author_slugs = [a.slug for a in resolved_authors]
    return authors_json, author_slugs
