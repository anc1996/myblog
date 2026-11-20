"""
Wagtail 8.1 StreamField 后台左右分栏编辑器控件：
1. VditorMarkdownWidget: Markdown 分栏编辑 + 截图粘贴直传 CustomImage；
2. MermaidSplitWidget: 左侧 Mermaid 源码 + 右侧实时 SVG 预览；
3. TableJsonSplitWidget: 左侧复杂表格 JSON + 右侧实时表格（含公式/代码高亮）预览。
"""

from django import forms
from django.conf import settings
from django.urls import reverse
from wagtail.admin.staticfiles import versioned_static


CB_ADMIN_ASSET_VERSION = "20261120.4"


def cb_admin_static(path: str) -> str:
    url = versioned_static(path)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}v={CB_ADMIN_ASSET_VERSION}"


class VditorMarkdownWidget(forms.Textarea):
    """Markdown 后台 Vditor 控件：隐藏原生 textarea 作为唯一提交与自动保存源"""

    template_name = "content_blocks/widgets/vditor_markdown.html"

    def build_attrs(self, base_attrs=None, extra_attrs=None):
        attrs = super().build_attrs(base_attrs, extra_attrs)
        attrs["data-cb-vditor-markdown"] = "true"
        attrs["data-vditor-cdn"] = f"{settings.STATIC_URL.rstrip('/')}/vendor/vditor"
        attrs["data-vditor-mode"] = "sv"
        attrs["data-vditor-locale"] = "zh_CN"
        attrs["data-vditor-upload-url"] = reverse("content_blocks_vditor_image_upload")
        attrs["data-vditor-max-size"] = str(
            getattr(settings, "WAGTAILIMAGES_MAX_UPLOAD_SIZE", 10 * 1024 * 1024)
        )
        attrs["class"] = " ".join(
            part for part in (attrs.get("class", ""), "cb-vditor-source") if part
        )
        return attrs

    @property
    def media(self):
        return forms.Media(
            css={
                "all": (
                    versioned_static("vendor/vditor/dist/index.css"),
                    cb_admin_static("content_blocks/css/table.css"),
                    cb_admin_static("content_blocks/css/content-images.css"),
                    cb_admin_static("content_blocks/css/admin-split-editors.css"),
                )
            },
            js=(
                versioned_static("vendor/vditor/dist/index.min.js"),
                cb_admin_static("content_blocks/js/mermaid-renderer.js"),
                cb_admin_static("content_blocks/js/admin-vditor-markdown.js"),
            ),
        )


class MermaidSplitWidget(forms.Textarea):
    """Mermaid 后台左右分栏编辑控件：左源码 + 右实时安全 SVG 渲染"""

    template_name = "content_blocks/widgets/mermaid_split.html"

    def build_attrs(self, base_attrs=None, extra_attrs=None):
        attrs = super().build_attrs(base_attrs, extra_attrs)
        attrs["data-cb-admin-mermaid-input"] = "true"
        attrs["class"] = " ".join(
            part for part in (attrs.get("class", ""), "cb-admin-split-textarea") if part
        )
        return attrs

    @property
    def media(self):
        return forms.Media(
            css={
                "all": (
                    cb_admin_static("content_blocks/css/admin-split-editors.css"),
                )
            },
            js=(
                cb_admin_static("content_blocks/js/mermaid-renderer.js"),
                cb_admin_static("content_blocks/js/admin-mermaid-split.js"),
            ),
        )


class TableJsonSplitWidget(forms.Textarea):
    """复杂表格后台左右分栏编辑控件：左 JSON 编辑 + 右实时服务端渲染预览"""

    template_name = "content_blocks/widgets/table_json_split.html"

    def build_attrs(self, base_attrs=None, extra_attrs=None):
        attrs = super().build_attrs(base_attrs, extra_attrs)
        attrs["data-cb-admin-table-input"] = "true"
        attrs["data-preview-url"] = reverse("content_blocks_table_preview")
        attrs["class"] = " ".join(
            part for part in (attrs.get("class", ""), "cb-admin-split-textarea") if part
        )
        return attrs

    @property
    def media(self):
        return forms.Media(
            css={
                "all": (
                    versioned_static("vendor/katex/katex.min.css"),
                    versioned_static("vendor/highlightjs/styles/github-dark.min.css"),
                    cb_admin_static("content_blocks/css/table.css"),
                    cb_admin_static("content_blocks/css/math.css"),
                    cb_admin_static("content_blocks/css/code-highlighting.css"),
                    cb_admin_static("content_blocks/css/content-images.css"),
                    cb_admin_static("content_blocks/css/admin-split-editors.css"),
                )
            },
            js=(
                cb_admin_static("content_blocks/js/math-render.js"),
                cb_admin_static("content_blocks/js/code-highlighting.js"),
                cb_admin_static("content_blocks/js/admin-table-split.js"),
            ),
        )
