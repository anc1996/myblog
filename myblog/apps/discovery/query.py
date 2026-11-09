"""
全站统一内容查询与发现核心服务 (ContentQuery)。

落实契约：
1. 单次只读快照 (REPEATABLE READ READ ONLY) 保障列表、精确总数与分桶统计快照一致 (SEARCH-RANK-02)；
2. 严格遵循 SEARCH-MODE-02 规则：
   - 模式 A（原句连续包含）：A >= 5 条时默认仅展示 A；1 <= A < 5 时默认仅展示 A 并在底部提供手动展开 B 按钮；
   - 模式 A 命中为 0 时自动转入模式 B（Jieba 分词多词组 AND）；
   - 模式 B 命中少于 10 条且 expand!='off' 时自动触发模式 C（受控同义词扩展）；
   - 若 spec.secondary == 'words'，在模式 A 底部手动展开模式 B；
   - 无关键词查询走 FILTER 模式；
3. 游标失效自动刷新机制 (RECYCLE-DISCOVERY-02)：
   - 游标中编码当前 Epoch；若因内容更新导致 Epoch 不一致，自动重置回第 1 页并标记 cursor_reset=True；
4. 组合筛选：支持类型、栏目树（descendants / children / include_root）、标签（ANY / ALL）、成文日期闭区间。
"""

import base64
from datetime import date
import json
import logging
from typing import Any
from django.db import connection, transaction
from django.db.models import Q
from django.contrib.postgres.search import SearchQuery
from django.http import Http404

from discovery.contracts import QuerySpec, QueryResult, SearchResultItem
from discovery.eligibility import apply_public_eligibility_filter
from discovery.ranking import (
    MATCH_MODE_TO_TIER_RANK,
    TIER_RANK_FILTER,
    compute_query_fingerprint,
    encode_keyset_cursor,
    decode_keyset_cursor,
)
from discovery.models import (
    ContentDiscoveryEpoch,
    PublishedSearchDocument,
    PublishedSearchChunk,
    PublishedSearchTag,
)
from discovery.analysis import normalize_text, protect_tech_terms, tokenize_text
from discovery.synonyms import expand_terms
from discovery.facets import compute_facets, compute_archive_groups
from content_lifecycle.access import AccessContext, AccessMode

logger = logging.getLogger(__name__)


def _encode_cursor(epoch: int, offset: int) -> str:
    """将翻页位置与当前 Epoch 打包为 Base64 游标"""
    data = {"epoch": epoch, "offset": offset}
    payload = json.dumps(data).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("utf-8")


def _decode_cursor(cursor_str: str | None) -> tuple[int | None, int]:
    """解析游标，返回 (cursor_epoch, offset)"""
    if not cursor_str:
        return None, 0
    try:
        payload = base64.urlsafe_b64decode(cursor_str.encode("utf-8")).decode("utf-8")
        data = json.loads(payload)
        return data.get("epoch"), int(data.get("offset", 0))
    except Exception:
        return None, 0


def _extract_highlight(text: str, query_term: str, window_size: int = 120) -> str:
    """从原文提取包含关键词的高亮摘要片段"""
    if not text or not query_term:
        return text[:window_size] if text else ""
    idx = text.lower().find(query_term.lower())
    if idx == -1:
        return text[:window_size]
    start = max(0, idx - 40)
    end = min(len(text), idx + len(query_term) + 80)
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


class ContentQuery:
    """全站统一内容检索与发现核心入口"""

    @classmethod
    def execute(cls, spec: QuerySpec, context: AccessContext | None = None) -> QueryResult:
        """
        执行全站统一内容检索与组合筛选：
        在单一 REPEATABLE READ READ ONLY 事务中完成匹配、排序、精确总数与分桶统计。
        """
        if context is None:
            context = AccessContext(mode=AccessMode.PUBLIC)

        page_size = max(1, min(spec.page_size, 50))
        query_fp = compute_query_fingerprint(
            q=spec.q,
            search_fields=spec.search_fields,
            root_page_id=spec.root_page_id,
            tree_scope=spec.tree_scope,
            type_keys=spec.type_keys,
            date_from=spec.date_from,
            date_to=spec.date_to,
            sort=spec.sort,
            page_size=page_size,
            extra_filters=spec.extra_filters,
        )

        # 快照事务状态机检查 (B-03)
        is_outer_testcase = False
        if connection.in_atomic_block:
            if all(getattr(b, "_from_testcase", False) for b in connection.atomic_blocks):
                is_outer_testcase = True
            elif getattr(connection, "_in_discovery_snapshot", False):
                # 嵌套在同一个快照事务中
                pass
            else:
                raise RuntimeError("ContentQuery cannot be called inside a foreign read-write atomic transaction block.")

        # 决定是否需要在本层开启事务与设置只读隔离级别
        should_manage_atomic = not connection.in_atomic_block
        atomic_ctx = transaction.atomic() if should_manage_atomic else transaction.atomic(savepoint=False)

        with atomic_ctx:
            prev_snapshot_flag = getattr(connection, "_in_discovery_snapshot", False)
            connection._in_discovery_snapshot = True
            try:
                if connection.vendor == "postgresql" and should_manage_atomic and not is_outer_testcase:
                    try:
                        with connection.cursor() as cur:
                            cur.execute("SET LOCAL TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL statement_timeout = '2000ms';")
                    except Exception as exc:
                        logger.warning("Failed to set REPEATABLE READ isolation level: %s", exc)
                        raise

                return cls._execute_snapshot(spec, context, page_size, query_fp)
            finally:
                connection._in_discovery_snapshot = prev_snapshot_flag

    @classmethod
    def _execute_snapshot(
        cls,
        spec: QuerySpec,
        context: AccessContext,
        page_size: int,
        query_fp: str,
    ) -> QueryResult:
        """在已确立的快照环境中执行真正的过滤、检索、排序与统计"""
        current_epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()

        # 游标解析：双轨支持 v2 Keyset 游标与旧 Base64 Offset 游标
        is_keyset = spec.cursor is not None and spec.cursor.startswith("v2:")
        keyset_payload: dict[str, Any] | None = None
        cursor_reset = False
        offset = 0

        if is_keyset:
            valid_ks, ks_data, err = decode_keyset_cursor(spec.cursor, current_epoch, query_fp)
            if err == "reset":
                cursor_reset = True
            elif err == "invalid":
                raise ValueError("Invalid keyset cursor signature or format.")
            elif valid_ks:
                keyset_payload = ks_data
        else:
            c_epoch, c_offset = _decode_cursor(spec.cursor)
            offset = c_offset
            if spec.cursor is not None and c_epoch is not None and c_epoch != current_epoch:
                cursor_reset = True
                offset = 0

        # 1. 基础候选集与严格公开资格核验 (A-04 / B-03)
        candidates = PublishedSearchDocument.objects.filter(is_live=True, is_recycled=False)
        candidates = apply_public_eligibility_filter(candidates)

        # 站点过滤
        if spec.site_id is not None:
            candidates = candidates.filter(site_id=spec.site_id)

        # 类型过滤
        if spec.type_keys:
            candidates = candidates.filter(type_key__in=spec.type_keys)

        # 页面树层级范围过滤 (descendants / children / include_root)
        navigation_items: list[dict[str, Any]] = []
        navigation_total_count = 0
        root_doc = None

        if spec.root_page_id:
            root_doc = PublishedSearchDocument.objects.filter(page_id=spec.root_page_id, is_live=True, is_recycled=False).first()
            if not root_doc and spec.include_listing_scope:
                # 尝试为尚未初始投影的合法 live 页面生成副本
                from wagtail.models import Page
                db_page = Page.objects.filter(id=spec.root_page_id).first()
                if not db_page or not db_page.live or getattr(db_page, "is_recycled", False):
                    raise Http404(f"Root listing page {spec.root_page_id} not found or not published.")
                from discovery.projection import project_page_to_discovery
                root_doc = project_page_to_discovery(db_page)

            if root_doc:
                if spec.tree_scope == "children":
                    tree_q = Q(root_path__startswith=root_doc.root_path, depth=root_doc.depth + 1)
                else:
                    tree_q = Q(root_path__startswith=root_doc.root_path, depth__gt=root_doc.depth)
                if spec.include_root:
                    tree_q |= Q(page_id=root_doc.page_id)
                candidates = candidates.filter(tree_q)

                # D4-A: 获取直接子栏目导航项
                if spec.include_listing_scope:
                    child_indexes_qs = PublishedSearchDocument.objects.filter(
                        root_path__startswith=root_doc.root_path,
                        depth=root_doc.depth + 1,
                        type_key__endswith="_index",
                        is_live=True,
                        is_recycled=False,
                    ).order_by("title", "page_id")
                    child_indexes = list(child_indexes_qs[:50])
                    navigation_total_count = child_indexes_qs.count()
                    navigation_items = [
                        {
                            "page_id": c.page_id,
                            "title": c.title,
                            "url": c.url_path,
                            "summary": c.summary,
                            "extra_data": c.extra_data,
                        }
                        for c in child_indexes
                    ]
            else:
                candidates = candidates.none()

        # 单页面过滤
        if spec.page_id:
            candidates = candidates.filter(page_id=spec.page_id)

        # 标签过滤 (ANY / ALL)
        if spec.tag_ids:
            if spec.tag_mode == "ALL":
                for tid in spec.tag_ids:
                    candidates = candidates.filter(search_tags__tag_id=tid)
            else:
                candidates = candidates.filter(search_tags__tag_id__in=spec.tag_ids).distinct()

        # 日期过滤 (闭区间)
        if spec.year:
            candidates = candidates.filter(authored_on__year=spec.year)
        if spec.month:
            candidates = candidates.filter(authored_on__month=spec.month)
        if spec.date_from:
            candidates = candidates.filter(authored_on__gte=spec.date_from)
        if spec.date_to:
            candidates = candidates.filter(authored_on__lte=spec.date_to)

        # 专属业务筛选器过滤 (A-03)
        if spec.extra_filters:
            for f_param, f_val in spec.extra_filters.items():
                if f_val is not None and str(f_val).strip():
                    from discovery.listing.registry import _FILTER_REGISTRY
                    matched_filter = None
                    for filter_def in _FILTER_REGISTRY.values():
                        if filter_def.query_param == f_param:
                            matched_filter = filter_def
                            break
                    if matched_filter:
                        candidates = matched_filter.apply_filter(candidates, f_val)
                    else:
                        candidates = candidates.filter(**{f"extra_data__{f_param}": f_val})

        # 2. 搜索模式判定与执行
        raw_q = (spec.q or "").strip()
        match_mode = "FILTER"
        expansion_used = False
        secondary_used = False
        matched_doc_ids: list[int] = []
        doc_tier_map: dict[int, int] = {}  # doc_id -> match_tier (A=0, B=1, C=2, FILTER=0)
        doc_item_mode_map: dict[int, str] = {}

        if not raw_q:
            # 无关键词：纯 FILTER 模式
            match_mode = "FILTER"
            total_count = candidates.count()
            if spec.sort == "authored_asc":
                ordered_qs = candidates.order_by("authored_on", "page_id")
            else:
                ordered_qs = candidates.order_by("-authored_on", "-page_id")

            # Keyset 或 Offset 分页
            if is_keyset and keyset_payload:
                bound_ao = date.fromisoformat(keyset_payload["ao"])
                bound_pid = keyset_payload["pid"]
                if spec.sort == "authored_asc":
                    ordered_qs = ordered_qs.filter(
                        Q(authored_on__gt=bound_ao) | Q(authored_on=bound_ao, page_id__gt=bound_pid)
                    )
                else:
                    ordered_qs = ordered_qs.filter(
                        Q(authored_on__lt=bound_ao) | Q(authored_on=bound_ao, page_id__lt=bound_pid)
                    )
                paged_docs = list(ordered_qs[: page_size + 1])
            else:
                paged_docs = list(ordered_qs[offset : offset + page_size + 1])

            has_more = len(paged_docs) > page_size
            items_to_render = paged_docs[:page_size]

            facets_data = compute_facets(candidates, spec.facets)
            archive_data = compute_archive_groups(candidates)

            items = [
                SearchResultItem(
                    page_id=doc.page_id,
                    effective_revision_id=doc.effective_revision_id,
                    type_key=doc.type_key,
                    title=doc.title,
                    url=doc.url_path,
                    authored_on=doc.authored_on,
                    summary=doc.summary,
                    match_mode="FILTER",
                    score_key=0,
                    tags=[
                        {"id": t.tag_id, "name": t.tag_name, "slug": t.tag_slug}
                        for t in doc.search_tags.all()
                    ],
                    column_metadata=doc.extra_data,
                )
                for doc in items_to_render
            ]

            next_cursor = None
            previous_cursor = None
            if has_more and items_to_render:
                last_doc = items_to_render[-1]
                if is_keyset or spec.include_listing_scope:
                    next_cursor = encode_keyset_cursor(
                        epoch=current_epoch,
                        direction="next",
                        tier_rank=TIER_RANK_FILTER,
                        score_key=0,
                        authored_on=last_doc.authored_on,
                        page_id=last_doc.page_id,
                        fingerprint=query_fp,
                    )
                else:
                    next_cursor = _encode_cursor(current_epoch, offset + page_size)

            if (is_keyset and keyset_payload) or offset > 0:
                previous_cursor = "prev:available"

            return QueryResult(
                items=items,
                total_count=total_count,
                facets=facets_data,
                archive_groups=archive_data,
                next_cursor=next_cursor,
                previous_cursor=previous_cursor,
                navigation_items=navigation_items,
                navigation_total_count=navigation_total_count,
                match_policy="filter_only",
                expansion_used=False,
                secondary_used=False,
                corpus_epoch=current_epoch,
                cursor_reset=cursor_reset,
            )

        # 有关键词检索：落实 SEARCH-MODE-02 规则
        norm_q = normalize_text(raw_q)
        prot_q = protect_tech_terms(norm_q)

        # Step 1: 优先检查模式 A（连续原句匹配）
        chunks_A_qs = PublishedSearchChunk.objects.filter(document__in=candidates)
        if spec.search_fields == "title":
            chunks_A_qs = chunks_A_qs.filter(field_name="title")

        chunks_A = chunks_A_qs.filter(
            Q(normalized_text__contains=norm_q) | Q(normalized_text__contains=prot_q)
        )
        doc_ids_A = list(chunks_A.values_list("document_id", flat=True).distinct())
        count_A = len(doc_ids_A)

        if count_A >= 5:
            match_mode = "A"
            secondary_used = False
            matched_doc_ids = doc_ids_A
            for did in doc_ids_A:
                doc_tier_map[did] = 0
                doc_item_mode_map[did] = "A"
        elif 1 <= count_A < 5:
            match_mode = "A"
            if spec.secondary == "words":
                secondary_used = True
                for did in doc_ids_A:
                    doc_tier_map[did] = 0
                    doc_item_mode_map[did] = "A"
                terms = tokenize_text(norm_q) or [norm_q]
                doc_ids_B_extra = cls._match_mode_b_doc_ids(
                    candidates.exclude(id__in=doc_ids_A), terms, spec.search_fields
                )
                for did in doc_ids_B_extra:
                    doc_tier_map[did] = 1
                    doc_item_mode_map[did] = "B"
                matched_doc_ids = doc_ids_A + doc_ids_B_extra
            else:
                secondary_used = False
                matched_doc_ids = doc_ids_A
                for did in doc_ids_A:
                    doc_tier_map[did] = 0
                    doc_item_mode_map[did] = "A"
        else:
            # 模式 A 命中 0 条：自动转入模式 B
            terms = tokenize_text(norm_q) or [norm_q]
            doc_ids_B = cls._match_mode_b_doc_ids(candidates, terms, spec.search_fields)
            count_B = len(doc_ids_B)
            for did in doc_ids_B:
                doc_tier_map[did] = 1
                doc_item_mode_map[did] = "B"

            doc_ids_C_extra: list[int] = []
            if count_B < 10 and spec.expand != "off":
                concept_groups, exp_used = expand_terms(terms)
                if exp_used:
                    all_doc_ids_C = cls._match_mode_c_doc_ids(candidates, concept_groups, spec.search_fields)
                    doc_ids_C_extra = [did for did in all_doc_ids_C if did not in doc_ids_B]
                    if doc_ids_C_extra:
                        expansion_used = True
                        for did in doc_ids_C_extra:
                            doc_tier_map[did] = 2
                            doc_item_mode_map[did] = "C"

            matched_doc_ids = doc_ids_B + doc_ids_C_extra
            match_mode = "B" if doc_ids_B else ("C" if doc_ids_C_extra else "B")

        # 3. 排序与分页
        matched_docs_qs = PublishedSearchDocument.objects.filter(id__in=matched_doc_ids).prefetch_related(
            "search_tags", "chunks"
        )
        doc_dict = {d.id: d for d in matched_docs_qs}

        # 严格等级优先排序：match_tier ASC -> authored_on DESC -> page_id DESC
        sort_direction = -1 if spec.sort != "authored_asc" else 1

        def sort_key(doc_id: int):
            tier = doc_tier_map.get(doc_id, 0)
            doc = doc_dict.get(doc_id)
            if not doc:
                return (tier, 0, 0)
            date_num = doc.authored_on.toordinal() * sort_direction
            pid = doc.page_id * sort_direction
            return (tier, -date_num, -pid)

        sorted_doc_ids = sorted(matched_doc_ids, key=sort_key)
        total_count = len(sorted_doc_ids)

        # Keyset 游标定位
        if is_keyset and keyset_payload:
            bound_tr = keyset_payload["tr"]
            bound_sk = keyset_payload["sk"]
            bound_ao = date.fromisoformat(keyset_payload["ao"])
            bound_pid = keyset_payload["pid"]
            filtered_ids = []
            for did in sorted_doc_ids:
                doc = doc_dict.get(did)
                if not doc:
                    continue
                t_rank = MATCH_MODE_TO_TIER_RANK.get(doc_item_mode_map.get(did, "FILTER"), 3)
                s_key = 100 - doc_tier_map.get(did, 0) * 20
                row_tuple = (t_rank, s_key, doc.authored_on, doc.page_id)
                bound_tuple = (bound_tr, bound_sk, bound_ao, bound_pid)
                if row_tuple < bound_tuple:
                    filtered_ids.append(did)
            paged_ids = filtered_ids[: page_size + 1]
        else:
            paged_ids = sorted_doc_ids[offset : offset + page_size + 1]

        has_more = len(paged_ids) > page_size
        items_ids = paged_ids[:page_size]
        paged_docs = [doc_dict[did] for did in items_ids if did in doc_dict]

        # 4. 生成展示项与高亮
        items: list[SearchResultItem] = []
        for doc in paged_docs:
            item_mode = doc_item_mode_map.get(doc.id, match_mode)
            first_chunk = doc.chunks.first()
            chunk_text = first_chunk.normalized_text if first_chunk else doc.summary
            highlight_snippet = _extract_highlight(chunk_text, norm_q)

            items.append(
                SearchResultItem(
                    page_id=doc.page_id,
                    effective_revision_id=doc.effective_revision_id,
                    type_key=doc.type_key,
                    title=doc.title,
                    url=doc.url_path,
                    authored_on=doc.authored_on,
                    summary=doc.summary,
                    highlights=[{"snippet": highlight_snippet, "term": norm_q}],
                    match_mode=item_mode,
                    score_key=100 - doc_tier_map.get(doc.id, 0) * 20,
                    tags=[
                        {"id": t.tag_id, "name": t.tag_name, "slug": t.tag_slug}
                        for t in doc.search_tags.all()
                    ],
                    column_metadata=doc.extra_data,
                )
            )

        # 5. 精确分桶与归档统计
        final_hit_qs = PublishedSearchDocument.objects.filter(id__in=matched_doc_ids)
        facets_data = compute_facets(final_hit_qs, spec.facets)
        archive_data = compute_archive_groups(final_hit_qs)

        next_cursor = None
        previous_cursor = None
        if has_more and items:
            last_item = items[-1]
            last_mode = last_item.match_mode
            t_rank = MATCH_MODE_TO_TIER_RANK.get(last_mode, 3)
            if is_keyset or spec.include_listing_scope:
                next_cursor = encode_keyset_cursor(
                    epoch=current_epoch,
                    direction="next",
                    tier_rank=t_rank,
                    score_key=last_item.score_key,
                    authored_on=last_item.authored_on,
                    page_id=last_item.page_id,
                    fingerprint=query_fp,
                )
            else:
                next_cursor = _encode_cursor(current_epoch, offset + page_size)

        if (is_keyset and keyset_payload) or offset > 0:
            previous_cursor = "prev:available"

        return QueryResult(
            items=items,
            total_count=total_count,
            facets=facets_data,
            archive_groups=archive_data,
            next_cursor=next_cursor,
            previous_cursor=previous_cursor,
            navigation_items=navigation_items,
            navigation_total_count=navigation_total_count,
            match_policy="tier_ranked",
            expansion_used=expansion_used,
            secondary_used=secondary_used,
            corpus_epoch=current_epoch,
            cursor_reset=cursor_reset,
        )

    @classmethod
    def _match_mode_b_doc_ids(cls, qs, terms: list[str], search_fields: str = "all") -> list[int]:
        """模式 B 匹配：所有必需词元组间严格 AND 命中"""
        if not terms:
            return list(qs.values_list("id", flat=True))

        current_matched_ids: set[int] | None = None

        for term in terms:
            t_norm = term.strip()
            if not t_norm:
                continue
            chunk_qs = PublishedSearchChunk.objects.filter(document__in=qs)
            if search_fields == "title":
                chunk_qs = chunk_qs.filter(field_name="title")

            chunk_q = Q(normalized_text__contains=t_norm)
            if connection.vendor == "postgresql":
                chunk_q = Q(search_vector=SearchQuery(t_norm, config="simple"))

            hit_doc_ids = set(
                chunk_qs.filter(chunk_q).values_list("document_id", flat=True).distinct()
            )

            if current_matched_ids is None:
                current_matched_ids = hit_doc_ids
            else:
                current_matched_ids &= hit_doc_ids

            if not current_matched_ids:
                return []

        return list(current_matched_ids or [])

    @classmethod
    def _match_mode_c_doc_ids(cls, qs, concept_groups: list[list[str]], search_fields: str = "all") -> list[int]:
        """模式 C 匹配：每个概念组内部为 OR，概念组之间为 AND"""
        if not concept_groups:
            return list(qs.values_list("id", flat=True))

        current_matched_ids: set[int] | None = None

        for group in concept_groups:
            group_q = Q()
            for word in group:
                w = word.strip()
                if not w:
                    continue
                term_q = Q(normalized_text__contains=w)
                if connection.vendor == "postgresql":
                    term_q = Q(search_vector=SearchQuery(w, config="simple"))
                group_q |= term_q

            chunk_qs = PublishedSearchChunk.objects.filter(document__in=qs)
            if search_fields == "title":
                chunk_qs = chunk_qs.filter(field_name="title")

            hit_doc_ids = set(
                chunk_qs.filter(group_q).values_list("document_id", flat=True).distinct()
            )

            if current_matched_ids is None:
                current_matched_ids = hit_doc_ids
            else:
                current_matched_ids &= hit_doc_ids

            if not current_matched_ids:
                return []

        return list(current_matched_ids or [])
