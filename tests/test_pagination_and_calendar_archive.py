"""
千万级混合分页协议（浅页直跳 + 双向 Keyset + 日期定位 + 有界计数）与月历归档预聚合系统测试。
"""

from datetime import date, timedelta
from unittest.mock import patch
from uuid import uuid4
import json

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.utils import timezone
from wagtail.models import PageViewRestriction, Site

from api.views import archive_calendar_api, timeline_archives_api
from blog.models import BlogIndexPage, BlogPage
from discovery.archive_counts import (
    enqueue_subtree_archive_reconcile,
    format_compact_count,
    get_archive_calendar_data,
    process_archive_outbox,
    reconcile_archive_counts,
)
from discovery.models import (
    ArchiveContribution,
    ArchiveCountState,
    ArchiveDailyCount,
    PublishedSearchDocument,
)
from discovery.pagination import build_page_links
from discovery.timeline import timeline_view
from home.models import HomePage


class PaginationAndCalendarArchiveTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.home = HomePage.objects.first()
        if not self.home:
            from wagtail.models import Page
            root = Page.get_first_root_node() or Page.add_root(title="Root")
            self.home = HomePage(title="Home", slug=f"home-{uuid4().hex[:6]}")
            root.add_child(instance=self.home)
            self.home.save_revision().publish()

        self.site = Site.objects.filter(is_default_site=True).first()
        if not self.site:
            self.site = Site.objects.create(
                hostname="localhost",
                port=80,
                root_page=self.home,
                is_default_site=True,
            )

        self.index = BlogIndexPage(title="分页测试专栏", slug=f"pag-col-{uuid4().hex[:6]}")
        self.home.add_child(instance=self.index)
        self.index.save_revision().publish()

        self.created_pages = []
        self.created_groups = []

        # 创建 6 篇覆盖多日期的文章（包含同日 2 篇测试同日 tie-breaker）
        dates = [
            date(2026, 11, 20),
            date(2026, 11, 20),
            date(2026, 11, 18),
            date(2026, 11, 15),
            date(2026, 10, 5),
            date(2025, 8, 12),
        ]
        for idx, d in enumerate(dates, start=1):
            p = BlogPage(
                title=f"分页文章-{idx}-{d.isoformat()}",
                slug=f"pag-p-{idx}-{uuid4().hex[:5]}",
                authored_on=d,
            )
            self.index.add_child(instance=p)
            p.save_revision().publish()
            self.created_pages.append(p)

    def tearDown(self):
        for p in reversed(self.created_pages):
            try:
                p.delete(allow_physical_purge=True)
            except Exception:
                pass
        try:
            self.index.delete(allow_physical_purge=True)
        except Exception:
            pass
        for g in self.created_groups:
            try:
                g.delete()
            except Exception:
                pass

    def test_page_links_ellipsis_window(self):
        """页码窗口正确生成带省略号的局部页码序列"""
        links = build_page_links(current_page=6, reachable_last_page=100, max_jump_page=100)
        labels = [x["label"] for x in links]
        self.assertEqual(labels, ["1", "…", "4", "5", "6", "7", "8", "…", "100"])
        self.assertTrue(any(x["page"] == 6 and x["is_current"] for x in links))

    def test_shallow_page_jump_and_422_limit(self):
        """浅页直跳返回真实页码与结果；超出浅页上限返回 HTTP 422 且不跑深 OFFSET"""
        req_p2 = self.factory.get(f"{self.index.url_path}?page_size=2&page=2")
        resp_p2 = self.index.serve(req_p2)
        self.assertEqual(resp_p2.status_code, 200)
        vm_p2 = resp_p2.context_data["view_model"]
        self.assertEqual(len(vm_p2.items), 2)
        self.assertEqual(vm_p2.pagination["page"], 2)
        self.assertEqual(vm_p2.pagination["total_pages"], 3)
        self.assertEqual(vm_p2.count_meta["kind"], "exact")
        self.assertEqual(vm_p2.count_meta["value"], 6)

        # 超出 max_jump_page (page_size=2 -> max=100) 返回 422
        req_over = self.factory.get(f"{self.index.url_path}?page_size=2&page=105")
        resp_over = self.index.serve(req_over)
        self.assertEqual(resp_over.status_code, 422)
        self.assertIn("超出直跳窗口上限", resp_over.context_data["view_model"].error_message)

    def test_bidirectional_keyset_and_seek_date_and_oldest(self):
        """双向 Keyset 游标（下一页->上一页精准回到第1页）、日期定位与最早一批"""
        # 第 1 页
        r1 = self.index.serve(self.factory.get(f"{self.index.url_path}?page_size=2&page=1"))
        vm1 = r1.context_data["view_model"]
        p1_ids = [it.page_id for it in vm1.items]
        self.assertEqual(len(p1_ids), 2)
        self.assertIsNone(vm1.previous_cursor)

        # 下一页 -> 第 2 页
        r2 = self.index.serve(self.factory.get(f"{self.index.url_path}?page_size=2&cursor={vm1.next_cursor}"))
        vm2 = r2.context_data["view_model"]
        p2_ids = [it.page_id for it in vm2.items]
        self.assertEqual(len(p2_ids), 2)
        self.assertNotEqual(p1_ids, p2_ids)
        self.assertIsNotNone(vm2.previous_cursor)

        # 上一页 -> 必须回到第 1 页（此前缺陷会继续往后翻！）
        r_back = self.index.serve(self.factory.get(f"{self.index.url_path}?page_size=2&cursor={vm2.previous_cursor}"))
        vm_back = r_back.context_data["view_model"]
        self.assertEqual([it.page_id for it in vm_back.items], p1_ids)
        self.assertIsNone(vm_back.previous_cursor)

        # seek_date 定位到 2026-11-15 及更早
        r_seek = self.index.serve(self.factory.get(f"{self.index.url_path}?page_size=2&seek_date=2026-11-15"))
        vm_seek = r_seek.context_data["view_model"]
        self.assertEqual(vm_seek.items[0].authored_on, date(2026, 11, 15))
        self.assertIsNone(vm_seek.pagination["page"])

        # seek_direction=oldest 定位最早一批（无需 OFFSET）
        r_old = self.index.serve(self.factory.get(f"{self.index.url_path}?page_size=2&seek_direction=oldest"))
        vm_old = r_old.context_data["view_model"]
        self.assertEqual(vm_old.items[-1].authored_on, date(2025, 8, 12))

    def test_bounded_count_probe_summary_and_lower_bound(self):
        """超过 COUNT_PROBE_LIMIT 时，纯站点/类型走汇总计数，复杂过滤标明 lower_bound"""
        with patch("discovery.query.COUNT_PROBE_LIMIT", 3):
            # 带 root_page_id 属复杂过滤 -> lower_bound
            r_sub = self.index.serve(self.factory.get(f"{self.index.url_path}?page_size=2"))
            vm_sub = r_sub.context_data["view_model"]
            self.assertEqual(vm_sub.count_meta["kind"], "lower_bound")
            self.assertEqual(vm_sub.count_meta["value"], 3)
            self.assertIn("超过 3 篇", vm_sub.count_meta["label"])

            # 时间线全站无 root_page_id -> 读取 ArchiveDailyCount 汇总计数
            r_tl = timeline_view(self.factory.get("/timeline/"))
            self.assertEqual(r_tl.status_code, 200)
            html = r_tl.content.decode("utf-8")
            self.assertIn("汇总", html)

    def test_archive_daily_count_incremental_lifecycle_and_reconcile(self):
        """验证发布、改期、权限收紧/放开、回收恢复的按日增量同步与双代次对账"""
        active_gen, _ = ArchiveCountState.get_active_generation_readonly()
        site_id = self.site.id

        def count_on(d: date) -> int:
            row = ArchiveDailyCount.objects.filter(
                generation=active_gen,
                site_id=site_id,
                type_key="blog_page",
                bucket_date=d,
            ).first()
            return row.article_count if row else 0

        self.assertGreaterEqual(count_on(date(2026, 11, 20)), 2)
        before_1120 = count_on(date(2026, 11, 20))
        before_1121 = count_on(date(2026, 11, 21))

        # 1. 修改第一篇文章成文日期并重新发布 -> 旧桶 -1，新桶 +1
        target = self.created_pages[0]
        target.authored_on = date(2026, 11, 21)
        target.save_revision().publish()
        self.assertEqual(count_on(date(2026, 11, 20)), before_1120 - 1)
        self.assertEqual(count_on(date(2026, 11, 21)), before_1121 + 1)

        # 2. 添加私有权限限制 -> 立即从日历计数扣除
        grp = Group.objects.create(name=f"priv-{uuid4().hex[:6]}")
        self.created_groups.append(grp)
        restr = PageViewRestriction.objects.create(
            page=target,
            restriction_type=PageViewRestriction.GROUPS,
        )
        restr.groups.add(grp)
        self.assertEqual(count_on(date(2026, 11, 21)), before_1121)

        # 删除限制 -> 恢复计入
        restr.delete()
        self.assertEqual(count_on(date(2026, 11, 21)), before_1121 + 1)

        # 3. 未来日期文章不应出现在当月日历输出中
        future_day = timezone.localdate() + timedelta(days=10)
        fut = BlogPage(title="未来排期", slug=f"fut-{uuid4().hex[:5]}", authored_on=future_day)
        self.index.add_child(instance=fut)
        fut.save_revision().publish()
        self.created_pages.append(fut)
        cal_fut = get_archive_calendar_data(site_id=site_id, year=future_day.year, month=future_day.month)
        fut_cell = next((d for d in cal_fut["days"] if d["date"] == future_day.isoformat()), None)
        self.assertIsNotNone(fut_cell)
        self.assertEqual(fut_cell["count"], 0)

        # 4. 制造人工漂移并用 dry-run / apply 双代次对账修复
        ArchiveDailyCount.objects.filter(
            generation=active_gen,
            site_id=site_id,
            type_key="blog_page",
            bucket_date=date(2026, 11, 21),
        ).update(article_count=999)

        dry = reconcile_archive_counts(apply=False)
        self.assertGreaterEqual(dry["diff_count"], 1)

        enqueue_subtree_archive_reconcile(root_path=self.index.path, page_id=self.index.id)
        outbox_res = process_archive_outbox()
        self.assertEqual(outbox_res["failed"], 0)
        new_gen, is_stale = ArchiveCountState.get_active_generation_readonly()
        self.assertFalse(is_stale)
        self.assertGreater(new_gen, active_gen)
        repaired_val = ArchiveDailyCount.objects.get(
            generation=new_gen,
            site_id=site_id,
            type_key="blog_page",
            bucket_date=date(2026, 11, 21),
        ).article_count
        self.assertEqual(repaired_val, before_1121 + 1)

    def test_calendar_view_drilldown_and_api_etag(self):
        """月历视图、按日下钻、/api/v1/archives/calendar/ 缓存命中与弱 ETag 304"""
        # 1. 月历视图 + 按日下钻
        r_cal = timeline_view(self.factory.get("/timeline/?year=2026&month=11&day=2026-11-20"))
        self.assertEqual(r_cal.status_code, 200)
        html = r_cal.content.decode("utf-8")
        self.assertIn("2026 年 11 月", html)
        self.assertIn("2026-11-20 当日发表", html)
        self.assertIn("分页文章-1-2026-11-20", html)
        self.assertNotIn("分页文章-5-2026-10-05", html)

        # 2. 日历 API + Redis 缓存 + 304
        r_api1 = archive_calendar_api(self.factory.get("/api/v1/archives/calendar/?month=2026-11&type=blog"))
        self.assertEqual(r_api1.status_code, 200)
        payload = json.loads(r_api1.content.decode("utf-8"))
        self.assertEqual(payload["data"]["month"], "2026-11")
        self.assertEqual(payload["data"]["date_basis"], "authored_on")
        etag = r_api1["ETag"]

        r_api2 = archive_calendar_api(self.factory.get("/api/v1/archives/calendar/?month=2026-11&type=blog"))
        self.assertEqual(r_api2["X-Cache"], "HIT")

        r_304 = archive_calendar_api(
            self.factory.get("/api/v1/archives/calendar/?month=2026-11&type=blog", HTTP_IF_NONE_MATCH=etag)
        )
        self.assertEqual(r_304.status_code, 304)

        # 3. 归档总接口弱 ETag 含站点日期
        r_arch = timeline_archives_api(self.factory.get("/api/v1/archives/"))
        self.assertIn(timezone.localdate().isoformat(), r_arch["ETag"])

        # 4. 管理命令 dry-run
        call_command("reconcile_archive_counts")

    def test_multi_decade_2002_time_travel_heatmap_and_in_period_search(self):
        """验证跨年代快速直达 2002 年、百万篇紧凑格式化、5 档热力等级与期内二次搜索"""
        self.assertEqual(format_compact_count(950), "950")
        self.assertEqual(format_compact_count(12500), "1.2万")
        self.assertEqual(format_compact_count(1000000), "100万")

        # 创建一篇 2002-05-18 的历史文章
        p_2002 = BlogPage(
            title="2002年分布式早期文献",
            slug=f"p-2002-{uuid4().hex[:5]}",
            authored_on=date(2002, 5, 18),
        )
        self.index.add_child(instance=p_2002)
        p_2002.save_revision().publish()
        self.created_pages.append(p_2002)

        # 1. 直接跳往 2002 年 5 月
        r_2002 = timeline_view(self.factory.get("/timeline/?year=2002&month=5"))
        self.assertEqual(r_2002.status_code, 200)
        html_2002 = r_2002.content.decode("utf-8")
        self.assertIn("2002 年 5 月", html_2002)
        self.assertIn("2000年代", html_2002)
        self.assertIn("2002年分布式早期文献", html_2002)
        self.assertIn("heat-", html_2002)

        # 2. 查看 2002 全年归档 + 期内关键词搜索
        r_yr = timeline_view(self.factory.get("/timeline/?year=2002&scope=year&q=早期文献"))
        self.assertEqual(r_yr.status_code, 200)
        html_yr = r_yr.content.decode("utf-8")
        self.assertIn("2002 年全年归档", html_yr)
        self.assertIn("2002年分布式早期文献", html_yr)

    def test_unified_pagination_prev_next_keeps_page_numbers_and_scheme_a_oldest_button(self):
        """验证页码态上一页/下一页保持 ?page=N、标准内联包住页码，且仅超页码上限才显示「直达最早」(方案 A)"""
        from pathlib import Path
        from django.template.loader import render_to_string
        from discovery.pagination import build_count_meta, build_pagination_payload

        # 1. 浅页第 1 页（共 68 页，未超 100 页上限）
        p1 = build_pagination_payload(
            mode="shallow_page",
            current_page=1,
            page_size=15,
            total_count=1006,
            count_kind="exact",
            has_next=True,
            has_previous=False,
            next_cursor="v2:next1",
            previous_cursor=None,
        )
        self.assertIsNone(p1["prev_page"])
        self.assertEqual(p1["next_page"], 2)
        self.assertFalse(p1["is_window_capped"])

        html_p1 = render_to_string(
            "discovery/listing/_listing_pagination.html",
            {
                "view_model": {
                    "total_count": 1006,
                    "count_meta": build_count_meta(value=1006, kind="exact"),
                    "pagination": p1,
                    "next_cursor": "v2:next1",
                    "previous_cursor": None,
                    "query_string": "sort=authored_desc",
                    "canonical_url": "/demo/",
                    "active_filters": {"seek_date": ""},
                }
            },
        )
        self.assertIn('data-page="2"', html_p1)
        self.assertIn('href="?page=2&amp;sort=authored_desc"', html_p1)
        self.assertNotIn('data-cursor="v2:next1"', html_p1)
        self.assertNotIn("listing-oldest-btn", html_p1)
        self.assertNotIn("最早一批", html_p1)
        # 确认布局顺序：上一页 -> [1] -> [68] -> 下一页 在同一 nav 内
        nav_start = html_p1.index('<nav class="listing-page-numbers"')
        nav_end = html_p1.index("</nav>", nav_start)
        nav_html = html_p1[nav_start:nav_end]
        self.assertLess(nav_html.index("上一页"), nav_html.index("listing-page-current"))
        self.assertLess(nav_html.index("listing-page-current"), nav_html.index(">68<"))
        self.assertLess(nav_html.index(">68<"), nav_html.index("下一页"))

        # 2. 超页码上限第 100 页（is_window_capped=True）：上一页走 page=99，下一页平滑降级为 cursor，且显示「直达最早」
        p100 = build_pagination_payload(
            mode="shallow_page",
            current_page=100,
            page_size=20,
            total_count=5000,
            count_kind="lower_bound",
            has_next=True,
            has_previous=True,
            next_cursor="v2:next100",
            previous_cursor="v2:prev100",
        )
        self.assertEqual(p100["prev_page"], 99)
        self.assertIsNone(p100["next_page"])
        self.assertTrue(p100["is_window_capped"])

        html_p100 = render_to_string(
            "discovery/listing/_listing_pagination.html",
            {
                "view_model": {
                    "total_count": 5000,
                    "count_meta": build_count_meta(value=5000, kind="lower_bound"),
                    "pagination": p100,
                    "next_cursor": "v2:next100",
                    "previous_cursor": "v2:prev100",
                    "query_string": "",
                    "canonical_url": "/demo/",
                    "active_filters": {"seek_date": ""},
                }
            },
        )
        self.assertIn('data-page="99"', html_p100)
        self.assertIn('data-cursor="v2:next100"', html_p100)
        self.assertIn("listing-oldest-btn", html_p100)
        self.assertIn("直达最早", html_p100)

        # 3. 游标深翻态（current_page=None）：提供「返回第 1 页」快捷入口
        p_cur = build_pagination_payload(
            mode="cursor",
            current_page=None,
            page_size=15,
            total_count=5000,
            count_kind="lower_bound",
            has_next=True,
            has_previous=True,
            next_cursor="v2:cnext",
            previous_cursor="v2:cprev",
        )
        html_cur = render_to_string(
            "discovery/listing/_listing_pagination.html",
            {
                "view_model": {
                    "total_count": 5000,
                    "count_meta": build_count_meta(value=5000, kind="lower_bound"),
                    "pagination": p_cur,
                    "next_cursor": "v2:cnext",
                    "previous_cursor": "v2:cprev",
                    "query_string": "",
                    "canonical_url": "/demo/",
                    "active_filters": {"seek_date": ""},
                }
            },
        )
        self.assertIn("返回第 1 页", html_cur)
        self.assertIn('data-cursor="v2:cprev"', html_cur)
        self.assertIn('data-cursor="v2:cnext"', html_cur)

        # 4. 全部 5 个前台 JS 控制器均优先读取 data-page 再回退 data-cursor
        project_root = Path(__file__).resolve().parents[1]
        for rel in (
            "myblog/static/discovery/js/listing_controller.js",
            "myblog/static/search/js/search_controller.js",
            "myblog/static/discovery/js/timeline_controller.js",
            "myblog/static/taxonomy/js/tag_controller.js",
            "myblog/static/people/js/author_controller.js",
        ):
            js_text = (project_root / rel).read_text(encoding="utf-8")
            self.assertIn("getAttribute('data-page')" if "'" in js_text and "getAttribute('data-page')" in js_text else 'getAttribute("data-page")', js_text, msg=f"{rel} 未优先处理 data-page")
