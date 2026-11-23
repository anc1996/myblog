"""
tests.test_analytics_phase1
全站数据分析系统 第 1 期综合测试：
1. TypeAdapter 多页面能力契约与模板标记 (#article-start / #article-end / analytics-descriptor)；
2. 公开准入过滤（预览、草稿、别名、回收站、祖先私有、跨站、Bot/Prefetch 拦截）；
3. Redis Lua 原子采集（30s PV 去重、seq 防重放、阅读深度、短正文、Redis 故障降级 202）；
4. 随时可补跑幂等结算（AnalyticsBatchReceipt 去重、多版本 SUNION 日 UV、全站 UV=1、缺测标 missing、过期清理）；
5. Purge 彻底销毁联删 SQL + 清理 Redis + 迟到批次防复活；
6. D5-A 安全搜索词白名单与来源脱敏；
7. 后台权限隔离（is_staff 拦截、受限作者只能看自己 page_id、全站大盘与抽屉渲染）。
"""

from datetime import date, timedelta
import json
import os
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django

django.setup()

from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.template import Context, Template
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from wagtail.models import Page, PageViewRestriction, Site

from analytics.collector import _get_redis_client, _resolve_prefix, record_analytics_events
from analytics.models import AnalyticsBatchReceipt, PageAnalyticsDaily, PageAnalyticsTotal, ScopeAnalyticsDaily
from analytics.permissions import can_view_page_analytics, can_view_site_analytics, filter_accessible_pages_for_analytics
from analytics.services import get_author_pages_analytics_list, get_page_analytics_detail, get_site_dashboard_data
from analytics.settlement import cleanup_expired_analytics, seal_daily_uv, settle_pv_and_reading_batches
from analytics.tokens import match_safe_search_term, parse_referrer_url, sign_descriptor_token, visitor_hmac
from blog.models import BlogIndexPage, BlogPage
from content_lifecycle.permissions import ensure_author_role_infrastructure
from content_lifecycle.recycle import RecycleService
from discovery.contracts import AnalyticsCapabilities, EntryAnalyticsSpec
from discovery.registry import get_analytics_capabilities, get_entry_analytics, list_analytics_adapters, list_entry_analytics
from guides.models import GuideIndexPage, GuidePage, GuideTopic
from home.models import HomePage
from identity.models import User
from portfolio.models import PortfolioIndexPage, ProjectPage
from taxonomy.models import TagIndexPage


def _cleanup_analytics_redis():
    client = _get_redis_client(write=True)
    if client is None:
        return
    prefix = _resolve_prefix()
    keys = list(client.scan_iter(match=f"{prefix}:analytics:*"))
    if keys:
        client.delete(*keys)


def _setup_site_and_pages():
    root = Page.get_first_root_node()
    home = HomePage.objects.order_by("path").first()
    if home is None:
        home = HomePage(title=f"Home-{uuid4().hex[:6]}", slug=f"home-{uuid4().hex[:6]}")
        root.add_child(instance=home)
        home.save_revision().publish()

    site = Site.objects.filter(is_default_site=True).order_by("id").first() or Site.objects.order_by("id").first()
    if site is None:
        site = Site.objects.create(
            hostname="testserver",
            port=80,
            root_page=home,
            is_default_site=True,
            site_name="Test Site",
        )
    else:
        site.hostname = "testserver"
        site.root_page = home
        site.is_default_site = True
        site.save()

    blog_idx = BlogIndexPage(title="Tech Blog", slug=f"blog-{uuid4().hex[:6]}")
    home.add_child(instance=blog_idx)
    blog_idx.save_revision().publish()

    guide_idx = GuideIndexPage(title="Guides", slug=f"guides-{uuid4().hex[:6]}")
    home.add_child(instance=guide_idx)
    guide_idx.save_revision().publish()

    port_idx = PortfolioIndexPage(title="Portfolio", slug=f"portfolio-{uuid4().hex[:6]}")
    home.add_child(instance=port_idx)
    port_idx.save_revision().publish()

    tag_idx = TagIndexPage(title="Tags", slug=f"tags-{uuid4().hex[:6]}")
    home.add_child(instance=tag_idx)
    tag_idx.save_revision().publish()

    return site, home, blog_idx, guide_idx, port_idx, tag_idx


class AnalyticsContractsAndPrivacyTests(TestCase):
    """契约校验、脱敏与 D5-A 安全搜索词测试。"""

    def test_capabilities_validation_and_registry(self):
        with self.assertRaises(ValueError):
            AnalyticsCapabilities(kind="invalid")
        with self.assertRaises(ValueError):
            AnalyticsCapabilities(kind="entry", entry_kind="home", rating_enabled=True)
        with self.assertRaises(ValueError):
            AnalyticsCapabilities(kind="entry", entry_kind="home", rank_eligible=True)
        with self.assertRaises(ValueError):
            AnalyticsCapabilities(kind="entry", entry_kind="home", curation_eligible=True)
        with self.assertRaises(ValueError):
            EntryAnalyticsSpec(entry_kind="")
        with self.assertRaises(ValueError):
            EntryAnalyticsSpec(
                entry_kind="bad",
                analytics_capabilities=AnalyticsCapabilities(kind="detail"),
            )

        adapters = {a.type_key: a for a in list_analytics_adapters()}
        for tk in ("blog_page", "guide", "project", "blog_index", "guide_index", "portfolio_index", "tag_index"):
            self.assertIn(tk, adapters)
            self.assertIsNotNone(get_analytics_capabilities(tk))

        entries = {e.entry_kind for e in list_entry_analytics()}
        self.assertTrue({"home", "author_detail", "search"}.issubset(entries))
        self.assertIsNotNone(get_entry_analytics("home"))

    def test_referrer_sanitization_and_daily_visitor_hmac(self):
        self.assertEqual(parse_referrer_url("", "example.com"), ("direct", "direct"))
        self.assertEqual(
            parse_referrer_url("https://example.com/search/?query=secret_pii&page=2", "example.com"),
            ("internal", "search"),
        )
        self.assertEqual(
            parse_referrer_url("https://sub.google.com/search?q=my+secret+email", "example.com"),
            ("external", "google.com"),
        )
        self.assertEqual(
            parse_referrer_url("https://user:pass@news.bbc.co.uk:8443/world/123?token=abc#frag", "example.com"),
            ("external", "bbc.co.uk"),
        )

        bid = uuid4().hex
        d1 = date(2026, 11, 22)
        d2 = date(2026, 11, 23)
        h1 = visitor_hmac(1, d1, bid)
        h2 = visitor_hmac(1, d2, bid)
        h3 = visitor_hmac(2, d1, bid)
        self.assertNotEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertEqual(h1, visitor_hmac(1, d1, bid))

    @override_settings(ANALYTICS_SAFE_SEARCH_TERMS=("PostgreSQL", "Wagtail 8"))
    def test_d5_safe_search_term_filtering(self):
        _site, _home, _, guide_idx, _, _ = _setup_site_and_pages()
        topic = GuideTopic.objects.create(name="分布式事务", slug=f"dist-tx-{uuid4().hex[:6]}")
        guide = GuidePage(
            title="Guide A",
            slug=f"guide-a-{uuid4().hex[:6]}",
            topic=topic,
            body=[("step", {"step_number": 1, "title": "Step 1", "content": "<p>hello</p>"})],
        )
        guide_idx.add_child(instance=guide)
        guide.save_revision().publish()

        self.assertEqual(match_safe_search_term("postgresql"), ("PostgreSQL", True))
        self.assertEqual(match_safe_search_term("分布式事务"), ("分布式事务", True))
        self.assertEqual(match_safe_search_term("alice@example.com"), ("unknown", False))
        self.assertEqual(match_safe_search_term("192.168.20.5"), ("unknown", False))
        self.assertEqual(match_safe_search_term("13800138000"), ("unknown", False))
        self.assertEqual(match_safe_search_term("https://evil.com/token=123"), ("unknown", False))
        self.assertEqual(match_safe_search_term("sk-1234567890abcdef"), ("unknown", False))
        self.assertEqual(match_safe_search_term("某不知名冷门词条"), ("unknown", False))


class AnalyticsCollectionAndSettlementTests(TestCase):
    """采集准入、Redis Lua 原子状态机、幂等结算、SUNION UV、缺失标记与 Purge 防复活测试。"""

    def setUp(self):
        super().setUp()
        _cleanup_analytics_redis()
        self.site, self.home, self.blog_idx, self.guide_idx, self.port_idx, self.tag_idx = _setup_site_and_pages()
        self.blog_page = BlogPage(
            title="Detail Blog",
            slug=f"detail-blog-{uuid4().hex[:6]}",
            intro=[("markdown", {"markdown": "Intro text"})],
            body=[("markdown", {"markdown": "## Section One\n\nBody content here."})],
        )
        self.blog_idx.add_child(instance=self.blog_page)
        self.blog_page.save_revision().publish()
        self.blog_page.refresh_from_db()

        topic = GuideTopic.objects.create(name="分析指南专题", slug=f"ana-topic-{uuid4().hex[:6]}")
        self.guide_page = GuidePage(
            title="Detail Guide",
            slug=f"detail-guide-{uuid4().hex[:6]}",
            topic=topic,
            body=[("step", {"step_number": 1, "title": "Init", "content": "<p>Step body</p>"})],
        )
        self.guide_idx.add_child(instance=self.guide_page)
        self.guide_page.save_revision().publish()
        self.guide_page.refresh_from_db()

        self.project_page = ProjectPage(
            title="Detail Project",
            slug=f"detail-proj-{uuid4().hex[:6]}",
            body=[("markdown", {"markdown": "## Architecture\n\nProject details."})],
        )
        self.port_idx.add_child(instance=self.project_page)
        self.project_page.save_revision().publish()
        self.project_page.refresh_from_db()

        self.client = Client(HTTP_HOST="testserver")

    def tearDown(self):
        _cleanup_analytics_redis()
        super().tearDown()

    def test_detail_templates_include_markers_and_no_sql_writes_on_get(self):
        for page in (self.blog_page, self.guide_page, self.project_page):
            resp = self.client.get(page.url)
            self.assertEqual(resp.status_code, 200)
            html = resp.content.decode("utf-8")
            self.assertIn('id="article-start"', html)
            self.assertIn('id="article-end"', html)
            self.assertIn('name="analytics-descriptor"', html)
            self.assertIn("analytics-tracker.js", html)

        self.assertEqual(PageAnalyticsDaily.objects.count(), 0)
        self.assertEqual(PageAnalyticsTotal.objects.count(), 0)

    def test_preview_draft_alias_recycled_and_blocked_ancestor_excluded(self):
        draft_page = BlogPage(
            title="Draft Only",
            slug=f"draft-only-{uuid4().hex[:6]}",
            live=False,
            body=[("markdown", {"markdown": "draft"})],
        )
        self.blog_idx.add_child(instance=draft_page)
        draft_page.save_revision()
        rf = RequestFactory(HTTP_HOST="testserver")
        req = rf.get("/")
        rendered = Template("{% load analytics_tags %}{% analytics_page_meta page %}").render(
            Context({"request": req, "page": draft_page})
        )
        self.assertEqual(rendered, "")

        req_prev = rf.get("/")
        req_prev.is_preview = True
        rendered_prev = Template("{% load analytics_tags %}{% analytics_page_meta page %}").render(
            Context({"request": req_prev, "page": self.blog_page})
        )
        self.assertEqual(rendered_prev, "")

        PageViewRestriction.objects.create(
            page=self.blog_idx,
            restriction_type=PageViewRestriction.LOGIN,
        )
        desc = sign_descriptor_token({
            "v": 1,
            "kind": "detail",
            "site_id": self.site.id,
            "page_id": self.blog_page.id,
            "live_revision_id": self.blog_page.live_revision_id,
            "type_key": "blog_page",
            "entry_kind": "",
            "entry_key": "",
            "body_markers": ["article-start", "article-end"],
        })
        ctx_resp = self.client.post(
            "/api/v1/analytics/context/",
            data=json.dumps({"descriptor": desc}),
            content_type="application/json",
        )
        self.assertEqual(ctx_resp.status_code, 200)
        self.assertFalse(ctx_resp.json()["enabled"])
        self.assertEqual(ctx_resp.json()["reason"], "blocked")

        PageViewRestriction.objects.filter(page=self.blog_idx).delete()
        bot_resp = self.client.post(
            "/api/v1/analytics/context/",
            data=json.dumps({"descriptor": desc}),
            content_type="application/json",
            HTTP_USER_AGENT="Googlebot/2.1 (+http://www.google.com/bot.html)",
        )
        self.assertFalse(bot_resp.json()["enabled"])
        self.assertEqual(bot_resp.json()["reason"], "bot_or_prefetch")

    def test_end_to_end_redis_lua_dedup_reading_and_settlement(self):
        html = self.client.get(self.blog_page.url).content.decode("utf-8")
        marker = 'name="analytics-descriptor" content="'
        idx = html.index(marker) + len(marker)
        descriptor = html[idx:html.index('"', idx)]

        ctx1 = self.client.post(
            "/api/v1/analytics/context/",
            data=json.dumps({"descriptor": descriptor, "referrer_host": "google.com", "referrer_path": "/search?q=secret"}),
            content_type="application/json",
        )
        self.assertEqual(ctx1.status_code, 200)
        data1 = ctx1.json()
        self.assertTrue(data1["enabled"])
        vt1 = data1["visit_token"]

        r1 = self.client.post(
            "/api/v1/analytics/events/",
            data=json.dumps({"visit_token": vt1, "seq": 1, "event": "start", "active_seconds": 0, "max_scroll_pct": 10}),
            content_type="application/json",
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["processed"], 1)

        r_dup = self.client.post(
            "/api/v1/analytics/events/",
            data=json.dumps({"visit_token": vt1, "seq": 1, "event": "heartbeat", "active_seconds": 999}),
            content_type="application/json",
        )
        self.assertEqual(r_dup.json()["duplicates"], 1)

        r2 = self.client.post(
            "/api/v1/analytics/events/",
            data=json.dumps({
                "visit_token": vt1,
                "seq": 2,
                "event": "heartbeat",
                "active_seconds": 20,
                "max_scroll_pct": 65,
                "reached_end": False,
            }),
            content_type="application/json",
        )
        self.assertEqual(r2.json()["processed"], 1)

        r3 = self.client.post(
            "/api/v1/analytics/events/",
            data=json.dumps({
                "visit_token": vt1,
                "seq": 3,
                "event": "end",
                "active_seconds": 30,
                "max_scroll_pct": 100,
                "reached_end": True,
            }),
            content_type="text/plain",
        )
        self.assertEqual(r3.status_code, 200)

        ctx2 = self.client.post(
            "/api/v1/analytics/context/",
            data=json.dumps({"descriptor": descriptor}),
            content_type="application/json",
        )
        vt2 = ctx2.json()["visit_token"]
        self.client.post(
            "/api/v1/analytics/events/",
            data=json.dumps({"visit_token": vt2, "seq": 1, "event": "start", "active_seconds": 5, "max_scroll_pct": 10}),
            content_type="application/json",
        )
        self.client.post(
            "/api/v1/analytics/events/",
            data=json.dumps({"visit_token": vt2, "seq": 2, "event": "end", "active_seconds": 4, "max_scroll_pct": 15, "reached_end": False}),
            content_type="application/json",
        )

        settle_res = settle_pv_and_reading_batches()
        self.assertFalse(settle_res["degraded"])
        self.assertGreaterEqual(settle_res["settled_batches"], 2)

        daily = PageAnalyticsDaily.objects.get(
            site=self.site,
            page=self.blog_page,
            stat_date=timezone.localdate(),
            live_revision_id=self.blog_page.live_revision_id,
        )
        self.assertEqual(daily.pv, 1)
        self.assertEqual(daily.organic_pv, 1)
        self.assertEqual(daily.measurable_views, 2)
        self.assertEqual(daily.valid_views, 1)
        self.assertEqual(daily.completed_views, 1)
        self.assertEqual(daily.short_bounce_views, 1)
        self.assertEqual(daily.total_active_seconds, 35)
        self.assertEqual(daily.conversions_json.get("scroll_25"), 1)
        self.assertEqual(daily.conversions_json.get("scroll_50"), 1)
        self.assertEqual(daily.conversions_json.get("scroll_90"), 1)
        self.assertEqual(daily.conversions_json.get("end_reached"), 1)
        self.assertEqual(daily.conversions_json.get("src_external_search"), 1)

        total = PageAnalyticsTotal.objects.get(site=self.site, page=self.blog_page)
        self.assertEqual(total.total_pv, 1)
        self.assertEqual(total.total_valid_views, 1)
        self.assertEqual(total.total_completed_views, 1)

        settle_pv_and_reading_batches()
        total.refresh_from_db()
        self.assertEqual(total.total_pv, 1)

    def test_redis_unavailable_graceful_degradation(self):
        desc = sign_descriptor_token({
            "v": 1,
            "kind": "detail",
            "site_id": self.site.id,
            "page_id": self.blog_page.id,
            "live_revision_id": self.blog_page.live_revision_id,
            "type_key": "blog_page",
            "entry_kind": "",
            "entry_key": "",
            "body_markers": ["article-start", "article-end"],
        })
        vt = self.client.post(
            "/api/v1/analytics/context/",
            data=json.dumps({"descriptor": desc}),
            content_type="application/json",
        ).json()["visit_token"]

        with patch("analytics.collector._get_redis_client", return_value=None):
            resp = self.client.post(
                "/api/v1/analytics/events/",
                data=json.dumps({"visit_token": vt, "seq": 1, "event": "start"}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json(), {"accepted": False, "degraded": True})
        self.assertEqual(PageAnalyticsDaily.objects.count(), 0)

    def test_cross_version_sunion_uv_site_uv_missing_day_and_wsl2_catchup(self):
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        two_days_ago = today - timedelta(days=2)

        browser_id = uuid4().hex
        v_hmac = visitor_hmac(self.site.id, yesterday, browser_id)
        v1 = self.blog_page.live_revision_id
        self.blog_page.title = "Detail Blog V2"
        self.blog_page.save_revision().publish()
        self.blog_page.refresh_from_db()
        v2 = self.blog_page.live_revision_id
        self.assertNotEqual(v1, v2)

        for pid, rev_id, tk in (
            (self.blog_page.id, v1, "blog_page"),
            (self.blog_page.id, v2, "blog_page"),
            (self.guide_page.id, self.guide_page.live_revision_id, "guide"),
            (self.project_page.id, self.project_page.live_revision_id, "project"),
        ):
            claims = {
                "v": 1,
                "visit_id": uuid4().hex,
                "visitor_hmac": v_hmac,
                "site_id": self.site.id,
                "stat_date": yesterday.isoformat(),
                "kind": "detail",
                "page_id": pid,
                "live_revision_id": rev_id,
                "type_key": tk,
                "column_page_id": None,
                "entry_kind": "",
                "entry_key": "",
                "referrer_type": "direct",
                "referrer_source": "direct",
                "is_organic": True,
                "safe_search_term": "",
                "search_matched": False,
                "search_zero_result": False,
                "has_body_markers": True,
                "allowed_conversions": [],
            }
            res = record_analytics_events(claims, [{"seq": 1, "event": "start"}])
            self.assertTrue(res["accepted"])

        settle_pv_and_reading_batches()
        seal_res = seal_daily_uv(days_back=2)
        self.assertFalse(seal_res["degraded"])
        self.assertIn(f"{self.site.id}:{yesterday.isoformat()}", seal_res["sealed_dates"])
        self.assertIn(f"{self.site.id}:{two_days_ago.isoformat()}", seal_res["missing_dates"])

        blog_total = PageAnalyticsTotal.objects.get(site=self.site, page=self.blog_page)
        self.assertEqual(blog_total.total_visitor_days, 1)

        site_scope_y = ScopeAnalyticsDaily.objects.get(
            site=self.site, stat_date=yesterday, scope_kind="site", scope_key="all"
        )
        self.assertEqual(site_scope_y.daily_uv, 1)
        self.assertEqual(site_scope_y.health_status, "normal")

        missing_scope = ScopeAnalyticsDaily.objects.get(
            site=self.site, stat_date=two_days_ago, scope_kind="site", scope_key="all"
        )
        self.assertEqual(missing_scope.health_status, "missing")

    def test_receipt_idempotency_and_purge_anti_resurrection(self):
        v_hmac = visitor_hmac(self.site.id, timezone.localdate(), uuid4().hex)
        claims = {
            "v": 1,
            "visit_id": uuid4().hex,
            "visitor_hmac": v_hmac,
            "site_id": self.site.id,
            "stat_date": timezone.localdate().isoformat(),
            "kind": "detail",
            "page_id": self.blog_page.id,
            "live_revision_id": self.blog_page.live_revision_id,
            "type_key": "blog_page",
            "column_page_id": None,
            "entry_kind": "",
            "entry_key": "",
            "referrer_type": "direct",
            "referrer_source": "direct",
            "is_organic": True,
            "safe_search_term": "",
            "search_matched": False,
            "search_zero_result": False,
            "has_body_markers": True,
            "allowed_conversions": [],
        }
        record_analytics_events(claims, [{"seq": 1, "event": "start"}])
        settle_pv_and_reading_batches()
        self.assertEqual(PageAnalyticsTotal.objects.get(site=self.site, page=self.blog_page).total_pv, 1)

        claims["visit_id"] = uuid4().hex
        claims["visitor_hmac"] = visitor_hmac(self.site.id, timezone.localdate(), uuid4().hex)
        record_analytics_events(claims, [{"seq": 1, "event": "start"}])

        superuser = User.objects.create_superuser("root_ana", "root@example.com", "Admin123456!")
        batch = RecycleService.recycle_page_or_subtree(self.blog_page, actor=superuser)
        RecycleService.purge_page_or_subtree(self.blog_page.id, actor=superuser, batch_id=batch.batch_id)

        self.assertFalse(PageAnalyticsDaily.objects.filter(page_id=self.blog_page.id).exists())
        self.assertFalse(PageAnalyticsTotal.objects.filter(page_id=self.blog_page.id).exists())
        settle_pv_and_reading_batches()
        self.assertFalse(PageAnalyticsDaily.objects.filter(page_id=self.blog_page.id).exists())
        self.assertFalse(PageAnalyticsTotal.objects.filter(page_id=self.blog_page.id).exists())

    def test_cleanup_expired_analytics_and_management_command(self):
        old_date = timezone.localdate() - timedelta(days=400)
        PageAnalyticsDaily.objects.create(
            site=self.site,
            page=self.blog_page,
            stat_date=old_date,
            live_revision_id=self.blog_page.live_revision_id,
            type_key="blog_page",
            pv=50,
        )
        PageAnalyticsTotal.objects.create(
            site=self.site,
            page=self.blog_page,
            total_pv=50,
        )
        AnalyticsBatchReceipt.objects.create(
            batch_id="old:batch:1",
            payload_hash="abc",
            batch_kind="pv_reading",
            expires_at=timezone.now() - timedelta(hours=1),
        )
        res = cleanup_expired_analytics()
        self.assertEqual(res["receipts_deleted"], 1)
        self.assertEqual(res["page_daily_deleted"], 1)
        self.assertTrue(PageAnalyticsTotal.objects.filter(page=self.blog_page).exists())

        call_command("settle_analytics", "--force", "--cleanup", "--days-back=1")


class AnalyticsPermissionAndAdminUITests(TestCase):
    """后台权限隔离、is_staff 拦截、受限作者与大盘/抽屉视图测试。"""

    def setUp(self):
        super().setUp()
        self.site, self.home, self.blog_idx, _, _, _ = _setup_site_and_pages()
        ensure_author_role_infrastructure()
        authors_group = Group.objects.get(name="Authors")

        self.author_a = User.objects.create_user("author_a", "a@example.com", "Pass123456!", is_staff=True)
        self.author_b = User.objects.create_user("author_b", "b@example.com", "Pass123456!", is_staff=True)
        self.staff_only = User.objects.create_user("staff_only", "s@example.com", "Pass123456!", is_staff=True)
        self.staff_only.user_permissions.add(Permission.objects.get(codename="access_admin"))
        self.author_a.groups.add(authors_group)
        self.author_b.groups.add(authors_group)

        self.chief_editor = User.objects.create_user("chief", "c@example.com", "Pass123456!", is_staff=True)
        self.chief_editor.user_permissions.add(
            Permission.objects.get(codename="access_admin"),
            Permission.objects.get(codename="view_site_analytics"),
        )

        self.page_a = BlogPage(
            title="Author A Post",
            slug=f"a-post-{uuid4().hex[:6]}",
            owner=self.author_a,
            body=[("markdown", {"markdown": "body a"})],
        )
        self.blog_idx.add_child(instance=self.page_a)
        self.page_a.save_revision().publish()
        self.page_a.refresh_from_db()

        self.page_b = BlogPage(
            title="Author B Post",
            slug=f"b-post-{uuid4().hex[:6]}",
            owner=self.author_b,
            body=[("markdown", {"markdown": "body b"})],
        )
        self.blog_idx.add_child(instance=self.page_b)
        self.page_b.save_revision().publish()
        self.page_b.refresh_from_db()

        today = timezone.localdate()
        PageAnalyticsDaily.objects.create(
            site=self.site,
            page=self.page_a,
            stat_date=today,
            live_revision_id=self.page_a.live_revision_id,
            type_key="blog_page",
            pv=42,
            measurable_views=30,
            valid_views=25,
            completed_views=20,
        )
        PageAnalyticsDaily.objects.create(
            site=self.site,
            page=self.page_b,
            stat_date=today,
            live_revision_id=self.page_b.live_revision_id,
            type_key="blog_page",
            pv=88,
        )
        ScopeAnalyticsDaily.objects.create(
            site=self.site,
            stat_date=today - timedelta(days=1),
            scope_kind="site",
            scope_key="all",
            health_status="missing",
        )

    def test_is_staff_alone_and_cross_author_denied(self):
        self.assertFalse(can_view_site_analytics(self.staff_only))
        self.assertFalse(can_view_page_analytics(self.staff_only, self.page_a))
        with self.assertRaises(PermissionDenied):
            get_site_dashboard_data(self.staff_only)
        with self.assertRaises(PermissionDenied):
            get_page_analytics_detail(self.staff_only, self.page_a)

        self.assertFalse(can_view_site_analytics(self.author_a))
        self.assertTrue(can_view_page_analytics(self.author_a, self.page_a))
        self.assertFalse(can_view_page_analytics(self.author_a, self.page_b))

        accessible_ids = set(
            filter_accessible_pages_for_analytics(self.author_a, Page.objects.all()).values_list("id", flat=True)
        )
        self.assertIn(self.page_a.id, accessible_ids)
        self.assertNotIn(self.page_b.id, accessible_ids)

        author_list = get_author_pages_analytics_list(self.author_a)
        self.assertEqual([item["page_id"] for item in author_list["top_pages"]], [self.page_a.id])

        c = Client(HTTP_HOST="testserver")
        c.force_login(self.author_a)
        self.assertEqual(c.get(reverse("analytics_admin_dashboard")).status_code, 403)
        self.assertEqual(c.get(reverse("analytics_admin_page_drawer", args=[self.page_b.id])).status_code, 403)
        ok_resp = c.get(reverse("analytics_admin_page_drawer", args=[self.page_a.id]))
        self.assertEqual(ok_resp.status_code, 200)
        self.assertIn("Author A Post", ok_resp.content.decode("utf-8"))
        partial_resp = c.get(reverse("analytics_admin_page_drawer", args=[self.page_a.id]) + "?partial=1")
        self.assertEqual(partial_resp.status_code, 200)
        self.assertIn("可测阅读", partial_resp.content.decode("utf-8"))

    def test_chief_editor_dashboard_and_missing_coverage_warning(self):
        self.assertTrue(can_view_site_analytics(self.chief_editor))
        dash = get_site_dashboard_data(self.chief_editor, days=7)
        self.assertGreaterEqual(dash["coverage_warning"]["missing_count"], 1)
        self.assertTrue(dash["coverage_warning"]["has_warning"])

        c = Client(HTTP_HOST="testserver")
        c.force_login(self.chief_editor)
        resp = c.get(reverse("analytics_admin_dashboard"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("全站数据分析大盘", html)
        self.assertIn("访客日", html)
        self.assertIn("缺测", html)

        # 后台可搜索任意已授权文章（含 0 PV 文章），不受前 20 榜限制
        page_zero = BlogPage(
            title="Hidden Zero PV Target",
            slug=f"hidden-zero-{uuid4().hex[:6]}",
            owner=self.author_a,
            body=[("markdown", {"markdown": "zero pv body"})],
        )
        self.blog_idx.add_child(instance=page_zero)
        page_zero.save_revision().publish()

        search_resp = c.get(reverse("analytics_admin_dashboard") + "?q=Hidden+Zero")
        self.assertEqual(search_resp.status_code, 200)
        self.assertIn("Hidden Zero PV Target", search_resp.content.decode("utf-8"))

    def test_10m_scale_bounded_count_bidirectional_keyset_and_422_limit(self):
        # 额外造 2 篇博文，凑够至少 4 篇用于翻页与有界计数测试
        extra_pages = []
        for idx in range(3):
            p = BlogPage(
                title=f"Scale Page {idx}",
                slug=f"scale-page-{idx}-{uuid4().hex[:6]}",
                owner=self.author_a,
                body=[("markdown", {"markdown": "scale body"})],
            )
            self.blog_idx.add_child(instance=p)
            p.save_revision().publish()
            extra_pages.append(p)

        c = Client(HTTP_HOST="testserver")
        c.force_login(self.chief_editor)

        # 1. 超出浅页直跳上限（per_page=20 -> max=100）返回 422 拒绝深 OFFSET
        resp_422 = c.get(reverse("analytics_admin_dashboard") + "?sort=latest&p=105")
        self.assertEqual(resp_422.status_code, 422)

        # 2. 有界计数探针（带筛选条件超过 COUNT_PROBE_LIMIT，返回 lower_bound 下限；无筛选走汇总或下限）
        with patch("analytics.services.COUNT_PROBE_LIMIT", 2):
            dash_lb = get_site_dashboard_data(self.chief_editor, sort="latest", q="Scale", per_page=2)
        self.assertEqual(dash_lb["count_meta"]["kind"], "lower_bound")
        self.assertIn("超过 2 篇", dash_lb["count_meta"]["label"])
        self.assertTrue(dash_lb["pages_pagination"]["has_next"])
        self.assertIsNotNone(dash_lb["pages_pagination"]["next_cursor"])

        # 3. 双向 Keyset 游标翻页（第1页 -> 下一页 -> 上一页精确回到第1页）
        p1_ids = [r["page_id"] for r in dash_lb["top_pages"]]
        self.assertEqual(len(p1_ids), 2)

        dash_p2 = get_site_dashboard_data(
            self.chief_editor,
            sort="latest",
            q="Scale",
            per_page=2,
            cursor=dash_lb["pages_pagination"]["next_cursor"],
        )
        p2_ids = [r["page_id"] for r in dash_p2["top_pages"]]
        self.assertEqual(len(p2_ids), 1)
        self.assertTrue(set(p1_ids).isdisjoint(set(p2_ids)))
        self.assertIsNotNone(dash_p2["pages_pagination"]["previous_cursor"])

        dash_back = get_site_dashboard_data(
            self.chief_editor,
            sort="latest",
            q="Scale",
            per_page=2,
            cursor=dash_p2["pages_pagination"]["previous_cursor"],
        )
        self.assertEqual([r["page_id"] for r in dash_back["top_pages"]], p1_ids)

    def test_public_pv_display_toggle_and_zero_result_safe_search_term(self):
        from analytics.models import AnalyticsSettings

        _cleanup_analytics_redis()
        c = Client(HTTP_HOST="testserver")
        site = self.site
        blog_p = BlogPage(
            title="Public Stat Post",
            slug=f"pub-stat-{uuid4().hex[:6]}",
            owner=self.author_a,
            body=[("markdown", {"markdown": "public stat body"})],
        )
        self.blog_idx.add_child(instance=blog_p)
        blog_p.save_revision().publish()
        blog_p.refresh_from_db()

        PageAnalyticsTotal.objects.create(site=site, page=blog_p, total_pv=12)
        PageAnalyticsDaily.objects.create(
            site=site,
            page=blog_p,
            stat_date=timezone.localdate(),
            live_revision_id=blog_p.live_revision_id,
            type_key="blog_page",
            pv=3,
        )

        # 默认开启前台阅读数展示
        html_on = c.get(blog_p.url).content.decode("utf-8")
        self.assertIn("累计阅读 12 · 今日 3", html_on)

        # 关闭后前台自动隐藏
        AnalyticsSettings.objects.update_or_create(site=site, defaults={"public_pv_display_enabled": False})
        html_off = c.get(blog_p.url).content.decode("utf-8")
        self.assertNotIn("累计阅读", html_off)
        AnalyticsSettings.objects.filter(site=site).update(public_pv_display_enabled=True)

        # D5 安全搜索词（有结果与无结果）端到端测试
        desc_zero = sign_descriptor_token({
            "v": 1,
            "kind": "entry",
            "site_id": site.id,
            "page_id": None,
            "live_revision_id": 0,
            "type_key": "",
            "entry_kind": "search",
            "entry_key": "site_search",
            "body_markers": [],
            "search_query": "PostgreSQL",
            "search_total": 0,
            "search_scoped": False,
        })
        vt_zero = c.post(
            "/api/v1/analytics/context/",
            data=json.dumps({"descriptor": desc_zero}),
            content_type="application/json",
        ).json()["visit_token"]
        c.post(
            "/api/v1/analytics/events/",
            data=json.dumps({"visit_token": vt_zero, "seq": 1, "event": "start"}),
            content_type="application/json",
        )
        settle_pv_and_reading_batches()

        dash = get_site_dashboard_data(self.chief_editor, days=7)
        zero_terms = [t["term"] for t in dash["safe_search_terms"]["zero_result_terms"]]
        self.assertIn("PostgreSQL", zero_terms)
        self.assertGreaterEqual(dash["safe_search_terms"]["zero_result_total"], 1)
        _cleanup_analytics_redis()
