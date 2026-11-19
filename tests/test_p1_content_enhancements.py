"""
P1 增强：Mermaid 流程图、LaTeX 数学公式、复杂表格加固与 SEO 路由测试。
"""

import json
import os
import unittest
from uuid import uuid4

from django.test import TestCase, RequestFactory
from django.core.exceptions import ValidationError
from wagtail.blocks import StructBlockValidationError


class P1ContentEnhancementsTests(TestCase):

    def setUp(self):
        from content_blocks.blocks.markdown import CustomMarkdownBlock
        from content_blocks.blocks.mermaid import CustomMermaidBlock
        from content_blocks.blocks.table import CustomTableBlock
        self.md_block = CustomMarkdownBlock()
        self.mermaid_block = CustomMermaidBlock()
        self.table_block = CustomTableBlock()
        self.factory = RequestFactory()

    def test_mermaid_unified_dom_and_chinese_flowchart(self):
        """验证独立 Mermaid 块与 Markdown 围栏输出统一的 DOM 与工具栏，支持中文节点与 CRLF"""
        code = (
            'flowchart LR\r\n'
            '  A["JS"] --> B["Mermaid"]\r\n'
            '  B --> C["Markdown 原生"]\r\n'
            '  C --> D["Markdown 定制"]'
        )

        # 1. 独立 CustomMermaidBlock
        res_block = self.mermaid_block.render_clean({"title": "技术演进图", "diagram_code": code, "theme": "dark"})
        self.assertIn('class="cb-mermaid mermaid-block-wrapper"', res_block.html)
        self.assertIn('data-cb-mermaid', res_block.html)
        self.assertIn('data-theme="dark"', res_block.html)
        self.assertIn('技术演进图', res_block.html)
        self.assertIn('Mermaid 11.17', res_block.html)
        self.assertIn('cb-btn-zoom-in', res_block.html)
        self.assertIn('cb-btn-fullscreen', res_block.html)
        self.assertIn('cb-btn-export', res_block.html)
        self.assertIn('Markdown 原生', res_block.html)

        # 2. CustomMarkdownBlock 围栏模式
        md_input = f'# 架构说明\r\n\r\n```mermaid\r\n{code}\r\n```\r\n'
        res_md = self.md_block.render_clean(md_input)
        self.assertIn('class="cb-mermaid mermaid-block-wrapper"', res_md.html)
        self.assertIn('data-cb-mermaid', res_md.html)
        self.assertIn('cb-btn-copy', res_md.html)
        self.assertIn('Markdown 定制', res_md.html)
        # 确保外层没有被非法包裹在 <p> 内
        self.assertNotIn('<p><div class="cb-mermaid', res_md.html)

    def test_mermaid_in_normal_code_fence_not_rendered(self):
        """验证普通代码块（如 python）内包含的代码示例绝不误渲染为流程图"""
        md_input = (
            '```python\n'
            '# 示例代码\n'
            'def demo():\n'
            '    return "```mermaid\\ngraph TD\\n```"\n'
            '```'
        )
        res = self.md_block.render_clean(md_input)
        self.assertNotIn('data-cb-mermaid', res.html)
        self.assertIn('<pre><code', res.html)

    def test_latex_math_formulas_protection(self):
        """验证 LaTeX 公式中的 _、换行与特殊字符完好保留，不被转义为 <em> 或 <br>"""
        md_input = (
            '正文行内公式 $a_1 + b_2 = c_3$ 且下划线不破坏。\n\n'
            '独立块公式：\n\n'
            '$$\n'
            '\\sum_{i=1}^{n} x_i^2 = \\alpha_k\n'
            '$$\n\n'
            '方括号块公式：\n\n'
            '\\[\n'
            'E = mc^2\n'
            '\\]\n\n'
            '圆括号行内公式 \\(x \\in \\mathbb{R}\\) 正常。'
        )
        res = self.md_block.render_clean(md_input)

        # 验证行内公式未被转义为 <em>
        self.assertNotIn('<em>', res.html)
        self.assertIn('class="cb-math cb-math-inline"', res.html)
        self.assertIn('data-tex="a_1 + b_2 = c_3"', res.html)

        # 验证块级公式保持完整
        self.assertIn('class="cb-math cb-math-block"', res.html)
        self.assertIn('\\sum_{i=1}^{n} x_i^2 = \\alpha_k', res.html)
        self.assertIn('E = mc^2', res.html)
        self.assertIn('x \\in \\mathbb{R}', res.html)

    def test_currency_and_code_spans_not_treated_as_math(self):
        """验证货币 $100、价格范围与代码反引号 `$not_math$` 不被误伤"""
        md_input = (
            '该服务价格为 $100，特惠区间为 $100 - $200，或者 \\$50。\n\n'
            '代码中的文本：`$var_name$` 与 `$price$` 保持代码属性。'
        )
        res = self.md_block.render_clean(md_input)
        # 货币应保留字面符号，不包装为 .cb-math
        self.assertIn('$100', res.html)
        self.assertIn('$100 - $200', res.html)
        # 代码内的 $ 保持为 <code> 内文本
        self.assertIn('<code>$var_name$</code>', res.html)
        self.assertNotIn('data-tex="var_name"', res.html)

    def test_math_in_markdown_table_cells_preserves_columns(self):
        """验证 Markdown 表格单元格内的公式（含绝对值管道符 |）不损坏列结构"""
        md_input = (
            '| 序号 | 数学表达式 | 说明 |\n'
            '|---|---|---|\n'
            '| 1 | $|x| + |y| \\le 1$ | 绝对值不等式 |\n'
            '| 2 | $\\frac{a}{b}$ | 分式 |\n'
        )
        res = self.md_block.render_clean(md_input)
        self.assertIn('<table>', res.html)
        self.assertIn('<th>序号</th>', res.html)
        self.assertIn('<td>1</td>', res.html)
        self.assertIn('绝对值不等式', res.html)
        self.assertIn('class="cb-math cb-math-inline"', res.html)
        self.assertIn('|x| + |y| \\le 1', res.html)

    def test_custom_table_clean_validation_contract(self):
        """验证 CustomTableBlock.clean() 在 Wagtail 8.1 格式损坏时抛出 StructBlockValidationError"""
        bad_value = {"table_data": "{corrupted json}"}
        with self.assertRaises(StructBlockValidationError) as ctx:
            self.table_block.clean(bad_value)
        self.assertIn("table_data", ctx.exception.block_errors)

        good_value = {
            "caption": "有效表格",
            "has_header_row": True,
            "has_header_col": False,
            "alignment": "center",
            "table_data": json.dumps({"rows": [["A", "B"], ["1", "2"]]}),
        }
        cleaned = self.table_block.clean(good_value)
        self.assertEqual(cleaned["caption"], "有效表格")

    def test_custom_table_math_rendering(self):
        """验证复杂表格中标量单元格与 dict 富文本单元格均支持 LaTeX 公式渲染"""
        table_data = {
            "rows": [
                ["公式名称", "标量公式", "富文本单元格公式"],
                [
                    "质能方程",
                    "$E = mc^2$",
                    {"value": '<strong>加粗说明</strong>: $\\lambda = \\frac{h}{p}$'}
                ]
            ]
        }
        value = {"table_data": table_data}
        res = self.table_block.render_clean(value)
        self.assertIn('class="cb-math cb-math-inline"', res.html)
        self.assertIn('E = mc^2', res.html)
        self.assertIn('<strong>加粗说明</strong>', res.html)
        self.assertIn('\\lambda = \\frac{h}{p}', res.html)

    def test_media_reference_clean_extraction_ignores_code_fences(self):
        """验证媒体引用提取器自动忽略代码块内的虚拟 UUID (MEDIA-REF-01)"""
        valid_img_id = str(uuid4())
        dummy_code_id = str(uuid4())

        md_input = f"""
正文配图：![实图](asset://image/{valid_img_id})

```python
# 教程示例（不应计入引用）
img_path = "asset://image/{dummy_code_id}"
```
"""
        refs = self.md_block.extract_media_refs(md_input, path="body.0")
        asset_ids = [r.asset_id for r in refs]
        self.assertIn(valid_img_id, asset_ids)
        self.assertNotIn(dummy_code_id, asset_ids)

    def test_seo_sitemap_and_robots(self):
        """验证 /sitemap.xml 过滤回收站页面且 /robots.txt 阻断管理与私密网关"""
        from discovery.seo import robots_txt_view, sitemap_view

        # 1. 验证 robots.txt
        req_robots = self.factory.get("/robots.txt")
        resp_robots = robots_txt_view(req_robots)
        self.assertEqual(resp_robots.status_code, 200)
        content_robots = resp_robots.content.decode("utf-8")
        self.assertIn("Disallow: /admin/", content_robots)
        self.assertIn("Disallow: /media-gateway/", content_robots)
        self.assertIn("Disallow: /avatar-gateway/", content_robots)
        self.assertIn("Disallow: /documents/", content_robots)
        self.assertIn("Sitemap: http://testserver/sitemap.xml", content_robots)

        # 2. 验证 sitemap.xml
        req_sitemap = self.factory.get("/sitemap.xml")
        resp_sitemap = sitemap_view(req_sitemap)
        if hasattr(resp_sitemap, "render"):
            resp_sitemap.render()
        self.assertEqual(resp_sitemap.status_code, 200)
        self.assertEqual(resp_sitemap["Content-Type"], "application/xml")
        content_sitemap = resp_sitemap.content.decode("utf-8")
        self.assertIn("<urlset", content_sitemap)
        self.assertIn("<loc>http://", content_sitemap)

    def test_custom_table_validation_rejects_negative_span_and_invalid_rows(self):
        """验证复杂表格校验能拦截负数跨度与损坏的 rows 结构"""
        bad_rows = {"table_data": json.dumps({"rows": "not a list"})}
        val_bad_rows = self.table_block.validate_value(bad_rows)
        self.assertFalse(val_bad_rows.is_valid)
        self.assertIn("rows 属性必须为数组列表", val_bad_rows.errors[0])

        bad_span = {"table_data": json.dumps({"rows": [[{"value": "A", "rowspan": -1}]]})}
        val_bad_span = self.table_block.validate_value(bad_span)
        self.assertFalse(val_bad_span.is_valid)
        self.assertIn("rowspan 必须大于等于 1", val_bad_span.errors[0])

    def test_mermaid_fence_with_backslashes_rendering(self):
        """验证 Mermaid 围栏中包含反斜杠字符（如 LaTeX 或特殊转义）时不发生正则模板解析异常"""
        md_with_bs = "```mermaid\ngraph TD\nA[\\text{公式}] --> B[\\sum]\n```"
        res = self.md_block.render_clean(md_with_bs)
        self.assertIn('class="cb-mermaid mermaid-block-wrapper"', res.html)
        self.assertIn('data-cb-mermaid-source', res.html)
        self.assertIn('\\text{公式}', res.html)

    def test_vendor_lock_and_sync_check(self):
        """验证 vendor.lock.json 校验逻辑与本地 vendor 资产完整性"""
        import importlib.util
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        script_path = repo_root / "scripts" / "sync_vendor_assets.py"
        spec = importlib.util.spec_from_file_location("sync_vendor_assets", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        lock = mod.load_lock(mod.DEFAULT_LOCK_FILE)
        self.assertIn("mermaid", lock["packages"])
        self.assertIn("katex", lock["packages"])
        self.assertIn("highlightjs", lock["packages"])
        self.assertEqual(mod.cmd_check(mod.DEFAULT_LOCK_FILE), 0)

    def test_unified_code_highlighting_contracts(self):
        """验证 Markdown 代码围栏、CustomCodeBlock 与 CustomStepBlock 均输出一致的 language-* 契约"""
        from content_blocks.blocks.code import CustomCodeBlock
        from content_blocks.blocks.structured import CustomStepBlock

        md_res = self.md_block.render_clean("```python\ndef hello():\n    return 42\n```")
        self.assertIn('<pre><code class="language-python">', md_res.html)

        code_res = CustomCodeBlock().render_clean({
            "title": "demo.sql",
            "language": "sql",
            "code": "SELECT 1;",
            "mode": "display",
        })
        self.assertIn('<code class="language-sql">', code_res.html)

        step_res = CustomStepBlock().render_clean({
            "step_number": 1,
            "title": "运行迁移",
            "content": "执行命令",
            "code_snippet": "python manage.py check",
            "code_language": "bash",
        })
        self.assertIn('<pre class="code-content"><code class="language-bash">', step_res.html)

    def test_table_multi_row_header_rowspan_and_cell_code_math(self):
        """验证复杂表格：多行表头不跨 thead/tbody、逻辑列 has_header_col、越界/重叠拦截与单元格代码/公式渲染"""
        table_data = {
            "rows": [
                [
                    {"value": "层级", "rowspan": 2, "header": True},
                    {"value": "指标", "colspan": 2, "header": True},
                ],
                [
                    {"value": "名称", "header": True},
                    {"value": "公式与代码", "header": True},
                ],
                [
                    {"value": "主库", "rowspan": 2},
                    "P99 耗时",
                    "延迟 $t \\le 2$ ms，执行 `SELECT 1`",
                ],
                [
                    "SQL 示例",
                    "```sql\nSELECT id FROM blog_blogpage;\n```",
                ],
            ]
        }
        res = self.table_block.render_clean({
            "caption": "跨行跨列+代码公式测试表",
            "has_header_row": True,
            "has_header_col": True,
            "alignment": "center",
            "table_data": table_data,
        })
        # 1. 第0、1行都必须留在 <thead> 内，避免 rowspan=2 跨 rowgroup 被浏览器截断
        thead_part = res.html.split("</thead>")[0]
        self.assertIn("层级", thead_part)
        self.assertIn("指标", thead_part)
        self.assertIn("公式与代码", thead_part)
        # 2. tbody 第2行被上方 rowspan=2 占掉第0列，因此"SQL 示例"处于逻辑第1列，不能错误变成 <th>
        tbody_part = res.html.split("<tbody>")[1]
        self.assertIn('<th rowspan="2" scope="rowgroup">主库</th>', tbody_part)
        self.assertIn("<td>SQL 示例</td>", tbody_part)
        # 3. 单元格内的 LaTeX 公式（含 data-cb-math 属性没被外层剥掉）、行内代码与代码块均正确渲染
        self.assertIn('data-cb-math="inline"', res.html)
        self.assertIn('data-tex="t \\le 2"', res.html)
        self.assertIn('<code class="cb-inline-code">SELECT 1</code>', res.html)
        self.assertIn('<code class="language-sql">SELECT id FROM blog_blogpage;</code>', res.html)
        # 4. 包含左右分栏源码对照骨架
        self.assertIn('data-cb-table', res.html)
        self.assertIn('data-cb-table-source', res.html)

        # 5. 校验重叠与越界跨度被准确拒绝
        overlap_data = {
            "rows": [
                [{"value": "A", "rowspan": 2}, {"value": "B"}],
                [{"value": "C", "colspan": 2}],
            ]
        }
        val_res = self.table_block.validate_value({"table_data": json.dumps(overlap_data)})
        self.assertFalse(val_res.is_valid)
        self.assertTrue(any("逻辑列宽" in err or "超出表格总行数" in err or "重叠" in err for err in val_res.errors))

        oob_data = {
            "rows": [
                [{"value": "A", "rowspan": 3}, {"value": "B"}],
                [{"value": "C"}],
            ]
        }
        val_oob = self.table_block.validate_value({"table_data": json.dumps(oob_data)})
        self.assertFalse(val_oob.is_valid)
        self.assertTrue(any("超出表格总行数" in err for err in val_oob.errors))

    def test_public_block_assets_templates_include_all_modules(self):
        """验证公共静态片段已挂载代码高亮、表格分栏、Embed、图片灯箱、媒体与结构化卡片资产"""
        from django.template.loader import render_to_string

        css_html = render_to_string("content_blocks/includes/_block_assets_css.html")
        js_html = render_to_string("content_blocks/includes/_block_assets_js.html")

        for needle in [
            "highlightjs/styles/github-dark.min.css",
            "content_blocks/css/code-highlighting.css",
            "content_blocks/css/embed-block.css",
            "content_blocks/css/content-images.css",
            "content_blocks/css/media-blocks.css",
            "content_blocks/css/structured-blocks.css",
        ]:
            self.assertIn(needle, css_html)

        for needle in [
            "content_blocks/js/code-highlighting.js",
            "content_blocks/js/table-block.js",
            "content_blocks/js/embed-render.js",
            "content_blocks/js/content-images.js",
        ]:
            self.assertIn(needle, js_html)
