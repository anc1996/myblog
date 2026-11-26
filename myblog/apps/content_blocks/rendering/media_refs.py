"""
媒体资产受控交付网关 URL 转换与引用提取 (MEDIA-REF-01)。
"""

import re
from uuid import UUID
from django.urls import reverse
from content_blocks.contracts import MediaReference

UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def resolve_media_urls(content: str) -> str:
    """
    将内容中的 asset://image/<uuid> 和 asset://document/<uuid>
    转换为受控媒体网关交付路径 (/media-gateway/image/<uuid>/)。
    """
    if not content or "asset://" not in content:
        return content

    def replace_image(m: re.Match) -> str:
        uuid_str = m.group(1)
        try:
            val = UUID(uuid_str)
            return reverse("media_gateway_asset", kwargs={"asset_type": "image", "asset_id": val})
        except Exception:
            return m.group(0)

    def replace_document(m: re.Match) -> str:
        uuid_str = m.group(1)
        try:
            val = UUID(uuid_str)
            return reverse("media_gateway_asset", kwargs={"asset_type": "document", "asset_id": val})
        except Exception:
            return m.group(0)

    res = re.sub(r"asset://image/(" + UUID_PATTERN + r")", replace_image, content)
    res = re.sub(r"asset://document/(" + UUID_PATTERN + r")", replace_document, res)
    return res


def strip_code_spans(text: str) -> str:
    """
    剔除 Markdown 代码块与行内代码，防止误抓代码示例中的测试/示例 asset ID。
    """
    if not text:
        return ""
    no_fence = re.sub(r"```[\s\S]*?```", " ", text)
    no_code = re.sub(r"`[^`\n]+`", " ", no_fence)
    return no_code


def extract_media_references(text: str, path: str = "") -> list[MediaReference]:
    """
    从文本中准确提取被引用的有效媒体资源列表。
    统一支持 asset://image/<uuid>、asset://document/<uuid>、data-asset-id="<uuid>" 与 asset-id="<uuid>"。
    """
    if not text:
        return []

    clean_text = strip_code_spans(str(text))
    refs: list[MediaReference] = []
    seen: set[tuple[str, str, str]] = set()

    # 1. 匹配图片引用
    img_pattern = re.compile(
        r'(?:asset://image/|(?:data-)?asset-id=["\']?)(' + UUID_PATTERN + r')'
    )
    for m in img_pattern.finditer(clean_text):
        aid = m.group(1).lower()
        key = ("image", aid, path)
        if key not in seen:
            seen.add(key)
            refs.append(MediaReference(asset_id=aid, asset_type="image", block_path=path))

    # 2. 匹配文档引用
    doc_pattern = re.compile(r'asset://document/(' + UUID_PATTERN + r')')
    for m in doc_pattern.finditer(clean_text):
        aid = m.group(1).lower()
        key = ("document", aid, path)
        if key not in seen:
            seen.add(key)
            refs.append(MediaReference(asset_id=aid, asset_type="document", block_path=path))

    # 3. 匹配直接在正文/表格 HTML 中书写的 /media-gateway/(image|document|audio|video)/<uuid>/ 路径，防止逃逸引用追踪与发布门禁
    gateway_pattern = re.compile(
        r'/media-gateway/(image|document|audio|video)/(' + UUID_PATTERN + r')/?',
        re.IGNORECASE,
    )
    for m in gateway_pattern.finditer(clean_text):
        atype = m.group(1).lower()
        aid = m.group(2).lower()
        key = (atype, aid, path)
        if key not in seen:
            seen.add(key)
            refs.append(MediaReference(asset_id=aid, asset_type=atype, block_path=path))

    return refs
