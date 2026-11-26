"""
Phase 3 统一排版、段落对齐、图片修饰与三层防留白图文混排测试：
1. Wagtail register_image_format (centered/fullwidth/left/right) 与 ControlledFormat 原图灯箱属性；
2. Draftail 段落对齐 (align-left/center/right) Contentstate 双向转换与 nh3 净化守卫；
3. Markdown ::: align-* 块指令、有限 6 档宽度类（无内联 style）、独立 <figure><figcaption> 升级；
4. CustomImageBlock 修饰档位与 CustomMediaTextBlock (Grid 图文并排) 渲染及 MediaReferenceRecord 追踪；
5. content-images.css 零裸 #hex 护栏与三层防留白 CSS 规则检查。
"""

from datetime import date
from io import BytesIO
import json
from pathlib import Path
import re
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image as PILImage
from wagtail.admin.rich_text.converters.contentstate import ContentstateConverter
from wagtail.images.formats import get_image_format, get_image_formats
from wagtail.models import Page
from wagtail.rich_text import RichText

from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from comments.sanitizer import sanitize_comment_html
from content_blocks.blocks.markdown import CustomMarkdownBlock
from content_blocks.blocks.media import CustomImageBlock, CustomMediaTextBlock
from content_blocks.blocks.rich_text import CustomRichTextBlock, DEFAULT_RICH_TEXT_FEATURES
from content_blocks.sanitizer import sanitize_html
from content_lifecycle.permissions import ensure_author_role_infrastructure
from guides.models import GuideIndexPage, GuidePage, GuidePageAuthor, GuideTopic
from home.models import HomePage
from media_library.models import CustomDocument, CustomImage, MediaReferenceRecord, VisibilityChoices
from people.models import Person

User = get_user_model()
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _make_png(name: str = "p3.png") -> SimpleUploadedFile:
    buf = BytesIO()
    PILImage.new("RGB", (120, 80), color=(37, 99, 235)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


class Phase3LayoutAndAlignmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.infra = ensure_author_role_infrastructure()
        cls.authors_group = cls.infra["authors_group"]
        cls.workspace_col = cls.infra["workspace_collection"]
        cls.public_col = cls.infra["public_collection"]

        cls.home = HomePage.objects.first()
        if cls.home is None:
            root = Page.get_first_root_node()
            cls.home = HomePage(title="P3 Home", slug=f"p3-home-{uuid4().hex[:6]}")
            root.add_child(instance=cls.home)

        cls.blog_index = BlogIndexPage(title="P3 Blog", slug=f"p3-blog-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.blog_index)
        cls.blog_index.save_revision().publish()

        cls.guide_index = GuideIndexPage(title="P3 Guide", slug=f"p3-guide-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.guide_index)
        cls.guide_index.save_revision().publish()

        cls.topic = GuideTopic.objects.create(
            name=f"P3Topic-{uuid4().hex[:6]}",
            slug=f"p3-topic-{uuid4().hex[:6]}",
        )

    def setUp(self):
        suffix = uuid4().hex[:6]
        self.author = User.objects.create_user(
            username=f"p3_a_{suffix}", password="Password123!", is_active=True
        )
        self.author.groups.add(self.authors_group)
        self.person = Person.objects.create(name=f"P3作者_{suffix}", linked_user=self.author)

    def test_01_registered_image_formats_and_lightbox_full_src(self):
        names = {fmt.name for fmt in get_image_formats()}
        self.assertTrue({"centered", "fullwidth", "left", "right"}.issubset(names))

        img = CustomImage.objects.create(
            title="Format Test",
            file=_make_png("fmt.png"),
            collection=self.public_col,
            visibility=VisibilityChoices.PUBLIC,
        )
        rt = CustomRichTextBlock()
        for fmt_name, expected_cls in (
            ("centered", "cb-image--center"),
            ("fullwidth", "cb-image--full"),
            ("left", "cb-image--left"),
            ("right", "cb-image--right"),
        ):
            fmt = get_image_format(fmt_name)
            self.assertIn(expected_cls, fmt.classname)
            res = rt.render_clean(RichText(f'<p><embed embedtype="image" id="{img.id}" format="{fmt_name}" alt="插图"/></p>'))
            self.assertIn(expected_cls, res.html)
            self.assertIn(f'data-asset-id="{img.public_id}"', res.html)
            self.assertIn(f'data-full-src="/media-gateway/image/{img.public_id}/"', res.html)
            self.assertIn('loading="lazy"', res.html)

    def test_02_draftail_paragraph_alignment_roundtrip_and_sanitizer(self):
        for feat in ("align-left", "align-center", "align-right"):
            self.assertIn(feat, DEFAULT_RICH_TEXT_FEATURES)

        converter = ContentstateConverter(features=DEFAULT_RICH_TEXT_FEATURES)
        db_html = (
            '<p class="cb-align-center">居中段落</p>'
            '<p class="cb-align-right">右对齐段落</p>'
            '<p class="cb-align-left">左对齐段落</p>'
        )
        state_json = converter.from_database_format(db_html)
        parsed_state = json.loads(state_json)
        block_types = [b["type"] for b in parsed_state["blocks"]]
        self.assertEqual(block_types, ["align-center", "align-right", "align-left"])

        roundtrip_html = converter.to_database_format(state_json)
        self.assertIn('class="cb-align-center"', roundtrip_html)
        self.assertIn('class="cb-align-right"', roundtrip_html)
        self.assertIn('class="cb-align-left"', roundtrip_html)

        # 净化器放行合法对齐类，但剥离越权类与内联 style
        dirty = '<p class="cb-align-center evil-injected" style="color:red;position:fixed">文本</p>'
        cleaned = sanitize_html(dirty)
        self.assertIn('class="cb-align-center"', cleaned)
        self.assertNotIn("evil-injected", cleaned)
        self.assertNotIn("style=", cleaned)

    def test_03_markdown_align_directives_width_buckets_and_standalone_figures(self):
        md = CustomMarkdownBlock()
        img_uuid = str(uuid4())
        md_src = (
            "::: align-center\n"
            "居中的 **加粗导语**\n"
            ":::\n\n"
            "```markdown\n"
            "::: align-right\n"
            "代码块内的指令不应触发对齐容器\n"
            ":::\n"
            "```\n\n"
            f'![架构图|md,left,card](asset://image/{img_uuid} "图 1：受控媒体流转")\n\n'
            f'![宽图|52%](asset://image/{img_uuid})\n\n'
            f'行内混排图片 ![行内|sm](asset://image/{img_uuid}) 不应包成 figure。\n\n'
            "::: align-right\n"
            "未闭合指令应保留原文"
        )
        res = md.render_clean(md_src)
        self.assertTrue(res.is_safe)
        self.assertIn('<div class="cb-markdown-body">', res.html)
        self.assertIn('<div class="cb-align-block cb-align-center">', res.html)
        self.assertIn("<strong>加粗导语</strong>", res.html)
        # 代码块内指令保持纯文本代码
        self.assertNotIn('class="cb-align-block cb-align-right"', res.html)
        self.assertIn("::: align-right", res.html)
        # 独立图升级为 <figure> + <figcaption>，且无 title 时不生成 <figcaption>
        self.assertIn('<figure class="media-image-wrapper cb-image cb-image--left format-left cb-image--card"', res.html)
        self.assertIn('<figcaption class="media-caption">图 1：受控媒体流转</figcaption>', res.html)
        self.assertIn('cb-img-w-50', res.html)
        self.assertNotIn('style="width:', res.html)
        self.assertEqual(res.html.count("<figure"), 2)
        self.assertEqual(res.html.count("<figcaption"), 1)

        # 评论区同样经过 render_markdown_to_html，但 nh3 禁止 div -> 安全退化为纯文本内容
        comment_html = sanitize_comment_html("::: align-center\n评论居中试探\n:::")
        self.assertNotIn("<div", comment_html)
        self.assertIn("评论居中试探", comment_html)

    def test_04_image_card_and_media_text_block_render_and_media_refs(self):
        img_main = CustomImage.objects.create(
            title="MediaText Main",
            file=_make_png("mt_main.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author,
            visibility=VisibilityChoices.RESTRICTED,
        )
        img_nested = CustomImage.objects.create(
            title="MediaText Nested",
            file=_make_png("mt_nested.png"),
            collection=self.workspace_col,
            uploaded_by_user=self.author,
            visibility=VisibilityChoices.RESTRICTED,
        )
        doc_nested = CustomDocument.objects.create(
            title="MediaText Doc",
            file=SimpleUploadedFile("mt.txt", b"hello", content_type="text/plain"),
            collection=self.workspace_col,
            uploaded_by_user=self.author,
            visibility=VisibilityChoices.RESTRICTED,
        )

        img_block = CustomImageBlock()
        img_res = img_block.render_clean(
            {"image": img_main, "caption": "修饰图", "display_format": "left", "accent_style": "card"}
        )
        self.assertIn("cb-image--left", img_res.html)
        self.assertIn("cb-image--card", img_res.html)
        self.assertIn(f'data-full-src="/media-gateway/image/{img_main.public_id}/"', img_res.html)

        mt_block = CustomMediaTextBlock()
        mt_val = {
            "image": img_main,
            "caption": "左侧配图",
            "alt_text": "架构示意图",
            "layout": "media-right",
            "ratio": "50-50",
            "vertical_align": "start",
            "body": RichText(
                f'<p class="cb-align-center">并排说明 <embed embedtype="image" id="{img_nested.id}" format="centered"/>'
                f'<a linktype="document" id="{doc_nested.id}">附件</a></p>'
            ),
        }
        mt_res = mt_block.render_clean(mt_val)
        self.assertIn("cb-media-text--media-right", mt_res.html)
        self.assertIn("cb-media-text--ratio-50-50", mt_res.html)
        self.assertIn("cb-media-text--valign-start", mt_res.html)
        self.assertIn("cb-align-center", mt_res.html)
        self.assertIn(f'data-asset-id="{img_main.public_id}"', mt_res.html)

        ext = mt_block.extract_text(mt_val)
        self.assertIn("并排说明", ext.plain_text)
        self.assertIn("左侧配图", ext.plain_text)

        # 挂载到真实页面保存 Revision + 发布，验证 MediaReferenceRecord 与自动转正
        blog = BlogPage(
            title="Phase 3 MediaText Blog",
            slug=f"p3-mt-{uuid4().hex[:6]}",
            owner=self.author,
            authored_on=date(2026, 11, 25),
            body=[("media_text", mt_val)],
        )
        self.blog_index.add_child(instance=blog)
        BlogPagePerson.objects.create(page=blog, person=self.person, role="primary_author")
        rev = blog.save_revision(user=self.author)

        for aid in (img_main.public_id, img_nested.public_id, doc_nested.public_id):
            self.assertTrue(
                MediaReferenceRecord.objects.filter(
                    page_id=blog.id, revision_id=rev.id, asset_id=aid
                ).exists()
            )
        rev.publish(user=self.author)
        for asset in (img_main, img_nested, doc_nested):
            asset.refresh_from_db()
            self.assertEqual(asset.visibility, VisibilityChoices.PUBLIC)

    def test_05_static_css_js_guardrails_and_anti_whitespace_rules(self):
        css_text = (
            PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/css/content-images.css"
        ).read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r"#[0-9a-fA-F]{3,8}\b", css_text),
            "content-images.css 不允许写死裸 #hex 色值",
        )
        for token in (
            "@container (min-width: 560px)",
            "display: flow-root",
            "clear: both",
            ".cb-media-text",
            ".cb-align-center",
            ".cb-img-w-50",
        ):
            self.assertIn(token, css_text)

        js_text = (
            PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/js/content-images.js"
        ).read_text(encoding="utf-8")
        self.assertIn('img.getAttribute("data-full-src")', js_text)

    def test_06_sanitizer_preserves_component_classes_and_blocks_evil_heading_classes(self):
        raw = (
            '<p class="step-caption">步骤说明</p>'
            '<p class="doc-desc">文档说明</p>'
            '<p class="portfolio-tech-desc">技术栈</p>'
            '<p class="featured-posts-desc">精选</p>'
            '<p class="cb-align-center evil-p">居中</p>'
            '<h3 class="step-heading cb-align-right evil-h3">步骤标题</h3>'
            '<h4 class="callout-title evil-h4">提示标题</h4>'
        )
        cleaned = sanitize_html(raw)
        for keep in (
            'class="step-caption"',
            'class="doc-desc"',
            'class="portfolio-tech-desc"',
            'class="featured-posts-desc"',
            'class="cb-align-center"',
            'class="step-heading cb-align-right"',
            'class="callout-title"',
        ):
            self.assertIn(keep, cleaned)
        for bad in ("evil-p", "evil-h3", "evil-h4"):
            self.assertNotIn(bad, cleaned)

    def test_07_markdown_figure_no_double_scale_nested_table_and_css_rules(self):
        md = CustomMarkdownBlock()
        img_uuid = str(uuid4())
        md_src = (
            "保留字面量 `__CBFIGZONE_0__` 不被误替换。\n\n"
            "    ::: align-center\n"
            "    四空格缩进代码块内指令不触发对齐容器\n\n"
            f'![缩放左浮图|50%,left,card](asset://image/{img_uuid} "图注")\n\n'
            f'<table><tr><td><table><tr><td><p><img src="/media-gateway/image/{img_uuid}/" alt="嵌套表内图" /></p></td></tr></table></td></tr></table>'
        )
        res = md.render_clean(md_src)
        self.assertIn("<code>__CBFIGZONE_0__</code>", res.html)
        self.assertNotIn('<div class="cb-align-block', res.html)
        # 50% 宽度类上移至外层 <figure>，内部 <img> 不再保留 cb-img-w-50（防双重缩放）
        self.assertEqual(res.html.count("<figure"), 1)
        self.assertIn('figure class="media-image-wrapper cb-image cb-image--left format-left cb-img-w-50 cb-image--card"', res.html)
        img_part = res.html.split("<figure")[1].split("</figure>")[0]
        self.assertNotIn('cb-img-w-50" ', img_part.split("<img", 1)[1])

        css_text = (
            PROJECT_ROOT / "myblog/apps/content_blocks/static/content_blocks/css/content-images.css"
        ).read_text(encoding="utf-8")
        for token in (
            ".detail-body-section > figure.cb-image--left",
            "@container (max-width: 559.98px)",
            "var(--hero-text",
        ):
            self.assertIn(token, css_text)
        self.assertNotIn("max-height: 440px", css_text.split(".cb-media-text")[0])
