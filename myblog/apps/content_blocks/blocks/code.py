"""
自定义代码块 (CustomCodeBlock)。

一个 Block 两种模式：
1. 展示模式 (display): 语法高亮、行号、一键复制、超长折叠；
2. 运行模式 (runner): 提供浏览器在线代码编辑区、标准输入/输出/错误展示区与协议约定；
   首期绝不在服务器上盲目执行不受信代码（严格满足 AC-13）。
"""

import html
import urllib.parse
from typing import Any
from django.utils.safestring import mark_safe
from wagtail import blocks
import pygments
from pygments.lexers import get_lexer_by_name, TextLexer
from pygments.formatters import HtmlFormatter

from content_blocks.contracts import (
    BlockContract,
    BlockValidationResult,
    BlockRenderResult,
    BlockTextExtraction,
    MediaReference,
)
from content_blocks.sanitizer import sanitize_html


def highlight_code(raw_code: str, lang: str = "text") -> str:
    """使用 Pygments 生成语法高亮 HTML 片段"""
    if not raw_code:
        return ""
    try:
        lexer = get_lexer_by_name(lang, stripall=True)
    except Exception:
        lexer = TextLexer(stripall=True)
    formatter = HtmlFormatter(nowrap=True, classprefix="hl-")
    return pygments.highlight(raw_code, lexer, formatter)


LANGUAGE_CHOICES = [
    ("python", "Python"),
    ("javascript", "JavaScript"),
    ("typescript", "TypeScript"),
    ("html", "HTML"),
    ("css", "CSS"),
    ("bash", "Bash / Shell"),
    ("sql", "SQL / PostgreSQL"),
    ("json", "JSON"),
    ("yaml", "YAML"),
    ("rust", "Rust"),
    ("go", "Go"),
    ("cpp", "C / C++"),
    ("markdown", "Markdown"),
    ("text", "纯文本 Plain Text"),
]

MODE_CHOICES = [
    ("display", "展示模式（高亮与行号）"),
    ("runner", "运行模式（首期仅界面协议，不执行未授权代码）"),
]


class CustomCodeBlock(blocks.StructBlock):
    type_name = "code"
    schema_version = 1

    title = blocks.CharBlock(required=False, max_length=120, label="代码标题 / 文件名", help_text="例如 main.py")
    language = blocks.ChoiceBlock(choices=LANGUAGE_CHOICES, default="python", label="编程语言")
    code = blocks.TextBlock(label="代码内容", rows=8)
    mode = blocks.ChoiceBlock(choices=MODE_CHOICES, default="display", label="运行模式")

    class Meta:
        icon = "code"
        label = "代码块"

    def validate_value(self, value: Any, context: Any = None) -> BlockValidationResult:
        if not value or not value.get("code"):
            return BlockValidationResult(is_valid=False, errors=["代码内容不能为空"])
        return BlockValidationResult(is_valid=True)

    def render_clean(self, value: Any, context: Any = None) -> BlockRenderResult:
        if not value or not value.get("code"):
            return BlockRenderResult(html="", is_safe=True)

        title = value.get("title") or ""
        raw_code = value.get("code") or ""
        lang = value.get("language") or "text"
        mode = value.get("mode") or "display"

        highlighted_code = highlight_code(raw_code, lang)

        safe_title = html.escape(title)
        encoded_code = urllib.parse.quote(raw_code)
        if mode == "runner":
            # 运行模式：渲染交互式原型界面容器与状态说明，严禁在后端执行代码 (AC-13)
            html_output = f"""
            <div class="code-block code-block-runner" data-mode="runner" data-language="{lang}">
                <div class="code-header">
                    <span class="code-title">{safe_title or lang}</span>
                    <span class="code-badge">交互预览模式</span>
                </div>
                <div class="code-body"><pre class="code-content"><code>{highlighted_code}</code></pre></div>
                <div class="code-runner-panel">
                    <div class="runner-status">代码在线执行沙箱将在后续阶段开放，第一阶段仅提供交互界面与协议约定。</div>
                    <button type="button" class="runner-btn" disabled>运行代码（第一阶段未开放）</button>
                </div>
            </div>
            """
        else:
            # 展示模式：行号、语言与一键复制按钮
            html_output = f"""
            <div class="code-block code-block-display" data-mode="display" data-language="{lang}">
                <div class="code-header">
                    <span class="code-title">{safe_title or lang}</span>
                    <button type="button" class="copy-code-btn" data-clipboard-text="{encoded_code}">复制</button>
                </div>
                <div class="code-body"><pre class="code-content"><code>{highlighted_code}</code></pre></div>
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
        title = value.get("title") or ""
        code = value.get("code") or ""
        full_text = f"{title}\n{code}".strip()
        return BlockTextExtraction(plain_text=full_text, logical_segments=[title, code] if title else [code])

    def extract_media_refs(self, value: Any, path: str = "") -> list[MediaReference]:
        return []
