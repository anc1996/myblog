"""
S4 复杂表格块 (CustomTableBlock) 单元测试。

测试覆盖：
1. 表格基础渲染：caption、thead (th)、tbody (td) 与对齐样式；
2. 单元格跨行跨列合并：rowspan 与 colspan 属性正确保留并在 HTML 中安全渲染；
3. 单元格富内容 XSS 安全净化：恶意脚本被剥离，保留合法加粗、合并属性与链接；
4. 全文提取：提取全部单元格文本与表格标题供搜索索引；
5. 媒体引用提取：准确提取单元格中嵌入的图片与文档资源 ID (MEDIA-REF-01)；
6. 移动端横向滚动自适应：外层包裹 overflow-x: auto 响应式容器；
7. 格式校验：非法 JSON 或损坏数据安全报错。
"""

import json
import os
from pathlib import Path
import unittest
from uuid import uuid4

PROJECT_DIR = Path(__file__).resolve().parents[1]


class S4TableBlockTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
        import django
        django.setup()

    def test_basic_table_rendering_and_mobile_wrapper(self):
        from content_blocks.blocks.table import CustomTableBlock

        block = CustomTableBlock()
        table_data = {
            "rows": [
                ["季度", "收入", "利润"],
                ["Q1", "¥100,000", "¥30,000"],
                ["Q2", "¥120,000", "¥35,000"],
            ]
        }
        value = {
            "caption": "2026年财务季度报表",
            "has_header_row": True,
            "has_header_col": False,
            "alignment": "center",
            "table_data": json.dumps(table_data),
        }
        res = block.render_clean(value)
        self.assertTrue(res.is_safe)
        # 验证包含响应式横向滚动外层容器
        self.assertIn('class="responsive-table-wrapper"', res.html)
        self.assertIn('class="custom-table-block table-align-center"', res.html)
        # 验证标题与居中对齐样式
        self.assertIn("2026年财务季度报表", res.html)
        # 验证表头与表体正确分拆
        self.assertIn("<thead><tr><th>季度</th><th>收入</th><th>利润</th></tr></thead>", res.html)
        self.assertIn("<tbody>", res.html)
        self.assertIn("<td>¥100,000</td>", res.html)

    def test_cell_merging_rowspan_and_colspan(self):
        from content_blocks.blocks.table import CustomTableBlock

        block = CustomTableBlock()
        table_data = {
            "rows": [
                [
                    {"value": "综合大类", "rowspan": 2, "colspan": 1, "header": True},
                    {"value": "子项目", "rowspan": 1, "colspan": 2, "header": True, "align": "center"},
                ],
                [
                    {"value": "子项A", "rowspan": 1, "colspan": 1},
                    {"value": "子项B", "rowspan": 1, "colspan": 1},
                ],
                [
                    {"value": "汇总数据", "rowspan": 1, "colspan": 3, "align": "center"},
                ],
            ]
        }
        value = {
            "caption": "多层合并表格",
            "has_header_row": True,
            "table_data": table_data,
        }
        res = block.render_clean(value)
        # 验证跨行 rowspan="2" 与跨列 colspan="2" / colspan="3"
        self.assertIn('rowspan="2"', res.html)
        self.assertIn('colspan="2"', res.html)
        self.assertIn('colspan="3"', res.html)
        self.assertIn('align="center"', res.html)
        self.assertIn("综合大类", res.html)
        self.assertIn("汇总数据", res.html)

    def test_cell_xss_sanitization_preserves_merge_attributes(self):
        from content_blocks.blocks.table import CustomTableBlock

        block = CustomTableBlock()
        table_data = {
            "rows": [
                [
                    {
                        "value": '<strong>安全标题</strong><script>alert("XSS")</script>',
                        "colspan": 2,
                        "header": True,
                    }
                ],
                [
                    {
                        "value": '<a href="javascript:alert(1)">恶意链接</a><a href="https://example.com">正常链接</a>',
                        "rowspan": 1,
                    },
                    {"value": "普通单元格"},
                ],
            ]
        }
        value = {"table_data": table_data}
        res = block.render_clean(value)

        # 验证：1. 恶意 script 标签被彻底剥离
        self.assertNotIn("<script>", res.html)
        self.assertNotIn('alert("XSS")', res.html)
        # 2. 恶意 javascript: 协议链接被彻底清空
        self.assertNotIn("javascript:", res.html)
        # 3. 合法的 strong 标签与 colspan 属性完好保留！
        self.assertIn("<strong>安全标题</strong>", res.html)
        self.assertIn('colspan="2"', res.html)
        self.assertIn('href="https://example.com"', res.html)

    def test_table_text_extraction(self):
        from content_blocks.blocks.table import CustomTableBlock

        block = CustomTableBlock()
        table_data = {
            "rows": [
                [{"value": "项目名称"}, {"value": "预算金额"}],
                [{"value": "Wagtail 开发"}, {"value": "¥50,000"}],
            ]
        }
        value = {"caption": "项目预算", "table_data": table_data}
        extracted = block.extract_text(value)
        self.assertIn("项目预算", extracted.plain_text)
        self.assertIn("Wagtail 开发", extracted.plain_text)
        self.assertIn("¥50,000", extracted.plain_text)

    def test_table_media_reference_extraction(self):
        from content_blocks.blocks.table import CustomTableBlock

        block = CustomTableBlock()
        img_uuid = str(uuid4())
        doc_uuid = str(uuid4())
        table_data = {
            "rows": [
                [{"value": f'图表单元格 <img data-asset-id="{img_uuid}" />'}],
                [{"value": f'文档链接 [下载](asset://document/{doc_uuid})'}],
            ]
        }
        value = {"table_data": table_data}
        refs = block.extract_media_refs(value, path="body.table_1")
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0].asset_id, img_uuid)
        self.assertEqual(refs[0].asset_type, "image")
        self.assertEqual(refs[0].block_path, "body.table_1.r0c0")
        self.assertEqual(refs[1].asset_id, doc_uuid)
        self.assertEqual(refs[1].asset_type, "document")
        self.assertEqual(refs[1].block_path, "body.table_1.r1c0")

    def test_table_validation_error_on_corrupted_json(self):
        from content_blocks.blocks.table import CustomTableBlock

        block = CustomTableBlock()
        value_bad_json = {"table_data": "{bad json: invalid}"}
        val_bad = block.validate_value(value_bad_json)
        self.assertFalse(val_bad.is_valid)
        self.assertTrue(len(val_bad.errors) > 0)

        value_ok = {"table_data": '{"rows": [["A", "B"]]}'}
        val_ok = block.validate_value(value_ok)
        self.assertTrue(val_ok.is_valid)


if __name__ == "__main__":
    unittest.main()
