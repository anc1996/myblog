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
import json
import logging
from typing import Any
from django.db import connection, transaction
from django.db.models import Q
from django.contrib.postgres.search import SearchQuery

from discovery.contracts import QuerySpec, QueryResult, SearchResultItem
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

        cursor_epoch, cursor_offset = _decode_cursor(spec.cursor)

        page_size = max(1, min(spec.page_size, 50))

        was_in_atomic = connection.in_atomic_block
        with transaction.atomic():
            # 开启只读可重复读隔离级别，确保同事务内单快照绝对一致 (SEARCH-RANK-02)
            if connection.vendor == "postgresql" and not was_in_atomic:
                try:
                    with connection.cursor() as cur:
                        cur.execute("SET LOCAL TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;")
                except Exception as exc:
                    logger.debug("Failed to set REPEATABLE READ isolation level: %s", exc)

            current_epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()

            # RECYCLE-DISCOVERY-02: 游标失效判定
            cursor_reset = False
            offset = cursor_offset
            if spec.cursor is not None:
                if cursor_epoch is not None and cursor_epoch != current_epoch:
                    cursor_reset = True
                    offset = 0  # 自动重置为第 1 页

            # 1. 基础合法候选集 E (仅限已上线、未回收内容)
            candidates = PublishedSearchDocument.objects.filter(is_live=True, is_recycled=False)

            # 站点过滤
            if spec.site_id is not None:
                candidates = candidates.filter(site_id=spec.site_id)

            # 类型过滤 (多选 OR)
            if spec.type_keys:
                candidates = candidates.filter(type_key__in=spec.type_keys)

            # 页面树层级范围过滤 (descendants / children / include_root)
            if spec.root_page_id:
                root_doc = PublishedSearchDocument.objects.filter(page_id=spec.root_page_id).first()
                if root_doc:
                    if spec.tree_scope == "children":
                        tree_q = Q(root_path__startswith=root_doc.root_path, depth=root_doc.depth + 1)
                    else:
                        tree_q = Q(root_path__startswith=root_doc.root_path, depth__gt=root_doc.depth)
                    if spec.include_root:
                        tree_q |= Q(page_id=root_doc.page_id)
                    candidates = candidates.filter(tree_q)

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

                paged_docs = list(ordered_qs[offset : offset + page_size])
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
                    )
                    for doc in paged_docs
                ]

                next_cursor = None
                if offset + page_size < total_count:
                    next_cursor = _encode_cursor(current_epoch, offset + page_size)

                return QueryResult(
                    items=items,
                    total_count=total_count,
                    facets=facets_data,
                    archive_groups=archive_data,
                    next_cursor=next_cursor,
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
            chunks_A = PublishedSearchChunk.objects.filter(
                document__in=candidates
            ).filter(
                Q(normalized_text__icontains=norm_q) | Q(normalized_text__icontains=prot_q)
            )
            doc_ids_A = list(chunks_A.values_list("document_id", flat=True).distinct())
            count_A = len(doc_ids_A)

            if count_A >= 5:
                # 模式 A >= 5 条：默认仅展示 A，严格原句优先、不自动混排
                match_mode = "A"
                secondary_used = False
                matched_doc_ids = doc_ids_A
                for did in doc_ids_A:
                    doc_tier_map[did] = 0
                    doc_item_mode_map[did] = "A"

            elif 1 <= count_A < 5:
                # 模式 A 命中 1~4 条
                match_mode = "A"
                if spec.secondary == "words":
                    # 用户主动点击底部按钮展开模式 B！
                    secondary_used = True
                    for did in doc_ids_A:
                        doc_tier_map[did] = 0
                        doc_item_mode_map[did] = "A"

                    # 在排除已命中 A 的候选集中执行模式 B
                    terms = tokenize_text(norm_q) or [norm_q]
                    doc_ids_B_extra = cls._match_mode_b_doc_ids(candidates.exclude(id__in=doc_ids_A), terms)
                    for did in doc_ids_B_extra:
                        doc_tier_map[did] = 1
                        doc_item_mode_map[did] = "B"

                    matched_doc_ids = doc_ids_A + doc_ids_B_extra
                else:
                    # 默认仅展示 A，并在结果中标注供前端显示展开按钮
                    secondary_used = False
                    matched_doc_ids = doc_ids_A
                    for did in doc_ids_A:
                        doc_tier_map[did] = 0
                        doc_item_mode_map[did] = "A"

            else:
                # 模式 A 命中 0 条：自动转入模式 B
                terms = tokenize_text(norm_q) or [norm_q]
                doc_ids_B = cls._match_mode_b_doc_ids(candidates, terms)
                count_B = len(doc_ids_B)

                # 模式 B 命中项赋予 tier 1
                for did in doc_ids_B:
                    doc_tier_map[did] = 1
                    doc_item_mode_map[did] = "B"

                # 检查模式 C 门槛（模式 B < 10 条且 expand != 'off'，且有受控同义词可供扩展）
                doc_ids_C_extra: list[int] = []
                if count_B < 10 and spec.expand != "off":
                    concept_groups, exp_used = expand_terms(terms)
                    if exp_used:
                        all_doc_ids_C = cls._match_mode_c_doc_ids(candidates, concept_groups)
                        # 模式 C 必须是实际使用了同义词扩展的新增命中，排除已有 B
                        doc_ids_C_extra = [did for did in all_doc_ids_C if did not in doc_ids_B]
                        if doc_ids_C_extra:
                            expansion_used = True
                            for did in doc_ids_C_extra:
                                doc_tier_map[did] = 2
                                doc_item_mode_map[did] = "C"

                matched_doc_ids = doc_ids_B + doc_ids_C_extra
                if doc_ids_B:
                    match_mode = "B"
                elif doc_ids_C_extra:
                    match_mode = "C"
                else:
                    match_mode = "B"

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

            paged_ids = sorted_doc_ids[offset : offset + page_size]
            paged_docs = [doc_dict[did] for did in paged_ids if did in doc_dict]

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
                    )
                )

            # 5. 精确分桶与归档统计（基于当前合法命中集）
            final_hit_qs = PublishedSearchDocument.objects.filter(id__in=matched_doc_ids)
            facets_data = compute_facets(final_hit_qs, spec.facets)
            archive_data = compute_archive_groups(final_hit_qs)

            next_cursor = None
            if offset + page_size < total_count:
                next_cursor = _encode_cursor(current_epoch, offset + page_size)

            return QueryResult(
                items=items,
                total_count=total_count,
                facets=facets_data,
                archive_groups=archive_data,
                next_cursor=next_cursor,
                match_policy="tier_ranked",
                expansion_used=expansion_used,
                secondary_used=secondary_used,
                corpus_epoch=current_epoch,
                cursor_reset=cursor_reset,
            )

    @classmethod
    def _match_mode_b_doc_ids(cls, qs, terms: list[str]) -> list[int]:
        """模式 B 匹配：所有必需词元组间严格 AND 命中"""
        if not terms:
            return list(qs.values_list("id", flat=True))

        current_matched_ids: set[int] | None = None

        for term in terms:
            t_norm = term.strip()
            if not t_norm:
                continue
            # 在块中查找包含该词元的文档 ID（结合 search_vector 或 normalized_text 包含）
            chunk_q = Q(normalized_text__icontains=t_norm)
            if connection.vendor == "postgresql":
                chunk_q |= Q(search_vector=SearchQuery(t_norm, config="simple"))

            hit_doc_ids = set(
                PublishedSearchChunk.objects.filter(document__in=qs)
                .filter(chunk_q)
                .values_list("document_id", flat=True)
                .distinct()
            )

            if current_matched_ids is None:
                current_matched_ids = hit_doc_ids
            else:
                current_matched_ids &= hit_doc_ids

            # 若某词元在所有候选文档中均未命中，整组 AND 立即归零
            if not current_matched_ids:
                return []

        return list(current_matched_ids or [])

    @classmethod
    def _match_mode_c_doc_ids(cls, qs, concept_groups: list[list[str]]) -> list[int]:
        """模式 C 匹配：每个概念组内部为 OR（原词 + 同义词），概念组之间为 AND"""
        if not concept_groups:
            return list(qs.values_list("id", flat=True))

        current_matched_ids: set[int] | None = None

        for group in concept_groups:
            group_q = Q()
            for word in group:
                w = word.strip()
                if not w:
                    continue
                term_q = Q(normalized_text__icontains=w)
                if connection.vendor == "postgresql":
                    term_q |= Q(search_vector=SearchQuery(w, config="simple"))
                group_q |= term_q

            hit_doc_ids = set(
                PublishedSearchChunk.objects.filter(document__in=qs)
                .filter(group_q)
                .values_list("document_id", flat=True)
                .distinct()
            )

            if current_matched_ids is None:
                current_matched_ids = hit_doc_ids
            else:
                current_matched_ids &= hit_doc_ids

            if not current_matched_ids:
                return []

        return list(current_matched_ids or [])
