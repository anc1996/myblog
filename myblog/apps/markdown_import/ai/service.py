"""
超长文 AI 元数据生成与三级降级兜底服务。

保证：
1. 先用 StratifiedDigestBuilder 压缩到安全预算（默认 16,000 字符）；
2. 最多 3 次出网尝试：遇 context_limit / payload_limit 时按 16,000 -> 6,000 -> 3,000（或 16,000 -> 8,000 -> 4,000）折半降级；
3. 总时限 <= 20s；遇 429 限流、不可用、未配置、或关闭外发时，立即无缝回退本地确定性提取（source="local"），
   绝不因大模型异常阻断 10 万字文章创建草稿！
"""

from html import unescape
import re
import time
from typing import Any

from markdown_import.parser import (
    align_tags_with_vocabulary,
    fallback_title_from_filename,
    normalize_tags_input,
)
from .contracts import LLMClientProtocol, LLMRequest, MetadataSuggestion
from .digest import StratifiedDigestBuilder, _extract_jieba_keywords, redact_sensitive_tokens
from .errors import LLMProviderError

_GENERIC_TAGS = {"技术", "博客", "文章", "教程", "笔记", "分享", "总结", "开发", "编程"}
_METADATA_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "tags"],
}


def _strip_markdown_noise_for_summary(markdown_text: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", markdown_text or "")
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s+.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"[>*_`~|]+", " ", text)
    text = re.sub(r"\s+", " ", unescape(text)).strip()
    return redact_sensitive_tokens(text)


def _local_deterministic_fallback(
    markdown_text: str,
    *,
    source_name: str,
    existing_title: str,
    existing_tags: list[str],
    vocabulary_tags: list[str],
    allow_new_tags: bool,
    digest_stats: dict[str, Any],
    warnings: list[str],
) -> MetadataSuggestion:
    title = (existing_title or "").strip()[:50] or fallback_title_from_filename(source_name, markdown_text)[:50]
    plain = _strip_markdown_noise_for_summary(markdown_text)
    summary = plain[:150].strip() or f"关于《{title}》的技术文章草稿。"

    tags = list(existing_tags)
    if not tags:
        sample_text = (markdown_text or "")[:120_000]
        sample_lower = sample_text.casefold()
        # 1. 优先扫描站内已有词库在正文中的真实命中（按出现次数与词长排序，排除测试随机标签）
        scored_vocab: list[tuple[int, int, str]] = []
        seen_cf: set[str] = set()
        for vtag in vocabulary_tags:
            clean_v = str(vtag or "").strip()
            if len(clean_v) < 2 or clean_v.casefold() in _GENERIC_TAGS:
                continue
            if re.match(r"^tag-[0-9a-f]{4,}$", clean_v, re.IGNORECASE):
                continue
            cf = clean_v.casefold()
            if cf in seen_cf:
                continue
            seen_cf.add(cf)
            if re.match(r"^[A-Za-z0-9_+#.-]+$", clean_v):
                hits = len(re.findall(rf"(?<![A-Za-z0-9_]){re.escape(cf)}(?![A-Za-z0-9_])", sample_lower))
            else:
                hits = sample_lower.count(cf)
            if hits > 0:
                scored_vocab.append((hits, len(clean_v), clean_v))
        scored_vocab.sort(key=lambda x: (-x[0], -x[1], x[2]))
        for _, _, matched_tag in scored_vocab:
            if matched_tag.casefold() not in {t.casefold() for t in tags}:
                tags.append(matched_tag)
            if len(tags) >= 5:
                break

        # 2. 命中不足且允许新建标签时，用 jieba 提取的高频技术词补足
        if len(tags) < 3 and allow_new_tags:
            keywords = list(digest_stats.get("keywords") or []) + _extract_jieba_keywords(plain[:60_000], top_n=20)
            for kw in keywords:
                clean_kw = str(kw or "").strip()
                if len(clean_kw) < 2 or clean_kw.casefold() in _GENERIC_TAGS:
                    continue
                if clean_kw.casefold() not in {t.casefold() for t in tags}:
                    tags.append(clean_kw[:30])
                if len(tags) >= 5:
                    break

    return MetadataSuggestion(
        title=title,
        summary=summary,
        tags=tags[:5],
        source="local",
        warnings=warnings,
        digest_stats=digest_stats,
    )


def _validate_ai_json(
    parsed: dict[str, Any],
    *,
    existing_title: str,
    vocabulary_tags: list[str],
    allow_new_tags: bool,
) -> tuple[str, str, list[str]]:
    raw_title = re.sub(r"\s+", " ", str(parsed.get("title") or "")).strip()
    raw_summary = re.sub(r"\s+", " ", str(parsed.get("summary") or "")).strip()
    raw_tags = parsed.get("tags")

    if not existing_title and (not raw_title or len(raw_title) > 80):
        raise LLMProviderError(category="invalid_output", message="模型返回标题为空或超长。")
    if not raw_summary or len(raw_summary) > 260:
        raise LLMProviderError(category="invalid_output", message="模型返回导读简介为空或超长。")
    if not isinstance(raw_tags, list):
        raise LLMProviderError(category="invalid_output", message="模型返回标签不是数组。")

    cleaned_tags = [
        t for t in normalize_tags_input(raw_tags)
        if t.casefold() not in _GENERIC_TAGS and len(t) <= 30
    ]
    if vocabulary_tags and not allow_new_tags:
        aligned, _ = align_tags_with_vocabulary(cleaned_tags, vocabulary_tags)
        final_tags = aligned[:5]
    elif vocabulary_tags:
        aligned, unmatched = align_tags_with_vocabulary(cleaned_tags, vocabulary_tags)
        final_tags = (aligned + unmatched)[:5]
    else:
        final_tags = cleaned_tags[:5]

    final_title = (raw_title or existing_title).strip()[:50]
    return final_title, raw_summary[:180], final_tags


def generate_metadata_with_fallback(
    markdown_text: str,
    *,
    source_name: str = "",
    existing_title: str = "",
    existing_tags: list[str] | None = None,
    allow_external_ai: bool = False,
    llm_client: LLMClientProtocol | None = None,
    vocabulary_tags: list[str] | None = None,
    allow_new_tags: bool = False,
    total_deadline_seconds: float = 20.0,
    initial_budget_chars: int = 16_000,
) -> MetadataSuggestion:
    """
    读取长文生成（或补全）标题、内容讲解导读与技术标签；
    失败时自动按预算阶梯折半重试，最终回退本地提取，确保永不抛异常中断导入。
    """
    existing_tags_list = normalize_tags_input(existing_tags or [])
    vocab = list(vocabulary_tags or [])
    builder = StratifiedDigestBuilder()
    base_digest = builder.build(markdown_text, budget_chars=initial_budget_chars)
    warnings: list[str] = []

    if not allow_external_ai or llm_client is None:
        if not allow_external_ai:
            warnings.append("未开启外发 AI 分析，已使用本地确定性提取补全元数据。")
        else:
            warnings.append("未配置可用 AI 驱动，已自动回退本地提取。")
        return _local_deterministic_fallback(
            markdown_text,
            source_name=source_name,
            existing_title=existing_title,
            existing_tags=existing_tags_list,
            vocabulary_tags=vocab,
            allow_new_tags=allow_new_tags,
            digest_stats=base_digest.stats,
            warnings=warnings,
        )

    instructions = (
        "你是中文技术博客编辑。请仅根据提供的分层抽样摘要完成以下 JSON 字段：\n"
        "1. title: 准确凝练的文章标题（<=45字；如果摘要已有明确主题请直接提炼，不得编造）；\n"
        "2. summary: 100-160字纯文本文章核心讲解与导读，说清解决了什么问题与核心做法；\n"
        "3. tags: 3到5个具体技术关键词（严禁返回“技术、博客、教程、文章”等泛词）。"
    )

    started_at = time.monotonic()
    budget = initial_budget_chars
    max_attempts = 3

    for attempt in range(max_attempts):
        elapsed = time.monotonic() - started_at
        remaining = total_deadline_seconds - elapsed
        if remaining <= 1.0:
            warnings.append("AI 响应总时长已达上限，已自动回退本地提取。")
            break

        digest_obj = base_digest if budget == initial_budget_chars else builder.build(markdown_text, budget_chars=budget)
        req = LLMRequest(
            digest_text=digest_obj.text,
            instructions=instructions,
            json_schema=_METADATA_JSON_SCHEMA,
            max_output_tokens=600,
            timeout_seconds=min(15.0, max(2.0, remaining)),
        )
        try:
            res = llm_client.generate(req)
            parsed = res.parsed_json or {}
            title, summary, tags = _validate_ai_json(
                parsed,
                existing_title=existing_title,
                vocabulary_tags=vocab,
                allow_new_tags=allow_new_tags,
            )
            if attempt > 0:
                warnings.append(f"长文已自动压缩至 {budget} 字符摘要后生成成功。")
            return MetadataSuggestion(
                title=title,
                summary=summary,
                tags=existing_tags_list or tags,
                source="ai",
                warnings=warnings,
                digest_stats=digest_obj.stats,
            )
        except LLMProviderError as err:
            if err.category in {"context_limit", "payload_limit"} and attempt < max_attempts - 1:
                budget = min(6_000, budget // 2) if attempt == 0 else max(2_000, budget // 2)
                continue
            if err.category == "timeout" and attempt < max_attempts - 1:
                budget = max(4_000, budget // 2)
                continue
            if err.category == "invalid_output" and attempt < max_attempts - 1:
                continue
            warnings.append(f"AI 生成未完成（{err.category}），已自动回退本地提取。")
            break
        except Exception:
            warnings.append("AI 生成出现未预期异常，已自动回退本地提取。")
            break

    return _local_deterministic_fallback(
        markdown_text,
        source_name=source_name,
        existing_title=existing_title,
        existing_tags=existing_tags_list,
        vocabulary_tags=vocab,
        allow_new_tags=allow_new_tags,
        digest_stats=base_digest.stats,
        warnings=warnings,
    )
