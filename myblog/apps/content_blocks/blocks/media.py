"""
自定义全站公共媒体块 (CustomImageBlock, CustomDocumentBlock, CustomAudioBlock, CustomVideoBlock)。

所有媒体块严格指向全站公共 media_library 模型：
1. Image: 引用 CustomImage，支持图注、无障碍替代文本与对齐模式；
2. Document: 引用 CustomDocument，支持自定义标题与安全下载；
3. Audio: 引用公共 Audio，支持基本播放与分段交付；
4. Video: 引用公共 Video，支持封面海报与 Range 请求。

所有媒体块严格实现 extract_media_refs()，向 MEDIA-REF-01 保护索引提供准确的稳定资源标识。
"""

import html
from typing import Any
from uuid import UUID
from django.urls import reverse
from django.utils.safestring import mark_safe
from wagtail import blocks
from wagtail.images.blocks import ImageChooserBlock
from wagtail.documents.blocks import DocumentChooserBlock
from wagtail.snippets.blocks import SnippetChooserBlock

from media_library.models import Audio, Video, CustomImage, CustomDocument
from content_blocks.contracts import (
    BlockContract,
    BlockValidationResult,
    BlockRenderResult,
    BlockTextExtraction,
    MediaReference,
)
from content_blocks.sanitizer import sanitize_html


FORMAT_CHOICES = [
    ("centered", "居中展示"),
    ("full", "全宽撑满"),
    ("left", "左浮动"),
    ("right", "右浮动"),
]


def _resolve_asset_id(instance_or_id: Any, expected_model: Any = None) -> str:
    """安全提取模型实例的 public_id；若传入整数主键，自动按模型查询对应 public_id (H-05)"""
    if not instance_or_id:
        return ""
    if hasattr(instance_or_id, "public_id"):
        return str(instance_or_id.public_id)
    val_str = str(instance_or_id).strip()
    if val_str.isdigit():
        pk_val = int(val_str)
        model = expected_model or CustomImage
        try:
            res = model.objects.filter(pk=pk_val).values_list("public_id", flat=True).first()
            if res:
                return str(res)
        except Exception:
            pass
    return val_str


def _format_file_size(num_bytes: Any) -> str:
    """
    格式化文件大小为易读字符串（如 500 B, 1.5 KB, 2.0 MB）。
    若数值缺失、非正数或类型非法，统一返回 '大小未知'。
    直接使用数据库中已有的元数据，严禁在页面渲染时发起对象存储网络 I/O。
    """
    if num_bytes is None or isinstance(num_bytes, bool):
        return "大小未知"
    try:
        val = float(num_bytes)
    except (TypeError, ValueError):
        return "大小未知"

    if val <= 0:
        return "大小未知"

    if val < 1024:
        return f"{int(val)} B"
    elif val < 1024 * 1024:
        return f"{val / 1024:.1f} KB"
    elif val < 1024 * 1024 * 1024:
        return f"{val / (1024 * 1024):.1f} MB"
    else:
        return f"{val / (1024 * 1024 * 1024):.1f} GB"


def _resolve_doc_extension(doc_instance: Any) -> str:
    """
    提取文档大写扩展名（如 MD, ZIP, PDF）。
    优先读取 doc_instance.file_extension，其次从 doc_instance.filename 截取后缀；
    若缺失或无扩展名，兜底返回 'FILE'。
    """
    if not doc_instance:
        return "FILE"
    try:
        ext = getattr(doc_instance, "file_extension", "") or ""
    except Exception:
        ext = ""
    if isinstance(ext, str) and ext.strip():
        clean_ext = ext.strip().lstrip(".").upper()
        if clean_ext:
            return clean_ext

    try:
        filename = getattr(doc_instance, "filename", "") or ""
    except Exception:
        filename = ""
    if isinstance(filename, str) and "." in filename:
        clean_ext = filename.rsplit(".", 1)[-1].strip().upper()
        if clean_ext:
            return clean_ext

    return "FILE"


def _build_gateway_url(asset_type: str, asset_id: str) -> str:
    """通过 Django URL 反解受控媒体网关地址；非法 UUID 一律 fail-closed 返回空串。"""
    try:
        uid = UUID(str(asset_id))
        return reverse("media_gateway_asset", kwargs={"asset_type": asset_type, "asset_id": uid})
    except Exception:
        return ""


class CustomImageBlock(blocks.StructBlock):
    type_name = "image"
    schema_version = 1

    image = ImageChooserBlock(label="选择图片")
    caption = blocks.CharBlock(required=False, max_length=255, label="图注说明")
    alt_text = blocks.CharBlock(required=False, max_length=255, label="替代文本（无障碍）")
    display_format = blocks.ChoiceBlock(choices=FORMAT_CHOICES, default="centered", label="展示排版")

    class Meta:
        icon = "image"
        label = "图片"

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("image"):
            return BlockValidationResult(is_valid=False, errors=["必须选择一张图片"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value or not value.get("image"):
            return BlockRenderResult(html="", is_safe=True)

        img_instance = value.get("image")
        caption = value.get("caption") or ""
        alt_text = value.get("alt_text") or (getattr(img_instance, "title", "") if img_instance else "")
        fmt = value.get("display_format") or "centered"
        asset_id = _resolve_asset_id(img_instance)

        safe_caption = html.escape(caption)
        safe_alt = html.escape(alt_text)

        html_output = f"""
        <figure class="media-image-wrapper format-{fmt}" data-asset-id="{asset_id}">
            <img src="/media-gateway/image/{asset_id}/" alt="{safe_alt}" loading="lazy" data-asset-id="{asset_id}" />
            {f'<figcaption class="media-caption">{safe_caption}</figcaption>' if caption else ''}
        </figure>
        """
        return BlockRenderResult(html=sanitize_html(html_output), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        """Wagtail 模板原生渲染集成入口"""
        res = self.render_clean(value, context)
        return mark_safe(res.html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        caption = value.get("caption") or ""
        alt = value.get("alt_text") or ""
        text = f"{caption} {alt}".strip()
        return BlockTextExtraction(plain_text=text, logical_segments=[text] if text else [])

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        if not value or not value.get("image"):
            return []
        asset_id = _resolve_asset_id(value.get("image"))
        if not asset_id:
            return []
        return [MediaReference(asset_id=asset_id, asset_type="image", block_path=path)]


class CustomDocumentBlock(blocks.StructBlock):
    type_name = "document"
    schema_version = 1

    document = DocumentChooserBlock(label="选择文档")
    title_override = blocks.CharBlock(required=False, max_length=255, label="下载显示标题")
    description = blocks.TextBlock(required=False, rows=3, label="文档简要说明")

    class Meta:
        icon = "doc-full"
        label = "文档附件"

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("document"):
            return BlockValidationResult(is_valid=False, errors=["必须选择一个文档"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value or not value.get("document"):
            return BlockRenderResult(html="", is_safe=True)

        doc_instance = value.get("document")
        title = value.get("title_override") or (getattr(doc_instance, "title", "下载文档") if doc_instance else "下载文档")
        desc = value.get("description") or ""
        asset_id = _resolve_asset_id(doc_instance, expected_model=CustomDocument)

        safe_title = html.escape(title)
        safe_desc = html.escape(desc)
        safe_ext = html.escape(_resolve_doc_extension(doc_instance))
        safe_size = html.escape(_format_file_size(getattr(doc_instance, "file_size", None)))

        html_output = f"""
        <div class="media-document-card" data-asset-id="{asset_id}">
            <div class="doc-icon">📄</div>
            <div class="doc-info">
                <a href="/media-gateway/document/{asset_id}/" class="doc-download-link" download>{safe_title}</a>
                <div class="doc-meta">
                    <span class="doc-ext-badge">{safe_ext}</span>
                    <span class="doc-file-size">{safe_size}</span>
                </div>
                {f'<p class="doc-desc">{safe_desc}</p>' if desc else ''}
            </div>
        </div>
        """
        return BlockRenderResult(html=sanitize_html(html_output), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        """Wagtail 模板原生渲染集成入口"""
        res = self.render_clean(value, context)
        return mark_safe(res.html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        title = value.get("title_override") or ""
        desc = value.get("description") or ""
        text = f"{title} {desc}".strip()
        return BlockTextExtraction(plain_text=text, logical_segments=[text] if text else [])

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        if not value or not value.get("document"):
            return []
        asset_id = _resolve_asset_id(value.get("document"), expected_model=CustomDocument)
        if not asset_id:
            return []
        return [MediaReference(asset_id=asset_id, asset_type="document", block_path=path)]


class CustomAttachmentListBlock(blocks.StructBlock):
    """
    配套课件与附件下载列表积木 (CustomAttachmentListBlock)。
    用于文章、教程底部展示批量课件材料（如 Markdown 讲义、ZIP 示例代码包、PDF 文档等）。
    严格保持每个附件的独立 MediaReference 引用追踪，避免多附件或重复附件时垃圾回收保护失效。
    """
    type_name = "attachment_list"
    schema_version = 1

    heading = blocks.CharBlock(required=False, max_length=120, default="配套课件与附件下载", label="附件区标题")
    items = blocks.ListBlock(CustomDocumentBlock(), label="附件列表")

    class Meta:
        icon = "folder-open-inverse"
        label = "课件/附件下载列表"

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value:
            return BlockValidationResult(is_valid=False, errors=["附件列表不能为空"])
        items = value.get("items")
        if not items:
            return BlockValidationResult(is_valid=False, errors=["附件列表不能为空"])

        doc_block = CustomDocumentBlock()
        errors: list[str] = []
        for idx, item in enumerate(items):
            res = doc_block.validate_value(item, context=context)
            if not res.is_valid:
                for err in res.errors:
                    errors.append(f"第 {idx + 1} 项附件无效: {err}")

        if errors:
            return BlockValidationResult(is_valid=False, errors=errors)
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)

        items = value.get("items") or []
        if not items:
            return BlockRenderResult(html="", is_safe=True)

        raw_heading = value.get("heading")
        heading = "配套课件与附件下载" if raw_heading is None else str(raw_heading).strip()
        safe_heading = html.escape(heading)

        li_items: list[str] = []
        for item in items:
            if not item:
                continue
            doc_instance = item.get("document") if isinstance(item, dict) else getattr(item, "get", lambda k: None)("document")
            if not doc_instance:
                continue

            title = (
                item.get("title_override")
                if isinstance(item, dict)
                else getattr(item, "get", lambda k: "")("title_override")
            ) or (getattr(doc_instance, "title", "下载文档") if doc_instance else "下载文档")
            desc = (
                item.get("description")
                if isinstance(item, dict)
                else getattr(item, "get", lambda k: "")("description")
            ) or ""
            asset_id = _resolve_asset_id(doc_instance, expected_model=CustomDocument)

            safe_title = html.escape(title)
            safe_desc = html.escape(desc)
            safe_ext = html.escape(_resolve_doc_extension(doc_instance))
            safe_size = html.escape(_format_file_size(getattr(doc_instance, "file_size", None)))

            li_html = f"""
            <li>
                <div class="doc-info">
                    <a href="/media-gateway/document/{asset_id}/" class="doc-download-link" download>{safe_title}</a>
                    <div class="doc-meta">
                        <span class="doc-ext-badge">{safe_ext}</span>
                        <span class="doc-file-size">{safe_size}</span>
                    </div>
                    {f'<p class="doc-desc">{safe_desc}</p>' if desc else ''}
                </div>
            </li>
            """
            li_items.append(li_html)

        if not li_items:
            return BlockRenderResult(html="", is_safe=True)

        heading_html = f'<div class="attachment-list-heading">{safe_heading}</div>' if safe_heading else ""
        html_output = f"""
        <div class="media-attachment-list">
            {heading_html}
            <ul>
                {"".join(li_items)}
            </ul>
        </div>
        """
        return BlockRenderResult(html=sanitize_html(html_output), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        """Wagtail 模板原生渲染集成入口"""
        res = self.render_clean(value, context)
        return mark_safe(res.html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        raw_heading = value.get("heading")
        heading = "" if raw_heading is None else str(raw_heading).strip()
        segments: list[str] = []
        if heading:
            segments.append(heading)

        items = value.get("items") or []
        for item in items:
            if not item:
                continue
            title = (
                item.get("title_override")
                if isinstance(item, dict)
                else getattr(item, "get", lambda k: "")("title_override")
            ) or ""
            desc = (
                item.get("description")
                if isinstance(item, dict)
                else getattr(item, "get", lambda k: "")("description")
            ) or ""
            item_text = f"{title} {desc}".strip()
            if item_text:
                segments.append(item_text)

        plain_text = " ".join(segments).strip()
        return BlockTextExtraction(plain_text=plain_text, logical_segments=segments)

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        if not value:
            return []
        items = value.get("items") or []
        refs: list[MediaReference] = []
        for idx, item in enumerate(items):
            if not item:
                continue
            doc_instance = item.get("document") if isinstance(item, dict) else getattr(item, "get", lambda k: None)("document")
            if not doc_instance:
                continue
            asset_id = _resolve_asset_id(doc_instance, expected_model=CustomDocument)
            if not asset_id:
                continue
            refs.append(
                MediaReference(
                    asset_id=asset_id,
                    asset_type="document",
                    block_path=f"{path}.items.{idx}.document",
                )
            )
        return refs


class CustomAudioBlock(blocks.StructBlock):
    type_name = "audio"
    schema_version = 1

    audio = SnippetChooserBlock(Audio, label="选择音频")
    title_override = blocks.CharBlock(required=False, max_length=255, label="自定义音频标题")

    class Meta:
        icon = "media"
        label = "音频"
        form_template = "content_blocks/widgets/media_split.html"

    def get_form_context(self, value, prefix="", errors=None):
        context = super().get_form_context(value, prefix=prefix, errors=errors)
        context["media_kind"] = "audio"
        context["chooser_field"] = "audio"
        return context

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("audio"):
            return BlockValidationResult(is_valid=False, errors=["必须选择一段音频"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value or not value.get("audio"):
            return BlockRenderResult(html="", is_safe=True)

        audio_inst = value.get("audio")
        title = value.get("title_override") or (getattr(audio_inst, "title", "音频播放") if audio_inst else "音频播放")
        asset_id = _resolve_asset_id(audio_inst, expected_model=Audio)
        gateway_url = _build_gateway_url("audio", asset_id)
        if not gateway_url:
            return BlockRenderResult(html="", is_safe=True)

        safe_title = html.escape(title)
        safe_asset_id = html.escape(asset_id, quote=True)
        safe_src = html.escape(gateway_url, quote=True)
        html_output = f"""
        <div class="media-audio-player" data-cb-media-kind="audio" data-asset-id="{safe_asset_id}">
            <div class="audio-title">{safe_title}</div>
            <div class="cb-media-stage">
                <audio controls preload="metadata" src="{safe_src}">
                    您的浏览器不支持音频播放。
                </audio>
            </div>
        </div>
        """
        return BlockRenderResult(html=sanitize_html(html_output), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        """Wagtail 模板原生渲染集成入口"""
        res = self.render_clean(value, context)
        return mark_safe(res.html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        title = value.get("title_override") or ""
        return BlockTextExtraction(plain_text=title, logical_segments=[title] if title else [])

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        if not value or not value.get("audio"):
            return []
        asset_id = _resolve_asset_id(value.get("audio"), expected_model=Audio)
        if not asset_id:
            return []
        return [MediaReference(asset_id=asset_id, asset_type="audio", block_path=path)]


class CustomVideoBlock(blocks.StructBlock):
    type_name = "video"
    schema_version = 1

    video = SnippetChooserBlock(Video, label="选择视频")
    title_override = blocks.CharBlock(required=False, max_length=255, label="自定义视频标题")

    class Meta:
        icon = "media"
        label = "视频"
        form_template = "content_blocks/widgets/media_split.html"

    def get_form_context(self, value, prefix="", errors=None):
        context = super().get_form_context(value, prefix=prefix, errors=errors)
        context["media_kind"] = "video"
        context["chooser_field"] = "video"
        return context

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("video"):
            return BlockValidationResult(is_valid=False, errors=["必须选择一段视频"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value or not value.get("video"):
            return BlockRenderResult(html="", is_safe=True)

        video_inst = value.get("video")
        title = value.get("title_override") or (getattr(video_inst, "title", "视频播放") if video_inst else "视频播放")
        asset_id = _resolve_asset_id(video_inst, expected_model=Video)
        gateway_url = _build_gateway_url("video", asset_id)
        if not gateway_url:
            return BlockRenderResult(html="", is_safe=True)

        safe_title = html.escape(title)
        safe_asset_id = html.escape(asset_id, quote=True)
        safe_src = html.escape(gateway_url, quote=True)
        poster_url = ""
        if hasattr(video_inst, "poster") and video_inst.poster:
            poster_id = _resolve_asset_id(video_inst.poster)
            resolved_poster = _build_gateway_url("image", poster_id)
            if resolved_poster:
                poster_url = f'poster="{html.escape(resolved_poster, quote=True)}"'

        html_output = f"""
        <div class="media-video-player" data-cb-media-kind="video" data-asset-id="{safe_asset_id}">
            {f'<div class="video-title">{safe_title}</div>' if title else ''}
            <div class="cb-media-stage">
                <video controls playsinline preload="metadata" {poster_url} src="{safe_src}">
                    您的浏览器不支持视频播放。
                </video>
            </div>
        </div>
        """
        return BlockRenderResult(html=sanitize_html(html_output), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        """Wagtail 模板原生渲染集成入口"""
        res = self.render_clean(value, context)
        return mark_safe(res.html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        title = value.get("title_override") or ""
        return BlockTextExtraction(plain_text=title, logical_segments=[title] if title else [])

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        if not value or not value.get("video"):
            return []
        refs: list[MediaReference] = []
        video_inst = value.get("video")
        asset_id = _resolve_asset_id(video_inst, expected_model=Video)
        if asset_id:
            refs.append(MediaReference(asset_id=asset_id, asset_type="video", block_path=path))
        # 检查是否有关联的封面海报图引用
        if hasattr(video_inst, "poster") and video_inst.poster:
            poster_id = _resolve_asset_id(video_inst.poster)
            if poster_id:
                refs.append(MediaReference(asset_id=poster_id, asset_type="image", block_path=f"{path}.poster"))
        return refs
