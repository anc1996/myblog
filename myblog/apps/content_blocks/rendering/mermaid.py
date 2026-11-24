"""
Mermaid 流程图统一服务端 HTML 容器构建与契约规范。

无论是独立 CustomMermaidBlock 还是 CustomMarkdownBlock 内嵌的 ```mermaid 围栏，
均输出 100% 相同的数据契约与结构化 DOM。
"""

import html
import textwrap

ALLOWED_THEMES = {"follow-site", "default", "dark", "forest", "neutral"}


def render_mermaid_dom(source_code: str, title: str = "", theme: str = "follow-site") -> str:
    """
    生成统一的 Mermaid 图表容器 HTML。
    包含标题、版本徽标、控制工具栏（缩放/自适应/全屏/折叠/深浅主题/源码/复制/导出）与源码降级容器。
    """
    if not source_code:
        return ""

    # 规范换行并剔除外层公共缩进，保留图表内部相对缩进
    normalized = source_code.replace("\r\n", "\n").replace("\r", "\n")
    dedented = textwrap.dedent(normalized).strip()
    if not dedented:
        return ""

    safe_theme = theme if theme in ALLOWED_THEMES else "follow-site"
    safe_title = html.escape(title.strip()) if title else "Mermaid 图表"
    safe_source = html.escape(dedented)

    return f"""
<div class="cb-mermaid mermaid-block-wrapper" data-cb-mermaid data-theme="{safe_theme}">
    <div class="cb-mermaid-header">
        <span class="cb-mermaid-title">{safe_title}</span>
        <span class="cb-mermaid-badge">Mermaid 11.17</span>
        <div class="cb-mermaid-actions">
            <button type="button" class="cb-btn cb-btn-zoom-in" title="放大 (+20%)" aria-label="放大">＋</button>
            <button type="button" class="cb-btn cb-btn-zoom-out" title="缩小 (-20%)" aria-label="缩小">－</button>
            <button type="button" class="cb-btn cb-btn-zoom-reset" title="重置 (100%)" aria-label="重置">1:1</button>
            <button type="button" class="cb-btn cb-btn-fit" title="自适应宽度" aria-label="自适应">⤢</button>
            <button type="button" class="cb-btn cb-btn-theme" title="切换深浅主题" aria-label="切换主题">🌓</button>
            <button type="button" class="cb-btn cb-btn-source" aria-pressed="false" title="分栏对照：左侧源码 / 右侧渲染" aria-label="分栏源码对照">⇄ 分栏源码</button>
            <button type="button" class="cb-btn cb-btn-copy" title="复制源码" aria-label="复制源码">📋</button>
            <button type="button" class="cb-btn cb-btn-export" title="导出 SVG" aria-label="导出 SVG">⬇ SVG</button>
            <button type="button" class="cb-btn cb-btn-fullscreen" title="全屏查看" aria-label="全屏">⛶</button>
            <button type="button" class="cb-btn cb-btn-collapse" title="折叠/展开" aria-label="折叠">▼</button>
        </div>
    </div>
    <div class="cb-mermaid-split-body">
        <div class="cb-mermaid-source-wrapper" hidden>
            <div class="cb-mermaid-pane-label">Mermaid 源码</div>
            <pre class="cb-mermaid-source"><code class="language-plaintext" data-cb-mermaid-source>{safe_source}</code></pre>
        </div>
        <div class="cb-mermaid-viewport">
            <div class="mermaid-diagram" data-mermaid="{safe_source}"></div>
        </div>
    </div>
    <div class="cb-mermaid-status" hidden></div>
</div>
""".strip()
