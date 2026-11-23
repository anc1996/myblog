"""
tests.test_analytics_phase2
全站数据分析系统 第 2 期综合验收测试：
1. 四档文章评价 API、阅读门槛、作者自评拦截、独立 Fail-Closed 限流、切换/撤销/跨版改投/并发首票与销户扣票；
2. 贝叶斯平滑 + Wilson 下界口碑模型、千万级 P90 高流量阈值、滞回告警状态机（2 日生效 / 3 日解除 / 缺测不解除）与榜单降权快照；
3. Quality Score、D3-A 自动候选、14 天拒绝冷却期、14 天限时豁免、并发审批与 RecommendationService 强核验投放；
4. HomePage 精选轮播/专栏/项目替换与跨站/祖先私有隔离、详情页评价挂件渲染、后台工作台权限与操作验收。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import threading
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django

django.setup()

from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.db import connections
from django.db.models import ProtectedError
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from wagtail.models import Page, PageViewRestriction, Site

from analytics.collector import _get_redis_client, _resolve_prefix
from analytics.curation import ConcurrencyConflictError, RecommendationService, approve_recommendation, generate_curated_candidates, nominate_recommendation, reject_recommendation
from analytics.models import AnalyticsAlert, AnalyticsSettings, ArticleRating, ArticleRatingAggregate, CuratedRecommendation, PageAnalyticsDaily, RankingSnapshot, ScopeAnalyticsDaily
from analytics.permissions import can_approve_curation, can_resolve_analytics_alerts
from analytics.rating_service import get_rating_state, submit_rating
from analytics.scoring import compute_ranking_snapshots, compute_reputation_metrics, evaluate_daily_alerts
from analytics.tokens import sign_reading_proof
from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from guides.models import GuideIndexPage, GuidePage, GuideTopic
from home.models import HomePage
from identity.account_lifecycle import AccountLifecycleService
from identity.models import User
from people.models import Person
from portfolio.models import PortfolioIndexPage, ProjectPage


def _cleanup_analytics_redis():
    client = _get_redis_client(write=True)
    if client is None:
        return
    prefix = _resolve_prefix()
    keys = list(client.scan_iter(match=f"{prefix}:analytics:*"))
    if keys:
        client.delete(*keys)


def _setup_site_tree():
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

    AnalyticsSettings.objects.update_or_create(
        site=site,
        defaults={
            "collection_enabled": True,
            "public_pv_display_enabled": True,
            "rating_enabled": True,
            "ranking_enabled": True,
            "min_rating_votes": 20,
            "observe_rating_votes": 5,
            "high_pv_min": 300,
            "min_healthy_days_in_30d": 25,
        },
    )

    blog_idx = BlogIndexPage(title="Tech", slug=f"blog-{uuid4().hex[:6]}")
    home.add_child(instance=blog_idx)
    blog_idx.save_revision().publish()

    guide_idx = GuideIndexPage(title="Guides", slug=f"guides-{uuid4().hex[:6]}")
    home.add_child(instance=guide_idx)
    guide_idx.save_revision().publish()

    port_idx = PortfolioIndexPage(title="Portfolio", slug=f"portfolio-{uuid4().hex[:6]}")
    home.add_child(instance=port_idx)
    port_idx.save_revision().publish()

    return site, home, blog_idx, guide_idx, port_idx


def _seed_healthy_window_days(site, start_days_ago=30, count=26):
    today = timezone.localdate()
    for d in range(1, start_days_ago + 1):
        if d > count:
            break
        dt = today - timedelta(days=d)
        ScopeAnalyticsDaily.objects.get_or_create(
            site=site,
            stat_date=dt,
            scope_kind="site",
            scope_key="all",
            defaults={"pv": 100, "daily_uv": 50, "health_status": "normal"},
        )


class RatingApiAndLifecycleTests(TestCase):
    """四档评价 API、阅读门槛、防刷限流、跨版改投与销户联动测试。"""

    def setUp(self):
        super().setUp()
        _cleanup_analytics_redis()
        self.site, self.home, self.blog_idx, _, _ = _setup_site_tree()
        self.author = User.objects.create_user("author_u", "au@example.com", "Pass123456!")
        self.coauthor_user = User.objects.create_user("coauthor_u", "co@example.com", "Pass123456!")
        self.coauthor_person = Person.objects.create(name="Co Author", slug=f"co-{uuid4().hex[:6]}", linked_user=self.coauthor_user)

        self.reader = User.objects.create_user("reader_u", "rd@example.com", "Pass123456!")
        User.objects.filter(pk=self.reader.pk).update(date_joined=timezone.now() - timedelta(days=30))
        self.reader.refresh_from_db()

        self.new_reader = User.objects.create_user("new_u", "nw@example.com", "Pass123456!")
        self.blog = BlogPage(
            title="Rating Target Post",
            slug=f"rate-post-{uuid4().hex[:6]}",
            owner=self.author,
            body=[("markdown", {"markdown": "## Body\n\nContent for rating test."})],
        )
        self.blog_idx.add_child(instance=self.blog)
        BlogPagePerson.objects.create(page=self.blog, person=self.coauthor_person, role="co_author", sort_order=0)
        self.blog.save_revision().publish()
        self.blog.refresh_from_db()

        self.client = Client(HTTP_HOST="testserver")

    def tearDown(self):
        _cleanup_analytics_redis()
        super().tearDown()

    def _make_proof(self, user, rev_id=None):
        return sign_reading_proof({
            "v": 1,
            "user_id": user.id,
            "site_id": self.site.id,
            "page_id": self.blog.id,
            "live_revision_id": rev_id or self.blog.live_revision_id,
        })

    def test_guest_and_author_self_rating_blocked_and_widget_visibility(self):
        html = self.client.get(self.blog.url).content.decode("utf-8")
        self.assertIn("data-analytics-rating-widget", html)
        self.assertIn("优秀", html)
        self.assertIn("良好", html)
        self.assertIn("及格", html)
        self.assertIn("差", html)
        self.assertIn("/account/login/", html)

        self.assertEqual(self.client.get(f"/api/v1/analytics/pages/{self.blog.id}/rating/").status_code, 401)
        self.assertEqual(
            self.client.post(
                f"/api/v1/analytics/pages/{self.blog.id}/rating/",
                data=json.dumps({"target_grade": "excellent"}),
                content_type="application/json",
            ).status_code,
            401,
        )

        for self_user in (self.author, self.coauthor_user):
            self.client.force_login(self_user)
            state = get_rating_state(self_user, self.blog, site=self.site)
            self.assertTrue(state["is_author"])
            self.assertFalse(state["can_rate"])
            resp = self.client.post(
                f"/api/v1/analytics/pages/{self.blog.id}/rating/",
                data=json.dumps({
                    "target_grade": "excellent",
                    "expected_revision_id": self.blog.live_revision_id,
                    "expected_version": 0,
                    "request_id": uuid4().hex,
                    "reading_proof": self._make_proof(self_user),
                }),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 403)

    def test_reading_proof_visit_threshold_and_rate_limit_fail_closed(self):
        self.client.force_login(self.reader)
        html = self.client.get(self.blog.url).content.decode("utf-8")
        m = 'name="analytics-descriptor" content="'
        idx = html.index(m) + len(m)
        desc = html[idx:html.index('"', idx)]
        ctx = self.client.post(
            "/api/v1/analytics/context/",
            data=json.dumps({"descriptor": desc}),
            content_type="application/json",
        ).json()
        vt = ctx["visit_token"]

        self.client.post(
            "/api/v1/analytics/events/",
            data=json.dumps({"visit_token": vt, "seq": 1, "event": "start", "active_seconds": 2, "max_scroll_pct": 10}),
            content_type="application/json",
        )
        rp_early = self.client.post(
            f"/api/v1/analytics/pages/{self.blog.id}/reading-proof/",
            data=json.dumps({"visit_token": vt}),
            content_type="application/json",
        )
        self.assertEqual(rp_early.status_code, 200)
        self.assertFalse(rp_early.json()["eligible"])

        self.client.post(
            "/api/v1/analytics/events/",
            data=json.dumps({"visit_token": vt, "seq": 2, "event": "heartbeat", "active_seconds": 18, "max_scroll_pct": 60}),
            content_type="application/json",
        )
        rp_ok = self.client.post(
            f"/api/v1/analytics/pages/{self.blog.id}/reading-proof/",
            data=json.dumps({"visit_token": vt}),
            content_type="application/json",
        )
        self.assertTrue(rp_ok.json()["eligible"])
        proof = rp_ok.json()["reading_proof"]
        self.assertTrue(bool(proof))

        with patch("analytics.rating_service._get_redis_client", return_value=None):
            r_503 = self.client.post(
                f"/api/v1/analytics/pages/{self.blog.id}/rating/",
                data=json.dumps({
                    "target_grade": "excellent",
                    "expected_revision_id": self.blog.live_revision_id,
                    "expected_version": 0,
                    "request_id": uuid4().hex,
                    "reading_proof": proof,
                }),
                content_type="application/json",
            )
        self.assertEqual(r_503.status_code, 503)

        client_r = _get_redis_client(write=False)
        for i in range(4):
            ok_res = self.client.post(
                f"/api/v1/analytics/pages/{self.blog.id}/rating/",
                data=json.dumps({
                    "target_grade": "excellent" if i % 2 == 0 else "good",
                    "expected_revision_id": self.blog.live_revision_id,
                    "expected_version": i,
                    "request_id": uuid4().hex,
                    "reading_proof": proof,
                }),
                content_type="application/json",
                REMOTE_ADDR="203.0.113.77",
            )
            self.assertEqual(ok_res.status_code, 200)

        r_429 = self.client.post(
            f"/api/v1/analytics/pages/{self.blog.id}/rating/",
            data=json.dumps({
                "target_grade": "pass",
                "expected_revision_id": self.blog.live_revision_id,
                "expected_version": 4,
                "request_id": uuid4().hex,
                "reading_proof": proof,
            }),
            content_type="application/json",
            REMOTE_ADDR="203.0.113.77",
        )
        self.assertEqual(r_429.status_code, 429)
        prefix = _resolve_prefix()
        rl_keys = [k.decode("utf-8", errors="ignore") if isinstance(k, bytes) else str(k) for k in client_r.scan_iter(match=f"{prefix}:analytics:ratelimit:rating:*")]
        self.assertTrue(len(rl_keys) >= 1)
        for k in rl_keys:
            self.assertNotIn("203.0.113.77", k)

    def test_add_switch_cancel_cross_revision_idempotency_and_account_hard_delete(self):
        proof_v1 = self._make_proof(self.reader)
        req1 = uuid4().hex
        res1 = submit_rating(
            self.reader,
            self.blog,
            target_grade="excellent",
            expected_revision_id=self.blog.live_revision_id,
            expected_version=0,
            request_id=req1,
            reading_proof=proof_v1,
            site=self.site,
        )
        self.assertEqual(res1["version"], 1)
        agg_v1 = ArticleRatingAggregate.objects.get(site=self.site, page=self.blog, live_revision_id=self.blog.live_revision_id)
        self.assertEqual(agg_v1.excellent_count, 1)

        res1_dup = submit_rating(
            self.reader,
            self.blog,
            target_grade="excellent",
            expected_revision_id=self.blog.live_revision_id,
            expected_version=0,
            request_id=req1,
            reading_proof=proof_v1,
            site=self.site,
        )
        self.assertEqual(res1_dup["version"], 1)
        with self.assertRaises(Exception) as ctx_err:
            submit_rating(
                self.reader,
                self.blog,
                target_grade="poor",
                expected_revision_id=self.blog.live_revision_id,
                expected_version=0,
                request_id=req1,
                reading_proof=proof_v1,
                site=self.site,
            )
        self.assertEqual(getattr(ctx_err.exception, "http_status", None), 409)

        res2 = submit_rating(
            self.reader,
            self.blog,
            target_grade="good",
            expected_revision_id=self.blog.live_revision_id,
            expected_version=1,
            request_id=uuid4().hex,
            reading_proof=proof_v1,
            site=self.site,
        )
        self.assertEqual(res2["version"], 2)
        agg_v1.refresh_from_db()
        self.assertEqual((agg_v1.excellent_count, agg_v1.good_count), (0, 1))

        AnalyticsSettings.objects.filter(site=self.site).update(rating_enabled=False)
        res3 = submit_rating(
            self.reader,
            self.blog,
            target_grade=None,
            expected_revision_id=self.blog.live_revision_id,
            expected_version=2,
            request_id=uuid4().hex,
            reading_proof="",
            site=self.site,
        )
        self.assertIsNone(res3["grade"])
        self.assertEqual(res3["version"], 3)
        agg_v1.refresh_from_db()
        self.assertEqual(agg_v1.good_count, 0)
        rating_obj = ArticleRating.objects.get(page=self.blog, user=self.reader)
        orig_day = rating_obj.first_eligible_day

        AnalyticsSettings.objects.filter(site=self.site).update(rating_enabled=True)
        res4 = submit_rating(
            self.reader,
            self.blog,
            target_grade="poor",
            expected_revision_id=self.blog.live_revision_id,
            expected_version=3,
            request_id=uuid4().hex,
            reading_proof=proof_v1,
            site=self.site,
        )
        self.assertEqual(res4["version"], 4)
        rating_obj.refresh_from_db()
        self.assertEqual(rating_obj.first_eligible_day, orig_day)

        v1_id = self.blog.live_revision_id
        self.blog.title = "Rating Target Post V2"
        self.blog.save_revision().publish()
        self.blog.refresh_from_db()
        v2_id = self.blog.live_revision_id
        self.assertNotEqual(v1_id, v2_id)

        res5 = submit_rating(
            self.reader,
            self.blog,
            target_grade="excellent",
            expected_revision_id=v2_id,
            expected_version=4,
            request_id=uuid4().hex,
            reading_proof=self._make_proof(self.reader, rev_id=v2_id),
            site=self.site,
        )
        self.assertEqual(res5["version"], 5)
        agg_v1.refresh_from_db()
        agg_v2 = ArticleRatingAggregate.objects.get(site=self.site, page=self.blog, live_revision_id=v2_id)
        self.assertEqual(agg_v1.poor_count, 0)
        self.assertEqual(agg_v2.excellent_count, 1)

        with self.assertRaises(ProtectedError):
            self.reader.delete()

        su = User.objects.create_superuser("su_del", "su@example.com", "Admin123456!")
        AccountLifecycleService.hard_delete(
            actor=su,
            target_user_id=self.reader.id,
            confirmed=True,
        )
        agg_v2.refresh_from_db()
        self.assertEqual(agg_v2.excellent_count, 0)
        self.assertFalse(ArticleRating.objects.filter(user_id=self.reader.id).exists())
        self.assertFalse(User.objects.filter(id=self.reader.id).exists())


class RatingConcurrencyRaceTests(TransactionTestCase):
    """真实 PostgreSQL 并发首票冲突测试。"""

    def test_concurrent_first_rating_insert_race_returns_single_row_and_409(self):
        _cleanup_analytics_redis()
        site, _, blog_idx, _, _ = _setup_site_tree()
        reader = User.objects.create_user("race_u", "rc@example.com", "Pass123456!")
        blog = BlogPage(
            title="Race Post",
            slug=f"race-{uuid4().hex[:6]}",
            body=[("markdown", {"markdown": "race body"})],
        )
        blog_idx.add_child(instance=blog)
        blog.save_revision().publish()
        blog.refresh_from_db()
        proof = sign_reading_proof({
            "v": 1,
            "user_id": reader.id,
            "site_id": site.id,
            "page_id": blog.id,
            "live_revision_id": blog.live_revision_id,
        })

        barrier = threading.Barrier(2)
        results = []

        def _worker():
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                res = submit_rating(
                    reader,
                    blog,
                    target_grade="excellent",
                    expected_revision_id=blog.live_revision_id,
                    expected_version=0,
                    request_id=uuid4().hex,
                    reading_proof=proof,
                    site=site,
                )
                results.append(("ok", res["version"]))
            except Exception as exc:
                results.append(("err", getattr(exc, "http_status", type(exc).__name__)))
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_worker)
            f2 = pool.submit(_worker)
            f1.result(timeout=15)
            f2.result(timeout=15)

        self.assertEqual(sorted(results), [("err", 409), ("ok", 1)])
        agg = ArticleRatingAggregate.objects.get(site=site, page=blog, live_revision_id=blog.live_revision_id)
        self.assertEqual(agg.excellent_count, 1)


class ReputationHysteresisCurationAndHomeTests(TestCase):
    """口碑数学基线、滞回告警、精选候选审批与首页投放隔离测试。"""

    def setUp(self):
        super().setUp()
        self.site, self.home, self.blog_idx, self.guide_idx, self.port_idx = _setup_site_tree()
        _seed_healthy_window_days(self.site, start_days_ago=30, count=26)
        self.editor = User.objects.create_user("chief_ed", "ed@example.com", "Pass123456!", is_staff=True)
        self.editor.user_permissions.add(
            Permission.objects.get(codename="access_admin"),
            Permission.objects.get(codename="view_site_analytics"),
            Permission.objects.get(codename="approve_curatedrecommendation"),
            Permission.objects.get(codename="resolve_analyticsalert"),
        )
        self.staff_plain = User.objects.create_user("staff_p", "sp@example.com", "Pass123456!", is_staff=True)
        self.staff_plain.user_permissions.add(Permission.objects.get(codename="access_admin"))

        self.blog = BlogPage(
            title="Quality Article",
            slug=f"qa-art-{uuid4().hex[:6]}",
            body=[("markdown", {"markdown": "## Deep Dive\n\nLong quality text."})],
        )
        self.blog_idx.add_child(instance=self.blog)
        self.blog.save_revision().publish()
        self.blog.refresh_from_db()

        self.topic = GuideTopic.objects.create(name="Postgres", slug=f"pg-{uuid4().hex[:6]}")
        self.guide = GuidePage(
            title="Featured Guide",
            slug=f"feat-guide-{uuid4().hex[:6]}",
            topic=self.topic,
            last_reviewed_at=timezone.now() - timedelta(days=30),
            body=[("step", {"step_number": 1, "title": "Step 1", "content": "<p>Step</p>"})],
        )
        self.guide_idx.add_child(instance=self.guide)
        self.guide.save_revision().publish()
        self.guide.refresh_from_db()

        self.project = ProjectPage(
            title="Featured Project",
            slug=f"feat-proj-{uuid4().hex[:6]}",
            body=[("markdown", {"markdown": "Project body"})],
        )
        self.port_idx.add_child(instance=self.project)
        self.project.save_revision().publish()
        self.project.refresh_from_db()

    def _create_voters(self, grades: list[str], *, new_member: bool = False, day=None):
        vote_day = day or (timezone.localdate() - timedelta(days=2))
        for g in grades:
            u = User.objects.create_user(f"v_{uuid4().hex[:8]}", f"{uuid4().hex[:8]}@example.com", "Pass123456!")
            ArticleRating.objects.create(
                site=self.site,
                page=self.blog,
                user=u,
                live_revision_id=self.blog.live_revision_id,
                grade=g,
                trust_tier="new_member" if new_member else "member",
                first_eligible_day=vote_day,
            )

    def test_reputation_math_hysteresis_and_new_member_isolation(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        two_days_ago = timezone.localdate() - timedelta(days=2)

        PageAnalyticsDaily.objects.create(
            site=self.site,
            page=self.blog,
            stat_date=two_days_ago,
            live_revision_id=self.blog.live_revision_id,
            type_key="blog_page",
            pv=600,
            organic_pv=600,
            measurable_views=100,
            valid_views=40,
            completed_views=25,
            short_bounce_views=60,
            total_active_seconds=12000,
            health_status="normal",
        )

        self._create_voters(["poor", "poor"])
        m2 = compute_reputation_metrics(self.site, self.blog, self.blog.live_revision_id, as_of_date=yesterday)
        self.assertAlmostEqual(m2["r_trusted"], 13.0 / 22.0, places=4)
        self.assertFalse(m2["is_low_reputation"])

        self._create_voters(["poor"] * 5 + ["pass"] * 12)
        self._create_voters(["poor"] * 10, new_member=True)
        m19 = compute_reputation_metrics(self.site, self.blog, self.blog.live_revision_id, as_of_date=yesterday)
        self.assertEqual(m19["n_trusted"], 19)
        self.assertFalse(m19["is_low_reputation"])

        self._create_voters(["poor"])
        m20 = compute_reputation_metrics(self.site, self.blog, self.blog.live_revision_id, as_of_date=yesterday)
        self.assertEqual(m20["n_trusted"], 20)
        self.assertAlmostEqual(m20["r_trusted"], 0.460, places=3)
        self.assertTrue(m20["wilson_lower"] >= 0.20)
        self.assertTrue(m20["is_low_reputation"])

        evaluate_daily_alerts(self.site, stat_date=two_days_ago)
        alert = AnalyticsAlert.objects.get(
            site=self.site,
            page=self.blog,
            live_revision_id=self.blog.live_revision_id,
            alert_kind="HIGH_TRAFFIC_LOW_REPUTATION",
        )
        self.assertEqual(alert.status, "observing")

        evaluate_daily_alerts(self.site, stat_date=two_days_ago)
        alert.refresh_from_db()
        self.assertEqual(alert.status, "observing")

        PageAnalyticsDaily.objects.create(
            site=self.site,
            page=self.blog,
            stat_date=yesterday,
            live_revision_id=self.blog.live_revision_id,
            type_key="blog_page",
            pv=50,
            organic_pv=50,
            measurable_views=20,
            completed_views=10,
            health_status="normal",
        )
        evaluate_daily_alerts(self.site, stat_date=yesterday)
        alert.refresh_from_db()
        self.assertEqual(alert.status, "active")

        self.assertTrue(
            AnalyticsAlert.objects.filter(
                site=self.site,
                page=self.blog,
                alert_kind="HIGH_TRAFFIC_LOW_ENGAGEMENT",
            ).exists()
        )

        compute_ranking_snapshots(self.site, stat_date=yesterday)
        snap = RankingSnapshot.objects.get(
            site=self.site, board_kind="popular_30d", type_key="blog_page", stat_date=yesterday
        )
        entry = next(x for x in snap.items_json if x["page_id"] == self.blog.id)
        self.assertEqual(entry["factor"], 0.35)
        self.assertLess(entry["final_score"], entry["raw_score"])

    def test_curation_d3a_cooldown_exempt_homepage_isolation_and_workbench_ui(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        for d in range(1, 26):
            dt = timezone.localdate() - timedelta(days=d)
            PageAnalyticsDaily.objects.create(
                site=self.site,
                page=self.blog,
                stat_date=dt,
                live_revision_id=self.blog.live_revision_id,
                type_key="blog_page",
                pv=20,
                organic_pv=20,
                daily_uv=5,
                measurable_views=14,
                valid_views=12,
                completed_views=11,
                short_bounce_views=0,
                total_active_seconds=2520,
                health_status="normal",
            )

        # 补充真实正面读者评价（使 I 分项达标，综合 Quality Score >= 70）
        self._create_voters(["excellent"] * 22, day=yesterday)

        res = generate_curated_candidates(self.site, stat_date=yesterday)
        self.assertGreaterEqual(res["candidates_created"], 1)
        rec = CuratedRecommendation.objects.get(
            site=self.site, page=self.blog, live_revision_id=self.blog.live_revision_id, position="home_carousel"
        )
        self.assertEqual(rec.status, "pending")
        self.assertGreaterEqual(rec.quality_score, 70.0)

        c = Client(HTTP_HOST="testserver")
        home_resp = c.get(self.home.url)
        self.assertEqual(home_resp.status_code, 200)
        self.assertEqual(home_resp.context["featured_carousel_items"], [])
        self.assertEqual(home_resp.context["featured_guides"], [])
        self.assertEqual(home_resp.context["featured_projects"], [])

        rec = approve_recommendation(rec.id, self.editor, expected_version=rec.version, priority=10, is_pinned=True)
        nominate_recommendation(
            self.site, self.guide, "home_guides", self.editor, reason="优质指南", auto_approve=True
        )
        nominate_recommendation(
            self.site, self.project, "home_projects", self.editor, reason="优质开源", auto_approve=True
        )

        with self.assertRaises(ConcurrencyConflictError):
            approve_recommendation(rec.id, self.editor, expected_version=rec.version - 1)

        carousel = RecommendationService.for_placement(self.site, "home_carousel", limit=8)
        self.assertEqual([it["page_id"] for it in carousel], [self.blog.id])
        home_ctx_resp = c.get(self.home.url)
        self.assertEqual(len(home_ctx_resp.context["featured_carousel_items"]), 1)
        self.assertEqual(len(home_ctx_resp.context["featured_guides"]), 1)
        self.assertEqual(len(home_ctx_resp.context["featured_projects"]), 1)

        PageViewRestriction.objects.create(page=self.blog_idx, restriction_type=PageViewRestriction.LOGIN)
        self.assertEqual(RecommendationService.for_placement(self.site, "home_carousel", limit=8), [])
        home_priv_resp = c.get(self.home.url)
        self.assertEqual(home_priv_resp.context["featured_carousel_items"], [])
        self.assertEqual(home_priv_resp.context["latest_articles"], [])
        PageViewRestriction.objects.filter(page=self.blog_idx).delete()

        rec = reject_recommendation(rec.id, self.editor, expected_version=rec.version, reason="需补充测试", cooldown_days=14)
        self.assertIsNotNone(rec.rejected_cooldown_until)
        res2 = generate_curated_candidates(self.site, stat_date=yesterday)
        self.assertGreaterEqual(res2["skipped_cooldown_or_decided"], 1)
        rec.refresh_from_db()
        self.assertEqual(rec.status, "rejected")

        self.assertFalse(can_approve_curation(self.staff_plain))
        self.assertFalse(can_resolve_analytics_alerts(self.staff_plain))
        c.force_login(self.staff_plain)
        self.assertEqual(c.get(reverse("analytics_admin_curation")).status_code, 403)
        self.assertEqual(c.get(reverse("analytics_admin_alerts")).status_code, 403)
        self.assertEqual(c.get(reverse("analytics_admin_rankings")).status_code, 403)

        c.force_login(self.editor)
        self.assertEqual(c.get(reverse("analytics_admin_curation")).status_code, 200)
        self.assertEqual(c.get(reverse("analytics_admin_alerts")).status_code, 200)
        self.assertEqual(c.get(reverse("analytics_admin_rankings")).status_code, 200)

        self.assertEqual(c.get(reverse("analytics_admin_curation") + "?p=105").status_code, 422)
        self.assertEqual(c.get(reverse("analytics_admin_alerts") + "?p=105").status_code, 422)

        call_command("settle_analytics", "--force", "--evaluate", f"--stat-date={yesterday.isoformat()}")
