"""
S3 全站公共基础积木单元测试。

测试覆盖：
1. 富文本块 (CustomRichTextBlock)：安全净化、纯文本提取、媒体引用提取；
2. Markdown 块 (CustomMarkdownBlock)：表格渲染、Mermaid 围栏转换、XSS 净化、文本与资源提取；
3. 代码块 (CustomCodeBlock)：展示模式语法高亮与转义、运行模式界面约定（不执行不受信代码，严格遵守 AC-13）；
4. Mermaid 块 (CustomMermaidBlock)：主题与安全容器渲染、文本提取；
5. 媒体块 (Image/Document/Audio/Video)：受控网关地址渲染、稳定资源标识 (public_id) 引用提取；
6. 嵌入块 (CustomEmbedBlock)：白名单域名许可、SSRF 与危险地址强力阻断 (AC-14)；
7. get_common_stream_blocks() 组合装配清单。
"""

import os
from pathlib import Path
import unittest
from uuid import uuid4

PROJECT_DIR = Path(__file__).resolve().parents[1]


class S3BlocksTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
        import django
        django.setup()

    def test_rich_text_block_sanitization_and_extraction(self):
        from content_blocks.blocks.rich_text import CustomRichTextBlock

        block = CustomRichTextBlock()
        # 1. 验证恶意脚本被 nh3 净化剥离
        malicious_input = '<p>正常段落</p><script>alert("XSS")</script><a href="javascript:alert(1)">恶意链接</a>'
        res = block.render_clean(malicious_input)
        self.assertTrue(res.is_safe)
        self.assertNotIn("<script>", res.html)
        self.assertNotIn("javascript:", res.html)
        self.assertIn("<p>正常段落</p>", res.html)

        # 2. 验证纯文本提取（剔除 HTML 标签）
        extracted = block.extract_text("<p>第一段</p><p>第二段文字</p>")
        self.assertIn("第一段 第二段文字", extracted.plain_text)
        self.assertNotIn("<p>", extracted.plain_text)

        # 3. 验证媒体引用提取
        img_uuid = str(uuid4())
        doc_uuid = str(uuid4())
        html_with_media = f'<p>测试</p><img data-asset-id="{img_uuid}" /><a linktype="document" id="{doc_uuid}">下载</a>'
        refs = block.extract_media_refs(html_with_media, path="intro")
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0].asset_id, img_uuid)
        self.assertEqual(refs[0].asset_type, "image")
        self.assertEqual(refs[1].asset_id, doc_uuid)
        self.assertEqual(refs[1].asset_type, "document")

    def test_markdown_block_rendering_and_mermaid_fences(self):
        from content_blocks.blocks.markdown import CustomMarkdownBlock

        block = CustomMarkdownBlock()
        # 1. 验证 Markdown 格式转换（标题、列表、表格）
        md_input = """# 标题\n\n- 列表项1\n- 列表项2\n\n| A | B |\n|---|---|\n| 1 | 2 |"""
        res = block.render_clean(md_input)
        self.assertIn("<h1>标题</h1>", res.html)
        self.assertIn("<table>", res.html)
        self.assertIn("<li>列表项1</li>", res.html)

        # 2. 验证 fenced mermaid 流程图围栏被转换为安全容器
        md_with_mermaid = """```mermaid\ngraph TD;\nA-->B;\n```"""
        res_mermaid = block.render_clean(md_with_mermaid)
        self.assertIn('class="mermaid-diagram"', res_mermaid.html)
        self.assertIn('data-mermaid="graph TD;', res_mermaid.html)

        # 3. 验证 Markdown 内嵌恶意 XSS 被拦截
        md_xss = """[点击测试](javascript:void(0))\n\n<script>alert(2)</script>"""
        res_xss = block.render_clean(md_xss)
        self.assertNotIn("<script>", res_xss.html)
        self.assertNotIn("javascript:", res_xss.html)

        # 4. 验证 Markdown 中的媒体引用提取
        img_uuid = str(uuid4())
        md_with_ref = f"正文段落 ![插图](asset://image/{img_uuid})"
        refs = block.extract_media_refs(md_with_ref, path="body.1")
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].asset_id, img_uuid)
        self.assertEqual(refs[0].asset_type, "image")

    def test_code_block_modes_and_ac13_compliance(self):
        from content_blocks.blocks.code import CustomCodeBlock

        block = CustomCodeBlock()

        # 1. 展示模式：转义与语法高亮
        display_value = {
            "title": "script.py",
            "language": "python",
            "code": 'print("<script>alert(1)</script>")',
            "mode": "display",
        }
        res_display = block.render_clean(display_value)
        self.assertIn("script.py", res_display.html)
        self.assertIn("code-block-display", res_display.html)
        # 确保代码中的 HTML 字符被转义，绝不在浏览器中被执行
        self.assertNotIn("<script>alert(1)</script>", res_display.html)
        self.assertIn("&lt;script&gt;", res_display.html)

        # 2. 运行模式：严格遵守 AC-13，仅展示原型界面与提示，绝不执行未受信代码
        runner_value = {
            "title": "demo.py",
            "language": "python",
            "code": "import os; os.system('ls')",
            "mode": "runner",
        }
        res_runner = block.render_clean(runner_value)
        self.assertIn("code-block-runner", res_runner.html)
        self.assertIn("代码在线执行沙箱将在后续阶段开放", res_runner.html)
        self.assertIn("disabled", res_runner.html)

        # 3. 文本提取
        extracted = block.extract_text(display_value)
        self.assertIn("script.py", extracted.plain_text)
        self.assertIn("print", extracted.plain_text)

    def test_mermaid_block_rendering_and_theme(self):
        from content_blocks.blocks.mermaid import CustomMermaidBlock

        block = CustomMermaidBlock()
        value = {
            "title": "系统架构图",
            "diagram_code": "graph LR; User-->App;",
            "theme": "forest",
        }
        res = block.render_clean(value)
        self.assertIn("系统架构图", res.html)
        self.assertIn('data-theme="forest"', res.html)
        self.assertIn("mermaid-diagram", res.html)

    def test_media_blocks_rendering_and_reference_extraction(self):
        from content_blocks.blocks.media import (
            CustomImageBlock,
            CustomDocumentBlock,
            CustomAudioBlock,
            CustomVideoBlock,
        )
        from media_library.models import CustomImage, CustomDocument, Audio, Video

        # 1. 模拟 CustomImage 实例
        img_uuid = uuid4()
        dummy_image = CustomImage(id=10, public_id=img_uuid, title="风景照")
        img_block = CustomImageBlock()
        img_val = {"image": dummy_image, "caption": "日出东方", "alt_text": "日出风景", "display_format": "full"}
        img_res = img_block.render_clean(img_val)
        self.assertIn(f"/media-gateway/image/{img_uuid}/", img_res.html)
        self.assertIn("日出东方", img_res.html)

        img_refs = img_block.extract_media_refs(img_val, path="body.2")
        self.assertEqual(len(img_refs), 1)
        self.assertEqual(img_refs[0].asset_id, str(img_uuid))
        self.assertEqual(img_refs[0].asset_type, "image")

        # 2. 模拟 CustomDocument 实例
        doc_uuid = uuid4()
        dummy_doc = CustomDocument(id=11, public_id=doc_uuid, title="Django手册.pdf")
        doc_block = CustomDocumentBlock()
        doc_val = {"document": dummy_doc, "title_override": "点击下载手册", "description": "学习资料"}
        doc_res = doc_block.render_clean(doc_val)
        self.assertIn(f"/media-gateway/document/{doc_uuid}/", doc_res.html)
        self.assertIn("点击下载手册", doc_res.html)

        doc_refs = doc_block.extract_media_refs(doc_val, path="body.3")
        self.assertEqual(len(doc_refs), 1)
        self.assertEqual(doc_refs[0].asset_id, str(doc_uuid))
        self.assertEqual(doc_refs[0].asset_type, "document")

        # 3. 模拟 Audio 与 Video 实例
        audio_uuid = uuid4()
        dummy_audio = Audio(id=12, public_id=audio_uuid, title="轻音乐")
        audio_block = CustomAudioBlock()
        audio_val = {"audio": dummy_audio}
        audio_res = audio_block.render_clean(audio_val)
        self.assertIn(f"/media-gateway/audio/{audio_uuid}/", audio_res.html)
        audio_refs = audio_block.extract_media_refs(audio_val, path="body.4")
        self.assertEqual(audio_refs[0].asset_id, str(audio_uuid))
        self.assertEqual(audio_refs[0].asset_type, "audio")

        video_uuid = uuid4()
        dummy_video = Video(id=13, public_id=video_uuid, title="教程视频", poster=dummy_image)
        video_block = CustomVideoBlock()
        video_val = {"video": dummy_video}
        video_res = video_block.render_clean(video_val)
        self.assertIn(f"/media-gateway/video/{video_uuid}/", video_res.html)
        self.assertIn(f'poster="/media-gateway/image/{img_uuid}/"', video_res.html)
        video_refs = video_block.extract_media_refs(video_val, path="body.5")
        # 包含视频本身与关联的海报图
        self.assertEqual(len(video_refs), 2)
        self.assertEqual(video_refs[0].asset_type, "video")
        self.assertEqual(video_refs[1].asset_type, "image")

    def test_embed_block_allowlist_and_ssrf_blocking(self):
        from content_blocks.blocks.embed import CustomEmbedBlock, is_safe_embed_url
        from django.core.exceptions import ValidationError

        block = CustomEmbedBlock()

        # 1. 白名单内合法地址：允许
        safe_urls = [
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://vimeo.com/76979871",
            "https://codepen.io/pen/12345",
        ]
        for u in safe_urls:
            is_safe, reason = is_safe_embed_url(u)
            self.assertTrue(is_safe, f"{u} should be safe, reason: {reason}")

        # 2. SSRF 恶意地址与未受信任域名：必须坚决阻断 (AC-14)
        dangerous_urls = [
            "http://127.0.0.1:8000/internal-api",
            "http://localhost:5432/",
            "http://192.168.1.1/router-admin",
            "http://10.0.0.1/metadata",
            "http://169.254.169.254/latest/meta-data/",
            "javascript:alert(1)",
            "ftp://ftp.example.com/file",
            "https://evil-hacker-site.com/malicious-iframe",
        ]
        for u in dangerous_urls:
            is_safe, _ = is_safe_embed_url(u)
            self.assertFalse(is_safe, f"{u} must be blocked!")
            with self.assertRaises(ValidationError):
                block.clean(u)

    def test_get_common_stream_blocks(self):
        from content_blocks.blocks import get_common_stream_blocks

        blocks_list = get_common_stream_blocks()
        block_keys = [b[0] for b in blocks_list]
        expected_keys = ["rich_text", "markdown", "code", "mermaid", "image", "document", "audio", "video", "embed"]
        self.assertEqual(block_keys, expected_keys)


if __name__ == "__main__":
    unittest.main()
