"""
LaTeX 数学公式保护与安全渲染转换。

功能特性：
1. 在 Python-Markdown 扩展（tables, nl2br 等）执行前，提前提取并保护数学公式；
2. 严格区分代码块与公式：跳过 fenced code、fenced mermaid 和行内反引号代码 `...`；
3. 区分货币与行内公式：避免误匹配 $100、$100 - $200 等价格文本；
4. 分层安全：公式源码经 html.escape 转义存入 data-tex 与 textContent，前台由 KaTeX 渲染；
5. HTML5 内容模型合规：避免在 <p> 内产生非法 <div> 嵌套，表格单元格内使用 block 级 span。
"""

import html
import re
from typing import Any

# 块级公式：$$...$$ 或 \[...\]
_BLOCK_MATH_DOLLAR_RE = re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", re.DOTALL)
_BLOCK_MATH_BRACKET_RE = re.compile(r"(?<!\\)\\\[(.+?)(?<!\\)\\\]", re.DOTALL)

# 行内公式：\(...\)
_INLINE_MATH_PAREN_RE = re.compile(r"(?<!\\)\\\((.+?)(?<!\\)\\\)", re.DOTALL)

# 行内公式：$...$
# 规则：
# 1. 前面不能是反斜杠或另一个 $
# 2. 开头 $ 后面紧接非空白字符且不能是 $
# 3. 结尾 $ 前面紧接非空白字符且不能是反斜杠
# 4. 结尾 $ 后面不能紧接数字（避免 $100 误判）
# 5. 内部不能包含换行符（行内公式不能跨行）
_INLINE_MATH_DOLLAR_RE = re.compile(
    r"(?<![\\\$])\$(?!\$)(?!\s)([^\$\n]+?)(?<!\s)(?<!\\)\$(?![0-9])"
)


def protect_math_in_markdown(text: str, nonce: str) -> tuple[str, list[dict[str, Any]]]:
    """
    扫描 Markdown 文本，保护代码块并提取数学公式，返回替换后的文本与公式元数据列表。
    """
    if not text:
        return "", []

    code_stash: list[str] = []
    math_items: list[dict[str, Any]] = []

    # 1. 保护代码块与行内代码，防止代码里的 $ 或 \\ 被误当成公式
    def stash_code(m: re.Match) -> str:
        idx = len(code_stash)
        code_stash.append(m.group(0))
        return f"CBCODE_{nonce}_{idx}_X"

    # 优先匹配代码围栏 (含 mermaid 围栏)，再匹配行内反引号
    protected = re.sub(r"```[\s\S]*?```", stash_code, text)
    protected = re.sub(r"`[^`\n]+`", stash_code, protected)

    # 2. 提取块级公式：$$...$$
    def stash_block_dollar(m: re.Match) -> str:
        raw_tex = m.group(1).strip()
        idx = len(math_items)
        math_items.append({"tex": raw_tex, "is_display": True, "token": f"CBMATH_{nonce}_{idx}_X"})
        return f"\n\nCBMATH_{nonce}_{idx}_X\n\n"

    protected = _BLOCK_MATH_DOLLAR_RE.sub(stash_block_dollar, protected)

    # 3. 提取块级公式：\[...\]
    def stash_block_bracket(m: re.Match) -> str:
        raw_tex = m.group(1).strip()
        idx = len(math_items)
        math_items.append({"tex": raw_tex, "is_display": True, "token": f"CBMATH_{nonce}_{idx}_X"})
        return f"\n\nCBMATH_{nonce}_{idx}_X\n\n"

    protected = _BLOCK_MATH_BRACKET_RE.sub(stash_block_bracket, protected)

    # 4. 提取行内公式：\(...\)
    def stash_inline_paren(m: re.Match) -> str:
        raw_tex = m.group(1).strip()
        idx = len(math_items)
        math_items.append({"tex": raw_tex, "is_display": False, "token": f"CBMATH_{nonce}_{idx}_X"})
        return f"CBMATH_{nonce}_{idx}_X"

    protected = _INLINE_MATH_PAREN_RE.sub(stash_inline_paren, protected)

    # 5. 提取行内公式：$...$
    def stash_inline_dollar(m: re.Match) -> str:
        content = m.group(1).strip()
        # 排除包含纯货币描述的误报，例如 "100" 或 "100 - 200"
        if re.fullmatch(r"^\d+(?:\.\d+)?(?:\s*[-~至到]\s*\d+(?:\.\d+)?)?$", content):
            return m.group(0)
        idx = len(math_items)
        math_items.append({"tex": content, "is_display": False, "token": f"CBMATH_{nonce}_{idx}_X"})
        return f"CBMATH_{nonce}_{idx}_X"

    protected = _INLINE_MATH_DOLLAR_RE.sub(stash_inline_dollar, protected)

    # 6. 还原被保护的代码块
    for idx, raw_code in enumerate(code_stash):
        protected = protected.replace(f"CBCODE_{nonce}_{idx}_X", raw_code)

    return protected, math_items


def restore_math_in_html(html_str: str, math_items: list[dict[str, Any]]) -> str:
    """
    将占位令牌还原为标准的安全 KaTeX 数学容器。
    严格防范 <p><div> 嵌套：段落内的块公式使用 <p> 替换，其余位置使用合法容器。
    """
    if not html_str or not math_items:
        return html_str

    res = html_str
    for item in math_items:
        token = item["token"]
        tex = item["tex"]
        is_display = item["is_display"]
        safe_tex = html.escape(tex)

        if is_display:
            # 若被 Markdown 外层自动套了 <p>，整段替换为 <div>
            block_html = (
                f'<div class="cb-math cb-math-block" data-cb-math="display" '
                f'data-tex="{safe_tex}">$${safe_tex}$$</div>'
            )
            p_pattern = re.compile(r"<p>\s*" + re.escape(token) + r"\s*</p>")
            if p_pattern.search(res):
                res = p_pattern.sub(lambda _: block_html, res)
            else:
                # 行内或表格单元格内出现的块公式，使用 display 模式的 <span>，避免破坏标签模型
                inline_display_html = (
                    f'<span class="cb-math cb-math-display" data-cb-math="display" '
                    f'data-tex="{safe_tex}">$${safe_tex}$$</span>'
                )
                res = res.replace(token, inline_display_html)
        else:
            inline_html = (
                f'<span class="cb-math cb-math-inline" data-cb-math="inline" '
                f'data-tex="{safe_tex}">${safe_tex}$</span>'
            )
            res = res.replace(token, inline_html)

    return res


def render_inline_math(text: str) -> str:
    """
    用于表格标量/文本单元格渲染时的轻量公式解析。
    将文本进行 html.escape 转义，同时将 LaTeX 公式解析为 KaTeX 容器。
    """
    if not text or ("$" not in text and "\\(" not in text and "\\[" not in text):
        return html.escape(text) if text else ""

    # 先提取块级与行内公式
    token_nonce = "TBL"
    protected, items = protect_math_in_markdown(text, token_nonce)
    # 转义普通文本
    escaped = html.escape(protected)
    # 还原公式
    return restore_math_in_html(escaped, items)


def render_math_in_html(html_str: str) -> str:
    """
    对已完成安全净化的 HTML 片段进行公式解析，保留已有的安全 HTML 标签不被二次转义。
    """
    if not html_str or ("$" not in html_str and "\\(" not in html_str and "\\[" not in html_str):
        return html_str

    token_nonce = "HTMLMATH"
    protected, items = protect_math_in_markdown(html_str, token_nonce)
    return restore_math_in_html(protected, items)
