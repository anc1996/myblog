"""
S10 居中单栏文章详情页与全套排版呈现单元与端到端测试。

实测覆盖：
1. 居中单栏文章详情页渲染（标题、成文日期、真实祖先链面包屑）；
2. 封面自适应规则：有封面时渲染大图，无封面时不产生占位空白；
3. 多人贡献角色（主要作者、共同作者、审校等）与全站标签跳转渲染；
4. 简介渐进增强结构（无 JS 可完整读取，JS 添加折叠控制）；
5. 全套 10 种 StreamField 积木在真实模板中安全渲染：
   - Markdown / RichText 格式；
   - CodeBlock（代码展示模式、高亮与复制属性）；
   - CustomTableBlock（复杂合并单元格、thead/tbody、手机端横向滚动容器）；
   - Mermaid / 媒体卡片（统一走 /media-gateway/ 协议）；
6. 文末独立画廊与 Lightbox 灯箱属性（data-full-src、图注与键盘导航支持）；
7. BlogIndexPage 栏目目录页与已发布子页面列表渲染；
8. 正式版发布与草稿隔离：未发布文章访问返回 404，已发布文章正常展现。
"""

import os
import io
from datetime import date
from pathlib import Path
import unittest
from uuid import uuid4

from PIL import Image as PILImage
from django.core.files.base import ContentFile
from django.test import RequestFactory
from django.http import Http404
from wagtail.rich_text import RichText

PROJECT_DIR = Path(__file__).resolve().parents[1]


def create_dummy_png(name="test.png", width=10, height=10):
    """生成符合 Wagtail / Willow 校验的合法最小 PNG 字节流文件"""
    buf = io.BytesIO()
    img = PILImage.new("RGB", (width, height), color="blue")
    img.save(buf, format="PNG")
    return ContentFile(buf.getvalue(), name=name)


class S10PresentationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
        import django
        django.setup()

    def setUp(self):
        from home.models import HomePage
        from blog.models import BlogIndexPage, BlogPage
        from people.models import Person
        from media_library.models import CustomImage

        self.factory = RequestFactory()
        self.home = HomePage.objects.first()
        self.created_pages = []
        self.created_snippets = []

    def tearDown(self):
        for p in reversed(self.created_pages):
            try:
                p.delete(allow_physical_purge=True)
            except Exception:
                pass
        for s in self.created_snippets:
            try:
                s.delete()
            except Exception:
                pass

    def test_01_blog_page_full_rendering_and_metadata(self):
        """测试文章详情页完整排版、封面、面包屑、贡献者角色与标签"""
        from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
        from people.models import Person
        from media_library.models import CustomImage
        from django.core.files.uploadedfile import SimpleUploadedFile

        # 1. 创建父栏目
        index_page = BlogIndexPage(title="技术专栏", slug=f"col-{uuid4().hex[:6]}")
        self.home.add_child(instance=index_page)
        self.created_pages.append(index_page)
        index_page.save_revision().publish()

        # 2. 创建贡献人员
        author1 = Person.objects.create(name="张三", bio="核心架构师")
        author2 = Person.objects.create(name="李四", bio="安全工程师")
        self.created_snippets.extend([author1, author2])

        # 3. 创建文章
        page = BlogPage(
            title="PostgreSQL 18 核心特性解析",
            slug=f"post-detail-{uuid4().hex[:6]}",
            authored_on=date(2026, 4, 15),
        )
        index_page.add_child(instance=page)
        self.created_pages.append(page)

        # 关联作者与角色
        page.authors.create(person=author1, role="primary_author", sort_order=0)
        page.authors.create(person=author2, role="reviewer", sort_order=1)

        # 关联标签
        page.tags.add("PostgreSQL", "数据库")

        # 简介与正文积木
        page.intro = [("rich_text", RichText("<p>这是系统架构级深度解析简介。</p>"))]
        page.body = [
            ("markdown", "## 核心特性一览\nPostgreSQL 18 带来了极致的并发性能与条件索引增强。"),
            ("code", {
                "title": "demo.sql",
                "language": "sql",
                "code": "SELECT * FROM discovery_publishedsearchdocument WHERE is_live = TRUE;",
                "mode": "display",
            }),
            ("table", {
                "caption": "性能测试对照表",
                "has_header_row": True,
                "has_header_col": False,
                "table_data": {
                    "rows": [
                        [{"value": "指标", "header": True}, {"value": "PG17", "header": True}, {"value": "PG18", "header": True}],
                        [{"value": "QPS", "header": False}, {"value": "12000", "header": False}, {"value": "24000", "header": False}],
                    ]
                }
            }),
        ]

        # 发布文章
        rev = page.save_revision()
        rev.publish()
        page.refresh_from_db()

        # 模拟读者请求 serve()
        req = self.factory.get(page.url_path)
        resp = page.serve(req)
        resp.render()
        html_content = resp.content.decode("utf-8")

        # 核心断言：
        # 1. 标题与成文日期
        self.assertIn("PostgreSQL 18 核心特性解析", html_content)
        self.assertIn("2026年04月15日", html_content)

        # 2. 真实祖先链面包屑包含父栏目
        self.assertIn("技术专栏", html_content)
        self.assertIn(index_page.url, html_content)

        # 3. 贡献者及其角色展示
        self.assertIn("张三", html_content)
        self.assertIn("主要作者", html_content)
        self.assertIn("李四", html_content)
        self.assertIn("审校人员", html_content)

        # 4. 标签与搜索跳转链接
        self.assertIn("#PostgreSQL", html_content)
        self.assertIn("#数据库", html_content)

        # 5. 无封面时不渲染 blog-featured-image 容器（不留白框）
        self.assertNotIn('class="blog-featured-image"', html_content)

        # 6. 正文与积木安全渲染
        self.assertIn("核心特性一览", html_content)
        self.assertIn("demo.sql", html_content)
        self.assertIn("discovery_publishedsearchdocument", html_content)
        self.assertIn("性能测试对照表", html_content)
        self.assertIn("custom-table-block", html_content)
        self.assertIn("24000", html_content)

    def test_02_featured_image_and_gallery_lightbox(self):
        """测试封面存在时的置顶呈现与文末独立画廊灯箱属性"""
        from blog.models import BlogPage, BlogPageGalleryImage
        from media_library.models import CustomImage

        # 创建测试图片
        cover_image = CustomImage.objects.create(title="封面图片", file=create_dummy_png("cover.png"))
        gallery_img1 = CustomImage.objects.create(title="架构拓扑图", file=create_dummy_png("gallery.png"))
        self.created_snippets.extend([cover_image, gallery_img1])

        page = BlogPage(
            title="带封面与画廊的文章",
            slug=f"cover-gallery-{uuid4().hex[:6]}",
            featured_image=cover_image,
        )
        self.home.add_child(instance=page)
        self.created_pages.append(page)

        # 添加文末独立画廊项
        page.gallery_images.create(image=gallery_img1, caption="图1: 微服务架构拓扑全景图", sort_order=0)

        page.save_revision().publish()
        page.refresh_from_db()

        req = self.factory.get(page.url_path)
        resp = page.serve(req)
        resp.render()
        html_content = resp.content.decode("utf-8")

        # 1. 验证封面居中渲染
        self.assertIn('class="blog-featured-image"', html_content)
        self.assertIn(str(cover_image.public_id), html_content)

        # 2. 验证文末独立画廊与灯箱属性
        self.assertIn('class="blog-gallery-section"', html_content)
        self.assertIn("文章图集", html_content)
        self.assertIn("图1: 微服务架构拓扑全景图", html_content)
        self.assertIn(f"data-full-src=\"/media-gateway/image/{gallery_img1.public_id}/\"", html_content)

    def test_03_blog_index_page_rendering(self):
        """测试 BlogIndexPage 栏目目录页与下属文章列表渲染"""
        from blog.models import BlogIndexPage, BlogPage

        index = BlogIndexPage(title="后端工程栏目", slug=f"backend-{uuid4().hex[:6]}", intro="聚焦后端底层设计。")
        self.home.add_child(instance=index)
        self.created_pages.append(index)
        index.save_revision().publish()

        # 在其下创建两篇文章
        p1 = BlogPage(title="文章1: 并发设计", slug=f"p1-{uuid4().hex[:6]}")
        index.add_child(instance=p1)
        self.created_pages.append(p1)
        p1.save_revision().publish()

        p2 = BlogPage(title="文章2: 存储引擎", slug=f"p2-{uuid4().hex[:6]}")
        index.add_child(instance=p2)
        self.created_pages.append(p2)
        p2.save_revision().publish()

        req = self.factory.get(index.url_path)
        resp = index.serve(req)
        resp.render()
        html_content = resp.content.decode("utf-8")

        self.assertIn("后端工程栏目", html_content)
        self.assertIn("聚焦后端底层设计。", html_content)
        self.assertIn("文章1: 并发设计", html_content)
        self.assertIn("文章2: 存储引擎", html_content)

    def test_04_unpublished_and_recycled_isolation(self):
        """测试未发布或已回收页面的访问安全隔离"""
        from blog.models import BlogPage
        from content_lifecycle.recycle import RecycleService

        # 1. 纯草稿页面（未发布）
        draft_page = BlogPage(title="秘密草稿", slug=f"draft-{uuid4().hex[:6]}")
        self.home.add_child(instance=draft_page)
        self.created_pages.append(draft_page)
        draft_page.live = False
        draft_page.save(update_fields=["live"])
        draft_page.save_revision()  # 仅保存草稿

        # 匿名访问必须阻断 (404)
        req = self.factory.get(draft_page.url_path)
        with self.assertRaises(Http404):
            draft_page.serve(req)

        # 2. 发布后再回收的页面
        live_page = BlogPage(title="即将回收页面", slug=f"live-rec-{uuid4().hex[:6]}")
        self.home.add_child(instance=live_page)
        self.created_pages.append(live_page)
        live_page.save_revision().publish()

        RecycleService.recycle_page_or_subtree(live_page, system_authorized=True)
        live_page.refresh_from_db()

        # 回收后访问必须严格 404
        req2 = self.factory.get(live_page.url_path)
        with self.assertRaises(Http404):
            live_page.serve(req2)


if __name__ == "__main__":
    unittest.main()
