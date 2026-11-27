"""
全站可折叠栏目目录抽屉与多类型 *IndexPage 目录树 (N-01 / N-02 / N-03) 专项回归测试。
"""

from datetime import date
from pathlib import Path
import re
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from wagtail.models import Page, PageViewRestriction, Site

from blog.models import BlogIndexPage, BlogPage
from content_lifecycle.recycle import RecycleService
from discovery.contracts import QuerySpec
from discovery.navigation import (
    annotate_active_directory_tree,
    get_site_directory_tree,
)
from discovery.projection import sync_subtree_tree_metadata
from discovery.query import ContentQuery
from discovery.registry import get_directory_index_type_keys, list_directory_index_models
from guides.models import GuideIndexPage, GuidePage, GuideTopic
from home.models import HomePage
from portfolio.models import PortfolioIndexPage, ProjectPage
from taxonomy.models import TagIndexPage
from content_lifecycle.access import AccessContext, AccessMode


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


class SiteDirectoryTreeTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        Site.clear_site_root_paths_cache()
        self.addCleanup(Site.clear_site_root_paths_cache)
        self.addCleanup(cache.clear)
        User = get_user_model()
        self.owner = User.objects.create_superuser(
            username=f"dir_owner_{uuid4().hex[:6]}",
            email="dir@example.com",
            password="Pass12345!",
        )
        self.site, self.home = _ensure_default_site_and_home()
        self.rf = RequestFactory()

    def test_registry_discovers_all_directory_index_models_without_suffix_hardcode(self):
        models = list_directory_index_models()
        self.assertIn(BlogIndexPage, models)
        self.assertIn(GuideIndexPage, models)
        self.assertIn(PortfolioIndexPage, models)
        self.assertNotIn(TagIndexPage, models)
        self.assertNotIn(HomePage, models)

        tks = get_directory_index_type_keys()
        self.assertIn("blog_index", tks)
        self.assertIn("guide_index", tks)
        self.assertIn("portfolio_index", tks)
        self.assertNotIn("tag_index", tks)

    def test_chinese_slug_multi_type_tree_and_abnormal_url_path_safe(self):
        # N-02：全部 show_in_menus=False 也自动进入目录树
        net = BlogIndexPage(title="网络技术", slug="network", show_in_menus=False)
        self.home.add_child(instance=net)
        net.save_revision(user=self.owner).publish(user=self.owner)

        toolkit = BlogIndexPage(title="服务器的网络工具包", slug="服务器的网络工具包", show_in_menus=False)
        net.add_child(instance=toolkit)
        toolkit.save_revision(user=self.owner).publish(user=self.owner)

        guides = GuideIndexPage(title="专题指南", slug=f"guides-{uuid4().hex[:4]}", show_in_menus=False)
        self.home.add_child(instance=guides)
        guides.save_revision(user=self.owner).publish(user=self.owner)

        port = PortfolioIndexPage(title="作品展示", slug=f"portfolio-{uuid4().hex[:4]}", show_in_menus=False)
        self.home.add_child(instance=port)
        port.save_revision(user=self.owner).publish(user=self.owner)

        # 制造一条异常 url_path（非站点根前缀 + 非法字符），验证不 500 而是安全跳过
        bad_col = BlogIndexPage(title="异常路径栏目", slug=f"bad-{uuid4().hex[:4]}", show_in_menus=False)
        self.home.add_child(instance=bad_col)
        bad_col.save_revision(user=self.owner).publish(user=self.owner)
        Page.objects.filter(id=bad_col.id).update(url_path="/broken-prefix/%E4%B8%AD%E6%96%87")

        req = self.rf.get("/")
        tree = get_site_directory_tree(req, site=self.site)
        titles = [n["title"] for n in tree]
        self.assertIn("网络技术", titles)
        self.assertIn("专题指南", titles)
        self.assertIn("作品展示", titles)
        self.assertNotIn("异常路径栏目", titles)

        net_node = next(n for n in tree if n["title"] == "网络技术")
        self.assertEqual(len(net_node["children"]), 1)
        tk_node = net_node["children"][0]
        self.assertEqual(tk_node["title"], "服务器的网络工具包")
        self.assertEqual(tk_node["url"], "/network/%E6%9C%8D%E5%8A%A1%E5%99%A8%E7%9A%84%E7%BD%91%E7%BB%9C%E5%B7%A5%E5%85%B7%E5%8C%85/")
        self.assertEqual(self.client.get(tk_node["url"]).status_code, 200)

        # 高亮当前节点与祖先节点；同时容忍 None 与未保存预览页
        annotated = annotate_active_directory_tree(tree, toolkit)
        ann_net = next(n for n in annotated if n["title"] == "网络技术")
        self.assertTrue(ann_net["is_ancestor"])
        self.assertFalse(ann_net["is_active"])
        self.assertTrue(ann_net["children"][0]["is_active"])

        self.assertEqual(len(annotate_active_directory_tree(tree, None)), len(tree))
        unsaved = BlogPage(title="Preview", slug="preview")
        self.assertEqual(len(annotate_active_directory_tree(tree, unsaved)), len(tree))

    def test_bfs_truncation_preserves_all_top_level_branches(self):
        b1 = BlogIndexPage(title="大栏目一", slug=f"big1-{uuid4().hex[:4]}")
        self.home.add_child(instance=b1)
        b1.save_revision(user=self.owner).publish(user=self.owner)
        for i in range(6):
            sub = BlogIndexPage(title=f"子项{i}", slug=f"sub-{i}-{uuid4().hex[:4]}")
            b1.add_child(instance=sub)
            sub.save_revision(user=self.owner).publish(user=self.owner)

        g1 = GuideIndexPage(title="独立指南大厅", slug=f"g1-{uuid4().hex[:4]}")
        self.home.add_child(instance=g1)
        g1.save_revision(user=self.owner).publish(user=self.owner)

        p1 = PortfolioIndexPage(title="独立作品大厅", slug=f"p1-{uuid4().hex[:4]}")
        self.home.add_child(instance=p1)
        p1.save_revision(user=self.owner).publish(user=self.owner)

        # max_nodes=4 时，BFS (depth, path) 仍能保住全部 3 个一级栏目
        req = self.rf.get("/")
        tree = get_site_directory_tree(req, site=self.site, max_nodes=4)
        top_titles = [n["title"] for n in tree]
        self.assertIn("大栏目一", top_titles)
        self.assertIn("独立指南大厅", top_titles)
        self.assertIn("独立作品大厅", top_titles)

    def test_public_eligibility_nested_site_and_homepage_restriction(self):
        live_col = BlogIndexPage(title="公开栏目", slug=f"pub-{uuid4().hex[:4]}")
        self.home.add_child(instance=live_col)
        live_col.save_revision(user=self.owner).publish(user=self.owner)

        rec_col = BlogIndexPage(title="回收栏目", slug=f"rec-{uuid4().hex[:4]}")
        self.home.add_child(instance=rec_col)
        rec_col.save_revision(user=self.owner).publish(user=self.owner)
        rec_child = BlogIndexPage(title="回收子栏目", slug=f"recc-{uuid4().hex[:4]}")
        rec_col.add_child(instance=rec_child)
        rec_child.save_revision(user=self.owner).publish(user=self.owner)
        RecycleService.recycle_page_or_subtree(rec_col, actor=self.owner)

        draft_col = BlogIndexPage(title="草稿栏目", slug=f"draft-{uuid4().hex[:4]}", live=False)
        self.home.add_child(instance=draft_col)
        draft_col.save_revision(user=self.owner)

        # 嵌套子站点隔离
        sub_home = HomePage(title="子站首页", slug=f"subhome-{uuid4().hex[:4]}")
        self.home.add_child(instance=sub_home)
        sub_home.save_revision(user=self.owner).publish(user=self.owner)
        Site.objects.create(hostname="sub.localhost", port=80, root_page=sub_home, is_default_site=False)
        Site.clear_site_root_paths_cache()
        sub_col = BlogIndexPage(title="子站专属栏目", slug=f"subcol-{uuid4().hex[:4]}")
        sub_home.add_child(instance=sub_col)
        sub_col.save_revision(user=self.owner).publish(user=self.owner)

        tree = get_site_directory_tree(self.rf.get("/"), site=self.site)
        flat_titles = []
        def _collect(nodes):
            for n in nodes:
                flat_titles.append(n["title"])
                _collect(n.get("children") or [])
        _collect(tree)

        self.assertIn("公开栏目", flat_titles)
        self.assertNotIn("回收栏目", flat_titles)
        self.assertNotIn("回收子栏目", flat_titles)
        self.assertNotIn("草稿栏目", flat_titles)
        self.assertNotIn("子站专属栏目", flat_titles)

        # HomePage 自身受限时 Fail-Closed 返回空树
        restriction = PageViewRestriction.objects.create(
            page=self.home,
            restriction_type=PageViewRestriction.LOGIN,
        )
        try:
            self.assertEqual(get_site_directory_tree(self.rf.get("/"), site=self.site), [])
        finally:
            restriction.delete()

    def test_child_indexes_qs_cross_site_move_and_no_private_count_leak(self):
        root = Page.get_first_root_node()
        other_home = HomePage(title="站点B", slug=f"siteb-{uuid4().hex[:4]}")
        root.add_child(instance=other_home)
        other_home.save_revision(user=self.owner).publish(user=self.owner)
        site_b = Site.objects.create(hostname="siteb.localhost", port=80, root_page=other_home, is_default_site=False)
        Site.clear_site_root_paths_cache()

        parent_col = BlogIndexPage(title="待迁移父栏目", slug=f"movep-{uuid4().hex[:4]}")
        other_home.add_child(instance=parent_col)
        parent_col.save_revision(user=self.owner).publish(user=self.owner)

        pub_child = BlogIndexPage(title="公开子栏目", slug=f"pubc-{uuid4().hex[:4]}")
        parent_col.add_child(instance=pub_child)
        pub_child.save_revision(user=self.owner).publish(user=self.owner)

        sec_child = BlogIndexPage(title="私密子栏目", slug=f"secc-{uuid4().hex[:4]}")
        parent_col.add_child(instance=sec_child)
        sec_child.save_revision(user=self.owner).publish(user=self.owner)
        PageViewRestriction.objects.create(page=sec_child, restriction_type=PageViewRestriction.LOGIN)

        # 从站点 B 移动到主站点
        parent_col.move(self.home, pos="last-child")
        parent_col.refresh_from_db()
        sync_subtree_tree_metadata(parent_col)

        # spec.site_id=None 时自动回退 root_page.get_site()，且 navigation_total_count 不泄漏私密子栏目数
        res = ContentQuery.execute(
            QuerySpec(
                type_keys=["blog_page"],
                site_id=None,
                root_page_id=parent_col.id,
                include_listing_scope=True,
                navigation_type_keys=["blog_index"],
            ),
            AccessContext(mode=AccessMode.PUBLIC),
        )
        self.assertEqual(res.navigation_total_count, 1)
        self.assertEqual([it["title"] for it in res.navigation_items], ["公开子栏目"])

        # 若显式传入旧站点 B 的 site_id 冲突，则拒绝越权列举
        res_conflict = ContentQuery.execute(
            QuerySpec(
                type_keys=["blog_page"],
                site_id=site_b.id,
                root_page_id=parent_col.id,
                include_listing_scope=True,
                navigation_type_keys=["blog_index"],
            ),
            AccessContext(mode=AccessMode.PUBLIC),
        )
        self.assertEqual(res_conflict.navigation_total_count, 0)
        self.assertEqual(res_conflict.navigation_items, [])

    def test_homepage_directory_cards_tag_degradation_and_404_safe(self):
        # 未建 TagIndexPage 时标签链接降级为纯文本 span，不出现死链 /tags/.../
        TagIndexPage.objects.all().delete()

        l1 = BlogIndexPage(title="技术架构", slug=f"arch-{uuid4().hex[:4]}")
        self.home.add_child(instance=l1)
        l1.save_revision(user=self.owner).publish(user=self.owner)
        l2 = BlogIndexPage(title="分布式存储", slug=f"dist-{uuid4().hex[:4]}")
        l1.add_child(instance=l2)
        l2.save_revision(user=self.owner).publish(user=self.owner)
        l3 = BlogIndexPage(title="PostgreSQL 18", slug=f"pg18-{uuid4().hex[:4]}")
        l2.add_child(instance=l3)
        l3.save_revision(user=self.owner).publish(user=self.owner)

        post = BlogPage(
            title="目录与标签降级测试",
            slug=f"home-tag-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 26),
            body=[("markdown", "hello")],
        )
        post.tags.add("NoTagIndex")
        l3.add_child(instance=post)
        post.save_revision(user=self.owner).publish(user=self.owner)

        home_resp = self.client.get("/")
        self.assertEqual(home_resp.status_code, 200)
        html_text = home_resp.content.decode("utf-8")

        self.assertIn("home-dir-card", html_text)
        self.assertIn("技术架构", html_text)
        self.assertIn("分布式存储", html_text)
        self.assertIn("PostgreSQL 18", html_text)
        # 降级容器绝不能带默认 open 属性（零 FOUC 要求）
        self.assertNotIn('<details class="site-dir-nojs" data-dir-fallback open', html_text)
        # 无 TagIndexPage 时不输出 /tags/ 死链
        self.assertNotIn("/tags/notagindex/", html_text.lower())
        self.assertIn("#NoTagIndex", html_text)

        # 404 页面无 page 对象也应正常渲染目录抽屉而不 500
        not_found = self.client.get(f"/not-found-{uuid4().hex[:8]}/")
        self.assertEqual(not_found.status_code, 404)
        self.assertIn("site-directory-dialog", not_found.content.decode("utf-8"))

    def test_b01_directory_tree_article_count_excludes_drafts_recycled_restricted_and_cross_site(self):
        backend = BlogIndexPage(title="后端技术", slug=f"backend-{uuid4().hex[:4]}")
        self.home.add_child(instance=backend)
        backend.save_revision(user=self.owner).publish(user=self.owner)

        pg_col = BlogIndexPage(title="PostgreSQL 专栏", slug=f"pg-{uuid4().hex[:4]}")
        backend.add_child(instance=pg_col)
        pg_col.save_revision(user=self.owner).publish(user=self.owner)

        guides_col = GuideIndexPage(title="指南大厅", slug=f"guides-cnt-{uuid4().hex[:4]}")
        self.home.add_child(instance=guides_col)
        guides_col.save_revision(user=self.owner).publish(user=self.owner)

        port_col = PortfolioIndexPage(title="作品大厅", slug=f"port-cnt-{uuid4().hex[:4]}")
        self.home.add_child(instance=port_col)
        port_col.save_revision(user=self.owner).publish(user=self.owner)

        # 1) 直属文章 + 子栏目 2 篇公开文章 -> 后端技术共 3 篇，PostgreSQL 共 2 篇
        p_direct = BlogPage(
            title="直属文章",
            slug=f"p-direct-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 20),
            body=[("markdown", "direct")],
        )
        backend.add_child(instance=p_direct)
        p_direct.save_revision(user=self.owner).publish(user=self.owner)

        for i in range(2):
            p_sub = BlogPage(
                title=f"PG 文章 {i}",
                slug=f"p-sub-{i}-{uuid4().hex[:4]}",
                authored_on=date(2026, 11, 21),
                body=[("markdown", f"pg {i}")],
            )
            pg_col.add_child(instance=p_sub)
            p_sub.save_revision(user=self.owner).publish(user=self.owner)

        # 2) 草稿、已回收、受限子树、跨站文章均绝不计入
        p_draft = BlogPage(
            title="草稿文章",
            slug=f"p-draft-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 22),
            live=False,
            body=[("markdown", "draft")],
        )
        pg_col.add_child(instance=p_draft)
        p_draft.save_revision(user=self.owner)

        p_recycled = BlogPage(
            title="回收文章",
            slug=f"p-rec-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 22),
            body=[("markdown", "recycled")],
        )
        pg_col.add_child(instance=p_recycled)
        p_recycled.save_revision(user=self.owner).publish(user=self.owner)
        RecycleService.recycle_page_or_subtree(p_recycled, actor=self.owner)

        restricted_col = BlogIndexPage(title="受限子栏目", slug=f"res-sub-{uuid4().hex[:4]}")
        backend.add_child(instance=restricted_col)
        restricted_col.save_revision(user=self.owner).publish(user=self.owner)
        p_restricted = BlogPage(
            title="受限文章",
            slug=f"p-res-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 22),
            body=[("markdown", "restricted")],
        )
        restricted_col.add_child(instance=p_restricted)
        p_restricted.save_revision(user=self.owner).publish(user=self.owner)
        PageViewRestriction.objects.create(
            page=restricted_col,
            restriction_type=PageViewRestriction.LOGIN,
        )

        # 指南与作品详情页各 1 篇
        from wagtail.rich_text import RichText
        topic_obj, _ = GuideTopic.objects.get_or_create(
            slug=f"wagtail-{uuid4().hex[:4]}",
            defaults={"name": "Wagtail 实践"},
        )
        g_page = GuidePage(
            title="指南文章",
            slug=f"g-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 22),
            topic=topic_obj,
            body=[("step", {"step_number": "1", "title": "步骤一", "content": RichText("<p>guide body</p>")})],
        )
        guides_col.add_child(instance=g_page)
        g_page.save_revision(user=self.owner).publish(user=self.owner)

        proj_page = ProjectPage(
            title="作品文章",
            slug=f"proj-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 22),
            body=[("markdown", "project body")],
        )
        port_col.add_child(instance=proj_page)
        proj_page.save_revision(user=self.owner).publish(user=self.owner)

        # 嵌套子站文章绝不计入主站目录计数
        sub_home = HomePage(title="嵌套子站", slug=f"subsite-{uuid4().hex[:4]}")
        self.home.add_child(instance=sub_home)
        sub_home.save_revision(user=self.owner).publish(user=self.owner)
        Site.objects.create(hostname="nested.localhost", port=80, root_page=sub_home, is_default_site=False)
        Site.clear_site_root_paths_cache()
        sub_col = BlogIndexPage(title="子站栏目", slug=f"sub-col-{uuid4().hex[:4]}")
        sub_home.add_child(instance=sub_col)
        sub_col.save_revision(user=self.owner).publish(user=self.owner)
        sub_post = BlogPage(
            title="子站文章",
            slug=f"sub-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 22),
            body=[("markdown", "sub post")],
        )
        sub_col.add_child(instance=sub_post)
        sub_post.save_revision(user=self.owner).publish(user=self.owner)

        tree = get_site_directory_tree(self.rf.get("/"), site=self.site)
        backend_node = next(n for n in tree if n["page_id"] == backend.id)
        self.assertEqual(backend_node["article_count"], 3)
        self.assertEqual(len(backend_node["children"]), 1)
        self.assertEqual(backend_node["children"][0]["page_id"], pg_col.id)
        self.assertEqual(backend_node["children"][0]["article_count"], 2)

        guides_node = next(n for n in tree if n["page_id"] == guides_col.id)
        self.assertEqual(guides_node["article_count"], 1)
        port_node = next(n for n in tree if n["page_id"] == port_col.id)
        self.assertEqual(port_node["article_count"], 1)

    def test_b03_breadcrumbs_exclude_duplicate_site_root_home(self):
        from content_blocks.rendering.detail import build_detail_presentation

        parent_col = BlogIndexPage(title="公文材料", slug=f"gongwen-{uuid4().hex[:4]}")
        self.home.add_child(instance=parent_col)
        parent_col.save_revision(user=self.owner).publish(user=self.owner)

        child_col = BlogIndexPage(title="政策解读", slug=f"zhengce-{uuid4().hex[:4]}")
        parent_col.add_child(instance=child_col)
        child_col.save_revision(user=self.owner).publish(user=self.owner)

        post = BlogPage(
            title="公文写作要点",
            slug=f"gw-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 25),
            body=[("markdown", "## 标题一\n正文")],
        )
        parent_col.add_child(instance=post)
        post.save_revision(user=self.owner).publish(user=self.owner)

        # 1) 顶层栏目祖先列表剔除站点根页 Home，不再出现重复的 Home
        req_parent = self.rf.get(parent_col.url)
        self.assertEqual(parent_col.get_listing_breadcrumbs(req_parent), [])
        parent_html = self.client.get(parent_col.url).content.decode("utf-8")
        self.assertNotIn(f">{self.home.title}<", parent_html)

        # 2) 二级栏目面包屑仅含「首页 / 公文材料 / 政策解读」，不含 Home
        req_child = self.rf.get(child_col.url)
        child_crumbs = child_col.get_listing_breadcrumbs(req_child)
        self.assertEqual([c["id"] for c in child_crumbs], [parent_col.id])
        child_html = self.client.get(child_col.url).content.decode("utf-8")
        child_nav = re.search(r'<nav class="detail-breadcrumbs[^>]*>.*?</nav>', child_html, re.S)
        self.assertIsNotNone(child_nav)
        self.assertIn(">首页<", child_nav.group(0))
        self.assertIn(">公文材料<", child_nav.group(0))
        self.assertIn(">政策解读<", child_nav.group(0))
        self.assertNotIn(f">{self.home.title}<", child_nav.group(0))

        # 3) 详情页面包屑仅含「首页 / 公文材料 / 公文写作要点」，不含 Home
        req_post = self.rf.get(post.url)
        pres = build_detail_presentation(post, req_post)
        self.assertEqual([a.id for a in pres["breadcrumbs"]], [parent_col.id])
        post_html = self.client.get(post.url).content.decode("utf-8")
        post_nav = re.search(r'<nav class="detail-breadcrumbs[^>]*>.*?</nav>', post_html, re.S)
        self.assertIsNotNone(post_nav)
        self.assertIn(">首页<", post_nav.group(0))
        self.assertIn(">公文材料<", post_nav.group(0))
        self.assertIn(">公文写作要点<", post_nav.group(0))
        self.assertNotIn(f">{self.home.title}<", post_nav.group(0))

    def test_header_no_emoji_monochrome_svg_active_state_and_drawer_filter(self):
        col = BlogIndexPage(title="工程实践", slug=f"eng-{uuid4().hex[:4]}")
        self.home.add_child(instance=col)
        col.save_revision(user=self.owner).publish(user=self.owner)

        post = BlogPage(
            title="单色图标测试文章",
            slug=f"icon-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 25),
            body=[("markdown", "hello")],
        )
        col.add_child(instance=post)
        post.save_revision(user=self.owner).publish(user=self.owner)

        home_html = self.client.get("/").content.decode("utf-8")
        header_match = re.search(r'<header class="site-header">.*?</header>', home_html, re.S)
        self.assertIsNotNone(header_match)
        header_html = header_match.group(0)
        dialog_match = re.search(r'<dialog id="site-directory-dialog".*?</dialog>', home_html, re.S)
        self.assertIsNotNone(dialog_match)
        dialog_html = dialog_match.group(0)

        # 顶栏与目录抽屉不包含任何 UI Emoji 字符
        for emoji_char in ("🚀", "📂", "🔍", "📡", "🌓", "☀️", "🌙", "☰", "✕"):
            self.assertNotIn(emoji_char, header_html)
            self.assertNotIn(emoji_char, dialog_html)

        # 包含单色 SVG ui-icon 与抽屉过滤/计数控件
        self.assertIn('class="ui-icon"', header_html)
        self.assertIn("data-dir-filter", dialog_html)
        self.assertIn("data-dir-expand-all", dialog_html)
        self.assertIn("data-dir-collapse-all", dialog_html)
        self.assertIn('class="site-dir-count">1</span>', home_html)
        self.assertIn('href="/feeds/"', home_html)

        # 首页精确匹配激活，作者专栏不激活
        self.assertRegex(header_html, r'<a href="/"[^>]*class="nav-link is-active"[^>]*aria-current="page"')
        self.assertNotRegex(header_html, r'<a href="/authors/"[^>]*class="nav-link is-active"')

        # 切换至作者专栏，作者专栏激活且首页不激活
        authors_html = self.client.get("/authors/").content.decode("utf-8")
        authors_header = re.search(r'<header class="site-header">.*?</header>', authors_html, re.S)
        self.assertIsNotNone(authors_header)
        self.assertRegex(authors_header.group(0), r'<a href="/authors/"[^>]*class="nav-link is-active"[^>]*aria-current="page"')
        self.assertNotRegex(authors_header.group(0), r'<a href="/"[^>]*class="nav-link is-active"')

    def test_b02_homepage_masthead_rendering_and_fallbacks(self):
        from site_settings.models import FooterSettings

        # 1) 默认空值时：主标题回退为 site_name 或 page.title，导语回退为 FooterSettings.about_text 或默认文案，且不含旧宣传横幅文案
        self.home.masthead_title = ""
        self.home.masthead_subtitle = ""
        self.home.save_revision(user=self.owner).publish(user=self.owner)
        self.site.site_name = "示例工程站"
        self.site.save()

        footer = FooterSettings._get_or_create()
        footer.about_text = ""
        footer.save()

        html_default = self.client.get("/").content.decode("utf-8")
        self.assertNotIn("探索高并发与现代分布式技术", html_default)
        self.assertIn('<h1 class="home-masthead-title">示例工程站</h1>', html_default)
        self.assertIn("记录思考、技术实践与专题归档。", html_default)

        # 2) 配置 FooterSettings.about_text 时导语回退到 about_text
        footer.about_text = "页脚简介回退导语"
        footer.save()
        html_footer_fallback = self.client.get("/").content.decode("utf-8")
        self.assertIn("页脚简介回退导语", html_footer_fallback)

        # 3) 显式配置 masthead_title / masthead_subtitle 时优先展示后台配置内容
        self.home.masthead_title = "定制刊头主标题"
        self.home.masthead_subtitle = "定制刊头副标题导语"
        self.home.save_revision(user=self.owner).publish(user=self.owner)
        html_custom = self.client.get("/").content.decode("utf-8")
        self.assertIn('<h1 class="home-masthead-title">定制刊头主标题</h1>', html_custom)
        self.assertIn("定制刊头副标题导语", html_custom)

    def test_search_sidebar_no_duplicate_content_type_and_no_published_badge(self):
        col = BlogIndexPage(title="搜索校验栏目", slug=f"s-col-{uuid4().hex[:4]}")
        self.home.add_child(instance=col)
        col.save_revision(user=self.owner).publish(user=self.owner)

        post = BlogPage(
            title="搜索卡片去噪测试",
            slug=f"s-post-{uuid4().hex[:4]}",
            authored_on=date(2026, 11, 26),
            body=[("markdown", "搜索验证正文")],
        )
        col.add_child(instance=post)
        post.save_revision(user=self.owner).publish(user=self.owner)

        search_html = self.client.get("/search/").content.decode("utf-8")
        aside_match = re.search(r'<aside class="search-facets-pane"[^>]*>.*?</aside>', search_html, re.S)
        self.assertIsNotNone(aside_match)
        self.assertNotIn("内容类型", aside_match.group(0))

        results_match = re.search(r'<ul class="search-result-list">.*?</ul>', search_html, re.S)
        self.assertIsNotNone(results_match)
        self.assertIn("搜索卡片去噪测试", results_match.group(0))
        self.assertNotIn("已发布", results_match.group(0))

    def test_listing_subcategories_template_and_render_clean_of_bootstrap(self):
        project_root = Path(__file__).resolve().parents[1]
        subcat_tpl = (project_root / "myblog/templates/discovery/listing/_listing_subcategories.html").read_text(encoding="utf-8")
        for legacy_cls in ("bg-light", "btn-outline-secondary", "badge bg-primary"):
            self.assertNotIn(legacy_cls, subcat_tpl)

        parent_col = BlogIndexPage(title="父级分类", slug=f"p-subcat-{uuid4().hex[:4]}")
        self.home.add_child(instance=parent_col)
        parent_col.save_revision(user=self.owner).publish(user=self.owner)

        child_col = BlogIndexPage(title="子级分类", slug=f"c-subcat-{uuid4().hex[:4]}")
        parent_col.add_child(instance=child_col)
        child_col.save_revision(user=self.owner).publish(user=self.owner)

        html = self.client.get(parent_col.url).content.decode("utf-8")
        subcat_match = re.search(r'<section class="listing-subcategories"[^>]*>.*?</section>', html, re.S)
        self.assertIsNotNone(subcat_match)
        subcat_html = subcat_match.group(0)
        self.assertIn("子级分类", subcat_html)
        for legacy_cls in ("bg-light", "btn-outline-secondary", "badge bg-primary"):
            self.assertNotIn(legacy_cls, subcat_html)

    def test_all_non_vendor_css_files_have_zero_transition_all(self):
        project_root = Path(__file__).resolve().parents[1]
        css_root = project_root / "myblog"
        offenders = []
        for css_file in sorted(css_root.rglob("*.css")):
            if "vendor" in css_file.parts:
                continue
            text = css_file.read_text(encoding="utf-8")
            if re.search(r"transition\s*:\s*all\b", text, re.IGNORECASE):
                offenders.append(str(css_file.relative_to(project_root)))
        self.assertEqual(offenders, [], f"非 vendor CSS 不得使用 transition: all: {offenders}")

    def test_stage3_quick_search_dialog_and_modular_page_css_loaded(self):
        home_html = self.client.get("/").content.decode("utf-8")
        self.assertIn('id="quick-search-dialog"', home_html)
        self.assertIn('data-quick-search-url="/search/quick/"', home_html)
        self.assertIn("css/pages/home.css", home_html)

        search_html = self.client.get("/search/").content.decode("utf-8")
        self.assertIn('id="quick-search-dialog"', search_html)
        self.assertIn("css/pages/search.css", search_html)

        project_root = Path(__file__).resolve().parents[1]
        hex_re = re.compile(r"#[0-9a-fA-F]{3,8}\b")
        for rel_css in ("myblog/static/css/pages/home.css", "myblog/static/css/pages/search.css"):
            text = (project_root / rel_css).read_text(encoding="utf-8")
            self.assertEqual(hex_re.findall(text), [], f"{rel_css} 不得包含裸 #hex 色值")
            self.assertNotIn("transition: all", text, f"{rel_css} 不得包含 transition: all")

    def test_homepage_featured_image_masthead_layouts_and_media_ref(self):
        from io import BytesIO
        from PIL import Image as PilImage
        from django.core.files.uploadedfile import SimpleUploadedFile
        from media_library.models import CustomImage, VisibilityChoices

        buf = BytesIO()
        PilImage.new("RGB", (640, 400), color=(30, 80, 140)).save(buf, format="PNG")
        img = CustomImage.objects.create(
            title="首页背景测试图",
            file=SimpleUploadedFile("home_bg.png", buf.getvalue(), content_type="image/png"),
            visibility=VisibilityChoices.PUBLIC,
        )

        self.home.featured_image = img
        self.home.masthead_layout = HomePage.MASTHEAD_LAYOUT_VISUAL_BANNER
        self.home.save_revision(user=self.owner).publish(user=self.owner)
        refs = self.home.collect_media_references()
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].asset_id, str(img.public_id))

        banner_html = self.client.get("/").content.decode("utf-8")
        self.assertIn("home-masthead--visual_banner", banner_html)
        self.assertIn(f"/media-gateway/image/{img.public_id}/", banner_html)

        self.home.masthead_layout = HomePage.MASTHEAD_LAYOUT_EDITORIAL_SPLIT
        self.home.save_revision(user=self.owner).publish(user=self.owner)
        split_html = self.client.get("/").content.decode("utf-8")
        self.assertIn("home-masthead--editorial_split", split_html)
        self.assertIn("home-masthead-side-media", split_html)

        self.home.masthead_layout = HomePage.MASTHEAD_LAYOUT_COMPACT_TEXT
        self.home.save_revision(user=self.owner).publish(user=self.owner)
        compact_html = self.client.get("/").content.decode("utf-8")
        self.assertNotIn("home-masthead--visual_banner", compact_html)
        self.assertNotIn("home-masthead-side-media", compact_html)
