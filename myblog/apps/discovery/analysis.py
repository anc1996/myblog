"""
搜索文本分析与分块服务 (Unicode NFKC 归一化、技术词保护、Jieba 中文分词与重叠分块)。

落实契约：
1. Unicode NFKC 归一化与空白收敛；
2. 关键技术词与版本保护 (C++ -> techcpp, C# -> techcsharp, PostgreSQL 18 -> techpostgres18, Django 6.1 -> techdjango61)；
3. SEARCH-CHUNK-02:
   - 仅在 normalized_text 保留相邻分块间 256 字符重叠（供模式 A 连续原句匹配）；
   - search_vector 严格按不重叠分块切分词元（供模式 B/C 分词检索），杜绝重复计分并节省索引空间。
"""

import re
import unicodedata
import jieba


# 常用标点与停用符号集合
PUNCTUATION_SET = set(" \t\r\n，。！？；：、“”‘’（）《》【】…—·,;!?:\"'()[]{}<>-/\\|`~@#$%^&*+=_")

# 技术词保护映射表（双向映射）
TECH_TERMS_MAP = [
    (re.compile(r"\bC\+\+\b", re.IGNORECASE), "techcpp"),
    (re.compile(r"\bC#\b", re.IGNORECASE), "techcsharp"),
    (re.compile(r"\bPostgreSQL\s+18\b", re.IGNORECASE), "techpostgres18"),
    (re.compile(r"\bPostgreSQL\s+17\b", re.IGNORECASE), "techpostgres17"),
    (re.compile(r"\bDjango\s+6\.1\b", re.IGNORECASE), "techdjango61"),
    (re.compile(r"\bWorkflowState\b", re.IGNORECASE), "techworkflowstate"),
]


def normalize_text(text: str) -> str:
    """Unicode NFKC 归一化、小写化与空白合并"""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKC", str(text))
    norm = norm.lower()
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm


def protect_tech_terms(text: str) -> str:
    """保护专业技术词与带版本号的词元，避免被通用分词切碎"""
    if not text:
        return ""
    res = text
    for pattern, placeholder in TECH_TERMS_MAP:
        res = pattern.sub(placeholder, res)
    return res


def tokenize_text(text: str) -> list[str]:
    """
    中文分词：
    1. 先归一化与技术词保护；
    2. 使用 Jieba 精确模式提取有意义词元；
    3. 过滤纯空白与无意义标点。
    """
    if not text:
        return []
    norm = normalize_text(text)
    protected = protect_tech_terms(norm)
    raw_tokens = jieba.cut(protected, cut_all=False)
    tokens: list[str] = []
    for tok in raw_tokens:
        t = tok.strip()
        if not t:
            continue
        if len(t) == 1 and t in PUNCTUATION_SET:
            continue
        tokens.append(t)
    return tokens


def chunk_text_for_search(
    text: str,
    max_chunk_chars: int = 1500,
    overlap_chars: int = 256,
) -> list[dict]:
    """
    按 SEARCH-CHUNK-02 规则切分文本块：
    1. normalized_text 保留相邻块间 overlap_chars (默认256) 字符重叠，供模式 A 匹配；
    2. search_vector 对应的词元严格按不重叠分块切分，防止重复计分。
    """
    if not text:
        return []

    norm = normalize_text(text)
    total_len = len(norm)
    if total_len <= max_chunk_chars:
        # 单块不超限：原文与分词无须拆分重叠
        tokens = tokenize_text(norm)
        return [
            {
                "chunk_index": 0,
                "char_start": 0,
                "char_end": total_len,
                "normalized_text": norm,
                "search_terms_str": " ".join(tokens),
            }
        ]

    chunks: list[dict] = []
    step = max_chunk_chars - overlap_chars
    if step <= 0:
        step = max_chunk_chars // 2

    non_overlap_step = max_chunk_chars
    chunk_index = 0
    curr_non_overlap_start = 0

    while curr_non_overlap_start < total_len:
        curr_non_overlap_end = min(curr_non_overlap_start + non_overlap_step, total_len)
        non_overlapping_slice = norm[curr_non_overlap_start:curr_non_overlap_end]

        # 模式 A 原文重叠窗口：向左回溯 overlap_chars
        overlap_start = max(0, curr_non_overlap_start - (overlap_chars if chunk_index > 0 else 0))
        overlap_end = curr_non_overlap_end
        overlapping_slice = norm[overlap_start:overlap_end]

        # 词向量只从无重叠的切片生成，严格保证每个词只在唯一块中出现一次！
        tokens = tokenize_text(non_overlapping_slice)
        search_terms_str = " ".join(tokens)

        chunks.append(
            {
                "chunk_index": chunk_index,
                "char_start": curr_non_overlap_start,
                "char_end": curr_non_overlap_end,
                "normalized_text": overlapping_slice,
                "search_terms_str": search_terms_str,
            }
        )

        chunk_index += 1
        curr_non_overlap_start = curr_non_overlap_end

    return chunks
