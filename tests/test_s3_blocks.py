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
        import warnings
        from content_blocks.blocks.rich_text import CustomRichTextBlock
        from wagtail.admin.rich_text.editors.draftail import DraftailRichTextArea
        from wagtail.rich_text import RichText

        block = CustomRichTextBlock()
        # 0. 验证完整工具栏特性与 underline 已注册，挂载 Draftail 不产生未知特性告警
        for feat in (
            "bold", "italic", "code", "superscript", "subscript", "strikethrough",
            "underline", "h1", "h2", "h3", "h4", "h5", "h6",
            "ol", "ul", "blockquote", "hr", "link", "document-link", "image", "embed",
        ):
            self.assertIn(feat, block.features)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            DraftailRichTextArea(features=block.features)
        unknown_warns = [w for w in caught if "unknown feature" in str(w.message).lower()]
        self.assertEqual(unknown_warns, [])

        # 1. 验证恶意脚本被 nh3 净化剥离
        malicious_input = '<p>正常段落</p><script>alert("XSS")</script><a href="javascript:alert(1)">恶意链接</a>'
        res = block.render_clean(malicious_input)
        self.assertTrue(res.is_safe)
        self.assertNotIn("<script>", res.html)
        self.assertNotIn("javascript:", res.html)
        self.assertIn("<p>正常段落</p>", res.html)

        # 1b. 验证下划线/删除线/上下标/引用/分隔线/行内代码与白名单 Embed / 防 SSRF
        rich_val = RichText(
            '<p><b>粗体</b><i>斜体</i><code>inline()</code><sup>1</sup><sub>2</sub>'
            '<s>删除</s><u>下划线</u></p>'
            '<blockquote>引用说明</blockquote><hr/>'
            '<embed embedtype="media" url="https://www.bilibili.com/video/BV1xx411c7mD"/>'
            '<embed embedtype="media" url="http://127.0.0.1:8000/internal"/>'
        )
        res_rich = block.render_clean(rich_val)
        self.assertIn('class="rich-text"', res_rich.html)
        self.assertIn("<u>下划线</u>", res_rich.html)
        self.assertIn("<s>删除</s>", res_rich.html)
        self.assertIn("<sup>1</sup>", res_rich.html)
        self.assertIn("<sub>2</sub>", res_rich.html)
        self.assertIn("<blockquote>引用说明</blockquote>", res_rich.html)
        self.assertIn("<hr>", res_rich.html)
        self.assertIn('data-embed-url="https://www.bilibili.com/video/BV1xx411c7mD"', res_rich.html)
        self.assertNotIn("127.0.0.1", res_rich.html)

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

        # 3b. 验证 RichText 对象传参也能从 .source 捕获 <embed embedtype="image">
        rt_media = RichText(f'<p><embed alt="图" embedtype="image" format="fullwidth" id="{img_uuid}"/></p>')
        rt_refs = block.extract_media_refs(rt_media, path="intro")
        self.assertEqual(len(rt_refs), 1)
        self.assertEqual(rt_refs[0].asset_id, img_uuid)

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

        # 2b. 验证带空格/缩进/CRLF 与中文节点标签的 fenced mermaid 围栏
        md_mermaid_variants = (
            "   ``` mermaid\r\n"
            '  flowchart LR\r\n'
            '    A["JS"] --制作了--> B["Markdown 定制"]\r\n'
            "  ```"
        )
        res_variants = block.render_clean(md_mermaid_variants)
        self.assertIn('class="cb-mermaid mermaid-block-wrapper"', res_variants.html)
        self.assertIn("flowchart LR", res_variants.html)
        self.assertIn("Markdown 定制", res_variants.html)

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
        dummy_image = CustomImage(id=10, collection_id=1, public_id=img_uuid, title="风景照")
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
        dummy_doc = CustomDocument(id=11, collection_id=1, public_id=doc_uuid, title="Django手册.pdf")
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

    def test_av2_media_blocks_player_contract_and_sanitizer(self):
        from django.template.loader import render_to_string
        from content_blocks.blocks.media import CustomAudioBlock, CustomVideoBlock
        from content_blocks.sanitizer import sanitize_html
        from media_library.models import Audio, CustomImage, Video

        audio_uuid = uuid4()
        video_uuid = uuid4()
        img_uuid = uuid4()
        dummy_audio = Audio(id=21, public_id=audio_uuid, title='Podcast <script>alert(1)</script>')
        dummy_poster = CustomImage(id=22, collection_id=1, public_id=img_uuid, title="封面")
        dummy_video = Video(id=23, public_id=video_uuid, title="演示视频", poster=dummy_poster)

        audio_res = CustomAudioBlock().render_clean(
            {"audio": dummy_audio, "title_override": 'EP1 "Pilot" <b>bold</b>'}
        )
        self.assertTrue(audio_res.is_safe)
        self.assertIn('data-cb-media-kind="audio"', audio_res.html)
        self.assertIn(f'data-asset-id="{audio_uuid}"', audio_res.html)
        self.assertIn(f'src="/media-gateway/audio/{audio_uuid}/"', audio_res.html)
        self.assertIn("<audio controls", audio_res.html)
        self.assertIn('EP1 "Pilot" &lt;b&gt;bold&lt;/b&gt;', audio_res.html)
        self.assertNotIn("<script", audio_res.html)

        video_res = CustomVideoBlock().render_clean({"video": dummy_video, "title_override": ""})
        self.assertTrue(video_res.is_safe)
        self.assertIn('data-cb-media-kind="video"', video_res.html)
        self.assertIn(f'data-asset-id="{video_uuid}"', video_res.html)
        self.assertIn(f'src="/media-gateway/video/{video_uuid}/"', video_res.html)
        self.assertIn(f'poster="/media-gateway/image/{img_uuid}/"', video_res.html)
        self.assertIn("playsinline", video_res.html)
        self.assertIn("<video controls", video_res.html)

        no_poster_video = Video(id=24, public_id=video_uuid, title="无封面视频")
        no_poster_res = CustomVideoBlock().render_clean({"video": no_poster_video})
        self.assertNotIn("poster=", no_poster_res.html)

        cleaned = sanitize_html(
            '<div data-cb-media-kind="evil"><video-player onclick="x()"></video-player></div>'
        )
        self.assertNotIn("data-cb-media-kind", cleaned)
        self.assertNotIn("video-player", cleaned)
        self.assertNotIn("onclick", cleaned)

        assets_js = render_to_string("content_blocks/includes/_block_assets_js.html")
        self.assertIn("content_blocks/js/media-player.js", assets_js)
        self.assertIn("data-videojs-audio-url=", assets_js)
        self.assertIn("data-videojs-video-url=", assets_js)
        self.assertIn("data-videojs-locale-url=", assets_js)
        self.assertIn("data-videojs-global-css-url=", assets_js)

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
            "https://v.qq.com/x/page/abc123456.html",
            "https://v.youku.com/v_show/id_XMTIzNDU2.html",
            "https://music.163.com/#/song?id=186016",
            "https://www.kugou.com/mixsong/abc123.html",
            "https://music.migu.cn/v3/music/song/60054701933",
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
            "https://y.qq.com/n/ryqq/songDetail/001Qu4I30eVFYb",
        ]
        for u in dangerous_urls:
            is_safe, _ = is_safe_embed_url(u)
            self.assertFalse(is_safe, f"{u} must be blocked!")
            with self.assertRaises(ValidationError):
                block.clean(u)

        # 3. 验证结构化 title + embed_url 渲染与酷狗/咪咕优雅卡片降级、历史纯字符串兼容
        res_bili = block.render_clean({
            "title": "B站架构演进课",
            "embed_url": "https://www.bilibili.com/video/BV1xx411c7mD",
        })
        self.assertIn('data-embed-title="B站架构演进课"', res_bili.html)
        self.assertIn('data-embed-provider="bilibili"', res_bili.html)
        self.assertIn('data-embed-src="https://player.bilibili.com/player.html?bvid=BV1xx411c7mD', res_bili.html)

        res_kugou = block.render_clean({
            "title": "酷狗测试曲目",
            "embed_url": "https://www.kugou.com/mixsong/abc123.html",
        })
        self.assertIn('data-embed-provider="kugou"', res_kugou.html)
        self.assertNotIn("data-embed-src=", res_kugou.html)

        legacy_val = block.to_python("https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(legacy_val["title"], "历史媒体档案")
        self.assertEqual(legacy_val["embed_url"], "https://www.bilibili.com/video/BV1xx411c7mD")

    def test_get_common_stream_blocks(self):
        from content_blocks.blocks import get_common_stream_blocks

        blocks_list = get_common_stream_blocks()
        block_keys = [b[0] for b in blocks_list]
        expected_keys = ["rich_text", "markdown", "code", "mermaid", "table", "image", "document", "audio", "video", "embed"]
        self.assertEqual(block_keys, expected_keys)


if __name__ == "__main__":
    unittest.main()
