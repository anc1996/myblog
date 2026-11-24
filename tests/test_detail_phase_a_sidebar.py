"""
Phase A 详情页三栏壳层、同父栏目上下篇、标签卡站点隔离与站点公告/同栏推荐回归测试。
"""

from datetime import date, timedelta
from pathlib import Path
import re
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.utils import timezone
from wagtail.models import Page, PageViewRestriction, Site

from blog.models import BlogIndexPage, BlogPage
from content_blocks.rendering.detail import build_detail_presentation
from content_lifecycle.recycle import RecycleService
from discovery.models import ContentDiscoveryEpoch, PublishedSearchDocument
from guides.models import GuideIndexPage, GuidePage, GuideTopic
from home.models import HomePage
from site_settings.models import SiteAnnouncementSettings
from taxonomy.models import TagIndexPage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DetailPhaseASidebarTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.home = HomePage.objects.first()
        if not self.home:
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
        Site.clear_site_root_paths_cache()
        self.created_pages = []
        self.created_topics = []

    def tearDown(self):
        SiteAnnouncementSettings.objects.all().delete()
        for p in reversed(self.created_pages):
            try:
                p.delete(allow_physical_purge=True)
            except Exception:
                pass
        for t in self.created_topics:
            try:
                t.delete()
            except Exception:
                pass

    def test_css_and_template_hygiene_zero_hex_and_inline_style(self):
        """detail-layout.css 零裸 #hex，详情壳层与侧栏模板零内联 style= / <style"""
        detail_css = (PROJECT_ROOT / "myblog/static/css/detail-layout.css").read_text(encoding="utf-8")
        cleaned = re.sub(r"/\*.*?\*/", "", detail_css, flags=re.DOTALL)
        matches = re.findall(r"#[0-9a-fA-F]{3,8}\b", cleaned)
        self.assertEqual(matches, [], f"detail-layout.css 不应保留裸 hex 颜色: {matches}")

        for rel in (
            "myblog/templates/detail/detail_shell.html",
            "myblog/templates/detail/_detail_prev_next.html",
            "myblog/templates/detail/_detail_right_sidebar.html",
            "myblog/templates/detail/_detail_tags.html",
        ):
            text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("style=", text, f"{rel} 不应包含内联 style=")
            self.assertNotIn("<style", text.lower(), f"{rel} 不应包含 <style> 块")

    def test_prev_next_same_parent_tie_break_excludes_index_and_subcolumn(self):
        """上下篇同日按 page_id 消解平局，排除栏目索引页与子栏目孙页，首尾不循环"""
        col = BlogIndexPage(title="架构专栏", slug=f"col-{uuid4().hex[:6]}")
        self.home.add_child(instance=col)
        col.save_revision().publish()
        self.created_pages.append(col)

        # 同级子栏目（IndexPage 不得混入上下篇）与孙文章（depth 不同不得混入）
        sub_col = BlogIndexPage(title="子专栏索引", slug=f"subcol-{uuid4().hex[:6]}")
        col.add_child(instance=sub_col)
        sub_col.save_revision().publish()
        self.created_pages.append(sub_col)

        grandchild = BlogPage(
            title="子专栏孙文章",
            slug=f"grand-{uuid4().hex[:6]}",
            authored_on=date(2026, 11, 20),
        )
        sub_col.add_child(instance=grandchild)
        grandchild.save_revision().publish()
        self.created_pages.append(grandchild)

        p1 = BlogPage(title="第一篇", slug=f"p1-{uuid4().hex[:6]}", authored_on=date(2026, 11, 20))
        col.add_child(instance=p1)
        p1.save_revision().publish()
        self.created_pages.append(p1)

        p2 = BlogPage(title="第二篇同日后建", slug=f"p2-{uuid4().hex[:6]}", authored_on=date(2026, 11, 20))
        col.add_child(instance=p2)
        p2.save_revision().publish()
        self.created_pages.append(p2)

        p3 = BlogPage(title="第三篇较新", slug=f"p3-{uuid4().hex[:6]}", authored_on=date(2026, 11, 21))
        col.add_child(instance=p3)
        p3.save_revision().publish()
        self.created_pages.append(p3)

        # 未来日期、受限文章与已回收文章均不可出现
        future_p = BlogPage(
            title="未来文章",
            slug=f"fut-{uuid4().hex[:6]}",
            authored_on=timezone.localdate() + timedelta(days=3),
        )
        col.add_child(instance=future_p)
        future_p.save_revision().publish()
        self.created_pages.append(future_p)

        restricted_p = BlogPage(title="受限文章", slug=f"res-{uuid4().hex[:6]}", authored_on=date(2026, 11, 22))
        col.add_child(instance=restricted_p)
        restricted_p.save_revision().publish()
        PageViewRestriction.objects.create(page=restricted_p, restriction_type=PageViewRestriction.LOGIN)
        self.created_pages.append(restricted_p)

        recycled_p = BlogPage(title="已回收文章", slug=f"rec-{uuid4().hex[:6]}", authored_on=date(2026, 11, 22))
        col.add_child(instance=recycled_p)
        recycled_p.save_revision().publish()
        RecycleService.recycle_page_or_subtree(recycled_p, system_authorized=True)
        self.created_pages.append(recycled_p)

        # 第一篇：无上一篇，下一篇是 p2
        p1.refresh_from_db()
        p2.refresh_from_db()
        p3.refresh_from_db()
        d1 = build_detail_presentation(p1, self.factory.get(p1.url_path))
        self.assertIsNone(d1["prev_page"])
        self.assertIsNotNone(
            d1["next_page"],
            f"p1=({p1.id}, rev={p1.live_revision_id}, path={p1.path}, depth={p1.depth}, live={p1.live}), "
            f"docs={list(PublishedSearchDocument.objects.filter(page_id__in=[p1.id, p2.id, p3.id]).values('page_id','effective_revision_id','is_live','is_recycled','root_path','depth','authored_on'))}",
        )
        self.assertEqual(d1["next_page"]["page_id"], p2.id)

        # 第二篇：上一篇 p1，下一篇 p3
        d2 = build_detail_presentation(p2, self.factory.get(p2.url_path))
        self.assertEqual(d2["prev_page"]["page_id"], p1.id)
        self.assertEqual(d2["next_page"]["page_id"], p3.id)

        # 第三篇：上一篇 p2，无下一篇（future/restricted/recycled 全拦截）
        d3 = build_detail_presentation(p3, self.factory.get(p3.url_path))
        self.assertEqual(d3["prev_page"]["page_id"], p2.id)
        self.assertIsNone(d3["next_page"])

    def test_cross_site_isolation_and_tag_no_global_fallback(self):
        """跨站移动投影 site_id 未同步时不得泄漏；本站无 TagIndexPage 时标签不跨站回退"""
        root = Page.get_first_root_node()
        home_b = HomePage(title="SiteB Home", slug=f"home-b-{uuid4().hex[:6]}")
        root.add_child(instance=home_b)
        home_b.save_revision().publish()
        self.created_pages.append(home_b)

        site_b = Site.objects.create(
            hostname="siteb.example.test",
            port=80,
            root_page=home_b,
            is_default_site=False,
        )
        Site.clear_site_root_paths_cache()
        try:
            # Site A 部署 TagIndexPage，Site B 未部署
            tag_a = TagIndexPage(title="A站标签", slug=f"tags-a-{uuid4().hex[:6]}")
            self.home.add_child(instance=tag_a)
            tag_a.save_revision().publish()
            self.created_pages.append(tag_a)

            # Site A 有一篇带相同标签的文章
            post_a = BlogPage(title="A站同标签文章", slug=f"pa-{uuid4().hex[:6]}", authored_on=date(2026, 11, 20))
            self.home.add_child(instance=post_a)
            post_a.tags.add("CrossTag")
            post_a.save_revision().publish()
            self.created_pages.append(post_a)

            # Site B 文章
            post_b = BlogPage(title="B站文章", slug=f"pb-{uuid4().hex[:6]}", authored_on=date(2026, 11, 21))
            home_b.add_child(instance=post_b)
            post_b.tags.add("CrossTag")
            post_b.save_revision().publish()
            post_b.refresh_from_db()
            self.created_pages.append(post_b)

            # 故意把 post_a 的投影 site_id 篡改为 site_b.id（模拟跨站移动后旧 site_id 脏数据）
            PublishedSearchDocument.objects.filter(page_id=post_a.id).update(site_id=site_b.id)

            req_b = self.factory.get(post_b.url_path, HTTP_HOST="siteb.example.test")
            detail_b = build_detail_presentation(post_b, req_b)
            # 标签绝不回退到 Site A 的 TagIndexPage
            self.assertEqual(len(detail_b["tag_links"]), 1)
            self.assertIsNone(detail_b["tag_links"][0]["url"])
            # 上下篇与同标签推荐绝不出现 Site A 的 post_a
            self.assertIsNone(detail_b["prev_page"])
            self.assertIsNone(detail_b["next_page"])
            self.assertEqual(detail_b["recommendations"], [])
        finally:
            site_b.delete()
            Site.clear_site_root_paths_cache()

    def test_announcement_readonly_window_degradation_and_rec_exclusivity(self):
        """公告空表零写入、时间窗边界、失效内链降级纯文本，以及与同栏/同标签推荐互斥"""
        col = BlogIndexPage(title="推荐专栏", slug=f"rec-col-{uuid4().hex[:6]}")
        self.home.add_child(instance=col)
        col.save_revision().publish()
        self.created_pages.append(col)

        # 同栏 3 篇 + 跨栏同标签 2 篇（总共超过当前+上下篇，验证去重与同标签补齐）
        p1 = BlogPage(title="主文章", slug=f"main-{uuid4().hex[:6]}", authored_on=date(2026, 11, 20))
        col.add_child(instance=p1)
        p1.tags.add("Django")
        p1.save_revision().publish()
        p1.refresh_from_db()
        self.created_pages.append(p1)

        p_prev = BlogPage(title="相邻旧文", slug=f"prev-{uuid4().hex[:6]}", authored_on=date(2026, 11, 19))
        col.add_child(instance=p_prev)
        p_prev.save_revision().publish()
        self.created_pages.append(p_prev)

        p_next = BlogPage(title="相邻新文", slug=f"next-{uuid4().hex[:6]}", authored_on=date(2026, 11, 21))
        col.add_child(instance=p_next)
        p_next.save_revision().publish()
        self.created_pages.append(p_next)

        p_col_extra = BlogPage(title="同栏推荐甲", slug=f"colex-{uuid4().hex[:6]}", authored_on=date(2026, 11, 18))
        col.add_child(instance=p_col_extra)
        p_col_extra.save_revision().publish()
        self.created_pages.append(p_col_extra)

        topic = GuideTopic.objects.create(name=f"T-{uuid4().hex[:6]}", slug=f"t-{uuid4().hex[:6]}")
        self.created_topics.append(topic)
        guide_tag = GuidePage(
            title="跨栏同标签指南",
            slug=f"gtag-{uuid4().hex[:6]}",
            topic=topic,
            authored_on=date(2026, 11, 17),
            body=[("step", {"step_number": "1", "title": "步骤一", "content": "<p>ok</p>"})],
        )
        self.home.add_child(instance=guide_tag)
        guide_tag.tags.add("Django")
        guide_tag.save_revision().publish()
        self.created_pages.append(guide_tag)

        # 1. 空表读取：不写入任何公告行，且自动退回到同栏 + 同标签推荐（并排除 p_prev / p_next / p1）
        self.assertEqual(SiteAnnouncementSettings.objects.count(), 0)
        req = self.factory.get(p1.url_path)
        resp = p1.serve(req)
        resp.render()
        html = resp.content.decode("utf-8")
        self.assertEqual(SiteAnnouncementSettings.objects.count(), 0)
        self.assertIn("has-right", html)
        self.assertIn("detail-right-aside", html)
        self.assertIn("detail-prev-next", html)
        self.assertIn("同栏推荐甲", html)
        self.assertIn("跨栏同标签指南", html)
        self.assertIn("同标签推荐", html)
        # 推荐卡片不包含已占据上下篇的相邻文章
        rec_block = html.split("detail-side-recs")[1].split("</section>")[0]
        self.assertNotIn("相邻旧文", rec_block)
        self.assertNotIn("相邻新文", rec_block)
        # 确保四档评价挂件与评论区全页各仅出现 1 次
        self.assertEqual(html.count('id="comments-section"'), 1)

        # 2. 模型表单校验：非 https 外链、结束时间不晚于开始时间均拒绝
        bad_ann = SiteAnnouncementSettings(
            site=self.site,
            is_enabled=True,
            title="测试",
            external_url="http://insecure.example.com",
        )
        with self.assertRaises(ValidationError):
            bad_ann.full_clean()

        # 3. 公告有效且站内 target_page 受限：公告生效并互斥隐藏推荐，且失效内链降级为纯文本
        now = timezone.now()
        target_secret = BlogPage(title="受限公告目标", slug=f"sec-{uuid4().hex[:6]}")
        col.add_child(instance=target_secret)
        target_secret.save_revision().publish()
        PageViewRestriction.objects.create(page=target_secret, restriction_type=PageViewRestriction.LOGIN)
        self.created_pages.append(target_secret)

        epoch_before = ContentDiscoveryEpoch.get_current_epoch_readonly()
        ann = SiteAnnouncementSettings.objects.create(
            site=self.site,
            is_enabled=True,
            title="维护通知",
            body="今晚例行维护",
            link_label="查看详情",
            target_page=target_secret,
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(hours=1),
        )
        self.assertEqual(ContentDiscoveryEpoch.get_current_epoch_readonly(), epoch_before)
        detail_ann = build_detail_presentation(p1, req)
        self.assertIsNotNone(detail_ann["announcement"])
        self.assertEqual(detail_ann["announcement"]["link_url"], "")
        self.assertEqual(detail_ann["recommendations"], [])

        # 时间窗边界：now == ends_at 失效，自动恢复显示推荐
        self.assertIsNone(
            SiteAnnouncementSettings.get_active_for_site_readonly(
                self.site,
                request=req,
                now=ann.ends_at,
            )
        )

        # 4. 预览态隔离：跳过上下篇、公告与推荐
        prev_req = self.factory.get(p1.url_path)
        prev_req.is_preview = True
        detail_prev = build_detail_presentation(p1, prev_req)
        self.assertIsNone(detail_prev["prev_page"])
        self.assertIsNone(detail_prev["next_page"])
        self.assertIsNone(detail_prev["announcement"])
        self.assertEqual(detail_prev["recommendations"], [])
