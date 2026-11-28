"""
正文引用块智能折叠（方案 A + 样式 2 渐隐预览）与原生 <details> 样式统一测试。
"""

from pathlib import Path
import re

from django.test import SimpleTestCase
from wagtail.rich_text import RichText

from content_blocks.blocks.markdown import CustomMarkdownBlock
from content_blocks.blocks.rich_text import CustomRichTextBlock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


class BlockquoteCollapseTests(SimpleTestCase):
    def test_block_assets_register_blockquote_collapse_script_and_css(self):
        css_tpl = (
            PROJECT_ROOT / "myblog/apps/content_blocks/templates/content_blocks/includes/_block_assets_css.html"
        ).read_text(encoding="utf-8")
        js_tpl = (
            PROJECT_ROOT / "myblog/apps/content_blocks/templates/content_blocks/includes/_block_assets_js.html"
        ).read_text(encoding="utf-8")

        self.assertIn("content_blocks/css/structured-blocks.css", css_tpl)
        self.assertIn("content_blocks/js/blockquote-collapse.js", js_tpl)

    def test_structured_blocks_css_has_collapse_styles_and_zero_raw_hex(self):
        css_text = (
            PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/css/structured-blocks.css"
        ).read_text(encoding="utf-8")
        cleaned = re.sub(r"/\*.*?\*/", "", css_text, flags=re.S)
        self.assertEqual(HEX_RE.findall(cleaned), [])
        for needle in (
            ".cb-bq-collapsible",
            ".cb-bq-header",
            ".cb-bq-collapsible.is-collapsed .cb-bq-viewport",
            "display: none;",
            ".cb-bq-toggle-pill",
            ".cb-bq-batch-bar",
            "@media print",
        ):
            self.assertIn(needle, css_text)

    def test_blockquote_collapse_js_implements_scheme_a_and_style_2_contract(self):
        js_text = (
            PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/js/blockquote-collapse.js"
        ).read_text(encoding="utf-8")
        for needle in (
            ".detail-body-section blockquote",
            "COLLAPSE_HEIGHT_PX = 180",
            "思路|解析|答案",
            "is-collapsed",
            "展开引用",
            "收起引用",
            "cb-bq-header",
            "cb-bq-batch-bar",
            "expandForHashTarget",
        ):
            self.assertIn(needle, js_text)

    def test_richtext_and_markdown_preserve_blockquote_and_details_markup(self):
        rt_res = CustomRichTextBlock().render_clean(RichText("<blockquote><p>思路：一步步推导结论。</p></blockquote>"))
        self.assertIn("<blockquote>", rt_res.html)
        self.assertIn("思路：一步步推导结论。", rt_res.html)

        md_src = (
            "> 思路（等价推出）：\n"
            "> 第一行条件\n"
            "> 第二行逆否命题\n\n"
            "<details><summary>点击展开手动补充</summary><p>扩展内容</p></details>"
        )
        md_res = CustomMarkdownBlock().render_clean(md_src)
        self.assertIn("<blockquote>", md_res.html)
        self.assertIn("<details>", md_res.html)
        self.assertIn("<summary>点击展开手动补充</summary>", md_res.html)
