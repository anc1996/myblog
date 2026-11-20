"""
受控嵌入内容块 (CustomEmbedBlock)。

复用全站统一 content_blocks.embeds 注册表：
1. 统一采集「媒体标题 / 无障碍描述」与「流媒体链接」，同时向下兼容历史纯字符串 URL 数据；
2. 支持 B站、YouTube、Vimeo、腾讯视频、优酷、网易云音乐、Spotify、酷狗、咪咕、CodePen 等；
3. 强制白名单平台校验与 SSRF 防御，满足 AC-14 安全验收要求；
4. 覆盖 deconstruct() 保持零 StreamField 迁移。
"""

from typing import Any
from django.core.exceptions import ValidationError
from django.utils.safestring import mark_safe
from wagtail import blocks
from wagtail.blocks import StructBlockValidationError
from wagtail.embeds.blocks import EmbedValue

from content_blocks.contracts import (
    BlockContract,
    BlockValidationResult,
    BlockRenderResult,
    BlockTextExtraction,
    MediaReference,
)
from content_blocks.embeds import (
    is_safe_embed_url,
    render_embed_wrapper_html,
    resolve_embed_spec,
)
from content_blocks.sanitizer import sanitize_html


class CustomEmbedBlock(blocks.StructBlock):
    type_name = "embed"
    schema_version = 2

    title = blocks.CharBlock(
        required=True,
        max_length=255,
        label="媒体标题 / 无障碍描述",
        help_text="请输入此视频或音频的真实名称。",
    )
    embed_url = blocks.URLBlock(
        required=True,
        label="流媒体链接",
        help_text="直接粘贴 B站、YouTube、Vimeo、腾讯视频、优酷、网易云音乐、Spotify、酷狗、咪咕、CodePen 等单页链接",
    )

    class Meta:
        icon = "media"
        label = "嵌入媒体"

    def deconstruct(self):
        # 保持与历史 StreamField block_lookup 一致，避免新增冗余迁移
        return ("content_blocks.blocks.embed.CustomEmbedBlock", (), {})

    @classmethod
    def construct_from_lookup(cls, lookup, *args, **kwargs):
        # 兼容历史迁移中 ("...CustomEmbedBlock", (), {}) 无 child_blocks 参数的反序列化
        if not args:
            return cls(**kwargs)
        return super().construct_from_lookup(lookup, *args, **kwargs)

    def to_python(self, value: Any) -> Any:
        # 兼容历史版本纯字符串 URL 或旧 EmbedValue
        if isinstance(value, EmbedValue):
            value = {"title": "历史媒体档案", "embed_url": value.url}
        elif isinstance(value, str):
            value = {"title": "历史媒体档案", "embed_url": value}
        elif isinstance(value, dict) and "url" in value and "embed_url" not in value:
            value = {
                "title": value.get("title") or "历史媒体档案",
                "embed_url": value.get("url") or "",
            }
        return super().to_python(value)

    def _extract_fields(self, value: Any) -> tuple[str, str]:
        if not value:
            return "", ""
        if isinstance(value, EmbedValue):
            return "历史媒体档案", str(value.url or "").strip()
        if isinstance(value, str):
            return "历史媒体档案", value.strip()
        if isinstance(value, dict):
            title = str(value.get("title") or "").strip()
            url = str(value.get("embed_url") or value.get("url") or "").strip()
            return title, url
        return "", str(value).strip()

    def clean(self, value: Any) -> Any:
        # 兼容旧调用方直接传入纯字符串 URL 或 EmbedValue
        if isinstance(value, (str, EmbedValue)):
            value = self.to_python(value)
        cleaned = super().clean(value)
        _, url = self._extract_fields(cleaned)
        if url:
            is_safe, reason = is_safe_embed_url(url)
            if not is_safe:
                raise StructBlockValidationError(
                    block_errors={"embed_url": ValidationError(reason)}
                )
        return cleaned

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value:
            return BlockValidationResult(is_valid=True)
        title, url = self._extract_fields(value)
        if not url:
            return BlockValidationResult(is_valid=False, errors=["流媒体链接不能为空"])
        spec, reason = resolve_embed_spec(url, title=title)
        if not spec:
            return BlockValidationResult(is_valid=False, errors=[reason])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value:
            return BlockRenderResult(html="", is_safe=True)
        title, url = self._extract_fields(value)
        raw_html = render_embed_wrapper_html(url, title=title)
        if not raw_html:
            return BlockRenderResult(html="", is_safe=True)
        return BlockRenderResult(html=sanitize_html(raw_html), is_safe=True)

    def render_basic(self, value: Any, context: Any = None):
        """Wagtail 模板原生渲染集成入口"""
        res = self.render_clean(value, context)
        return mark_safe(res.html)

    def extract_text(self, value: Any) -> BlockTextExtraction:
        if not value:
            return BlockTextExtraction(plain_text="", logical_segments=[])
        title, url = self._extract_fields(value)
        segments = [s for s in (title, url) if s]
        return BlockTextExtraction(plain_text=" ".join(segments), logical_segments=segments)

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        return []
