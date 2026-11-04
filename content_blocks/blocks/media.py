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
from django.utils.safestring import mark_safe
from wagtail import blocks
from wagtail.images.blocks import ImageChooserBlock
from wagtail.documents.blocks import DocumentChooserBlock
from wagtail.snippets.blocks import SnippetChooserBlock

from media_library.models import Audio, Video, CustomImage
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


def _resolve_asset_id(instance_or_id: Any) -> str:
    """安全提取模型实例的 public_id，未绑定时返回空字符串"""
    if not instance_or_id:
        return ""
    if hasattr(instance_or_id, "public_id"):
        return str(instance_or_id.public_id)
    return str(instance_or_id)


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
        asset_id = _resolve_asset_id(doc_instance)

        safe_title = html.escape(title)
        safe_desc = html.escape(desc)

        html_output = f"""
        <div class="media-document-card" data-asset-id="{asset_id}">
            <div class="doc-icon">📄</div>
            <div class="doc-info">
                <a href="/media-gateway/document/{asset_id}/" class="doc-download-link" download>{safe_title}</a>
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
        asset_id = _resolve_asset_id(value.get("document"))
        if not asset_id:
            return []
        return [MediaReference(asset_id=asset_id, asset_type="document", block_path=path)]


class CustomAudioBlock(blocks.StructBlock):
    type_name = "audio"
    schema_version = 1

    audio = SnippetChooserBlock(Audio, label="选择音频")
    title_override = blocks.CharBlock(required=False, max_length=255, label="自定义音频标题")

    class Meta:
        icon = "media"
        label = "音频"

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("audio"):
            return BlockValidationResult(is_valid=False, errors=["必须选择一段音频"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value or not value.get("audio"):
            return BlockRenderResult(html="", is_safe=True)

        audio_inst = value.get("audio")
        title = value.get("title_override") or (getattr(audio_inst, "title", "音频播放") if audio_inst else "音频播放")
        asset_id = _resolve_asset_id(audio_inst)

        safe_title = html.escape(title)
        html_output = f"""
        <div class="media-audio-player" data-asset-id="{asset_id}">
            <div class="audio-title">{safe_title}</div>
            <audio controls preload="metadata" src="/media-gateway/audio/{asset_id}/">
                您的浏览器不支持音频播放。
            </audio>
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
        asset_id = _resolve_asset_id(value.get("audio"))
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

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("video"):
            return BlockValidationResult(is_valid=False, errors=["必须选择一段视频"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value or not value.get("video"):
            return BlockRenderResult(html="", is_safe=True)

        video_inst = value.get("video")
        title = value.get("title_override") or (getattr(video_inst, "title", "视频播放") if video_inst else "视频播放")
        asset_id = _resolve_asset_id(video_inst)

        safe_title = html.escape(title)
        poster_url = ""
        if hasattr(video_inst, "poster") and video_inst.poster:
            poster_id = _resolve_asset_id(video_inst.poster)
            poster_url = f'poster="/media-gateway/image/{poster_id}/"'

        html_output = f"""
        <div class="media-video-player" data-asset-id="{asset_id}">
            {f'<div class="video-title">{safe_title}</div>' if title else ''}
            <video controls preload="metadata" {poster_url} src="/media-gateway/video/{asset_id}/">
                您的浏览器不支持视频播放。
            </video>
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
        asset_id = _resolve_asset_id(video_inst)
        if asset_id:
            refs.append(MediaReference(asset_id=asset_id, asset_type="video", block_path=path))
        # 检查是否有关联的封面海报图引用
        if hasattr(video_inst, "poster") and video_inst.poster:
            poster_id = _resolve_asset_id(video_inst.poster)
            if poster_id:
                refs.append(MediaReference(asset_id=poster_id, asset_type="image", block_path=f"{path}.poster"))
        return refs
