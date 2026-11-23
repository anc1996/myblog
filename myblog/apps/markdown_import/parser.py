"""
双模式 Markdown 解析器与字段规范化工具。

能力：
1. 安全解析 YAML Front Matter（禁止重复键、自定义 tag 与深层嵌套；单独分隔线 --- 不误判）；
2. 围栏感知地提取首个顶层 # 一级标题，且仅在首块匹配时精准剥离重复 H1；
3. 严格解析成文日期（YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD）与多分隔符标签归一化对齐；
4. 解析 GuidePage 显式 :::step 步骤块（普通 ## 标题绝不猜测为步骤）；
5. 安全提取并重写本地图片与文档链接为 asset://image/<uuid> / asset://document/<uuid>，坚决拦截路径越界。
"""

from datetime import date, datetime
from pathlib import PurePosixPath
import re
import unicodedata
from typing import Any
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
import yaml

from .contracts import ImportIssue, ImportMediaAssetRef

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d")
_TAG_SPLIT_RE = re.compile(r"[,，、;；]+")
_FILENAME_PREFIX_RE = re.compile(r"^(?:(?:\d{4}[-_.]\d{1,2}[-_.]\d{1,2}[-_. ]+)|(?:\d{1,4}[-_. ]+))+")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif"}
_STEP_OPEN_RE = re.compile(r"^:::\s*step(?:\s+(.*))?$", re.IGNORECASE)
_STEP_CLOSE_RE = re.compile(r"^:::\s*$")
_FENCE_OPEN_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LINK_OR_IMAGE_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)\n]+)\)")


class _DuplicateKeyYamlError(yaml.YAMLError):
    """YAML 存在重复键时抛出的明确校验异常。"""


class _StrictSafeLoader(yaml.SafeLoader):
    """禁止重复键的 YAML SafeLoader，避免同名字段静默覆盖。"""


def _construct_strict_mapping(loader: yaml.SafeLoader, node: yaml.Node, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if isinstance(key, (dict, list, set)):
            raise yaml.YAMLError("YAML 键名必须是简单标量。")
        if key in mapping:
            raise _DuplicateKeyYamlError(f"YAML 头部存在重复字段: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_strict_mapping,
)


def _normalize_newlines(text: str) -> str:
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _check_yaml_depth(obj: Any, depth: int = 0) -> None:
    if depth > 4:
        raise ValueError("YAML 头部嵌套层数过深。")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError("YAML 键名必须为字符串。")
            _check_yaml_depth(v, depth + 1)
    elif isinstance(obj, list):
        if len(obj) > 100:
            raise ValueError("YAML 列表项过多。")
        for item in obj:
            _check_yaml_depth(item, depth + 1)
    elif obj is not None and not isinstance(obj, (str, int, float, bool, date, datetime)):
        raise ValueError("YAML 包含不支持的复杂类型。")


def parse_front_matter(raw_text: str) -> tuple[dict[str, Any], str, list[ImportIssue]]:
    """
    仅当文档第一行严格为 `---` 时解析 YAML Front Matter；
    正文中间出现 `---` 分隔线时绝不误判；未闭合或语法错误返回 Issue 并保留全文。
    """
    normalized = _normalize_newlines(raw_text or "")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, normalized, []

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        stripped = lines[idx].strip()
        if stripped in {"---", "..."}:
            end_idx = idx
            break

    if end_idx is None:
        return {}, normalized, [
            ImportIssue(
                code="front_matter_unclosed",
                message="第 1 行的 YAML 头部缺少配对结束行 `---`，请补齐结束线或移除开头 `---`。",
                field="markdown",
                line=1,
                severity="error",
            )
        ]

    yaml_text = "\n".join(lines[1:end_idx])
    body_text = "\n".join(lines[end_idx + 1:]).lstrip("\n")
    if not yaml_text.strip():
        return {}, body_text, []

    try:
        data = yaml.load(yaml_text, Loader=_StrictSafeLoader)
    except _DuplicateKeyYamlError as exc:
        return {}, body_text, [
            ImportIssue(
                code="front_matter_duplicate_key",
                message=str(exc),
                field="markdown",
                line=1,
                severity="error",
            )
        ]
    except yaml.YAMLError:
        return {}, body_text, [
            ImportIssue(
                code="front_matter_invalid",
                message="YAML 头部语法格式无效（提示：冒号后请留空格，含冒号的文本请加引号）。",
                field="markdown",
                line=1,
                severity="error",
            )
        ]

    if data is None:
        return {}, body_text, []
    if not isinstance(data, dict):
        return {}, body_text, [
            ImportIssue(
                code="front_matter_not_mapping",
                message="YAML 头部必须是 `字段: 值` 的键值对格式。",
                field="markdown",
                line=1,
                severity="error",
            )
        ]

    try:
        _check_yaml_depth(data)
    except ValueError as exc:
        return {}, body_text, [
            ImportIssue(
                code="front_matter_unsafe",
                message=str(exc),
                field="markdown",
                line=1,
                severity="error",
            )
        ]

    return data, body_text, []


def _normalize_heading_cmp(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _extract_first_block_h1(body_markdown: str) -> tuple[str, tuple[int, int] | None]:
    """
    利用 markdown-it-py 识别正文第一个非空顶级块：
    仅当它就是 H1 标题时返回 (标题文本, (起始行, 结束行))；代码围栏里的 # 注释绝不误判。
    """
    if not body_markdown or not body_markdown.strip():
        return "", None
    md = MarkdownIt("commonmark")
    tokens = md.parse(body_markdown)
    for idx, tok in enumerate(tokens):
        if tok.level != 0:
            continue
        if tok.type == "heading_open":
            if tok.tag == "h1" and idx + 1 < len(tokens) and tokens[idx + 1].type == "inline":
                title_text = (tokens[idx + 1].content or "").strip()
                if title_text and tok.map:
                    return title_text, (tok.map[0], tok.map[1])
            return "", None
        # 首个顶级语义块不是 H1（例如普通段落、代码块、列表或 H2）
        if tok.type in {"paragraph_open", "fence", "code_block", "bullet_list_open", "ordered_list_open", "blockquote_open", "hr", "html_block"}:
            return "", None
    return "", None


def fallback_title_from_filename(source_name: str, body_markdown: str = "") -> str:
    """从文件名清洗候选标题，若无有效信息再退到正文首句。"""
    raw_stem = PurePosixPath(str(source_name or "").replace("\\", "/")).stem
    cleaned = _FILENAME_PREFIX_RE.sub("", raw_stem).replace("_", " ").replace("-", " ").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if cleaned and cleaned.lower() not in {"untitled", "readme", "index", "note", "notes"}:
        return cleaned[:255]

    for line in (body_markdown or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("```", "~~~", "---", "![", "#")):
            continue
        plain = re.sub(r"[*_`>\[\]()!]+", " ", stripped)
        plain = re.sub(r"\s+", " ", plain).strip()
        if plain:
            return plain[:50]
    return "未命名文章"


def extract_title_and_strip_h1(
    front_matter: dict[str, Any],
    body_markdown: str,
    source_name: str = "",
) -> tuple[str, str, str]:
    """
    提取标题并按需剥离正文首块重复 H1：
    返回 (title, title_source, stripped_body)。
    - title_source: "yaml" | "h1" | "missing"
    - 仅当正文第一个非空块为 H1 且与最终标题一致时才剥离该首块，绝不误删后文章节 H1。
    """
    yaml_title = str(front_matter.get("title") or "").strip()
    first_h1, h1_map = _extract_first_block_h1(body_markdown)

    if yaml_title:
        stripped_body = body_markdown
        if first_h1 and h1_map and _normalize_heading_cmp(yaml_title) == _normalize_heading_cmp(first_h1):
            lines = body_markdown.split("\n")
            stripped_body = "\n".join(lines[:h1_map[0]] + lines[h1_map[1]:]).lstrip("\n")
        return yaml_title[:255], "yaml", stripped_body

    if first_h1 and h1_map:
        lines = body_markdown.split("\n")
        stripped_body = "\n".join(lines[:h1_map[0]] + lines[h1_map[1]:]).lstrip("\n")
        return first_h1[:255], "h1", stripped_body

    return "", "missing", body_markdown


def strip_matching_leading_h1(body_markdown: str, final_title: str) -> str:
    """当标题由外部或 AI 补全后，若正文第一块 H1 恰好与之相同，再执行一次安全剥离。"""
    first_h1, h1_map = _extract_first_block_h1(body_markdown)
    if first_h1 and h1_map and _normalize_heading_cmp(first_h1) == _normalize_heading_cmp(final_title):
        lines = body_markdown.split("\n")
        return "\n".join(lines[:h1_map[0]] + lines[h1_map[1]:]).lstrip("\n")
    return body_markdown


def parse_authored_date(
    raw_value: Any,
    default_date: date,
) -> tuple[date, list[ImportIssue]]:
    """严格解析成文日期；支持 YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD，缺省补 default_date。"""
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return default_date, []

    if isinstance(raw_value, datetime):
        return raw_value.date(), []
    if isinstance(raw_value, date):
        return raw_value, []

    text = str(raw_value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date(), []
        except ValueError:
            continue

    return default_date, [
        ImportIssue(
            code="authored_date_invalid",
            message=f"成文日期 '{text}' 格式无效，请使用 YYYY-MM-DD、YYYY/MM/DD 或 YYYY.MM.DD。",
            field="authored_on",
            severity="error",
        )
    ]


def normalize_tags_input(raw_tags: Any) -> list[str]:
    """兼容列表或中英文逗号/顿号/分号分隔的标签，执行 NFKC 归一化与大小写无关去重。"""
    if not raw_tags:
        return []
    if isinstance(raw_tags, str):
        candidates = _TAG_SPLIT_RE.split(raw_tags)
    elif isinstance(raw_tags, (list, tuple)):
        candidates = []
        for item in raw_tags:
            if item is not None:
                candidates.extend(_TAG_SPLIT_RE.split(str(item)))
    else:
        candidates = [str(raw_tags)]

    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        cleaned = unicodedata.normalize("NFKC", item).strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned[:50])
    return result


def align_tags_with_vocabulary(
    tag_names: list[str],
    existing_tags: list[str],
) -> tuple[list[str], list[str]]:
    """
    将标签按大小写无关对齐到站内已有词库，避免在标签大厅拆成重复标签：
    返回 (命中的已有标准标签列表, 未命中站内词库的新标签候选列表)。
    """
    vocab_map: dict[str, str] = {}
    for existing in existing_tags:
        norm = unicodedata.normalize("NFKC", str(existing)).strip()
        if norm and norm.casefold() not in vocab_map:
            vocab_map[norm.casefold()] = norm

    aligned: list[str] = []
    unmatched: list[str] = []
    seen_aligned: set[str] = set()
    for tag in tag_names:
        key = tag.casefold()
        if key in vocab_map:
            canonical = vocab_map[key]
            if canonical.casefold() not in seen_aligned:
                seen_aligned.add(canonical.casefold())
                aligned.append(canonical)
        else:
            unmatched.append(tag)
    return aligned, unmatched


def parse_explicit_steps(body_markdown: str) -> tuple[list[dict[str, Any]], str, list[ImportIssue]]:
    """
    仅在非代码围栏区识别显式 `:::step 标题` ... `:::` 块；
    普通 `## 二级标题` 绝不误猜为教程步骤。
    返回 (steps, remaining_markdown, issues)。
    """
    lines = _normalize_newlines(body_markdown or "").split("\n")
    steps: list[dict[str, Any]] = []
    remaining_lines: list[str] = []
    issues: list[ImportIssue] = []

    fence_marker: str | None = None
    in_step = False
    step_title = ""
    step_start_line = 0
    step_lines: list[str] = []

    for idx, line in enumerate(lines, start=1):
        if not in_step:
            fm = _FENCE_OPEN_RE.match(line)
            if fm:
                marker = fm.group(1)
                if fence_marker is None:
                    fence_marker = marker
                elif line.strip().startswith(fence_marker[0] * len(fence_marker)) and len(line.strip()) >= len(fence_marker):
                    fence_marker = None
                remaining_lines.append(line)
                continue

            if fence_marker is None:
                sm = _STEP_OPEN_RE.match(line.strip())
                if sm:
                    in_step = True
                    step_title = (sm.group(1) or "").strip() or f"步骤 {len(steps) + 1}"
                    step_start_line = idx
                    step_lines = []
                    continue
            remaining_lines.append(line)
        else:
            if _STEP_CLOSE_RE.match(line.strip()):
                steps.append(
                    {
                        "step_number": str(len(steps) + 1),
                        "title": step_title[:120],
                        "content": "\n".join(step_lines).strip(),
                    }
                )
                in_step = False
                step_title = ""
                step_lines = []
            else:
                step_lines.append(line)

    if in_step:
        issues.append(
            ImportIssue(
                code="step_block_unclosed",
                message=f"第 {step_start_line} 行的 `:::step` 块缺少结束标记 `:::`。",
                field="body",
                line=step_start_line,
                severity="error",
            )
        )
        remaining_lines.append(f":::step {step_title}")
        remaining_lines.extend(step_lines)

    return steps, "\n".join(remaining_lines).strip(), issues


def validate_relative_asset_path(raw_path: str) -> str:
    """
    校验并归一化 Markdown 中的本地相对资源路径：
    坚决拒绝绝对路径、Windows 盘符、UNC 网络路径、file: URI、URL 编码越界(..)、控制字符。
    返回干净 POSIX 相对路径（如 'images/arch.png'）。
    """
    candidate = str(raw_path or "").strip()
    if candidate.startswith("<") and ">" in candidate:
        candidate = candidate[1:candidate.index(">")].strip()
    else:
        # 剥除 Markdown 链接可选 title： path/to/file.png "title"
        candidate = re.split(r"\s+(?=[\"'])", candidate, maxsplit=1)[0].strip()

    decoded = unquote(candidate)
    if not decoded:
        raise ValueError("资源路径不能为空。")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded):
        raise ValueError("资源路径包含非法控制字符。")

    lowered = decoded.lower()
    if lowered.startswith(("file:", "javascript:", "data:", "vbscript:")):
        raise ValueError(f"禁止的资源协议: {decoded}")
    if decoded.startswith(("/", "\\")) or _DRIVE_RE.match(decoded):
        raise ValueError(f"禁止使用本地绝对路径或盘符: {decoded}")

    posix_str = decoded.replace("\\", "/")
    parts = PurePosixPath(posix_str).parts
    if ".." in parts:
        raise ValueError(f"禁止使用 `..` 越界路径: {decoded}")
    clean_parts = [p for p in parts if p not in ("", ".")]
    if not clean_parts:
        raise ValueError(f"无效的相对路径: {decoded}")
    return "/".join(clean_parts)


def _is_external_or_special_link(raw_target: str) -> bool:
    target = str(raw_target or "").strip()
    if not target or target.startswith("#"):
        return True
    parsed = urlsplit(target)
    scheme = (parsed.scheme or "").lower()
    if scheme in {"http", "https", "mailto", "tel", "asset"}:
        return True
    return False


def _iter_non_code_line_spans(text: str) -> list[tuple[int, int, str]]:
    """按行拆分，标注处于围栏代码块之外且跳过行内反引号的安全区间，供引用提取和重写使用。"""
    lines = text.splitlines(keepends=True)
    spans: list[tuple[int, int, str]] = []
    offset = 0
    fence_marker: str | None = None
    for line in lines:
        fm = _FENCE_OPEN_RE.match(line)
        if fm:
            marker = fm.group(1)
            if fence_marker is None:
                fence_marker = marker
            elif line.strip().startswith(fence_marker[0] * len(fence_marker)) and len(line.strip()) >= len(fence_marker):
                fence_marker = None
            offset += len(line)
            continue
        if fence_marker is None:
            # 屏蔽本行内反引号 `...` 区间
            i = 0
            seg_start = 0
            in_code = False
            tick_len = 0
            while i < len(line):
                if line[i] == "`":
                    j = i
                    while j < len(line) and line[j] == "`":
                        j += 1
                    run = j - i
                    if not in_code:
                        if i > seg_start:
                            spans.append((offset + seg_start, offset + i, line[seg_start:i]))
                        in_code = True
                        tick_len = run
                    elif run == tick_len:
                        in_code = False
                        seg_start = j
                    i = j
                    continue
                i += 1
            if not in_code and seg_start < len(line):
                spans.append((offset + seg_start, offset + len(line), line[seg_start:]))
            elif in_code and seg_start < len(line):
                # 未闭合反引号视为普通文本
                spans.append((offset + seg_start, offset + len(line), line[seg_start:]))
        offset += len(line)
    return spans


def extract_local_media_refs(
    body_markdown: str,
    front_matter: dict[str, Any] | None = None,
) -> tuple[list[ImportMediaAssetRef], list[ImportIssue]]:
    """从 Front Matter cover 与正文非代码区提取所有本地图片与附件引用。"""
    refs: list[ImportMediaAssetRef] = []
    issues: list[ImportIssue] = []
    seen_keys: dict[str, str] = {}

    def _register(raw_path: str, media_type: str, alt: str, title: str, ctx: str) -> None:
        try:
            norm_path = validate_relative_asset_path(raw_path)
        except ValueError as exc:
            issues.append(ImportIssue(code="invalid_asset_path", message=str(exc), field="assets", severity="error"))
            return
        if norm_path in seen_keys and seen_keys[norm_path] != media_type:
            issues.append(
                ImportIssue(
                    code="media_type_conflict",
                    message=f"本地文件 '{norm_path}' 同时被当成图片与文档引用。",
                    field="assets",
                    severity="error",
                )
            )
            return
        if norm_path not in seen_keys:
            seen_keys[norm_path] = media_type
            refs.append(
                ImportMediaAssetRef(
                    asset_key=norm_path,
                    source_path=norm_path,
                    media_type=media_type,
                    alt=alt,
                    title=title,
                    context=ctx,
                )
            )

    if front_matter and front_matter.get("cover"):
        cover_raw = str(front_matter["cover"]).strip()
        if not _is_external_or_special_link(cover_raw):
            _register(cover_raw, "image", "", "封面图", "cover")
        else:
            issues.append(
                ImportIssue(
                    code="remote_cover_not_allowed",
                    message="封面图只允许使用本地图片文件，不支持远程 URL。",
                    field="cover",
                    severity="error",
                )
            )

    for _, _, segment in _iter_non_code_line_spans(body_markdown or ""):
        for m in _LINK_OR_IMAGE_RE.finditer(segment):
            bang, label, raw_target = m.group(1), m.group(2), m.group(3).strip()
            if _is_external_or_special_link(raw_target):
                continue
            try:
                norm = validate_relative_asset_path(raw_target)
            except ValueError as exc:
                issues.append(ImportIssue(code="invalid_asset_path", message=str(exc), field="assets", severity="error"))
                continue
            ext = PurePosixPath(norm).suffix.lower()
            if bang == "!" or ext in _IMAGE_EXTS:
                media_type = "image"
            else:
                media_type = "document"
            _register(raw_target, media_type, label.strip(), label.strip(), "markdown")

    return refs, issues


def rewrite_asset_links(
    body_markdown: str,
    replacements: dict[str, tuple[str, str]],
) -> str:
    """
    将正文非代码区内的本地路径替换为受控引用：
    replacements: {normalized_rel_path: (media_type, public_uuid)}
    输出 `asset://image/<uuid>` 或 `asset://document/<uuid>`，代码块内原样保留。
    """
    if not body_markdown or not replacements:
        return body_markdown

    edits: list[tuple[int, int, str]] = []
    for seg_start, _, segment in _iter_non_code_line_spans(body_markdown):
        for m in _LINK_OR_IMAGE_RE.finditer(segment):
            bang, label, raw_target = m.group(1), m.group(2), m.group(3).strip()
            if _is_external_or_special_link(raw_target):
                continue
            try:
                norm = validate_relative_asset_path(raw_target)
            except ValueError:
                continue
            if norm not in replacements:
                continue
            media_type, asset_uuid = replacements[norm]
            scheme_type = "image" if media_type == "image" else "document"
            new_link = f"{bang}[{label}](asset://{scheme_type}/{asset_uuid})"
            edits.append((seg_start + m.start(), seg_start + m.end(), new_link))

    if not edits:
        return body_markdown

    out = body_markdown
    for start, end, new_str in sorted(edits, key=lambda x: x[0], reverse=True):
        out = out[:start] + new_str + out[end:]
    return out
