"""
P0-1 公开 API 与推荐块受限页权限加固回归测试矩阵。

覆盖：
1. 旧版 /api/v1/{posts,guides,projects}/ 与通用 /api/v1/content/<type_slug>/ 及 /api/v1/search/
   对自身受限、祖先受限、未发布祖先、祖先回收、别名页的统一拦截（匿名与已登录结果一致）；
2. 双站同 slug、嵌套子站点、同站重名 slug 歧义拦截与缓存预热后即时撤权；
3. FeaturedPostsBlock、GuidePage.related_guides 与富文本站内链接在网页 HTML、API JSON 与搜索索引三处的权限隔离。
"""

import json
from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase
from wagtail.models import Collection, Locale, Page, PageViewRestriction, Site
from wagtail.rich_text import RichText
from django.conf import settings
from wagtail.coreutils import get_supported_content_language_variant

from blog.models import BlogIndexPage, BlogPage
from content_lifecycle.models import RecycleEntry
from content_lifecycle.public_access import (
    filter_public_page_queryset,
    filter_public_pages,
    get_public_blocked_page_ids,
)
from content_lifecycle.recycle import RecycleService
from discovery.models import ContentDiscoveryEpoch, PublishedSearchChunk, PublishedSearchDocument
from discovery.projection import project_page_to_discovery
from guides.models import GuideIndexPage, GuidePage, GuideRelatedPage, GuideTopic
from home.models import HomePage
from portfolio.blocks import FeaturedPostsBlock
from portfolio.models import PortfolioIndexPage, ProjectPage
from site_settings.models import NavigationSettings

User = get_user_model()


class P0PublicApiAndBlockAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Locale.objects.get_or_create(
            language_code=get_supported_content_language_variant(settings.LANGUAGE_CODE)
        )
        if Collection.get_first_root_node() is None:
            Collection.add_root(name="Root")

        cls.root = Page.get_first_root_node() or Page.add_root(title="Root")
        cls.home = HomePage.objects.first()
        if cls.home is None:
            cls.home = HomePage(title="Home", slug=f"home-p0-{uuid4().hex[:6]}")
            cls.root.add_child(instance=cls.home)
            cls.home.save_revision().publish()

        cls.site = Site.objects.filter(is_default_site=True).first()
        if cls.site is None:
            cls.site = Site.objects.create(
                hostname="localhost",
                port=80,
                root_page=cls.home,
                is_default_site=True,
                site_name="MainSite",
            )
        Site.clear_site_root_paths_cache()

        cls.superuser = User.objects.create_superuser(
            username=f"p0_super_{uuid4().hex[:6]}",
            email="p0super@example.com",
            password="StrongPass123!",
        )
        cls.topic = GuideTopic.objects.create(
            name=f"P0Topic-{uuid4().hex[:4]}",
            slug=f"p0t-{uuid4().hex[:6]}",
        )

    def setUp(self):
        super().setUp()
        cache.clear()
        Site.clear_site_root_paths_cache()
        self.client = Client(HTTP_HOST="localhost")
        self.factory = RequestFactory()
        self.created_pages = []

    def tearDown(self):
        for p in reversed(self.created_pages):
            try:
                p.delete(allow_physical_purge=True)
            except Exception:
                pass
        Site.clear_site_root_paths_cache()
        cache.clear()
        super().tearDown()

    def _pub_blog(self, parent, title: str, slug: str, *, body=None, authored_on=None) -> BlogPage:
        page = BlogPage(
            title=title,
            slug=slug,
            authored_on=authored_on or date(2026, 11, 20),
            body=body or [("markdown", {"markdown_text": f"正文内容 {title}"})],
        )
        parent.add_child(instance=page)
        page.save_revision().publish()
        page.refresh_from_db()
        project_page_to_discovery(page)
        self.created_pages.append(page)
        return page

    def test_public_apis_block_self_ancestor_restricted_unlive_recycled_and_alias_even_when_logged_in(self):
        """公开 API（详情/列表/搜索，旧版+通用）统一拦截自身受限、祖先受限、未发布祖先、回收祖先与别名页，且不因登录放行"""
        pub_col = BlogIndexPage(title="公开专栏", slug=f"p0-pub-{uuid4().hex[:6]}")
        self.home.add_child(instance=pub_col)
        pub_col.save_revision().publish()
        self.created_pages.append(pub_col)

        pub_post = self._pub_blog(pub_col, "正常公开文章", f"pub-ok-{uuid4().hex[:6]}")
        self_rest_post = self._pub_blog(pub_col, "自身加密文章", f"self-rest-{uuid4().hex[:6]}")
        PageViewRestriction.objects.create(page=self_rest_post, restriction_type=PageViewRestriction.LOGIN)

        rest_col = BlogIndexPage(title="受限父栏目", slug=f"p0-restcol-{uuid4().hex[:6]}")
        self.home.add_child(instance=rest_col)
        rest_col.save_revision().publish()
        self.created_pages.append(rest_col)
        PageViewRestriction.objects.create(page=rest_col, restriction_type=PageViewRestriction.PASSWORD, password="sec")
        anc_rest_post = self._pub_blog(rest_col, "祖先受限子文章", f"anc-rest-{uuid4().hex[:6]}")

        unlive_col = BlogIndexPage(title="未发布父栏目", slug=f"p0-unlive-{uuid4().hex[:6]}", live=False)
        self.home.add_child(instance=unlive_col)
        unlive_col.save_revision()  # 不发布
        self.created_pages.append(unlive_col)
        anc_unlive_post = self._pub_blog(unlive_col, "未发布祖先子文章", f"anc-unlive-{uuid4().hex[:6]}")

        rec_col = BlogIndexPage(title="待回收父栏目", slug=f"p0-rec-{uuid4().hex[:6]}")
        self.home.add_child(instance=rec_col)
        rec_col.save_revision().publish()
        self.created_pages.append(rec_col)
        anc_rec_post = self._pub_blog(rec_col, "待回收祖先子文章", f"anc-rec-{uuid4().hex[:6]}")
        RecycleService.recycle_page_or_subtree(rec_col, system_authorized=True)

        alias_post = pub_post.create_alias(update_slug=f"alias-post-{uuid4().hex[:6]}", parent=pub_col)
        self.created_pages.append(alias_post)

        # 同套检查指南与项目类型祖先受限
        g_rest_idx = GuideIndexPage(title="受限指南栏", slug=f"p0-gidx-{uuid4().hex[:6]}")
        self.home.add_child(instance=g_rest_idx)
        g_rest_idx.save_revision().publish()
        self.created_pages.append(g_rest_idx)
        PageViewRestriction.objects.create(page=g_rest_idx, restriction_type=PageViewRestriction.LOGIN)
        g_rest = GuidePage(
            title="祖先受限指南",
            slug=f"g-rest-{uuid4().hex[:6]}",
            topic=self.topic,
            body=[("step", {"step_number": "1", "title": "保密步骤", "content": "<p>secret</p>"})],
        )
        g_rest_idx.add_child(instance=g_rest)
        g_rest.save_revision().publish()
        project_page_to_discovery(g_rest)
        self.created_pages.append(g_rest)

        p_rest_idx = PortfolioIndexPage(title="受限项目栏", slug=f"p0-pidx-{uuid4().hex[:6]}")
        self.home.add_child(instance=p_rest_idx)
        p_rest_idx.save_revision().publish()
        self.created_pages.append(p_rest_idx)
        PageViewRestriction.objects.create(page=p_rest_idx, restriction_type=PageViewRestriction.LOGIN)
        p_rest = ProjectPage(
            title="祖先受限项目",
            slug=f"p-rest-{uuid4().hex[:6]}",
            body=[("markdown", {"markdown_text": "受限项目描述"})],
        )
        p_rest_idx.add_child(instance=p_rest)
        p_rest.save_revision().publish()
        project_page_to_discovery(p_rest)
        self.created_pages.append(p_rest)

        blocked_blog_slugs = [self_rest_post.slug, anc_rest_post.slug, anc_unlive_post.slug, anc_rec_post.slug, alias_post.slug]
        auth_client = Client(HTTP_HOST="localhost")
        auth_client.force_login(self.superuser)

        for cli in (self.client, auth_client):
            # 1. 公开博客详情（旧版 + 通用）：正常页 200，五个异常页全 404
            self.assertEqual(cli.get(f"/api/v1/posts/{pub_post.slug}/").status_code, 200)
            self.assertEqual(cli.get(f"/api/v1/content/blog/{pub_post.slug}/").status_code, 200)
            for bad_slug in blocked_blog_slugs:
                self.assertEqual(cli.get(f"/api/v1/posts/{bad_slug}/").status_code, 404, bad_slug)
                self.assertEqual(cli.get(f"/api/v1/content/blog/{bad_slug}/").status_code, 404, bad_slug)

            # 2. 受限指南与项目详情全 404
            self.assertEqual(cli.get(f"/api/v1/guides/{g_rest.slug}/").status_code, 404)
            self.assertEqual(cli.get(f"/api/v1/content/guide/{g_rest.slug}/").status_code, 404)
            self.assertEqual(cli.get(f"/api/v1/projects/{p_rest.slug}/").status_code, 404)
            self.assertEqual(cli.get(f"/api/v1/content/project/{p_rest.slug}/").status_code, 404)

            # 3. 列表 API（旧版 + 通用）：不含任何异常 slug
            for list_url in ("/api/v1/posts/?page_size=50", "/api/v1/content/blog/?page_size=50"):
                res = cli.get(list_url)
                self.assertEqual(res.status_code, 200)
                slugs = {it["slug"] for it in res.json()["items"]}
                self.assertIn(pub_post.slug, slugs)
                for bad_slug in blocked_blog_slugs:
                    self.assertNotIn(bad_slug, slugs)

            g_slugs = {it["slug"] for it in cli.get("/api/v1/guides/?page_size=50").json()["items"]}
            self.assertNotIn(g_rest.slug, g_slugs)
            p_slugs = {it["slug"] for it in cli.get("/api/v1/projects/?page_size=50").json()["data"]}
            self.assertNotIn(p_rest.slug, p_slugs)

            # 4. 搜索 API：命中公开标题，绝不命中受限/未发布祖先/回收祖先标题
            s_res = cli.get("/api/v1/search/?query=文章&page_size=50")
            self.assertEqual(s_res.status_code, 200)
            s_titles = {it["title"] for it in s_res.json()["items"]}
            self.assertIn("正常公开文章", s_titles)
            self.assertNotIn("自身加密文章", s_titles)
            self.assertNotIn("祖先受限子文章", s_titles)
            self.assertNotIn("未发布祖先子文章", s_titles)
            self.assertNotIn("待回收祖先子文章", s_titles)

    def test_multisite_same_slug_nested_subsite_ambiguity_and_instant_revocation(self):
        """双站同 slug 隔离、嵌套子站点隔离、同站重名 slug 返回 404 以及缓存后加限制即时撤权"""
        col_a1 = BlogIndexPage(title="A站栏目1", slug=f"a1-{uuid4().hex[:4]}")
        col_a2 = BlogIndexPage(title="A站栏目2", slug=f"a2-{uuid4().hex[:4]}")
        self.home.add_child(instance=col_a1)
        self.home.add_child(instance=col_a2)
        col_a1.save_revision().publish()
        col_a2.save_revision().publish()
        self.created_pages.extend([col_a1, col_a2])

        shared_slug = f"same-slug-{uuid4().hex[:6]}"
        post_a = self._pub_blog(col_a1, "主站文章标题", shared_slug)

        # 第二站点（独立根与嵌套子站点）
        home_b = HomePage(title="SiteB", slug=f"home-b-{uuid4().hex[:6]}")
        self.root.add_child(instance=home_b)
        home_b.save_revision().publish()
        self.created_pages.append(home_b)
        site_b = Site.objects.create(hostname="siteb.test", port=80, root_page=home_b, site_name="SiteB")
        Site.clear_site_root_paths_cache()
        post_b = self._pub_blog(home_b, "B站文章标题", shared_slug)

        # 嵌套子站点挂载在主站 HomePage 下
        nested_home = HomePage(title="NestedSite", slug=f"nested-{uuid4().hex[:6]}")
        self.home.add_child(instance=nested_home)
        nested_home.save_revision().publish()
        self.created_pages.append(nested_home)
        Site.objects.create(hostname="nested.test", port=80, root_page=nested_home, site_name="Nested")
        Site.clear_site_root_paths_cache()
        nested_post = self._pub_blog(nested_home, "嵌套子站专属文章", f"nested-only-{uuid4().hex[:6]}")

        # 1. 双站同 slug：各自域名只看到自己的文章，主站看不到嵌套子站
        r_a = self.client.get(f"/api/v1/posts/{shared_slug}/", HTTP_HOST="localhost")
        self.assertEqual(r_a.status_code, 200)
        self.assertEqual(r_a.json()["data"]["id"], post_a.id)
        self.assertEqual(r_a.headers.get("X-Cache"), "BYPASS")
        self.assertEqual(r_a.headers.get("Cache-Control"), "no-store")

        r_b = self.client.get(f"/api/v1/posts/{shared_slug}/", HTTP_HOST="siteb.test")
        self.assertEqual(r_b.status_code, 200)
        self.assertEqual(r_b.json()["data"]["id"], post_b.id)
        self.assertEqual(r_b.headers.get("X-Cache"), "BYPASS")

        self.assertEqual(self.client.get(f"/api/v1/posts/{nested_post.slug}/", HTTP_HOST="localhost").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/posts/{nested_post.slug}/", HTTP_HOST="nested.test").status_code, 200)

        # 1.5 未知域名一律 Fail-Closed 拒绝（不回退默认站点）
        self.assertEqual(self.client.get(f"/api/v1/posts/{shared_slug}/", HTTP_HOST="unknown.evil.test").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/posts/", HTTP_HOST="unknown.evil.test").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/site/settings/", HTTP_HOST="unknown.evil.test").status_code, 404)
        unk_search = self.client.get("/api/v1/search/?query=文章", HTTP_HOST="unknown.evil.test")
        self.assertEqual(unk_search.status_code, 200)
        self.assertEqual(unk_search.json()["items"], [])

        # 2. 详情/列表不走共享缓存 + 搜索缓存预热后即便 epoch 刷新失败也能立即撤权
        search_pre = self.client.get("/api/v1/search/?query=主站文章标题", HTTP_HOST="localhost")
        self.assertEqual(search_pre.headers.get("X-Cache"), "MISS")
        self.assertEqual(len(search_pre.json()["items"]), 1)
        search_hit = self.client.get("/api/v1/search/?query=主站文章标题", HTTP_HOST="localhost")
        self.assertEqual(search_hit.headers.get("X-Cache"), "HIT")

        # 第2页含专属受限标签，预热第1页搜索缓存后即便 epoch 刷新失败，第1页的分面与总数也必须立即扣减
        facet_pub = self._pub_blog(pub_parent_for_facet := col_a2, "分页搜索公开文章", f"fp-ok-{uuid4().hex[:6]}")
        facet_sec = self._pub_blog(facet_pub_col := col_a2, "分页搜索待受限文章", f"fp-sec-{uuid4().hex[:6]}")
        facet_sec.tags.add("绝密分面标签99")
        facet_sec.save_revision().publish()
        project_page_to_discovery(facet_sec)
        pre_p1 = self.client.get("/api/v1/search/?query=分页搜索&page=1&page_size=1", HTTP_HOST="localhost")
        self.assertEqual(pre_p1.json()["total_count"], 2)
        self.assertIn("绝密分面标签99", [t["name"] for t in pre_p1.json()["facets"].get("tags", [])])
        self.assertEqual(self.client.get("/api/v1/search/?query=分页搜索&page=1&page_size=1", HTTP_HOST="localhost").headers.get("X-Cache"), "HIT")

        with patch.object(ContentDiscoveryEpoch, "increment_epoch", side_effect=RuntimeError("epoch down")):
            PageViewRestriction.objects.create(page=col_a1, restriction_type=PageViewRestriction.LOGIN)
            PageViewRestriction.objects.create(page=facet_sec, restriction_type=PageViewRestriction.LOGIN)
        r_a_revoked = self.client.get(f"/api/v1/posts/{shared_slug}/", HTTP_HOST="localhost")
        self.assertEqual(r_a_revoked.status_code, 404)
        search_revoked = self.client.get("/api/v1/search/?query=主站文章标题", HTTP_HOST="localhost")
        self.assertEqual(search_revoked.json()["items"], [])
        post_p1 = self.client.get("/api/v1/search/?query=分页搜索&page=1&page_size=1", HTTP_HOST="localhost")
        self.assertEqual(post_p1.json()["total_count"], 1)
        self.assertNotIn("绝密分面标签99", [t["name"] for t in post_p1.json()["facets"].get("tags", [])])

        # 同数叶子页替换（P 下架 + Q 发布且 epoch 刷新失败）：缓存键指纹仍必须变化，绝不返回已下架 P
        leaf_p = self._pub_blog(col_a2, "叶子替换关键词P", f"leaf-p-{uuid4().hex[:6]}")
        leaf_q = BlogPage(
            title="叶子替换关键词Q",
            slug=f"leaf-q-{uuid4().hex[:6]}",
            authored_on=date(2026, 11, 20),
            live=False,
            body=[("markdown", {"markdown_text": "叶子替换关键词"})],
        )
        col_a2.add_child(instance=leaf_q)
        leaf_q.save_revision()
        self.created_pages.append(leaf_q)
        swap_pre = self.client.get("/api/v1/search/?query=叶子替换关键词", HTTP_HOST="localhost")
        self.assertEqual([it["title"] for it in swap_pre.json()["items"]], ["叶子替换关键词P"])
        self.assertEqual(self.client.get("/api/v1/search/?query=叶子替换关键词", HTTP_HOST="localhost").headers.get("X-Cache"), "HIT")
        frozen_epoch = ContentDiscoveryEpoch.get_current_epoch()
        with patch.object(ContentDiscoveryEpoch, "increment_epoch", return_value=frozen_epoch):
            leaf_p.unpublish()
            leaf_q.save_revision().publish()
        swap_post = self.client.get("/api/v1/search/?query=叶子替换关键词", HTTP_HOST="localhost")
        self.assertEqual([it["title"] for it in swap_post.json()["items"]], ["叶子替换关键词Q"])

        # 3. 同站同类型多候选同名 slug 歧义时返回 404，不猜测取第一条
        amb_slug = f"dup-slug-{uuid4().hex[:6]}"
        self._pub_blog(col_a2, "同名文章1", amb_slug)
        col_a3 = BlogIndexPage(title="A站栏目3", slug=f"a3-{uuid4().hex[:4]}")
        self.home.add_child(instance=col_a3)
        col_a3.save_revision().publish()
        self.created_pages.append(col_a3)
        self._pub_blog(col_a3, "同名文章2", amb_slug)
        self.assertEqual(self.client.get(f"/api/v1/posts/{amb_slug}/", HTTP_HOST="localhost").status_code, 404)

        site_b.delete()

    def test_featured_posts_related_guides_and_richtext_links_enforce_access_across_html_api_and_search(self):
        """FeaturedPostsBlock、GuidePage.related_guides 与富文本站内链接在网页、API JSON 与搜索索引均不泄漏受限目标"""
        pub_col = BlogIndexPage(title="公开推荐源", slug=f"rec-pub-{uuid4().hex[:6]}")
        rest_col = BlogIndexPage(title="私密推荐源", slug=f"rec-sec-{uuid4().hex[:6]}")
        self.home.add_child(instance=pub_col)
        self.home.add_child(instance=rest_col)
        pub_col.save_revision().publish()
        rest_col.save_revision().publish()
        self.created_pages.extend([pub_col, rest_col])
        PageViewRestriction.objects.create(page=rest_col, restriction_type=PageViewRestriction.LOGIN)

        pub_blog = self._pub_blog(pub_col, "公开可推荐博文", f"rec-ok-{uuid4().hex[:6]}")
        secret_blog = self._pub_blog(rest_col, "绝密关联博文SECRET99", f"rec-secret-{uuid4().hex[:6]}")
        draft_blog = BlogPage(title="未发布草稿推荐DRAFT88", slug=f"rec-draft-{uuid4().hex[:6]}", live=False)
        pub_col.add_child(instance=draft_blog)
        draft_blog.save_revision()
        self.created_pages.append(draft_blog)

        # 带富文本站内链接指向 secret_blog 的宿主页
        host_blog = self._pub_blog(
            pub_col,
            "含内链宿主文章",
            f"host-rt-{uuid4().hex[:6]}",
            body=[("rich_text", RichText(f'<p>请看 <a linktype="page" id="{secret_blog.id}">私密引用文本</a> 与 <a linktype="page" id="{pub_blog.id}">公开引用文本</a></p>'))],
        )
        host_blog.hydrate_from_mongo()
        host_blog.intro = [("rich_text", RichText(f'<p>简介含 <a linktype="page" id="{secret_blog.id}">简介私密文本</a></p>'))]
        host_blog.save_revision().publish()
        host_blog.refresh_from_db()
        html_host = host_blog.serve(self.factory.get(host_blog.url_path, HTTP_HOST="localhost")).render().content.decode("utf-8")
        self.assertIn(pub_blog.slug, html_host)
        self.assertNotIn(secret_blog.slug, html_host)
        self.assertIn("私密引用文本", html_host)

        api_host = self.client.get(f"/api/v1/posts/{host_blog.slug}/", HTTP_HOST="localhost").json()["data"]
        self.assertIn(pub_blog.slug, api_host["safe_html"])
        self.assertNotIn(secret_blog.slug, api_host["safe_html"])
        self.assertNotIn(str(secret_blog.id), json.dumps(api_host["blocks"], ensure_ascii=False))
        self.assertNotIn(secret_blog.slug, api_host["intro"])
        self.assertIn("简介私密文本", api_host["intro"])

        # ProjectPage 带 FeaturedPostsBlock
        p_idx = PortfolioIndexPage(title="作品栏", slug=f"p-vis-{uuid4().hex[:6]}")
        self.home.add_child(instance=p_idx)
        p_idx.save_revision().publish()
        self.created_pages.append(p_idx)

        proj = ProjectPage(
            title="混合推荐测试项目",
            slug=f"proj-mix-{uuid4().hex[:6]}",
            body=[
                ("featured_posts", {
                    "heading": "关联技术文章",
                    "description": "项目配套阅读",
                    "posts": [pub_blog, secret_blog, draft_blog],
                }),
                ("markdown", {"markdown_text": "## 概要\n正文"}),
            ],
        )
        p_idx.add_child(instance=proj)
        proj.save_revision().publish()
        proj.refresh_from_db()
        project_page_to_discovery(proj)
        self.created_pages.append(proj)

        # 1. 网页公开态：仅见公开博文，不见绝密/草稿标题或 URL
        proj_html = proj.serve(self.factory.get(proj.url_path, HTTP_HOST="localhost")).render().content.decode("utf-8")
        self.assertIn("公开可推荐博文", proj_html)
        self.assertNotIn("绝密关联博文SECRET99", proj_html)
        self.assertNotIn("未发布草稿推荐DRAFT88", proj_html)
        self.assertNotIn(secret_blog.slug, proj_html)

        # 2. 缺站点上下文时块级渲染不输出任何引用；预览态不泄漏私密标题/ID/链接
        no_ctx_res = FeaturedPostsBlock().render_clean({"heading": "关联", "description": "", "posts": [pub_blog]})
        self.assertEqual(no_ctx_res.html, "")
        prev_req = self.factory.get(proj.url_path, HTTP_HOST="localhost")
        prev_req.in_preview_panel = True
        prev_html = proj.serve_preview(prev_req, proj.default_preview_mode).render().content.decode("utf-8")
        self.assertIn("公开可推荐博文", prev_html)
        self.assertIn("公开页面将自动隐藏", prev_html)
        self.assertNotIn("绝密关联博文SECRET99", prev_html)

        # 3. API JSON（旧版 + 通用）：safe_html 与 blocks.value.posts 均只留 pub_blog.id
        for api_url in (f"/api/v1/projects/{proj.slug}/", f"/api/v1/content/project/{proj.slug}/"):
            pdata = self.client.get(api_url, HTTP_HOST="localhost").json()["data"]
            self.assertIn("公开可推荐博文", pdata["safe_html"])
            self.assertNotIn("绝密关联博文SECRET99", pdata["safe_html"])
            fp_block = next(b for b in pdata["blocks"] if b["type"] == "featured_posts")
            self.assertEqual(fp_block["value"]["posts"], [pub_blog.id])

        # 4. 搜索索引隔离：FeaturedPostsBlock.extract_text 与宿主页搜索均不索引目标标题
        ext = FeaturedPostsBlock().extract_text({"heading": "关联标题", "description": "关联导语", "posts": [pub_blog, secret_blog]})
        self.assertNotIn("公开可推荐博文", ext.plain_text)
        self.assertNotIn("绝密关联博文SECRET99", ext.plain_text)
        s_secret = self.client.get("/api/v1/search/?query=SECRET99", HTTP_HOST="localhost").json()
        self.assertEqual(s_secret["total_count"], 0)

        # 4.5 历史投影版本号变更强制重建分块（清掉旧版残留在分块中的 SECRET99）
        proj_doc = PublishedSearchDocument.objects.get(page_id=proj.id, index_generation=1)
        proj_doc.extra_data = {**proj_doc.extra_data, "extractor_version": 1}
        proj_doc.save(update_fields=["extra_data"])
        PublishedSearchChunk.objects.create(
            document=proj_doc,
            field_name="body",
            chunk_index=99,
            normalized_text="历史残留绝密词 LEGACYSECRET888",
        )
        project_page_to_discovery(proj)
        self.assertEqual(
            self.client.get("/api/v1/search/?query=LEGACYSECRET888", HTTP_HOST="localhost").json()["total_count"],
            0,
        )

        # 5. GuidePage.related_guides 在网页 HTML 与 API JSON 中均过滤受限/草稿指南
        g_pub_idx = GuideIndexPage(title="公开指南栏", slug=f"g-pub-{uuid4().hex[:6]}")
        g_sec_idx = GuideIndexPage(title="私密指南栏", slug=f"g-sec-{uuid4().hex[:6]}")
        self.home.add_child(instance=g_pub_idx)
        self.home.add_child(instance=g_sec_idx)
        g_pub_idx.save_revision().publish()
        g_sec_idx.save_revision().publish()
        self.created_pages.extend([g_pub_idx, g_sec_idx])
        PageViewRestriction.objects.create(page=g_sec_idx, restriction_type=PageViewRestriction.LOGIN)

        g_target_pub = GuidePage(
            title="公开关联指南",
            slug=f"gt-pub-{uuid4().hex[:6]}",
            topic=self.topic,
            body=[("step", {"step_number": "1", "title": "S1", "content": "<p>ok</p>"})],
        )
        g_target_sec = GuidePage(
            title="祖先受限关联指南",
            slug=f"gt-sec-{uuid4().hex[:6]}",
            topic=self.topic,
            body=[("step", {"step_number": "1", "title": "S1", "content": "<p>secret</p>"})],
        )
        g_pub_idx.add_child(instance=g_target_pub)
        g_sec_idx.add_child(instance=g_target_sec)
        g_target_pub.save_revision().publish()
        g_target_sec.save_revision().publish()

        g_host = GuidePage(
            title="宿主指南",
            slug=f"g-host-{uuid4().hex[:6]}",
            topic=self.topic,
            body=[("step", {
                "step_number": "1",
                "title": "S1",
                "content": f'<p>步骤关联 <a linktype="page" id="{g_target_sec.id}">私密步骤引用</a></p>',
            })],
        )
        g_host.related_guides = [
            GuideRelatedPage(related_page=g_target_pub, sort_order=0),
            GuideRelatedPage(related_page=g_target_sec, sort_order=1),
        ]
        g_pub_idx.add_child(instance=g_host)
        g_host.save_revision().publish()
        self.created_pages.extend([g_host, g_target_pub, g_target_sec])

        g_html = g_host.serve(self.factory.get(g_host.url_path, HTTP_HOST="localhost")).render().content.decode("utf-8")
        self.assertIn("公开关联指南", g_html)
        self.assertNotIn("祖先受限关联指南", g_html)
        self.assertNotIn(g_target_sec.slug, g_html)
        self.assertIn("私密步骤引用", g_html)
        for g_api_url in (f"/api/v1/guides/{g_host.slug}/", f"/api/v1/content/guide/{g_host.slug}/"):
            g_data = self.client.get(g_api_url, HTTP_HOST="localhost").json()["data"]
            rel_titles = [r["title"] for r in g_data["related_guides"]]
            self.assertEqual(rel_titles, ["公开关联指南"])
            self.assertNotIn(g_target_sec.slug, g_data["safe_html"])
            self.assertNotIn(str(g_target_sec.id), json.dumps(g_data["blocks"], ensure_ascii=False))

    def test_alias_ancestor_source_stale_instance_restoring_and_site_settings_revocation(self):
        """补充覆盖：别名祖先源链阻断、未刷新实例借用旧路径、restoring 回收状态及导航设置即时撤权"""
        src_col = BlogIndexPage(title="源受限栏目", slug=f"src-rest-{uuid4().hex[:6]}")
        pub_parent = BlogIndexPage(title="公开父栏目", slug=f"pub-par-{uuid4().hex[:6]}")
        self.home.add_child(instance=src_col)
        self.home.add_child(instance=pub_parent)
        src_col.save_revision().publish()
        pub_parent.save_revision().publish()
        self.created_pages.extend([src_col, pub_parent])
        PageViewRestriction.objects.create(page=src_col, restriction_type=PageViewRestriction.LOGIN)

        alias_col = src_col.create_alias(update_slug=f"alias-col-{uuid4().hex[:6]}", parent=pub_parent)
        self.created_pages.append(alias_col)
        child_under_alias = self._pub_blog(alias_col, "别名栏下子文章", f"under-alias-{uuid4().hex[:6]}")

        self.assertFalse(filter_public_page_queryset(BlogPage.objects.filter(id=child_under_alias.id), site=self.site).exists())
        self.assertEqual(filter_public_pages([child_under_alias], site=self.site), [])
        self.assertEqual(self.client.get(f"/api/v1/posts/{child_under_alias.slug}/", HTTP_HOST="localhost").status_code, 404)

        # 多跳别名链路：S(src_col) -> 别名 A(alias_col) -> 子栏 C -> 别名 B -> 子页 D
        sub_c = BlogIndexPage(title="别名下普通子栏C", slug=f"sub-c-{uuid4().hex[:6]}")
        alias_col.add_child(instance=sub_c)
        sub_c.save_revision().publish()
        alias_b = sub_c.create_alias(update_slug=f"alias-b-{uuid4().hex[:6]}", parent=pub_parent)
        sub_d = self._pub_blog(alias_b, "多跳别名后代文章D", f"multi-hop-d-{uuid4().hex[:6]}")
        # 增加同源双分支 S -> E -> X，同批检查 [D, X] 时两者均须被阻断
        alias_e = src_col.create_alias(update_slug=f"alias-e-{uuid4().hex[:6]}", parent=pub_parent)
        sub_x = self._pub_blog(alias_e, "同源双分支文章X", f"multi-hop-x-{uuid4().hex[:6]}")
        self.created_pages.extend([sub_c, alias_b, alias_e])
        self.assertFalse(filter_public_page_queryset(BlogPage.objects.filter(id=sub_d.id), site=self.site).exists())
        self.assertEqual(filter_public_pages([sub_d], site=self.site), [])
        self.assertEqual(filter_public_pages([sub_d, sub_x], site=self.site), [])
        self.assertEqual(self.client.get(f"/api/v1/posts/{sub_d.slug}/", HTTP_HOST="localhost").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/posts/{sub_x.slug}/", HTTP_HOST="localhost").status_code, 404)

        # 未刷新内存实例旧路径被另一公开文章占用时，不得借用新位置放行
        mov_col = BlogIndexPage(title="移动测试栏", slug=f"mov-{uuid4().hex[:6]}")
        self.home.add_child(instance=mov_col)
        mov_col.save_revision().publish()
        self.created_pages.append(mov_col)

        stale_page = self._pub_blog(pub_parent, "将被移走的文章", f"stale-mov-{uuid4().hex[:6]}")
        old_path = stale_page.path
        stale_page.move(src_col, pos="last-child")  # 数据库已移到受限栏目，但内存 stale_page 仍持 old_path
        occupier = self._pub_blog(pub_parent, "占用旧路径的新公开文章", f"occ-{uuid4().hex[:6]}")
        Page.objects.filter(id=occupier.id).update(path=old_path)

        self.assertIn(stale_page.id, get_public_blocked_page_ids([(stale_page.id, old_path)], site=self.site))
        self.assertEqual(filter_public_pages([stale_page], site=self.site), [])

        # RecycleEntry restoring 状态阻断
        restoring_post = self._pub_blog(pub_parent, "正在恢复的文章", f"restoring-{uuid4().hex[:6]}")
        batch = RecycleService.recycle_page_or_subtree(restoring_post, system_authorized=True)
        RecycleEntry.objects.filter(batch=batch).update(status="restoring")
        self.assertEqual(self.client.get(f"/api/v1/posts/{restoring_post.slug}/", HTTP_HOST="localhost").status_code, 404)

        # site_settings_api：加限制即使 epoch 刷新失败也立刻剔除导航项且不使用 120s 公开缓存
        nav_post = self._pub_blog(pub_parent, "导航目标文章", f"nav-post-{uuid4().hex[:6]}")
        nav_cfg, _ = NavigationSettings.objects.get_or_create(singleton_key=1)
        nav_cfg.nav_items = [("menu_item", {"label": "精选入口", "target_page": nav_post})]
        nav_cfg.save()

        r_nav_ok = self.client.get("/api/v1/site/settings/", HTTP_HOST="localhost")
        self.assertEqual(r_nav_ok.status_code, 200)
        self.assertEqual(r_nav_ok.headers.get("Cache-Control"), "private, no-cache")
        self.assertIn("精选入口", [it["label"] for it in r_nav_ok.json()["data"]["navigation"]])
        etag_before = r_nav_ok.headers["ETag"]

        with patch.object(ContentDiscoveryEpoch, "increment_epoch", side_effect=RuntimeError("epoch down")):
            nav_post.slug = f"nav-renamed-{uuid4().hex[:6]}"
            nav_post.save_revision().publish()
        r_nav_renamed = self.client.get("/api/v1/site/settings/", HTTP_HOST="localhost", HTTP_IF_NONE_MATCH=etag_before)
        self.assertEqual(r_nav_renamed.status_code, 200)
        self.assertIn(nav_post.slug, r_nav_renamed.json()["data"]["navigation"][0]["url"])
        etag_renamed = r_nav_renamed.headers["ETag"]

        with patch.object(ContentDiscoveryEpoch, "increment_epoch", side_effect=RuntimeError("epoch down")):
            PageViewRestriction.objects.create(page=nav_post, restriction_type=PageViewRestriction.LOGIN)
        r_nav_revoked = self.client.get("/api/v1/site/settings/", HTTP_HOST="localhost", HTTP_IF_NONE_MATCH=etag_renamed)
        self.assertEqual(r_nav_revoked.status_code, 200)
        self.assertNotIn("精选入口", [it["label"] for it in r_nav_revoked.json()["data"]["navigation"]])
