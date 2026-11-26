"""
全站可折叠栏目目录抽屉与多类型 *IndexPage 目录树 (N-01 / N-02 / N-03) 专项回归测试。
"""

from datetime import date
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
