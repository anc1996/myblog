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
    【核心搜索分块算法】：按 SEARCH-CHUNK-02 规则切分长文本。

    【教学点：为什么需要“非对称重叠分块”？】
    长篇技术文章动辄数万字，如果整体存为单条数据库记录，会导致 GIN 索引膨胀、高亮截取困难，且距离评分失效。
    因此必须切分为约 1500 字符的微型分块（PublishedSearchChunk）。
    但在切分时面临经典的两难困境：
    - 困境 1（硬切断）：如果切块之间完全不重叠，若用户搜一句连续原话（如“PostgreSQL 18 事务隔离”），
      恰好被硬生生切在块 1 的末尾与块 2 的开头，原话就会被截断，导致模式 A（字面匹配）彻底搜不到！
    - 困境 2（双重重叠）：如果让两个分块都带 256 字符重叠，那么重叠区间的词元就会在两块的倒排索引（search_vector）
      里重复出现，导致模式 B/C 分词检索时产生“重复计分（Double Counting）”与索引体积膨胀！

    【破局方案：非对称分块（Asymmetric Slicing）】：
    1. normalized_text（供模式 A 原文连续匹配）：保留相邻块间 256 字符重叠，无惧跨边界截断；
    2. search_vector（供模式 B/C 词向量检索）：词元严格从无重叠的独占区间生成，保证全篇每个词在索引中有且仅有一次！

    【参数与变量解析】：
    - text (str): 待分块处理的原始正文内容。
    - max_chunk_chars (int): 单块核心字符容量上限（默认 1500 字符）。
    - overlap_chars (int): 相邻块原文滑动重叠窗口大小（默认 256 字符）。
    - norm (str): 局部变量，全文经 Unicode NFKC 归一化并合并多余空白后的纯净文本。
    - total_len (int): 局部变量，规范化全文的总字符数。
    - non_overlapping_slice (str): 局部变量，当前分块独占的核心正文切片（0 重叠），用于提取分词。
    - overlapping_slice (str): 局部变量，向左回溯了 256 字符的重叠原文切片，存入 normalized_text。
    - search_terms_str (str): 局部变量，由独占切片分词后以空格连接的词串，供 PostgreSQL 生成 GIN 向量。
    """
    if not text:
        return []

    norm = normalize_text(text)
    total_len = len(norm)
    # 分支 1：短文本无需切分（单块容纳）
    if total_len <= max_chunk_chars:
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

    # 分支 2：长文本执行“非对称滑动分块”
    chunks: list[dict] = []
    step = max_chunk_chars - overlap_chars
    if step <= 0:
        step = max_chunk_chars // 2

    non_overlap_step = max_chunk_chars
    chunk_index = 0              # 分块序号（从 0 递增）
    curr_non_overlap_start = 0   # 当前独占核心区间的起始偏移（左闭）

    while curr_non_overlap_start < total_len:
        # 计算当前独占核心区间的结束偏移（右开）
        curr_non_overlap_end = min(curr_non_overlap_start + non_overlap_step, total_len)
        # 1. 独占切片：严格无重叠，文章全局无交集
        non_overlapping_slice = norm[curr_non_overlap_start:curr_non_overlap_end]

        # 2. 模式 A 原文重叠窗口：除第 0 块外，向左回溯 overlap_chars（256字符）
        overlap_start = max(0, curr_non_overlap_start - (overlap_chars if chunk_index > 0 else 0))
        overlap_end = curr_non_overlap_end
        overlapping_slice = norm[overlap_start:overlap_end]

        # 3. 词向量生成铁律：只从独占切片生成词元，严格保证每个词在全站索引中仅计数一次！
        tokens = tokenize_text(non_overlapping_slice)
        search_terms_str = " ".join(tokens)

        chunks.append(
            {
                "chunk_index": chunk_index,           # 分块下标 (0, 1, 2...)
                "char_start": curr_non_overlap_start, # 核心区间物理起始字符位
                "char_end": curr_non_overlap_end,     # 核心区间物理结束字符位
                "normalized_text": overlapping_slice, # 带重叠的纯文本 (供模式 A 原文连续字面匹配)
                "search_terms_str": search_terms_str, # 无重叠的词元串 (供模式 B/C GIN 倒排索引)
            }
        )

        # 推进到下一个无重叠步长
        chunk_index += 1
        curr_non_overlap_start = curr_non_overlap_end

    return chunks
