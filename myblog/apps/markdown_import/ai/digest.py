"""
10 万字以上超长文结构化分层采样压缩器 (StratifiedDigestBuilder)。

工作方式：
1. 先对全文做一次线性扫描：剥除 Base64/data URI、截断长表格和超长代码正文、脱敏疑似密钥凭据；
2. 按总字符预算（默认 16,000，支持降级缩小）划分为四个区域：
   - 20% 全文 H1~H4 章节骨架 + jieba 高频技术关键词；
   - 30% 开篇背景段落；
   - 30% 中段跨所有 ## 章节均匀轮转抽样（绝不从头顺序截断，确保中段核心概念可见）；
   - 20% 结尾总结与结论段落。
所有裁剪与脱敏仅作用于发往模型的临时摘要，绝不改写草稿原文。
"""

from collections import Counter
from dataclasses import dataclass, field
import re
from typing import Any

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_BASE64_DATA_URI_RE = re.compile(r"data:[a-zA-Z0-9/+.-]+;base64,[A-Za-z0-9+/=]{32,}")
_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@[^\s)\"']+"),
    re.compile(r"(?i)(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9._-]{8,}['\"]?"),
)
_TECH_STOPWORDS = {
    "我们", "可以", "通过", "使用", "进行", "一个", "这个", "如果", "需要", "支持",
    "以及", "然后", "时候", "因为", "所以", "已经", "没有", "不是", "如何", "什么",
    "文章", "教程", "博客", "内容", "问题", "方法", "实现", "系统", "步骤", "结果",
    "检查", "项目", "测试", "修改", "提交", "文件", "用户", "授权", "服务", "明确",
    "保持", "确保", "禁止", "允许", "完成", "继续", "当前", "必须", "不得", "相关",
    "对应", "现有", "默认", "配置", "规则", "要求", "验证", "操作", "记录", "目录",
    "说明", "工作", "以下", "以上", "其他", "所有", "部分", "直接", "单独", "正常",
}


@dataclass(frozen=True)
class DigestOutput:
    text: str
    stats: dict[str, Any] = field(default_factory=dict)


def redact_sensitive_tokens(text: str) -> str:
    """只对发往外部模型的样本剥离 Base64 噪声与疑似密钥/带密码连接串。"""
    cleaned = _BASE64_DATA_URI_RE.sub("[BASE64_DATA_OMITTED]", text or "")
    for pat in _SECRET_PATTERNS:
        cleaned = pat.sub("[REDACTED]", cleaned)
    return cleaned


def _extract_jieba_keywords(text: str, top_n: int = 12) -> list[str]:
    """安全使用 jieba 提取技术关键词；即使 jieba 异常也不阻断主流程。"""
    if not text:
        return []
    sample = text[:80_000]
    tokens: list[str] = []
    try:
        import jieba  # 仅在需要提词时导入
        for w in jieba.cut(sample, cut_all=False):
            token = w.strip()
            if len(token) < 2 or token in _TECH_STOPWORDS:
                continue
            if token.isdigit():
                continue
            if re.match(r"^[A-Za-z0-9_+#.-]+$", token) or any("\u4e00" <= c <= "\u9fff" for c in token):
                tokens.append(token)
    except Exception:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,20}", sample)

    counts = Counter(tokens)
    return [word for word, _ in counts.most_common(top_n)]


def _truncate_clean(text: str, limit: int) -> str:
    if limit <= 0 or not text:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    cut = text[:limit - 1]
    for sep in ("\n", "。", "！", "？", ".", " "):
        pos = cut.rfind(sep)
        if pos > limit * 0.6:
            return cut[:pos + 1].rstrip() + "…"
    return cut.rstrip() + "…"


def _compress_markdown_sections(raw_markdown: str) -> tuple[list[tuple[int, str]], list[dict[str, Any]]]:
    """
    单遍线性扫描全文：
    - 识别代码块并截断超长代码正文（保留语言+前8行签名/注释）；
    - 截断超长表格；
    - 按 ## 二级章节切分语义段，记录 H1~H4 骨架。
    """
    lines = (raw_markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headings: list[tuple[int, str]] = []
    sections: list[dict[str, Any]] = [{"title": "(开篇)", "paragraphs": []}]

    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_lang = ""
    fence_buf: list[str] = []
    para_buf: list[str] = []
    table_buf: list[str] = []

    def flush_para():
        if para_buf:
            txt = redact_sensitive_tokens("\n".join(para_buf).strip())
            if txt:
                sections[-1]["paragraphs"].append(txt)
            para_buf.clear()

    def flush_table():
        if table_buf:
            if len(table_buf) > 6:
                kept = table_buf[:3] + [f"| …（表格共 {len(table_buf)} 行，已省略中间行）|"]
                txt = redact_sensitive_tokens("\n".join(kept))
            else:
                txt = redact_sensitive_tokens("\n".join(table_buf))
            sections[-1]["paragraphs"].append(txt)
            table_buf.clear()

    for line in lines:
        fm = _FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)
            if not in_fence:
                flush_para()
                flush_table()
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
                fence_lang = (fm.group(2) or "").strip()
                fence_buf = []
                continue
            if line.strip().startswith(fence_char * fence_len) and len(line.strip()) >= len(fence_len * "x"):
                if fence_lang.lower() == "mermaid":
                    first_non_empty = next((ln.strip() for ln in fence_buf if ln.strip()), "图表")
                    sections[-1]["paragraphs"].append(f"[Mermaid 图表: {redact_sensitive_tokens(first_non_empty[:100])}]")
                elif len(fence_buf) > 25:
                    head_lines = "\n".join(redact_sensitive_tokens(ln) for ln in fence_buf[:8])
                    sections[-1]["paragraphs"].append(
                        f"[代码块({fence_lang or 'text'}, 共{len(fence_buf)}行)]\n{head_lines}\n…(省略 {len(fence_buf) - 8} 行)"
                    )
                else:
                    code_txt = "\n".join(redact_sensitive_tokens(ln) for ln in fence_buf)
                    if code_txt.strip():
                        sections[-1]["paragraphs"].append(f"```{fence_lang}\n{code_txt}\n```")
                in_fence = False
                fence_buf = []
                continue
        if in_fence:
            fence_buf.append(line)
            continue

        hm = _HEADING_RE.match(line)
        if hm:
            flush_para()
            flush_table()
            level = len(hm.group(1))
            htitle = redact_sensitive_tokens(hm.group(2).strip())
            headings.append((level, htitle))
            if level <= 2:
                sections.append({"title": htitle, "paragraphs": []})
            else:
                sections[-1]["paragraphs"].append(f"{'#' * level} {htitle}")
            continue

        stripped = line.strip()
        if stripped.startswith("|") and "|" in stripped[1:]:
            flush_para()
            table_buf.append(stripped)
            continue
        else:
            flush_table()

        if not stripped:
            flush_para()
        else:
            para_buf.append(line)

    flush_para()
    flush_table()
    return headings, [s for s in sections if s["paragraphs"] or s["title"] != "(开篇)"]


class StratifiedDigestBuilder:
    """将任意超长 Markdown 压缩为可控四区摘要。"""

    def build(self, raw_markdown: str, *, budget_chars: int = 16_000) -> DigestOutput:
        source_chars = len(raw_markdown or "")
        budget = max(1_200, int(budget_chars))
        headings, sections = _compress_markdown_sections(raw_markdown or "")

        overhead = 240
        usable = max(800, budget - overhead)
        b_skeleton = int(usable * 0.20)
        b_opening = int(usable * 0.30)
        b_middle = int(usable * 0.30)
        b_ending = usable - b_skeleton - b_opening - b_middle

        heading_lines: list[str] = []
        used_h = 0
        omitted_subheadings = 0
        for lvl, title in headings:
            line = f"{'  ' * (lvl - 1)}- H{lvl}: {title[:60]}"
            if used_h + len(line) + 1 <= max(200, int(b_skeleton * 0.8)):
                heading_lines.append(line)
                used_h += len(line) + 1
            else:
                omitted_subheadings += 1
        if omitted_subheadings:
            heading_lines.append(f"...(另有 {omitted_subheadings} 条下级标题已省略)")

        all_para_text = "\n".join(p for sec in sections for p in sec["paragraphs"])
        keywords = _extract_jieba_keywords(all_para_text, top_n=12)
        kw_line = f"高频技术词: {', '.join(keywords)}" if keywords else ""
        skeleton_sec = _truncate_clean(("\n".join(heading_lines) + "\n" + kw_line).strip(), b_skeleton)

        all_paras_flat: list[tuple[str, str]] = [
            (sec["title"], p)
            for sec in sections
            for p in sec["paragraphs"]
            if p.strip()
        ]
        used_paras: set[str] = set()

        opening_parts: list[str] = []
        opening_used = 0
        per_para_cap = max(300, int(b_opening * 0.55))
        for sec_title, p in all_paras_flat:
            snippet = _truncate_clean(p, per_para_cap)
            block = f"[{sec_title}] {snippet}" if sec_title != "(开篇)" else snippet
            if opening_used + len(block) + 2 > b_opening:
                if not opening_parts:
                    opening_parts.append(_truncate_clean(block, b_opening))
                break
            opening_parts.append(block)
            used_paras.add(p)
            opening_used += len(block) + 2
        opening_sec = _truncate_clean("\n\n".join(opening_parts), b_opening)

        ending_parts: list[str] = []
        ending_used = 0
        tail_candidates = list(reversed(all_paras_flat[-10:]))
        for sec_title, p in tail_candidates:
            if p in used_paras:
                continue
            snippet = _truncate_clean(p, max(260, int(b_ending * 0.6)))
            block = f"[{sec_title}] {snippet}"
            if ending_used + len(block) + 2 > b_ending:
                if not ending_parts:
                    ending_parts.append(_truncate_clean(block, b_ending))
                break
            ending_parts.insert(0, block)
            used_paras.add(p)
            ending_used += len(block) + 2
        ending_sec = _truncate_clean("\n\n".join(ending_parts), b_ending)

        mid_sections = [s for s in sections if any(p not in used_paras for p in s["paragraphs"])]
        if len(mid_sections) <= 1 and len(all_paras_flat) > 6:
            step = max(1, len(all_paras_flat) // 6)
            bucket_secs = []
            for i in range(0, len(all_paras_flat), step):
                chunk_paras = [p for _, p in all_paras_flat[i:i + step] if p not in used_paras]
                if chunk_paras:
                    bucket_secs.append({"title": f"位置区段{len(bucket_secs)+1}", "paragraphs": chunk_paras})
            mid_sections = bucket_secs

        mid_parts: list[str] = []
        mid_used = 0
        covered_sections = 0
        if mid_sections:
            max_groups = 16
            if len(mid_sections) > max_groups:
                step = len(mid_sections) / max_groups
                sampled_groups = [mid_sections[int(i * step)] for i in range(max_groups)]
            else:
                sampled_groups = mid_sections

            per_group_cap = max(180, (b_middle // max(1, len(sampled_groups))) - 24)
            # 轮转抽样：先保证每章至少取 1 段，若预算仍有余量再轮转取后续段落
            group_queues = [
                (grp["title"], [p for p in grp["paragraphs"] if p not in used_paras])
                for grp in sampled_groups
            ]
            round_idx = 0
            while mid_used < b_middle:
                progressed = False
                for title_grp, queue in group_queues:
                    if round_idx >= len(queue):
                        continue
                    p = queue[round_idx]
                    if p in used_paras:
                        continue
                    rep = _truncate_clean(p, per_group_cap)
                    entry = f"[{title_grp}] {rep}"
                    if mid_used + len(entry) + 2 > b_middle:
                        continue
                    mid_parts.append(entry)
                    used_paras.add(p)
                    mid_used += len(entry) + 2
                    if round_idx == 0:
                        covered_sections += 1
                    progressed = True
                if not progressed:
                    break
                round_idx += 1
        mid_sec = _truncate_clean("\n\n".join(mid_parts), b_middle)

        final_text = (
            f"=== 1. 全文目录骨架与核心词 ===\n{skeleton_sec or '(无标题目录)'}\n\n"
            f"=== 2. 开篇背景 ===\n{opening_sec or '(无)'}\n\n"
            f"=== 3. 中段各章节抽样 ===\n{mid_sec or '(无)'}\n\n"
            f"=== 4. 结尾总结 ===\n{ending_sec or '(无)'}"
        ).strip()
        final_text = _truncate_clean(final_text, budget)

        return DigestOutput(
            text=final_text,
            stats={
                "source_chars": source_chars,
                "digest_chars": len(final_text),
                "budget_chars": budget,
                "total_headings": len(headings),
                "total_sections": len(sections),
                "covered_mid_sections": covered_sections,
                "keywords": keywords,
            },
        )
