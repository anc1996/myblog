"""
纯确定性数据转换与边界校验模块（零数据库/网络 IO）。

涵盖经生产核查确认的所有转换规则（表 A-01~A-09、B-01~B-11、C-01~C-11、D-01~D-08、E-01~E-06）：
- E-02=B: 剔除 测试页（id=526）及其子树；
- B-03: 重写富文本 <embed embedtype="image"> / <a linktype="page|document">；
- B-04=A: 精准重写代码块外的 <embed embedtype="image" format="fullwidth_web" ...> 为受控网关 Markdown 语法；
- B-05=A: 保留合法外链，登记历史破损本地相对路径图到异常清单；
- C-01=A / C-02=A: 作者 HTML bio 清洗与「数据库」分类转标签；
- C-07=A: 仅迁 approved 评论并将深度>2或父节点已删除的回复向上折叠为二级扁平；
- C-09=A: PageViewCount v1/v2 双列互斥合并。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any

_EMBED_TAG_RE = re.compile(r"<embed\b([^>]*?)/?>", re.IGNORECASE)
_A_TAG_RE = re.compile(r"<a\b([^>]*?)>", re.IGNORECASE)
_ATTR_PAIR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"')
_ATTR_PAIR_SINGLE_RE = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*'([^']*)'")
_FENCE_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)")
_INLINE_CODE_RE = re.compile(r"(`[^`\n]+`)")
_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)")

_EMBED_FORMAT_TO_MD = {
    "fullwidth_web": "full",
    "fullwidth": "full",
    "full": "full",
    "centered": "center",
    "center": "center",
    "left": "left",
    "right": "right",
}


def sha256_json(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_attrs(attr_str: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in _ATTR_PAIR_RE.findall(attr_str or ""):
        out[k.lower()] = html.unescape(v)
    for k, v in _ATTR_PAIR_SINGLE_RE.findall(attr_str or ""):
        lk = k.lower()
        if lk not in out:
            out[lk] = html.unescape(v)
    return out


def _format_attrs(attrs: dict[str, str]) -> str:
    return " ".join(f'{k}="{html.escape(str(v), quote=True)}"' for k, v in attrs.items())


def strip_html_to_plain_text(raw: Any) -> str:
    """将旧站 Author.bio (StreamField JSON 或 HTML) 清洗为简洁纯文本。"""
    if not raw:
        return ""
    text = ""
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict):
                parts.append(str(item.get("value") or ""))
            else:
                parts.append(str(item))
        text = "\n".join(parts)
    elif isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return strip_html_to_plain_text(parsed)
            except Exception:
                pass
        text = raw
    else:
        text = str(raw)

    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|h[1-6]|li|blockquote)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def plain_text_to_richtext_html(raw: str) -> str:
    """纯文本转安全的 Wagtail DB HTML（若本身已是富文本块结构则保留清洗）。"""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if re.match(r"^\s*<(?:p|h[1-6]|ul|ol|blockquote)\b", text, re.IGNORECASE):
        return text
    paragraphs = re.split(r"\n\s*\n+", text)
    chunks = []
    for para in paragraphs:
        lines = [html.escape(line) for line in para.split("\n")]
        chunks.append(f"<p>{'<br/>'.join(lines)}</p>")
    return "".join(chunks)


def compute_excluded_test_page_ids(
    index_pages: list[dict[str, Any]],
    blog_pages: list[dict[str, Any]],
    *,
    test_root_id: int = 526,
    include_test_pages: bool = False,
) -> set[int]:
    """E-02=B: 识别测试页 (id=526) 及其全部分支子页 ID；传入 include_test_pages=True 时全量迁入。"""
    if include_test_pages:
        return set()
    test_root_path = ""
    for idx in index_pages:
        if int(idx["id"]) == int(test_root_id) or str(idx.get("title") or "").strip() == "测试页":
            test_root_path = str(idx.get("path") or "")
            break
    if not test_root_path:
        return set()
    excluded: set[int] = set()
    for row in index_pages + blog_pages:
        p = str(row.get("path") or "")
        if p.startswith(test_root_path):
            excluded.add(int(row["id"]))
    return excluded


def rewrite_richtext_db_html(
    raw_html: str,
    *,
    image_id_map: dict[int, int],
    page_id_map: dict[int, int],
    doc_id_map: dict[int, int],
    excluded_page_ids: set[int],
    anomalies: list[dict[str, Any]],
    source_page_id: int | None = None,
) -> str:
    """B-03: 重写富文本内的图片嵌入、站内页链接与文档链接 ID。"""
    if not raw_html:
        return ""

    def _sub_embed(match: re.Match) -> str:
        attrs = _parse_attrs(match.group(1))
        if attrs.get("embedtype") == "image" and attrs.get("id"):
            raw_id = attrs["id"].strip()
            if raw_id.isdigit():
                old_id = int(raw_id)
                new_id = image_id_map.get(old_id)
                if new_id is None:
                    anomalies.append({
                        "kind": "missing_richtext_image",
                        "page_id": source_page_id,
                        "old_image_id": old_id,
                    })
                    return match.group(0)
                attrs["id"] = str(new_id)
                if attrs.get("format") == "fullwidth_web":
                    attrs["format"] = "fullwidth"
                return f"<embed {_format_attrs(attrs)}/>"
        return match.group(0)

    def _sub_anchor(match: re.Match) -> str:
        attrs = _parse_attrs(match.group(1))
        linktype = attrs.get("linktype")
        if linktype == "page" and attrs.get("id"):
            raw_id = attrs["id"].strip()
            if raw_id.isdigit():
                old_id = int(raw_id)
                if old_id in excluded_page_ids:
                    anomalies.append({
                        "kind": "link_to_excluded_test_page",
                        "page_id": source_page_id,
                        "target_old_page_id": old_id,
                    })
                    return "<a href=\"#\">"
                new_id = page_id_map.get(old_id)
                if new_id is None:
                    anomalies.append({
                        "kind": "unresolved_page_link",
                        "page_id": source_page_id,
                        "target_old_page_id": old_id,
                    })
                    return match.group(0)
                attrs["id"] = str(new_id)
                return f"<a {_format_attrs(attrs)}>"
        elif linktype == "document" and attrs.get("id"):
            raw_id = attrs["id"].strip()
            if raw_id.isdigit():
                old_id = int(raw_id)
                new_id = doc_id_map.get(old_id)
                if new_id is not None:
                    attrs["id"] = str(new_id)
                    return f"<a {_format_attrs(attrs)}>"
                anomalies.append({
                    "kind": "missing_richtext_document",
                    "page_id": source_page_id,
                    "old_doc_id": old_id,
                })
        return match.group(0)

    out = _EMBED_TAG_RE.sub(_sub_embed, raw_html)
    out = _A_TAG_RE.sub(_sub_anchor, out)
    return out


def rewrite_markdown_content(
    raw_md: str,
    *,
    image_id_map: dict[int, int],
    image_uuid_map: dict[int, str],
    anomalies: list[dict[str, Any]],
    source_page_id: int | None = None,
) -> str:
    """
    B-04=A & B-05=A:
    1. 保护代码块与行内代码，绝不误改示例代码中的 /images/ 或 <table>；
    2. 将代码块外的 <embed embedtype="image" ...> 精准重写为 ![alt|format](/media-gateway/image/<uuid>/)；
    3. 保留外链图不变，并将旧站遗留的破损本地相对路径图记录进 anomalies。
    """
    if not raw_md:
        return ""

    stashed_code: list[str] = []

    def _stash(match: re.Match) -> str:
        idx = len(stashed_code)
        stashed_code.append(match.group(0))
        return f"@@ETLCODE_{idx}@@"

    protected = _FENCE_RE.sub(_stash, raw_md)
    protected = _INLINE_CODE_RE.sub(_stash, protected)

    def _sub_md_embed(match: re.Match) -> str:
        attrs = _parse_attrs(match.group(1))
        if attrs.get("embedtype") != "image":
            return match.group(0)
        raw_id = (attrs.get("id") or "").strip()
        if not raw_id.isdigit():
            return match.group(0)
        old_id = int(raw_id)
        new_uuid = image_uuid_map.get(old_id)
        if not new_uuid:
            anomalies.append({
                "kind": "missing_markdown_embed_image",
                "page_id": source_page_id,
                "old_image_id": old_id,
            })
            return match.group(0)
        alt = (attrs.get("alt") or "").replace("|", "\\|").strip()
        fmt_raw = (attrs.get("format") or "").strip().lower()
        fmt_token = _EMBED_FORMAT_TO_MD.get(fmt_raw, "full")
        return f"![{alt}|{fmt_token}](/media-gateway/image/{new_uuid}/)"

    rewritten = _EMBED_TAG_RE.sub(_sub_md_embed, protected)

    # 检测旧站遗留的本地相对路径图（如 20大报告.assets/...），记录异常但不破坏正文
    for m in _MD_IMG_RE.finditer(rewritten):
        url = (m.group(2) or "").strip()
        if not url:
            continue
        lower = url.lower()
        if lower.startswith(("http://", "https://", "/media-gateway/", "asset://")):
            continue
        anomalies.append({
            "kind": "unmapped_local_relative_image",
            "page_id": source_page_id,
            "raw_url": url,
        })

    for idx, raw_code in enumerate(stashed_code):
        rewritten = rewritten.replace(f"@@ETLCODE_{idx}@@", raw_code)
    return rewritten


def convert_wagtail_table_to_custom_table(val: Any) -> dict[str, Any]:
    """B-08: 将 Wagtail 原生 table_block 负载无损转换为 CustomTableBlock 结构。"""
    if not isinstance(val, dict):
        return {
            "caption": "",
            "has_header_row": True,
            "has_header_col": False,
            "alignment": "center",
            "table_data": '{"rows": []}',
        }
    header_choice = str(val.get("table_header_choice") or "").lower()
    first_row_hdr = bool(val.get("first_row_is_table_header")) or header_choice in ("row", "both")
    first_col_hdr = bool(val.get("first_col_is_header")) or header_choice in ("col", "both")
    raw_data = val.get("data") or []
    rows_out: list[list[dict[str, Any]]] = []
    for r_idx, row in enumerate(raw_data):
        if not isinstance(row, list):
            continue
        row_cells = []
        for c_idx, cell in enumerate(row):
            is_hdr = (first_row_hdr and r_idx == 0) or (first_col_hdr and c_idx == 0)
            row_cells.append({
                "value": "" if cell is None else str(cell),
                "header": is_hdr,
            })
        rows_out.append(row_cells)
    return {
        "caption": str(val.get("table_caption") or "").strip()[:255],
        "has_header_row": first_row_hdr,
        "has_header_col": first_col_hdr,
        "alignment": "center",
        "table_data": json.dumps({"rows": rows_out}, ensure_ascii=False),
    }


def transform_stream_blocks(
    raw_blocks: list[dict[str, Any]],
    *,
    image_id_map: dict[int, int],
    image_uuid_map: dict[int, str],
    image_caption_map: dict[int, str],
    doc_id_map: dict[int, int],
    doc_desc_map: dict[int, str],
    audio_id_map: dict[int, int],
    video_id_map: dict[int, int],
    page_id_map: dict[int, int],
    excluded_page_ids: set[int],
    anomalies: list[dict[str, Any]],
    source_page_id: int,
) -> list[dict[str, Any]]:
    """B-01~B-10: 将旧站 StreamField JSON 数组无损转换为 myblog BlogBodyBlocks 兼容 JSON 数组。"""
    out: list[dict[str, Any]] = []
    for blk in raw_blocks or []:
        btype = str(blk.get("type") or "").strip()
        val = blk.get("value")
        blk_id = blk.get("id")
        item: dict[str, Any]

        if btype == "rich_text":
            item = {
                "type": "rich_text",
                "value": rewrite_richtext_db_html(
                    str(val or ""),
                    image_id_map=image_id_map,
                    page_id_map=page_id_map,
                    doc_id_map=doc_id_map,
                    excluded_page_ids=excluded_page_ids,
                    anomalies=anomalies,
                    source_page_id=source_page_id,
                ),
            }
        elif btype == "markdown_block":
            raw_md = val.get("markdown", "") if isinstance(val, dict) else str(val or "")
            # CustomMarkdownBlock 继承 blocks.TextBlock，StreamField 存储值为纯字符串而非 dict
            item = {
                "type": "markdown",
                "value": rewrite_markdown_content(
                    raw_md,
                    image_id_map=image_id_map,
                    image_uuid_map=image_uuid_map,
                    anomalies=anomalies,
                    source_page_id=source_page_id,
                ),
            }
        elif btype == "image_block":
            if isinstance(val, dict):
                old_img_id = val.get("image")
                caption = str(val.get("caption") or "").strip()
                alt_text = str(val.get("alt_text") or "").strip()
                raw_fmt = str(val.get("display_format") or val.get("align") or "centered").strip().lower()
            else:
                old_img_id = val
                caption = ""
                alt_text = ""
                raw_fmt = "centered"
            old_int = int(old_img_id) if old_img_id not in (None, "") else None
            new_img_id = image_id_map.get(old_int) if old_int is not None else None
            if new_img_id is None:
                anomalies.append({
                    "kind": "missing_image_block_asset",
                    "page_id": source_page_id,
                    "old_image_id": old_img_id,
                })
                continue
            if not caption and old_int in image_caption_map:
                caption = image_caption_map[old_int]
            fmt_map = {"center": "centered", "centered": "centered", "full": "full", "fullwidth": "full", "fullwidth_web": "full", "left": "left", "right": "right"}
            display_format = fmt_map.get(raw_fmt, "centered")
            item = {
                "type": "image",
                "value": {
                    "image": new_img_id,
                    "caption": caption[:255],
                    "alt_text": alt_text[:255],
                    "display_format": display_format,
                    "accent_style": "minimal",
                },
            }
        elif btype == "document_block":
            if isinstance(val, dict):
                old_doc_id = val.get("document")
                title = str(val.get("title_override") or val.get("title") or "").strip()
                desc = str(val.get("description") or "").strip()
            else:
                old_doc_id = val
                title = ""
                desc = ""
            old_int = int(old_doc_id) if old_doc_id not in (None, "") else None
            new_doc_id = doc_id_map.get(old_int) if old_int is not None else None
            if new_doc_id is None:
                anomalies.append({
                    "kind": "missing_document_block_asset",
                    "page_id": source_page_id,
                    "old_doc_id": old_doc_id,
                })
                continue
            if not desc and old_int in doc_desc_map:
                desc = doc_desc_map[old_int]
            item = {
                "type": "document",
                "value": {
                    "document": new_doc_id,
                    "title_override": title[:255],
                    "description": desc,
                },
            }
        elif btype == "audio_block":
            if isinstance(val, dict):
                old_aid = val.get("audio")
                title = str(val.get("title_override") or val.get("title") or "").strip()
            else:
                old_aid = val
                title = ""
            old_int = int(old_aid) if old_aid not in (None, "") else None
            new_aid = audio_id_map.get(old_int) if old_int is not None else None
            if new_aid is None:
                anomalies.append({
                    "kind": "missing_audio_block_asset",
                    "page_id": source_page_id,
                    "old_audio_id": old_aid,
                })
                continue
            item = {
                "type": "audio",
                "value": {"audio": new_aid, "title_override": title[:255]},
            }
        elif btype == "video_block":
            if isinstance(val, dict):
                old_vid = val.get("video")
                title = str(val.get("title_override") or val.get("title") or "").strip()
            else:
                old_vid = val
                title = ""
            old_int = int(old_vid) if old_vid not in (None, "") else None
            new_vid = video_id_map.get(old_int) if old_int is not None else None
            if new_vid is None:
                anomalies.append({
                    "kind": "missing_video_block_asset",
                    "page_id": source_page_id,
                    "old_video_id": old_vid,
                })
                continue
            item = {
                "type": "video",
                "value": {"video": new_vid, "title_override": title[:255]},
            }
        elif btype == "mermaid_chart":
            if isinstance(val, dict):
                code = str(val.get("code") or val.get("diagram_code") or "").strip()
                title = str(val.get("title") or "").strip()
            else:
                code = str(val or "").strip()
                title = ""
            item = {
                "type": "mermaid",
                "value": {
                    "title": title[:120],
                    "diagram_code": code,
                    "theme": "follow-site",
                },
            }
        elif btype == "code_block":
            if isinstance(val, dict):
                lang = str(val.get("language") or "text").strip().lower() or "text"
                code = str(val.get("code") or "")
                title = str(val.get("title") or "").strip()
            else:
                lang = "text"
                code = str(val or "")
                title = ""
            item = {
                "type": "code",
                "value": {
                    "title": title[:120],
                    "language": lang,
                    "code": code,
                    "mode": "display",
                },
            }
        elif btype == "table_block":
            item = {
                "type": "table",
                "value": convert_wagtail_table_to_custom_table(val),
            }
        elif btype == "embed_block":
            if isinstance(val, dict):
                url = str(val.get("embed_url") or val.get("url") or "").strip()
                title = str(val.get("title") or "").strip()
            else:
                url = str(val or "").strip()
                title = ""
            item = {
                "type": "embed",
                "value": {"title": title[:255] or "嵌入媒体", "embed_url": url},
            }
        else:
            anomalies.append({
                "kind": "unknown_block_type",
                "page_id": source_page_id,
                "block_type": btype,
            })
            raise ValueError(f"Unsupported block type '{btype}' on source page {source_page_id}")

        if blk_id:
            item["id"] = str(blk_id)
        out.append(item)
    return out


def transform_comments_with_folding(
    raw_comments: list[dict[str, Any]],
    *,
    excluded_page_ids: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    C-07=A: 仅保留未删除页面的 approved 评论，并将深度 > 2 或父节点已被软删除的回复
    向上折叠至最近的合法根评论（满足 myblog C-05 二级扁平结构）。
    返回 (根评论优先有序列表, 异常日志列表)。
    """
    by_id = {int(c["id"]): c for c in raw_comments}
    anomalies: list[dict[str, Any]] = []
    approved: list[dict[str, Any]] = []

    for c in raw_comments:
        if int(c["page_id"]) in excluded_page_ids:
            continue
        if str(c.get("status") or "").lower() != "approved":
            continue
        approved.append(dict(c))

    approved_ids = {int(c["id"]) for c in approved}
    folded_rows: list[dict[str, Any]] = []

    for c in approved:
        cid = int(c["id"])
        orig_parent_id = int(c["parent_id"]) if c.get("parent_id") is not None else None
        if orig_parent_id is None:
            c["folded_parent_old_id"] = None
            c["folded_reply_to_old_id"] = None
            folded_rows.append(c)
            continue

        # 沿父链上溯找根评论，同时兼容直接父节点被软删的情况
        visited = {cid}
        chain: list[int] = []
        cur = orig_parent_id
        while cur is not None and cur not in visited:
            visited.add(cur)
            node = by_id.get(cur)
            if node is None:
                break
            chain.append(cur)
            nxt = int(node["parent_id"]) if node.get("parent_id") is not None else None
            cur = nxt

        approved_ancestors = [aid for aid in chain if aid in approved_ids]
        if not approved_ancestors:
            # 所有祖先均已删除：提升为根评论避免悬空外键
            anomalies.append({
                "kind": "comment_promoted_to_root",
                "old_comment_id": cid,
                "orig_parent_id": orig_parent_id,
            })
            c["folded_parent_old_id"] = None
            c["folded_reply_to_old_id"] = None
        else:
            root_old_id = approved_ancestors[-1]
            c["folded_parent_old_id"] = root_old_id
            if orig_parent_id in approved_ids and orig_parent_id != root_old_id:
                c["folded_reply_to_old_id"] = orig_parent_id
            elif orig_parent_id not in approved_ids:
                c["folded_reply_to_old_id"] = None
                anomalies.append({
                    "kind": "comment_folded_past_deleted_parent",
                    "old_comment_id": cid,
                    "deleted_parent_id": orig_parent_id,
                    "new_root_parent_id": root_old_id,
                })
            else:
                c["folded_reply_to_old_id"] = None
        folded_rows.append(c)

    # 根评论先入库，其次按 id 升序导入回复
    folded_rows.sort(key=lambda r: (0 if r["folded_parent_old_id"] is None else 1, int(r["id"])))
    return folded_rows, anomalies


def merge_page_view_counts(
    raw_pv_rows: list[dict[str, Any]],
    *,
    excluded_page_ids: set[int],
) -> dict[int, list[dict[str, Any]]]:
    """
    C-09=A: 合并旧站互斥双列 (count + view_count_v2, unique_count + unique_visitor_count_v2)，
    按 old_page_id 分组返回按日明细列表。
    """
    by_page: dict[int, list[dict[str, Any]]] = {}
    for row in raw_pv_rows or []:
        pid = int(row["page_id"])
        if pid in excluded_page_ids:
            continue
        pv = max(0, int(row.get("count") or 0)) + max(0, int(row.get("view_count_v2") or 0))
        uv = max(0, int(row.get("unique_count") or 0)) + max(0, int(row.get("unique_visitor_count_v2") or 0))
        valid_views = max(0, int(row.get("engaged") or 0))
        completed_views = max(0, int(row.get("scroll_90") or 0))
        active_secs = max(0, int(row.get("active_reading_seconds") or 0))
        by_page.setdefault(pid, []).append({
            "stat_date": str(row["date"]),
            "pv": pv,
            "uv": uv,
            "valid_views": valid_views,
            "completed_views": completed_views,
            "total_active_seconds": active_secs,
        })
    for rows in by_page.values():
        rows.sort(key=lambda r: r["stat_date"])
    return by_page


def normalize_legacy_redirect_old_path(old_path: str) -> str:
    """D-01/D-02: 剥离旧站 /zh-hans 语言前缀，规范化重定向源路径。"""
    path = "/" + str(old_path or "").strip().lstrip("/")
    if path == "/zh-hans" or path == "/zh-hans/":
        return "/"
    if path.startswith("/zh-hans/"):
        path = "/" + path[len("/zh-hans/"):].lstrip("/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path
