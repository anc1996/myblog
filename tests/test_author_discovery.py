"""
全站作者专栏与按作者搜索（MYBLOG-RFC-AUTHOR-DISCOVERY-01）综合验收测试套件。

覆盖验收矩阵 A01–A09、A11 与 reconcile_author_projection 对账命令：
- A01: Person 拼音/英文 Slug 生成、保留词/纯数字/符号兜底、改名不改 URL、系统「佚名」唯一性；
- A02: 受限作者预填（Blog/Guide=primary_author, Project=core_dev）、禁止清空/重复/选佚名/冒用、管理员留空走佚名；
- A03: 三级署名解析（显式 > LegacyAuthorAttribution > 内置佚名）、跨类型/新接入类型、重发脱离佚名、解绑/停用不影响已发布署名；
- A04: 详情页署名链接（含佚名）与后台预览隔离；
- A05: /search/ 多作者 OR、同一关系行角色精确匹配、authors 分面计数与实时更名、非法参数 400 与未知 slug 归零；
- A06: /authors/<slug>/ 跨类型 Tab、角色芯片、万字正文检索、仅搜标题、A/B 展开、日期校验；
- A07: /authors/<slug>/ 千万级双模分页（浅页 1..100、422 超窗、双向 Keyset 游标、seek_date、Epoch 失效重置）；
- A08: 作者 RSS/Atom 订阅、UUID/ID 301 重定向、no-store 防陈旧、零篇 404 与空匿名合法 XML；
- A09: 跨站点/回收/权限/未来排期隔离、/authors/ 大厅、/authors/lookup/、友好 404、AuthorSitemap；
- A11: 系统「佚名」ORM/信号/后台 UI/选择器多防线保护与 XSS 转义。
"""

from datetime import date, timedelta
from io import StringIO
import json
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError
from django.test import Client, RequestFactory, TestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from wagtail.coreutils import get_supported_content_language_variant
from wagtail.models import Collection, Locale, Page, PageViewRestriction, Site

from blog.models import BlogIndexPage, BlogPage
from content_blocks.rendering.detail import build_detail_presentation
from content_lifecycle.models import bind_linked_person_for_page_owner
from content_lifecycle.permissions import ensure_author_role_infrastructure
from content_lifecycle.recycle import RecycleService
from discovery.contracts import PublicListingCapabilities, PublishedAuthorRef, PublishedPayload, TextSegment
from discovery.models import ContentDiscoveryEpoch, PublishedSearchAuthor, PublishedSearchDocument
from discovery.projection import project_page_to_discovery
from discovery.registry import get_type_adapter, register_type_adapter
from guides.models import GuideIndexPage, GuidePage, GuideTopic
from home.models import HomePage
from people.author_identity import generate_base_slug
from people.models import LegacyAuthorAttribution, Person, get_or_create_anonymous_person
from portfolio.models import PortfolioIndexPage, ProjectPage

User = get_user_model()


class AuthorDiscoveryComprehensiveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Locale.objects.get_or_create(
            language_code=get_supported_content_language_variant(settings.LANGUAGE_CODE)
        )
        if Collection.get_first_root_node() is None:
            Collection.add_root(name="Root")
        cls.anon_person = get_or_create_anonymous_person()
        cls.superuser = User.objects.create_superuser(
            username=f"au_super_{uuid4().hex[:6]}",
            email="super@example.com",
            password="Password123!",
        )
        cls.home = HomePage.objects.first()
        if not cls.home:
            root = Page.get_first_root_node() or Page.add_root(title="Root")
            cls.home = HomePage(title="Home", slug=f"home-au-{uuid4().hex[:6]}")
            root.add_child(instance=cls.home)
            cls.home.save_revision().publish()
        ensure_author_role_infrastructure()
        cls.authors_group = Group.objects.get(name="Authors")
        cls.author_user = User.objects.create_user(
            username=f"au_writer_{uuid4().hex[:6]}",
            password="Password123!",
            is_staff=False,
        )
        cls.author_user.groups.add(cls.authors_group)

        cls.site = Site.objects.filter(is_default_site=True).first()
        if not cls.site:
            cls.site = Site.objects.create(
                hostname="localhost",
                port=80,
                root_page=cls.home,
                is_default_site=True,
                site_name="myblog",
            )

        cls.blog_index = BlogIndexPage(title="博客专栏", slug=f"blog-au-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.blog_index)
        cls.blog_index.save_revision().publish()

        cls.guide_index = GuideIndexPage(title="指南专栏", slug=f"guide-au-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.guide_index)
        cls.guide_index.save_revision().publish()

        cls.portfolio_index = PortfolioIndexPage(title="项目专栏", slug=f"proj-au-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.portfolio_index)
        cls.portfolio_index.save_revision().publish()

        cls.topic = GuideTopic.objects.create(
            name="数据库内核",
            slug=f"db-core-{uuid4().hex[:6]}",
        )

    def _publish_blog(self, title: str, *, authored_on: date | None = None, authors=None, body_md="正文内容", owner=None, parent=None, tags=None):
        parent_page = parent or self.blog_index
        page = BlogPage(
            title=title,
            slug=f"b-{uuid4().hex[:8]}",
            authored_on=authored_on or date(2026, 11, 10),
            owner=owner or self.superuser,
        )
        parent_page.add_child(instance=page)
        page.body = [("markdown", body_md)]
        if tags:
            for t in tags:
                page.tags.add(t)
        if authors:
            for idx, (p_obj, role_val) in enumerate(authors):
                page.authors.create(person=p_obj, role=role_val, sort_order=idx)
        rev = page.save_revision(user=owner or self.superuser)
        rev.publish(user=owner or self.superuser)
        page.refresh_from_db()
        return page

    def _publish_guide(self, title: str, *, authored_on: date | None = None, authors=None, body_md="第一步操作"):
        page = GuidePage(
            title=title,
            slug=f"g-{uuid4().hex[:8]}",
            topic=self.topic,
            authored_on=authored_on or date(2026, 11, 11),
            owner=self.superuser,
            live=False,
        )
        self.guide_index.add_child(instance=page)
        page.body = [("step", {"step_number": 1, "title": "步骤一", "content": body_md})]
        if authors:
            for idx, (p_obj, role_val) in enumerate(authors):
                page.authors.create(person=p_obj, role=role_val, sort_order=idx)
        rev = page.save_revision(user=self.superuser)
        rev.publish(user=self.superuser)
        page.refresh_from_db()
        return page

    def _publish_project(self, title: str, *, authored_on: date | None = None, authors=None):
        page = ProjectPage(
            title=title,
            slug=f"p-{uuid4().hex[:8]}",
            authored_on=authored_on or date(2026, 11, 12),
            owner=self.superuser,
        )
        self.portfolio_index.add_child(instance=page)
        page.body = [("markdown", "项目架构介绍")]
        if authors:
            for idx, (p_obj, role_val) in enumerate(authors):
                page.authors.create(person=p_obj, role=role_val, sort_order=idx)
        rev = page.save_revision(user=self.superuser)
        rev.publish(user=self.superuser)
        page.refresh_from_db()
        return page

    # ------------------------------------------------------------------
    # A01: Slug 生成、保留词回避、改名稳定性与系统匿名约束
    # ------------------------------------------------------------------
    def test_a01_person_slug_generation_rename_stability_and_reserved_words(self):
        self.assertEqual(generate_base_slug("张三"), "zhang-san")
        self.assertEqual(generate_base_slug("Ada Lovelace"), "ada-lovelace")
        self.assertEqual(generate_base_slug("anonymous"), "")
        self.assertEqual(generate_base_slug("123456"), "")
        self.assertEqual(generate_base_slug("★☆"), "")

        p1 = Person.objects.create(name="张三")
        p2 = Person.objects.create(name="张三")
        self.assertEqual(p1.slug, "zhang-san")
        self.assertTrue(p2.slug.startswith("zhang-san-"))
        self.assertNotEqual(p1.slug, p2.slug)
        self.assertEqual(p1.initial, "Z")
        self.assertEqual(p1.initial_group_rank, 0)

        # 符号名与保留词绝不占用 anonymous / lookup
        p_sym = Person.objects.create(name="★☆")
        self.assertTrue(p_sym.slug.startswith("person-"))
        self.assertEqual(p_sym.initial, "#")
        self.assertEqual(p_sym.initial_group_rank, 1)

        p_res = Person.objects.create(name="anonymous")
        self.assertNotEqual(p_res.slug, "anonymous")
        self.assertTrue(p_res.slug.startswith("person-"))

        # 改名后 slug 保持不变
        orig_slug = p1.slug
        p1.name = "张三丰"
        p1.save()
        p1.refresh_from_db()
        self.assertEqual(p1.slug, orig_slug)

        # DB CheckConstraint 阻止将普通人物设为 slug='anonymous'
        with self.assertRaises(ValidationError):
            Person(name="冒名匿名", slug="anonymous", is_system_anonymous=False).save()

        # 仅允许存在一条系统匿名记录
        anon = get_or_create_anonymous_person()
        self.assertTrue(anon.is_system_anonymous)
        self.assertEqual(anon.slug, "anonymous")
        self.assertEqual(Person.objects.filter(slug="anonymous").count(), 1)

    # ------------------------------------------------------------------
    # A02: 受限作者写端校验与管理员留空语义
    # ------------------------------------------------------------------
    def test_a02_restricted_author_prefill_and_write_guards(self):
        bound_person = Person.objects.create(name="作者林一", linked_user=self.author_user)
        other_person = Person.objects.create(name="作者陈二")

        # 1. ProjectPage 预填使用 core_dev，BlogPage 使用 primary_author
        proj_draft = ProjectPage(title="预填项目", slug=f"prj-{uuid4().hex[:6]}", owner=self.author_user)
        self.portfolio_index.add_child(instance=proj_draft)
        self.assertTrue(bind_linked_person_for_page_owner(proj_draft))
        self.assertEqual(proj_draft.authors.all()[0].role, "core_dev")
        # 重复调用不追加
        self.assertFalse(bind_linked_person_for_page_owner(proj_draft))

        # 2. 受限作者提交/发布空白署名、重复人物、选系统佚名、将他人设为主要角色均被拦截
        blog_empty = BlogPage(title="空白署名草稿", slug=f"emp-{uuid4().hex[:6]}", owner=self.author_user, live=False)
        self.blog_index.add_child(instance=blog_empty)
        rev_empty = blog_empty.save_revision(user=self.author_user)
        with self.assertRaises(ValidationError):
            rev_empty.publish(user=self.author_user)

        blog_sys = BlogPage(title="误选佚名", slug=f"sys-{uuid4().hex[:6]}", owner=self.author_user, live=False)
        self.blog_index.add_child(instance=blog_sys)
        blog_sys.authors.create(person=self.anon_person, role="primary_author", sort_order=0)
        rev_sys = blog_sys.save_revision(user=self.author_user)
        with self.assertRaises(ValidationError):
            rev_sys.publish(user=self.author_user)

        blog_dup = BlogPage(title="重复人物", slug=f"dup-{uuid4().hex[:6]}", owner=self.author_user, live=False)
        self.blog_index.add_child(instance=blog_dup)
        blog_dup.authors.create(person=bound_person, role="primary_author", sort_order=0)
        blog_dup.authors.create(person=bound_person, role="co_author", sort_order=1)
        rev_dup = blog_dup.save_revision(user=self.author_user)
        with self.assertRaises(ValidationError):
            rev_dup.publish(user=self.author_user)

        blog_other_major = BlogPage(title="他人主笔", slug=f"oth-{uuid4().hex[:6]}", owner=self.author_user, live=False)
        self.blog_index.add_child(instance=blog_other_major)
        blog_other_major.authors.create(person=bound_person, role="primary_author", sort_order=0)
        blog_other_major.authors.create(person=other_person, role="primary_author", sort_order=1)
        rev_other = blog_other_major.save_revision(user=self.author_user)
        with self.assertRaises(ValidationError):
            rev_other.publish(user=self.author_user)

        # 3. 超管留空发布不修改原表 authors，自动投影归入「佚名」
        admin_bound = Person.objects.create(name="总编", linked_user=self.superuser)
        try:
            admin_post = self._publish_blog("管理员未填作者文章", authors=None, owner=self.superuser)
            self.assertEqual(admin_post.authors.count(), 0)
            doc = PublishedSearchDocument.objects.get(page_id=admin_post.id)
            sa = PublishedSearchAuthor.objects.get(document=doc)
            self.assertEqual(sa.author_slug, "anonymous")
            self.assertEqual(sa.role, "anonymous")
            self.assertEqual(doc.extra_data["author_slugs"], ["anonymous"])
            self.assertNotEqual(sa.person_id, admin_bound.pk)
        finally:
            admin_bound.linked_user = None
            admin_bound.save()

    # ------------------------------------------------------------------
    # A03 & A04: 三级署名解析、历史核准、脱离匿名与详情页链接
    # ------------------------------------------------------------------
    def test_a03_a04_attribution_tiers_republish_detach_and_detail_links(self):
        alice = Person.objects.create(name="审校爱丽丝", bio="专注系统评审")
        bob = Person.objects.create(name="主笔鲍勃", bio="专注存储引擎")

        # 1. 仅审校人员也算有效显式署名，不补「佚名」
        rev_only_post = self._publish_blog("纯审校文章", authors=[(alice, "技术审校")])
        doc_rev = PublishedSearchDocument.objects.get(page_id=rev_only_post.id)
        authors_rev = list(PublishedSearchAuthor.objects.filter(document=doc_rev))
        self.assertEqual(len(authors_rev), 1)
        self.assertEqual(authors_rev[0].author_slug, alice.slug)
        self.assertEqual(authors_rev[0].role, "reviewer")

        # 2. 无署名文章先归入「佚名」，详情页显示可点击「佚名」
        unattrib = self._publish_blog("待认领旧文", authors=None)
        client = Client()
        resp_anon = client.get(unattrib.url)
        self.assertEqual(resp_anon.status_code, 200)
        self.assertContains(resp_anon, 'href="/authors/anonymous/"')
        self.assertContains(resp_anon, "佚名")

        # 3. 历史指定 Revision 人工核准只作用于该 Revision
        LegacyAuthorAttribution.objects.create(
            page_id=unattrib.id,
            revision_id=unattrib.live_revision_id,
            person=bob,
        )
        project_page_to_discovery(unattrib)
        doc_u = PublishedSearchDocument.objects.get(page_id=unattrib.id)
        self.assertEqual(
            list(PublishedSearchAuthor.objects.filter(document=doc_u).values_list("author_slug", flat=True)),
            [bob.slug],
        )
        # 新建并发布新 Revision（仍无显式作者）不得跨 Revision 继承历史核准，回到「佚名」
        unattrib.title = "待认领旧文 v2"
        unattrib.save_revision(user=self.superuser).publish(user=self.superuser)
        unattrib.refresh_from_db()
        doc_u.refresh_from_db()
        self.assertEqual(
            list(PublishedSearchAuthor.objects.filter(document=doc_u).values_list("author_slug", flat=True)),
            ["anonymous"],
        )

        # 4. 补填真名作者重发后自动脱离「佚名」，详情页显示真名链接且不显示「佚名」
        unattrib.authors.create(person=bob, role="primary_author", sort_order=0)
        unattrib.save_revision(user=self.superuser).publish(user=self.superuser)
        unattrib.refresh_from_db()
        doc_u.refresh_from_db()
        self.assertEqual(
            list(PublishedSearchAuthor.objects.filter(document=doc_u).values_list("author_slug", flat=True)),
            [bob.slug],
        )
        resp_bob = client.get(unattrib.url)
        self.assertContains(resp_bob, f'href="/authors/{bob.slug}/"')
        self.assertNotContains(resp_bob, 'href="/authors/anonymous/"')

        # 5. 草稿编辑/预览不得污染已发布详情页或作者索引
        unattrib.authors.clear()
        unattrib.authors.create(person=alice, role="primary_author", sort_order=0)
        draft_rev = unattrib.save_revision(user=self.superuser)
        self.assertEqual(
            list(PublishedSearchAuthor.objects.filter(document=doc_u).values_list("author_slug", flat=True)),
            [bob.slug],
        )
        req_pub = RequestFactory().get(unattrib.url)
        pub_pres = build_detail_presentation(unattrib, req_pub)
        self.assertEqual([a["slug"] for a in pub_pres["authors"]], [bob.slug])

        req_prev = RequestFactory().get(unattrib.url)
        req_prev.is_preview = True
        prev_obj = draft_rev.as_object()
        prev_pres = build_detail_presentation(prev_obj, req_prev)
        self.assertEqual([a["slug"] for a in prev_pres["authors"]], [alice.slug])

    # ------------------------------------------------------------------
    # A05: /search/ 多作者 OR、同一行角色、分面与更名一致
    # ------------------------------------------------------------------
    def test_a05_search_multi_author_same_row_role_and_facets(self):
        alice = Person.objects.create(name="搜索甲", bio="A")
        bob = Person.objects.create(name="搜索乙", bio="B")
        # 合著文：搜索甲=审校, 搜索乙=主笔
        co_post = self._publish_blog(
            "合著并发文章",
            authors=[(bob, "primary_author"), (alice, "reviewer")],
            tags=["Concurrency"],
        )
        alice_only = self._publish_blog(
            "甲主笔独立文章",
            authors=[(alice, "primary_author")],
            tags=["Concurrency"],
        )

        client = Client()
        # 多选 OR 命中两篇且合著只算 1 行
        r_multi = client.get("/search/", {"author": [alice.slug, bob.slug]})
        self.assertEqual(r_multi.status_code, 200)
        self.assertContains(r_multi, co_post.title)
        self.assertContains(r_multi, alice_only.title)

        # 同一关系行角色约束：搜 alice + major 只命中 alice_only，不误命中 co_post
        r_major = client.get("/search/", {"author": alice.slug, "author_role": "major"})
        self.assertEqual(r_major.status_code, 200)
        self.assertNotContains(r_major, co_post.title)
        self.assertContains(r_major, alice_only.title)

        # 未知合法 slug 不退回全站无条件
        r_unknown = client.get("/search/", {"author": "non-existent-author-xyz"})
        self.assertEqual(r_unknown.status_code, 200)
        self.assertNotContains(r_unknown, co_post.title)

        # 非法语法拒绝 400
        r_bad = client.get("/search/", {"author": "bad slug!"})
        self.assertEqual(r_bad.status_code, 400)

        # 人物改名后，分面与胶囊立刻显示新名字（不按快照拆桶）
        alice.name = "搜索甲改名版"
        alice.save()
        r_renamed = client.get("/search/", {"author": alice.slug})
        self.assertEqual(r_renamed.status_code, 200)
        self.assertContains(r_renamed, "搜索甲改名版")

    # ------------------------------------------------------------------
    # A06 & A07: 作者个人专栏跨类型 Tab、万字正文检索、分页、422 与游标
    # ------------------------------------------------------------------
    def test_a06_a07_author_detail_tabs_long_body_and_hybrid_pagination(self):
        writer = Person.objects.create(name="万字专栏作家", bio="深耕内核与架构")
        long_body = ("普通铺垫段落。" * 1800) + " 末段核心关键词：AuroraStorageEngine2026。"
        self._publish_blog("深文一篇", authored_on=date(2026, 10, 1), authors=[(writer, "primary_author")], body_md=long_body)
        self._publish_guide("步骤指南一篇", authored_on=date(2026, 10, 2), authors=[(writer, "co_author")], body_md="指南操作步骤")
        self._publish_project("开源项目一篇", authored_on=date(2026, 10, 3), authors=[(writer, "架构设计与主研发")])

        # 同日再造 3 篇用于测试页码与双向 Keyset 游标
        for i in range(3):
            self._publish_blog(f"同日文章-{i}", authored_on=date(2026, 10, 4), authors=[(writer, "primary_author")])

        client = Client()
        base_url = f"/authors/{writer.slug}/"
        r_all = client.get(base_url)
        self.assertEqual(r_all.status_code, 200)
        self.assertContains(r_all, "博客文章")
        self.assertContains(r_all, "专题指南")
        self.assertContains(r_all, "开源项目")
        self.assertContains(r_all, "共 <strong>6</strong> 篇公开内容")

        # 角色芯片：项目默认「架构设计与主研发」归一化后进入「主笔 / 主要作者」major
        r_major = client.get(base_url, {"role": "major"})
        self.assertEqual(r_major.status_code, 200)
        self.assertContains(r_major, "开源项目一篇")
        self.assertNotContains(r_major, "步骤指南一篇")

        # 万字正文末段检索 + 仅搜标题隔离
        r_kw = client.get(base_url, {"q": "AuroraStorageEngine2026"})
        self.assertEqual(r_kw.status_code, 200)
        self.assertContains(r_kw, "深文一篇")

        r_title_only = client.get(base_url, {"q": "AuroraStorageEngine2026", "title_only": "1"})
        self.assertEqual(r_title_only.status_code, 200)
        self.assertNotContains(r_title_only, "深文一篇")

        # 日期校验 400
        self.assertEqual(client.get(base_url, {"date_from": "2026-11-10", "date_to": "2026-10-01"}).status_code, 400)

        # 浅页超窗 422
        self.assertEqual(client.get(base_url, {"page": "101"}).status_code, 422)

        # 双向游标往返零丢重
        p1 = client.get(base_url, {"page_size": "2"}, HTTP_X_LISTING_RESPONSE="fragment-v1")
        self.assertEqual(p1.status_code, 200)
        d1 = p1.json()
        self.assertTrue(d1["next_cursor"].startswith("v2:"))

        p2 = client.get(base_url, {"page_size": "2", "cursor": d1["next_cursor"]}, HTTP_X_LISTING_RESPONSE="fragment-v1")
        self.assertEqual(p2.status_code, 200)
        d2 = p2.json()
        self.assertTrue(d2["previous_cursor"].startswith("v2:"))

        p1_back = client.get(base_url, {"page_size": "2", "cursor": d2["previous_cursor"]}, HTTP_X_LISTING_RESPONSE="fragment-v1")
        self.assertEqual(p1_back.status_code, 200)
        self.assertIn("同日文章-2", p1_back.json()["html"])

        # 篡改游标返回 400；Epoch 递增触发 cursor_reset
        self.assertEqual(client.get(base_url, {"cursor": "v2:tampered-bad-token"}).status_code, 400)
        ContentDiscoveryEpoch.increment_epoch()
        p_reset = client.get(base_url, {"page_size": "2", "cursor": d1["next_cursor"]}, HTTP_X_LISTING_RESPONSE="fragment-v1")
        self.assertEqual(p_reset.status_code, 200)
        self.assertTrue(p_reset.json()["cursor_reset"])

    # ------------------------------------------------------------------
    # A08 & A09 & A11: Feed, 目录, Lookup, 404 导流, 隔离, Sitemap, 保护与对账
    # ------------------------------------------------------------------
    def test_a08_a09_a11_feeds_directory_isolation_sitemap_protection_and_reconcile(self):
        author = Person.objects.create(name="欧阳前端", email="private-should-not-feed@example.com", bio="前端架构")
        zero_person = Person.objects.create(name="从未发文隐藏作者", bio="不应暴露")
        post = self._publish_blog("公开前端实践", authored_on=date(2026, 11, 15), authors=[(author, "primary_author")])
        future_post = self._publish_blog("未来定时文", authored_on=timezone.localdate() + timedelta(days=5), authors=[(author, "primary_author")])
        restricted_post = self._publish_blog("受限内部文章", authored_on=date(2026, 11, 14), authors=[(author, "primary_author")])
        PageViewRestriction.objects.create(page=restricted_post, restriction_type=PageViewRestriction.LOGIN)

        client = Client()

        # 1. 作者专栏大厅 & 远程联想（隐藏 0 篇作者，不泄漏邮箱）
        r_dir = client.get("/authors/")
        self.assertEqual(r_dir.status_code, 200)
        self.assertContains(r_dir, author.name)
        self.assertNotContains(r_dir, zero_person.name)
        self.assertNotContains(r_dir, "private-should-not-feed@example.com")

        r_lookup = client.get("/authors/lookup/", {"q": "欧阳"})
        self.assertEqual(r_lookup.status_code, 200)
        lookup_data = r_lookup.json()
        self.assertEqual(lookup_data["items"], [{"value": author.slug, "label": author.name}])

        # 2. 零篇已知人物与不存在路径统一返回 404 + noindex，不泄漏 zero_person 姓名
        for missing_path in (f"/authors/{zero_person.slug}/", "/authors/not-found-author/"):
            r_miss = client.get(missing_path)
            self.assertEqual(r_miss.status_code, 404)
            self.assertEqual(r_miss["X-Robots-Tag"], "noindex")
            self.assertNotContains(r_miss, zero_person.name, status_code=404)

        # 3. UUID / PK 访问活跃作者 301 重定向到规范 slug；个人页排除未来排期与受限文
        r_uuid = client.get(f"/authors/{author.public_id}/?role=major")
        self.assertEqual(r_uuid.status_code, 301)
        self.assertIn(f"/authors/{author.slug}/", r_uuid["Location"])

        r_detail = client.get(f"/authors/{author.slug}/")
        self.assertEqual(r_detail.status_code, 200)
        self.assertContains(r_detail, post.title)
        self.assertNotContains(r_detail, future_post.title)
        self.assertNotContains(r_detail, restricted_post.title)

        # 4. 作者 RSS & Atom Feed（no-store，无邮箱，含 301 兼容，零篇普通作者 404）
        r_rss = client.get(f"/feeds/author/{author.slug}/")
        self.assertEqual(r_rss.status_code, 200)
        self.assertEqual(r_rss["Cache-Control"], "no-store")
        rss_body = r_rss.content.decode("utf-8")
        self.assertIn(post.title, rss_body)
        self.assertNotIn(future_post.title, rss_body)
        self.assertNotIn(restricted_post.title, rss_body)
        self.assertNotIn("private-should-not-feed@example.com", rss_body)

        r_atom = client.get(f"/feeds/atom/author/{author.public_id}/")
        self.assertEqual(r_atom.status_code, 301)
        self.assertEqual(client.get(f"/feeds/author/{zero_person.slug}/").status_code, 404)

        # 5. Sitemap 收录活跃作者并排除 zero_person
        r_sm = client.get("/sitemap.xml")
        self.assertEqual(r_sm.status_code, 200)
        sm_xml = r_sm.content.decode("utf-8")
        self.assertIn(f"/authors/{author.slug}/", sm_xml)
        self.assertNotIn(f"/authors/{zero_person.slug}/", sm_xml)

        # 6. 系统内置「佚名」受 ORM / QuerySet pre_delete 保护，禁止删除
        with self.assertRaises(PermissionDenied):
            self.anon_person.delete()
        from django.db import transaction
        with transaction.atomic():
            with self.assertRaises(PermissionDenied):
                Person.objects.filter(slug="anonymous").delete()
            transaction.set_rollback(True)

        # 7. 后台 Snippet 删除被阻断，Chooser 列表不出现系统「佚名」
        admin_client = Client()
        admin_client.force_login(self.superuser)
        del_url = reverse("wagtailsnippets_people_person:delete", args=[self.anon_person.pk])
        self.assertEqual(admin_client.post(del_url).status_code, 302)
        self.assertEqual(admin_client.post(del_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 403)
        self.assertTrue(Person.objects.filter(slug="anonymous", is_system_anonymous=True).exists())
        chooser_url = reverse("wagtailsnippetchoosers_people_person:choose")
        r_ch = admin_client.get(chooser_url)
        self.assertEqual(r_ch.status_code, 200)
        ch_html = r_ch.content.decode("utf-8")
        self.assertNotIn(f">{self.anon_person.name}<", ch_html)

        # 8. reconcile_author_projection 幂等补齐被清空的作者投影行（无需 Mongo 水合）
        doc = PublishedSearchDocument.objects.get(page_id=post.id)
        PublishedSearchAuthor.objects.filter(document=doc).delete()
        out_dry = StringIO()
        call_command("reconcile_author_projection", stdout=out_dry)
        dry_res = json.loads(out_dry.getvalue())
        self.assertGreaterEqual(dry_res["json_row_mismatches"], 1)

        out_apply = StringIO()
        call_command("reconcile_author_projection", "--apply", stdout=out_apply)
        apply_res = json.loads(out_apply.getvalue())
        self.assertGreaterEqual(apply_res["updated_authors_only"], 1)
        self.assertEqual(PublishedSearchAuthor.objects.filter(document=doc).count(), 1)

        # 9. 回收页面后立即从作者专栏退出
        RecycleService.recycle_page_or_subtree(post, actor=self.superuser)
        self.assertEqual(client.get(f"/authors/{author.slug}/").status_code, 404)

    # ------------------------------------------------------------------
    # Terra 反例加固回归测试：跨站点隔离、XSS、陈旧投影/Outbox、审批链、缺投影细节、功能开关
    # ------------------------------------------------------------------
    def test_terra_counterexamples_hardening(self):
        from content_lifecycle.forms import CustomBasePageAdminForm
        from discovery.feeds import public_feed_view
        from discovery.projection import recycle_page_discovery, unpublish_page_discovery

        long_bio_author = Person.objects.create(
            name="长简介专家",
            bio=("前缀铺垫文字" * 30) + " 核心长简介关键字KernelBioToken99",
        )
        author_b = Person.objects.create(name="作者乙", bio="B")

        # 1. 全文简介搜索（非仅 110 字截断）
        p_bio = self._publish_blog("长简介测试文", authors=[(long_bio_author, "primary_author")])
        client = Client()
        r_bio_dir = client.get("/authors/", {"q": "KernelBioToken99"})
        self.assertEqual(r_bio_dir.status_code, 200)
        self.assertContains(r_bio_dir, long_bio_author.name)

        # 2. AJAX 400 错误绝不反射恶意 HTML
        r_xss = client.get(
            f"/authors/{long_bio_author.slug}/",
            {"type": "<img src=x onerror=alert(1)>"},
            HTTP_X_LISTING_RESPONSE="fragment-v1",
        )
        self.assertEqual(r_xss.status_code, 400)
        self.assertNotIn("<img", r_xss.json()["html"])
        self.assertNotIn("onerror", r_xss.json()["html"])

        # 3. 缺投影时公开详情从 live_revision.content 读取，绝不串入未保存草稿 page.authors
        p_bio.authors.clear()
        p_bio.authors.create(person=author_b, role="primary_author", sort_order=0)
        PublishedSearchDocument.objects.filter(page_id=p_bio.id).delete()
        req_pub = RequestFactory().get(p_bio.url)
        pres = build_detail_presentation(p_bio, req_pub)
        self.assertEqual([a["slug"] for a in pres["authors"]], [long_bio_author.slug])
        # 还原投影供后续验证
        project_page_to_discovery(p_bio)

        # 4. Feed 严格核对 live_revision_id：投影滞后时甲的 Feed 绝不输出旧正式版本
        PublishedSearchDocument.objects.filter(page_id=p_bio.id).update(effective_revision_id=99999999)
        req_feed = RequestFactory().get(f"/feeds/author/{long_bio_author.slug}/")
        with self.assertRaises(Exception):
            public_feed_view(req_feed, dimension="author", slug=long_bio_author.slug)
        project_page_to_discovery(p_bio)

        # 5. 陈旧下架/回收信号绝不把当前仍 live 的页面置为下架/回收
        unpublish_page_discovery(p_bio.id)
        recycle_page_discovery(p_bio.id)
        doc_live = PublishedSearchDocument.objects.get(page_id=p_bio.id)
        self.assertTrue(doc_live.is_live)
        self.assertFalse(doc_live.is_recycled)

        # 6. 双站点隔离：站点 B 的文章与作者绝不泄漏到站点 A 的 /search/?author= 与 /authors/
        root = Page.get_first_root_node()
        home_b = HomePage(title="SiteB Home", slug=f"home-b-{uuid4().hex[:6]}")
        root.add_child(instance=home_b)
        home_b.save_revision().publish()
        site_b = Site.objects.create(hostname="siteb.test", port=80, root_page=home_b, site_name="SiteB")
        blog_idx_b = BlogIndexPage(title="SiteB Blog", slug=f"blog-b-{uuid4().hex[:6]}")
        home_b.add_child(instance=blog_idx_b)
        blog_idx_b.save_revision().publish()
        site_b_author = Person.objects.create(name="仅限B站作者")
        post_b = self._publish_blog("B站专有文章", authors=[(site_b_author, "primary_author")], parent=blog_idx_b)

        Site.clear_site_root_paths_cache()
        r_site_a_search = client.get("/search/", {"author": site_b_author.slug})
        self.assertEqual(r_site_a_search.status_code, 200)
        self.assertNotContains(r_site_a_search, post_b.title)
        self.assertEqual(client.get(f"/authors/{site_b_author.slug}/").status_code, 404)
        self.assertEqual(client.get("/authors/lookup/", {"q": site_b_author.name}).json()["items"], [])

        # 访问 B 站 Host 时可以正常看到 site_b_author
        r_site_b_detail = client.get(f"/authors/{site_b_author.slug}/", HTTP_HOST="siteb.test")
        self.assertEqual(r_site_b_detail.status_code, 200)
        self.assertContains(r_site_b_detail, post_b.title)
        site_b.delete()
        Site.clear_site_root_paths_cache()

        # 7. 超管表单禁止选入系统「佚名」或重复同一人物；管理员审批受限作者空白版本仍被拦截
        form_cls = BlogPage.get_edit_handler().get_form_class()
        bad_super_form = form_cls(
            data={
                "title": "超管误选佚名",
                "slug": p_bio.slug,
                "authored_on": "2026-11-10",
                "intro-count": "0",
                "body-count": "0",
                "gallery_images-TOTAL_FORMS": "0",
                "gallery_images-INITIAL_FORMS": "0",
                "gallery_images-MIN_NUM_FORMS": "0",
                "gallery_images-MAX_NUM_FORMS": "1000",
                "authors-TOTAL_FORMS": "1",
                "authors-INITIAL_FORMS": "0",
                "authors-MIN_NUM_FORMS": "0",
                "authors-MAX_NUM_FORMS": "1000",
                "authors-0-person": str(self.anon_person.id),
                "authors-0-role": "primary_author",
                "authors-0-ORDER": "0",
                "authors-0-DELETE": "",
            },
            instance=p_bio,
            parent_page=self.blog_index,
            for_user=self.superuser,
        )
        self.assertFalse(bad_super_form.is_valid())

        Person.objects.filter(linked_user=self.author_user).delete()
        Person.objects.create(name="受限作者真名", linked_user=self.author_user)
        tampered_page = BlogPage(title="待审批空署名", slug=f"appr-{uuid4().hex[:6]}", owner=self.author_user, live=False)
        self.blog_index.add_child(instance=tampered_page)
        tampered_rev = tampered_page.save_revision(user=self.author_user)
        with self.assertRaises(ValidationError):
            tampered_rev.publish(user=self.superuser)

        # 8. 统一开关关闭时回退保护
        with override_settings(AUTHOR_DISCOVERY_ENABLED=False):
            self.assertEqual(client.get("/authors/").status_code, 404)
            self.assertEqual(client.get(f"/authors/{long_bio_author.slug}/").status_code, 404)
            self.assertEqual(client.get(f"/feeds/author/{long_bio_author.slug}/").status_code, 404)
            sm_off = client.get("/sitemap.xml").content.decode("utf-8")
            self.assertNotIn(f"/authors/{long_bio_author.slug}/", sm_off)
            pres_off = build_detail_presentation(p_bio, RequestFactory().get(p_bio.url))
            self.assertEqual(pres_off["authors"][0]["url"], "")

    def test_person_snippet_admin_edit_and_add_views_work_with_anonymous_guard(self):
        """回归：PersonSnippetViewSet 通过 ObjectList(base_form_class=PersonAdminForm) 正常打开新建/编辑页并保护系统佚名"""
        admin_client = Client()
        admin_client.force_login(self.superuser)

        def _draftail_payload(
            text: str,
            *,
            bold_end: int = 0,
            underline_slice: tuple[int, int] | None = None,
            link_slice: tuple[int, int, str] | None = None,
        ) -> str:
            styles = []
            if bold_end > 0:
                styles.append({"offset": 0, "length": bold_end, "style": "BOLD"})
            if underline_slice:
                styles.append({"offset": underline_slice[0], "length": underline_slice[1], "style": "UNDERLINE"})
            entity_ranges = []
            entity_map = {}
            if link_slice:
                entity_ranges.append({"key": 0, "offset": link_slice[0], "length": link_slice[1]})
                entity_map["0"] = {"mutability": "MUTABLE", "type": "LINK", "data": {"url": link_slice[2]}}
            return json.dumps({
                "blocks": [{
                    "key": "bio1",
                    "text": text,
                    "type": "unstyled",
                    "depth": 0,
                    "inlineStyleRanges": styles,
                    "entityRanges": entity_ranges,
                    "data": {},
                }],
                "entityMap": entity_map,
            })

        # 1. 列表页、新建页、普通人物编辑页与系统佚名编辑页全部返回 200
        self.assertEqual(admin_client.get("/admin/snippets/people/person/").status_code, 200)
        self.assertEqual(admin_client.get("/admin/snippets/people/person/add/").status_code, 200)

        normal_person = Person.objects.create(name="后台编辑测试作者", bio="旧简介")
        edit_url = f"/admin/snippets/people/person/edit/{normal_person.id}/"
        resp_edit = admin_client.get(edit_url)
        self.assertEqual(resp_edit.status_code, 200)
        edit_html = resp_edit.content.decode("utf-8")
        self.assertIn("后台编辑测试作者", edit_html)
        self.assertIn("data-draftail-input", edit_html)
        self.assertIn("UNDERLINE", edit_html)

        # 2. 未绑定后台账号时勾选 show_public_comments 会触发模型校验拦截（200 返回表单错误）
        invalid_post = admin_client.post(
            edit_url,
            {
                "name": "后台编辑测试作者",
                "bio": _draftail_payload("尝试非法开启评论聚合"),
                "email": "editor@example.com",
                "show_public_comments": "on",
            },
        )
        self.assertEqual(invalid_post.status_code, 200)
        self.assertIn("show_public_comments", invalid_post.context["form"].errors)

        # 3. 保存含加粗、下划线、链接的富文本公开简介生效，并在作者专栏与目录摘要安全渲染
        rich_bio_json = _draftail_payload(
            "核心作者，专注架构与主页",
            bold_end=4,
            underline_slice=(5, 4),
            link_slice=(10, 2, "https://example.com/author"),
        )
        post_resp = admin_client.post(
            edit_url,
            {
                "name": "后台编辑测试作者",
                "bio": rich_bio_json,
                "email": "editor@example.com",
            },
        )
        self.assertEqual(post_resp.status_code, 302)
        normal_person.refresh_from_db()
        self.assertIn("<b>核心作者</b>", normal_person.bio)
        self.assertIn("<u>专注架构</u>", normal_person.bio)
        self.assertIn('href="https://example.com/author"', normal_person.bio)
        self.assertEqual(normal_person.email, "editor@example.com")

        # 发布一篇文章使该作者成为活跃作者，验证前台专栏富文本渲染、XSS 过滤与目录纯文本摘要
        self._publish_blog("富文本简介测试文章", authors=[(normal_person, "primary_author")])
        pub_client = Client()
        detail_resp = pub_client.get(f"/authors/{normal_person.slug}/")
        self.assertEqual(detail_resp.status_code, 200)
        detail_html = detail_resp.content.decode("utf-8")
        self.assertIn('<div class="author-profile-bio rich-text">', detail_html)
        self.assertIn("<b>核心作者</b>", detail_html)
        self.assertIn("<u>专注架构</u>", detail_html)
        self.assertIn('href="https://example.com/author"', detail_html)
        self.assertIn('rel="noopener noreferrer"', detail_html)

        dir_resp = pub_client.get("/authors/", {"q": "专注架构"})
        self.assertEqual(dir_resp.status_code, 200)
        dir_html = dir_resp.content.decode("utf-8")
        self.assertIn("核心作者，专注架构与主页", dir_html)
        self.assertNotIn("<b>核心作者</b>", dir_html)

        # XSS 防护：直接写入恶意脚本与伪协议链接，渲染前被 nh3 彻底剥离
        normal_person.bio = '<p>安全文本<script>alert(1)</script><a href="javascript:alert(2)">坏链接</a></p>'
        normal_person.save()
        xss_resp = pub_client.get(f"/authors/{normal_person.slug}/")
        xss_html = xss_resp.content.decode("utf-8")
        self.assertIn("安全文本", xss_html)
        self.assertNotIn("alert(1)", xss_html)
        self.assertNotIn("javascript:alert", xss_html)

        # 4. 系统内置「佚名」名片编辑页可正常打开且 linked_user / email / show_public_comments 被禁用且无法写入
        anon_edit_url = f"/admin/snippets/people/person/edit/{self.anon_person.id}/"
        anon_resp = admin_client.get(anon_edit_url)
        self.assertEqual(anon_resp.status_code, 200)
        anon_post = admin_client.post(
            anon_edit_url,
            {
                "name": self.anon_person.name,
                "bio": _draftail_payload("系统默认署名"),
                "email": "hack@example.com",
                "linked_user": str(self.superuser.id),
                "show_public_comments": "on",
            },
        )
        self.assertEqual(anon_post.status_code, 302)
        self.anon_person.refresh_from_db()
        self.assertIsNone(self.anon_person.linked_user)
        self.assertEqual(self.anon_person.email, "")
        self.assertFalse(self.anon_person.show_public_comments)

        # 5. 后台硬删入口保持阻断（浏览器 302 回退、AJAX 403，且记录不被删除）
        del_url = f"/admin/snippets/people/person/delete/{normal_person.id}/"
        self.assertEqual(admin_client.post(del_url).status_code, 302)
        self.assertEqual(admin_client.post(del_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 403)
        self.assertTrue(Person.objects.filter(pk=normal_person.id).exists())
