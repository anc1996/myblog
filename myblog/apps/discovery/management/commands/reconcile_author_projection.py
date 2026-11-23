"""
管理命令：全站发布副本作者投影核对与安全回填 (reconcile_author_projection)。

特点（RFC 4.3）：
1. 默认只读报告（--dry-run）；传入 --apply 执行幂等补写；
2. 对已核验当前 live_revision 且可从 Revision.content 解出 authors 关系的现有文档，
   仅补齐 PublishedSearchAuthor 关系行与兼容 JSON，绝不重切正文或调用 MongoDB；
3. 无法核验/缺失文档支持交由全量重建入口按需重建；
4. 按 page_id 升序分批短事务处理，支持 --start-page-id / --site-id / --batch-size 断点续跑。
"""

import json
from django.core.management.base import BaseCommand
from django.db import transaction
from wagtail.models import Page

from discovery.models import (
    ContentDiscoveryEpoch,
    PublishedSearchAuthor,
    PublishedSearchDocument,
)
from discovery.projection import _find_adapter_for_page, project_page_to_discovery
from discovery.registry import is_author_discovery_enabled
from people.attribution import (
    AUTHOR_PROJECTION_VERSION,
    build_projection_author_payload,
    extract_raw_author_refs_from_revision_content,
    resolve_author_attributions,
)
from people.models import get_or_create_anonymous_person


class Command(BaseCommand):
    help = "只读审计或幂等回填 PublishedSearchAuthor 作者索引与兼容 JSON"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="执行幂等补写与对账修复（默认只读审计）")
        parser.add_argument("--batch-size", type=int, default=200, help="单批处理的文档数（默认 200）")
        parser.add_argument("--start-page-id", type=int, default=0, help="起始 page_id（断点续跑）")
        parser.add_argument("--site-id", type=int, default=None, help="仅限指定 Site ID")

    def handle(self, *args, **options):
        apply_changes = bool(options["apply"])
        batch_size = max(10, int(options["batch_size"] or 200))
        last_page_id = max(0, int(options["start_page_id"] or 0))
        site_id = options["site_id"]

        if apply_changes:
            get_or_create_anonymous_person()

        summary = {
            "mode": "apply" if apply_changes else "dry_run",
            "checked": 0,
            "missing_or_stale": 0,
            "json_row_mismatches": 0,
            "rebuilt_full": 0,
            "updated_authors_only": 0,
            "unchanged": 0,
            "errors": [],
        }

        doc_qs = PublishedSearchDocument.objects.filter(
            index_generation=1,
            is_live=True,
            is_recycled=False,
        )
        if site_id is not None:
            doc_qs = doc_qs.filter(site_id=site_id)

        any_updated = False
        while True:
            batch = list(
                doc_qs.filter(page_id__gt=last_page_id)
                .order_by("page_id")[:batch_size]
            )
            if not batch:
                break

            page_ids = [d.page_id for d in batch]
            pages_map = {
                p.id: p
                for p in Page.objects.filter(id__in=page_ids).select_related("live_revision")
            }

            for doc in batch:
                last_page_id = doc.page_id
                summary["checked"] += 1
                page = pages_map.get(doc.page_id)
                if not page or not page.live or page.alias_of_id is not None or not page.live_revision:
                    summary["missing_or_stale"] += 1
                    continue

                specific_page = page.specific
                if getattr(specific_page, "is_recycled", False):
                    summary["missing_or_stale"] += 1
                    continue

                adapter = _find_adapter_for_page(specific_page)
                if not is_author_discovery_enabled(adapter):
                    continue

                live_rev = page.live_revision
                if doc.effective_revision_id != live_rev.id:
                    summary["missing_or_stale"] += 1
                    if apply_changes:
                        try:
                            project_page_to_discovery(specific_page)
                            summary["rebuilt_full"] += 1
                            any_updated = True
                        except Exception as exc:
                            summary["errors"].append({"page_id": doc.page_id, "error": str(exc)})
                    continue

                raw_refs = extract_raw_author_refs_from_revision_content(live_rev.content)
                resolved = resolve_author_attributions(
                    raw_refs,
                    revision_id=live_rev.id,
                    allow_anonymous=True,
                )
                expected_json, expected_slugs = build_projection_author_payload(resolved)
                expected_rows = [
                    (a.person_id, a.name, a.slug, a.role, a.sort_order)
                    for a in resolved
                ]

                existing_rows = list(
                    PublishedSearchAuthor.objects.filter(document=doc)
                    .order_by("sort_order", "id")
                    .values_list("person_id", "author_name", "author_slug", "role", "sort_order")
                )
                extra = doc.extra_data or {}
                json_matches = (
                    extra.get("authors") == expected_json
                    and extra.get("author_slugs") == expected_slugs
                    and extra.get("author_projection_version") == AUTHOR_PROJECTION_VERSION
                )
                rows_match = existing_rows == expected_rows

                if json_matches and rows_match:
                    summary["unchanged"] += 1
                    continue

                summary["json_row_mismatches"] += 1
                if apply_changes:
                    reconciled_ok = False
                    with transaction.atomic():
                        locked_page = (
                            Page.objects.select_for_update()
                            .filter(id=doc.page_id)
                            .first()
                        )
                        locked_doc = (
                            PublishedSearchDocument.objects.select_for_update()
                            .filter(pk=doc.pk)
                            .first()
                        )
                        if (
                            locked_page is None
                            or locked_doc is None
                            or not locked_page.live
                            or locked_page.alias_of_id is not None
                            or locked_page.live_revision_id is None
                            or getattr(locked_page.specific, "is_recycled", False)
                        ):
                            summary["missing_or_stale"] += 1
                            continue
                        if locked_doc.effective_revision_id != locked_page.live_revision_id:
                            summary["missing_or_stale"] += 1
                            continue
                        locked_live_rev = locked_page.live_revision
                        if locked_live_rev is None:
                            summary["missing_or_stale"] += 1
                            continue
                        locked_refs = extract_raw_author_refs_from_revision_content(locked_live_rev.content)
                        locked_resolved = resolve_author_attributions(
                            locked_refs,
                            revision_id=locked_page.live_revision_id,
                            allow_anonymous=True,
                        )
                        locked_json, locked_slugs = build_projection_author_payload(locked_resolved)
                        locked_rows = [
                            (a.person_id, a.name, a.slug, a.role, a.sort_order)
                            for a in locked_resolved
                        ]
                        new_extra = dict(locked_doc.extra_data or {})
                        new_extra["authors"] = locked_json
                        new_extra["author_slugs"] = locked_slugs
                        new_extra["author_projection_version"] = AUTHOR_PROJECTION_VERSION
                        locked_doc.extra_data = new_extra
                        locked_doc.save(update_fields=["extra_data", "synced_at"])

                        PublishedSearchAuthor.objects.filter(document=locked_doc).delete()
                        PublishedSearchAuthor.objects.bulk_create([
                            PublishedSearchAuthor(
                                document=locked_doc,
                                person_id=pid,
                                author_name=pname,
                                author_slug=pslug,
                                role=prole,
                                sort_order=psort,
                            )
                            for pid, pname, pslug, prole, psort in locked_rows
                        ])
                        reconciled_ok = True
                    if reconciled_ok:
                        summary["updated_authors_only"] += 1
                        any_updated = True

        if apply_changes and any_updated:
            ContentDiscoveryEpoch.increment_epoch()

        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
