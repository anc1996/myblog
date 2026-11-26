"""
全站 Ctrl+K 快捷搜索只读接口 (/search/quick/) 专项回归测试 (B-04)。
"""

from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse
from wagtail.models import Page, PageViewRestriction, Site
from wagtail.rich_text import RichText

from blog.models import BlogIndexPage, BlogPage
from content_lifecycle.recycle import RecycleService
from guides.models import GuideIndexPage, GuidePage, GuideTopic
from home.models import HomePage
from portfolio.models import PortfolioIndexPage, ProjectPage
from taxonomy.models import TagIndexPage


def _ensure_default_site_and_home():
    site = Site.objects.filter(is_default_site=True).select_related("root_page").first()
    if site and isinstance(site.root_page.specific, HomePage):
        return site, site.root_page.specific
    root = Page.get_first_root_node() or Page.add_root(title="Root")
    home = HomePage.objects.first()
    if not home:
        home = HomePage(title="Home", slug=f"home-{uuid4().hex[:6]}")
        root.add_child(instance=home)
        home.save_revision().publish()
    if not site:
        site = Site.objects.create(hostname="localhost", port=80, root_page=home, is_default_site=True)
    elif site.root_page_id != home.id:
        site.root_page = home
        site.save()
    Site.clear_site_root_paths_cache()
    return site, home


class QuickSearchViewTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        Site.clear_site_root_paths_cache()
        self.addCleanup(Site.clear_site_root_paths_cache)
        self.addCleanup(cache.clear)
        User = get_user_model()
        self.owner = User.objects.create_superuser(
            username=f"qs_owner_{uuid4().hex[:6]}",
            email="qs@example.com",
            password="Pass12345!",
        )
        self.site, self.home = _ensure_default_site_and_home()
        self.quick_url = reverse("search_quick")

    def test_empty_query_returns_top5_primary_directories_and_empty_articles_tags(self):
        # 创建 6 个一级公开栏目和 1 个二级子栏目，空查询仅返回前 5 个一级栏目
        created_cols = []
        for idx in range(6):
            col = BlogIndexPage(title=f"一级栏目{idx}", slug=f"top-{idx}-{uuid4().hex[:4]}")
            self.home.add_child(instance=col)
            col.save_revision(user=self.owner).publish(user=self.owner)
            created_cols.append(col)

        sub_col = BlogIndexPage(title="二级子栏目不应在空查询出现", slug=f"sub-{uuid4().hex[:4]}")
        created_cols[0].add_child(instance=sub_col)
        sub_col.save_revision(user=self.owner).publish(user=self.owner)

        post = BlogPage(
            title="空查询不应返还的文章",
            slug=f"empty-q-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 25),
            body=[("markdown", "hello")],
        )
        post.tags.add("EmptySkipTag")
        created_cols[0].add_child(instance=post)
        post.save_revision(user=self.owner).publish(user=self.owner)

        # 仅允许 GET
        post_resp = self.client.post(self.quick_url)
        self.assertEqual(post_resp.status_code, 405)

        resp = self.client.get(self.quick_url, {"q": "   "})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Cache-Control"], "no-store")
        data = resp.json()

        self.assertEqual(data["articles"], [])
        self.assertEqual(data["tags"], [])
        self.assertLessEqual(len(data["directories"]), 5)
        self.assertEqual(len(data["directories"]), 5)
        for d in data["directories"]:
            self.assertEqual(set(d.keys()), {"title", "url", "article_count"})
            self.assertNotEqual(d["title"], "二级子栏目不应在空查询出现")
        self.assertEqual(data["directories"][0]["article_count"], 1)

    def test_chinese_and_english_queries_return_canonical_json_and_tag_fallback(self):
        blog_col = BlogIndexPage(title="PostgreSQL 数据库专栏", slug=f"pg-col-{uuid4().hex[:4]}")
        self.home.add_child(instance=blog_col)
        blog_col.save_revision(user=self.owner).publish(user=self.owner)

        child_col = BlogIndexPage(title="微服务架构实践", slug=f"micro-{uuid4().hex[:4]}")
        blog_col.add_child(instance=child_col)
        child_col.save_revision(user=self.owner).publish(user=self.owner)

        guide_col = GuideIndexPage(title="指南大厅", slug=f"guide-col-{uuid4().hex[:4]}")
        self.home.add_child(instance=guide_col)
        guide_col.save_revision(user=self.owner).publish(user=self.owner)

        port_col = PortfolioIndexPage(title="作品大厅", slug=f"port-col-{uuid4().hex[:4]}")
        self.home.add_child(instance=port_col)
        port_col.save_revision(user=self.owner).publish(user=self.owner)

        post = BlogPage(
            title="PostgreSQL 索引调优实战",
            slug=f"pg-opt-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 20),
            body=[("markdown", "深入介绍 PostgreSQL 查询优化与微服务架构拆分。")],
        )
        post.tags.add("PostgreSQL", "微服务")
        blog_col.add_child(instance=post)
        post.save_revision(user=self.owner).publish(user=self.owner)

        topic = GuideTopic.objects.create(name="PG专题", slug=f"pg-topic-{uuid4().hex[:4]}")
        guide = GuidePage(
            title="PostgreSQL 部署步骤",
            slug=f"pg-guide-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 21),
            topic=topic,
            body=[("step", {"step_number": "1", "title": "安装", "content": RichText("<p>PostgreSQL initdb</p>")})],
        )
        guide.tags.add("PostgreSQL")
        guide_col.add_child(instance=guide)
        guide.save_revision(user=self.owner).publish(user=self.owner)

        proj = ProjectPage(
            title="PostgreSQL 工具集",
            slug=f"pg-proj-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 22),
            primary_language="Python",
            body=[("markdown", "PostgreSQL CLI helper")],
        )
        port_col.add_child(instance=proj)
        proj.save_revision(user=self.owner).publish(user=self.owner)

        # 1) 无 TagIndexPage 时标签 URL 回退到 /search/?tag=<slug>
        TagIndexPage.objects.all().delete()
        resp_en = self.client.get(self.quick_url, {"q": "postgresql"})
        self.assertEqual(resp_en.status_code, 200)
        self.assertEqual(resp_en["Cache-Control"], "no-store")
        payload_en = resp_en.json()

        self.assertEqual(len(payload_en["directories"]), 1)
        self.assertEqual(payload_en["directories"][0]["title"], "PostgreSQL 数据库专栏")
        self.assertEqual(payload_en["directories"][0]["article_count"], 1)

        self.assertEqual(len(payload_en["articles"]), 3)
        for art in payload_en["articles"]:
            self.assertEqual(set(art.keys()), {"title", "url", "type_label", "authored_on"})
            self.assertTrue(art["url"].startswith("/"))
            self.assertRegex(art["authored_on"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(
            {a["type_label"] for a in payload_en["articles"]},
            {"博客文章", "专题指南", "开源项目"},
        )

        self.assertEqual(len(payload_en["tags"]), 1)
        pg_tag = payload_en["tags"][0]
        self.assertEqual(set(pg_tag.keys()), {"name", "slug", "url", "count"})
        self.assertEqual(pg_tag["name"], "PostgreSQL")
        self.assertEqual(pg_tag["count"], 2)
        self.assertEqual(pg_tag["url"], f"/search/?tag={pg_tag['slug']}")

        # 2) 创建 TagIndexPage 后，中文查询命中子栏目 + 文章 + 标签详情页 URL
        tag_index = TagIndexPage(title="全站标签", slug=f"tags-{uuid4().hex[:4]}")
        self.home.add_child(instance=tag_index)
        tag_index.save_revision(user=self.owner).publish(user=self.owner)

        resp_zh = self.client.get(self.quick_url, {"q": "微服务"})
        self.assertEqual(resp_zh.status_code, 200)
        payload_zh = resp_zh.json()
        self.assertEqual([d["title"] for d in payload_zh["directories"]], ["微服务架构实践"])
        self.assertEqual([a["title"] for a in payload_zh["articles"]], ["PostgreSQL 索引调优实战"])
        self.assertEqual(len(payload_zh["tags"]), 1)
        self.assertEqual(payload_zh["tags"][0]["name"], "微服务")
        self.assertTrue(payload_zh["tags"][0]["url"].startswith(tag_index.url))

    def test_excludes_draft_recycled_restricted_and_cross_or_nested_sites(self):
        keyword = "IsolationProbeToken"
        pub_col = BlogIndexPage(title=f"公开 {keyword}", slug=f"pub-{uuid4().hex[:4]}")
        self.home.add_child(instance=pub_col)
        pub_col.save_revision(user=self.owner).publish(user=self.owner)

        pub_post = BlogPage(
            title=f"公开文章 {keyword}",
            slug=f"pub-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 20),
            body=[("markdown", keyword)],
        )
        pub_post.tags.add(f"Pub{keyword}")
        pub_col.add_child(instance=pub_post)
        pub_post.save_revision(user=self.owner).publish(user=self.owner)

        # 1) 草稿文章 + 草稿栏目
        draft_col = BlogIndexPage(title=f"草稿栏目 {keyword}", slug=f"draft-col-{uuid4().hex[:4]}", live=False)
        self.home.add_child(instance=draft_col)
        draft_col.save_revision(user=self.owner)

        draft_post = BlogPage(
            title=f"草稿文章 {keyword}",
            slug=f"draft-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 21),
            live=False,
            body=[("markdown", keyword)],
        )
        draft_post.tags.add(f"Draft{keyword}")
        pub_col.add_child(instance=draft_post)
        draft_post.save_revision(user=self.owner)

        # 2) 已回收文章 + 已回收栏目
        rec_col = BlogIndexPage(title=f"回收栏目 {keyword}", slug=f"rec-col-{uuid4().hex[:4]}")
        self.home.add_child(instance=rec_col)
        rec_col.save_revision(user=self.owner).publish(user=self.owner)
        rec_post = BlogPage(
            title=f"回收文章 {keyword}",
            slug=f"rec-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 21),
            body=[("markdown", keyword)],
        )
        rec_post.tags.add(f"Rec{keyword}")
        rec_col.add_child(instance=rec_post)
        rec_post.save_revision(user=self.owner).publish(user=self.owner)
        RecycleService.recycle_page_or_subtree(rec_col, actor=self.owner)

        # 3) 受限栏目与受限子文章
        res_col = BlogIndexPage(title=f"受限栏目 {keyword}", slug=f"res-col-{uuid4().hex[:4]}")
        self.home.add_child(instance=res_col)
        res_col.save_revision(user=self.owner).publish(user=self.owner)
        res_post = BlogPage(
            title=f"受限文章 {keyword}",
            slug=f"res-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 21),
            body=[("markdown", keyword)],
        )
        res_post.tags.add(f"Restricted{keyword}")
        res_col.add_child(instance=res_post)
        res_post.save_revision(user=self.owner).publish(user=self.owner)
        PageViewRestriction.objects.create(page=res_col, restriction_type=PageViewRestriction.LOGIN)

        # 4) 嵌套子站点
        sub_home = HomePage(title="子站首页", slug=f"subhome-{uuid4().hex[:4]}")
        self.home.add_child(instance=sub_home)
        sub_home.save_revision(user=self.owner).publish(user=self.owner)
        Site.objects.create(hostname="sub.localhost", port=80, root_page=sub_home, is_default_site=False)
        Site.clear_site_root_paths_cache()
        nested_col = BlogIndexPage(title=f"子站栏目 {keyword}", slug=f"nested-col-{uuid4().hex[:4]}")
        sub_home.add_child(instance=nested_col)
        nested_col.save_revision(user=self.owner).publish(user=self.owner)
        nested_post = BlogPage(
            title=f"子站文章 {keyword}",
            slug=f"nested-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 21),
            body=[("markdown", keyword)],
        )
        nested_post.tags.add(f"Nested{keyword}")
        nested_col.add_child(instance=nested_post)
        nested_post.save_revision(user=self.owner).publish(user=self.owner)

        # 5) 独立跨站站点 B
        root = Page.get_first_root_node()
        other_home = HomePage(title="站点B首页", slug=f"otherhome-{uuid4().hex[:4]}")
        root.add_child(instance=other_home)
        other_home.save_revision(user=self.owner).publish(user=self.owner)
        Site.objects.create(hostname="other.localhost", port=80, root_page=other_home, is_default_site=False)
        Site.clear_site_root_paths_cache()
        other_col = BlogIndexPage(title=f"跨站栏目 {keyword}", slug=f"other-col-{uuid4().hex[:4]}")
        other_home.add_child(instance=other_col)
        other_col.save_revision(user=self.owner).publish(user=self.owner)
        other_post = BlogPage(
            title=f"跨站文章 {keyword}",
            slug=f"other-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 21),
            body=[("markdown", keyword)],
        )
        other_post.tags.add(f"Other{keyword}")
        other_col.add_child(instance=other_post)
        other_post.save_revision(user=self.owner).publish(user=self.owner)

        resp = self.client.get(self.quick_url, {"q": keyword})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertEqual([d["title"] for d in data["directories"]], [f"公开 {keyword}"])
        self.assertEqual([a["title"] for a in data["articles"]], [f"公开文章 {keyword}"])
        self.assertEqual([t["name"] for t in data["tags"]], [f"Pub{keyword}"])

    def test_overlong_input_special_chars_and_db_error_degrade_safely(self):
        long_q = "A" * 500
        resp_long = self.client.get(self.quick_url, {"q": long_q})
        self.assertEqual(resp_long.status_code, 200)
        self.assertEqual(len(resp_long.json()["q"]), 80)

        for weird_q in (
            "'; DROP TABLE discovery_publishedsearchdocument; --",
            "%_\\[]()*+?^$|<>\"'\x00\n\t🚀",
            "C++ / C# & PostgreSQL 18",
        ):
            resp = self.client.get(self.quick_url, {"q": weird_q})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp["Cache-Control"], "no-store")
            body = resp.json()
            self.assertIsInstance(body["directories"], list)
            self.assertIsInstance(body["articles"], list)
            self.assertIsInstance(body["tags"], list)

        with (
            patch("search.views.get_site_directory_tree", side_effect=DatabaseError("db down")),
            patch("search.views.ContentQuery.execute", side_effect=DatabaseError("db down")),
            patch("search.views._query_public_tag_items", side_effect=DatabaseError("db down")),
        ):
            resp_err = self.client.get(self.quick_url, {"q": "test"})
            self.assertEqual(resp_err.status_code, 200)
            self.assertEqual(resp_err["Cache-Control"], "no-store")
            self.assertEqual(resp_err.json()["directories"], [])
            self.assertEqual(resp_err.json()["articles"], [])
            self.assertEqual(resp_err.json()["tags"], [])
