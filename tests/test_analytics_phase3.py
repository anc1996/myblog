"""
第 3 期类型专属转化、/media-gateway/ 课件下载归因（含 Range 合并）、
作者产能与审核效率看板、受控 CSV 导出与防小样本反推测试。
"""

from datetime import timedelta
import json
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.test import Client, TestCase
from django.utils import timezone
from wagtail.models import Collection, Page, PageLogEntry, Site

from analytics.collector import _get_redis_client, _resolve_prefix
from analytics.exports import MAX_EXPORT_ROWS, export_analytics_csv, sanitize_csv_cell
from analytics.models import (
    AnalyticsExportAudit,
    AnalyticsSettings,
    ArticleRating,
    ArticleRatingAggregate,
    PageAnalyticsDaily,
    PageAnalyticsTotal,
)
from analytics.operations import get_operations_dashboard_data
from analytics.services import get_page_analytics_detail, get_project_tech_stack_heat
from analytics.settlement import settle_pv_and_reading_batches
from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from comments.models import Comment, CommentReviewAction, CommentVersion
from content_lifecycle.permissions import ensure_author_role_infrastructure
from guides.models import GuideIndexPage, GuidePage, GuidePageAuthor, GuideTopic
from home.models import HomePage
from media_library.models import CustomDocument, VisibilityChoices
from people.models import Person
from portfolio.models import PortfolioIndexPage, ProjectPage, ProjectPagePerson

User = get_user_model()


def _cleanup_redis():
    client = _get_redis_client(write=True)
    if client is None:
        return
    prefix = _resolve_prefix()
    for pattern in (f"{prefix}:analytics:*",):
        cursor = 0
        while True:
            cursor, keys = client.scan(cursor=cursor, match=pattern, count=200)
            if keys:
                client.delete(*keys)
            if cursor == 0:
                break


def _extract_descriptor(html_text: str) -> str:
    marker = 'name="analytics-descriptor" content="'
    idx = html_text.index(marker) + len(marker)
    return html_text[idx : html_text.index('"', idx)]


def _setup_tree():
    root = Page.get_first_root_node() or Page.add_root(title="Root")
    home = HomePage.objects.first()
    if not home:
        home = HomePage(title="P3 Home", slug=f"p3h-{uuid4().hex[:6]}")
        root.add_child(instance=home)
        home.save_revision().publish()
        home.refresh_from_db()
    site = Site.objects.filter(is_default_site=True).first()
    if not site:
        site = Site.objects.create(hostname="testserver", port=80, root_page=home, is_default_site=True)
    else:
        site.hostname = "testserver"
        site.root_page = home
        site.save()

    blog_idx = BlogIndexPage(title="P3 Blog", slug=f"p3b-{uuid4().hex[:6]}")
    home.add_child(instance=blog_idx)
    blog_idx.save_revision().publish()

    guide_idx = GuideIndexPage(title="P3 Guides", slug=f"p3g-{uuid4().hex[:6]}")
    home.add_child(instance=guide_idx)
    guide_idx.save_revision().publish()

    port_idx = PortfolioIndexPage(title="P3 Portfolio", slug=f"p3p-{uuid4().hex[:6]}")
    home.add_child(instance=port_idx)
    port_idx.save_revision().publish()

    s = AnalyticsSettings.for_site(site)
    s.collection_enabled = True
    s.rating_enabled = True
    s.ranking_enabled = True
    s.save()
    return site, home, blog_idx, guide_idx, port_idx


class Phase3ConversionsAndMediaGatewayTests(TestCase):
    def setUp(self):
        _cleanup_redis()
        self.site, self.home, self.blog_idx, self.guide_idx, self.port_idx = _setup_tree()
        self.client = Client(HTTP_HOST="testserver")
        self.admin = User.objects.create_superuser("p3_admin", "p3a@example.com", "Pass123456!")
        self.topic = GuideTopic.objects.create(name="P3 Topic", slug=f"p3t-{uuid4().hex[:6]}")
        self.root_col = Collection.get_first_root_node()

    def tearDown(self):
        _cleanup_redis()

    def _issue_visit_token(self, page_obj) -> str:
        html = self.client.get(page_obj.url).content.decode("utf-8")
        desc = _extract_descriptor(html)
        resp = self.client.post(
            "/api/v1/analytics/context/",
            data=json.dumps({"descriptor": desc}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()["visit_token"]

    def test_blog_code_copy_mermaid_dedup_and_client_doc_download_blocked(self):
        blog = BlogPage(
            title="Code & Mermaid Blog",
            slug=f"cmb-{uuid4().hex[:6]}",
            body=[
                ("markdown", {"markdown": "## 详情\n" + ("深度技术长文段落。" * 220)}),
                ("code", {"language": "python", "code": "print('hi')"}),
                ("mermaid", {"title": "Flow", "diagram_code": "flowchart LR\nA-->B"}),
            ],
        )
        self.blog_idx.add_child(instance=blog)
        blog.save_revision().publish()
        blog.refresh_from_db()

        vt = self._issue_visit_token(blog)
        # 同一代码块重复发送 2 次仅记 1 次，不同代码块记第 2 次；客户端伪造 doc_download 必须被拒
        r_ev = self.client.post(
            "/api/v1/analytics/events/",
            data=json.dumps({
                "visit_token": vt,
                "events": [
                    {"seq": 1, "event": "conversion", "conversion_key": "code_copy", "conversion_target": "code:b1"},
                    {"seq": 2, "event": "conversion", "conversion_key": "code_copy", "conversion_target": "code:b1"},
                    {"seq": 3, "event": "conversion", "conversion_key": "code_copy", "conversion_target": "code:b2"},
                    {"seq": 4, "event": "conversion", "conversion_key": "mermaid_view", "conversion_target": "mermaid:m1"},
                    {"seq": 5, "event": "conversion", "conversion_key": "mermaid_view", "conversion_target": "mermaid:m1"},
                    {"seq": 6, "event": "conversion", "conversion_key": "doc_download", "conversion_target": "doc:fake"},
                ],
            }),
            content_type="application/json",
        )
        self.assertEqual(r_ev.status_code, 200)
        self.assertEqual(r_ev.json()["processed"], 3)
        self.assertEqual(r_ev.json()["duplicates"], 2)

        settle_pv_and_reading_batches()
        detail = get_page_analytics_detail(self.admin, blog)
        items = {it["key"]: it["count"] for it in detail["type_conversions"]["items"]}
        self.assertEqual(items["code_copy"], 2)
        self.assertEqual(items["mermaid_view"], 1)
        self.assertTrue(detail["type_conversions"]["is_long_article"])

    def test_guide_step_and_media_gateway_range_dedup_and_misattribution_guard(self):
        pub_doc = CustomDocument.objects.create(
            title="课件 ZIP",
            file=ContentFile(b"0123456789ABCDEF", name="course.zip"),
            collection=self.root_col,
            visibility=VisibilityChoices.PUBLIC,
        )
        other_doc = CustomDocument.objects.create(
            title="别处公共附件",
            file=ContentFile(b"OTHER-DOC-DATA", name="other.zip"),
            collection=self.root_col,
            visibility=VisibilityChoices.PUBLIC,
        )
        restricted_doc = CustomDocument.objects.create(
            title="受限附件",
            file=ContentFile(b"SECRET-DATA", name="secret.zip"),
            collection=self.root_col,
            visibility=VisibilityChoices.RESTRICTED,
        )

        guide = GuidePage(
            title="Docker 实战指南",
            slug=f"gd-{uuid4().hex[:6]}",
            topic=self.topic,
            last_reviewed_at=timezone.localdate() - timedelta(days=10),
            body=[
                ("step", {"step_number": "1", "title": "安装", "content": "<p>步骤 1</p>"}),
                ("step", {"step_number": "2", "title": "运行", "content": "<p>步骤 2</p>"}),
                ("document", {"document": pub_doc}),
            ],
        )
        self.guide_idx.add_child(instance=guide)
        guide.save_revision().publish()
        guide.refresh_from_db()

        vt = self._issue_visit_token(guide)
        self.client.post(
            "/api/v1/analytics/events/",
            data=json.dumps({
                "visit_token": vt,
                "events": [
                    {"seq": 1, "event": "conversion", "conversion_key": "step_reach", "conversion_target": "step:step-1"},
                    {"seq": 2, "event": "conversion", "conversion_key": "step_complete", "conversion_target": "step:step-1"},
                    {"seq": 3, "event": "conversion", "conversion_key": "step_complete", "conversion_target": "step:step-1"},
                    {"seq": 4, "event": "conversion", "conversion_key": "doc_attempt", "conversion_target": f"doc:{pub_doc.public_id}"},
                ],
            }),
            content_type="application/json",
        )

        # 携带同页下载归因 Cookie 发起 HEAD（不计）、两次 Range 206 分段（合并为 1 次）、416 越界（不计）
        self.client.cookies["myblog_ana_dl_vt"] = vt
        r_head = self.client.head(f"/media-gateway/document/{pub_doc.public_id}/")
        self.assertEqual(r_head.status_code, 200)
        r_416 = self.client.get(f"/media-gateway/document/{pub_doc.public_id}/", HTTP_RANGE="bytes=9999-10000")
        self.assertEqual(r_416.status_code, 416)
        r_206_a = self.client.get(f"/media-gateway/document/{pub_doc.public_id}/", HTTP_RANGE="bytes=0-3")
        self.assertEqual(r_206_a.status_code, 206)
        r_206_b = self.client.get(f"/media-gateway/document/{pub_doc.public_id}/", HTTP_RANGE="bytes=4-7")
        self.assertEqual(r_206_b.status_code, 206)

        # 反例：未挂载到本指南的公共附件 other_doc 与 403 受限附件均绝不计入本指南 doc_download
        r_other = self.client.get(f"/media-gateway/document/{other_doc.public_id}/")
        self.assertEqual(r_other.status_code, 200)
        r_forbid = self.client.get(f"/media-gateway/document/{restricted_doc.public_id}/")
        self.assertEqual(r_forbid.status_code, 403)

        settle_pv_and_reading_batches()
        detail = get_page_analytics_detail(self.admin, guide)
        items = {it["key"]: it["count"] for it in detail["type_conversions"]["items"]}
        self.assertEqual(items["step_reach"], 1)
        self.assertEqual(items["step_complete"], 1)
        self.assertEqual(items["doc_attempt"], 1)
        self.assertEqual(items["doc_download"], 1)
        self.assertEqual(detail["type_conversions"]["meta"]["step_count"], 2)
        self.assertEqual(detail["type_conversions"]["meta"]["freshness_status"], "healthy")

    def test_project_repo_demo_clicks_and_tech_stack_heat(self):
        proj = ProjectPage(
            title="FastGate",
            slug=f"fg-{uuid4().hex[:6]}",
            primary_language="Rust",
            star_count=9999,
            repo_url="https://github.com/example/fastgate",
            demo_url="https://demo.example.com",
            body=[("repository", {"platform": "github", "repo_url": "https://github.com/example/fastgate", "demo_url": "https://demo.example.com"})],
        )
        proj.tags.add("Rust", "Gateway")
        self.port_idx.add_child(instance=proj)
        proj.save_revision().publish()
        proj.refresh_from_db()

        html = self.client.get(proj.url).content.decode("utf-8")
        self.assertIn('data-analytics-conv="repo_click"', html)
        self.assertIn('data-analytics-conv="demo_click"', html)

        vt = self._issue_visit_token(proj)
        self.client.post(
            "/api/v1/analytics/events/",
            data=json.dumps({
                "visit_token": vt,
                "events": [
                    {"seq": 1, "event": "conversion", "conversion_key": "repo_click", "conversion_target": "repo_click"},
                    {"seq": 2, "event": "conversion", "conversion_key": "demo_click", "conversion_target": "demo_click"},
                ],
            }),
            content_type="application/json",
        )
        settle_pv_and_reading_batches()
        # 注入昨日健康日验证技术栈热度（绝不把 star_count=9999 纳入）
        PageAnalyticsDaily.objects.create(
            site=self.site,
            page=proj,
            live_revision_id=proj.live_revision_id,
            type_key="project",
            stat_date=timezone.localdate() - timedelta(days=1),
            health_status="normal",
            pv=20,
            measurable_views=16,
            valid_views=12,
            conversions_json={"repo_click": 3, "demo_click": 1},
        )
        heat = get_project_tech_stack_heat(self.site, days=30)
        rust_row = [r for r in heat["languages"] if r["name"] == "Rust"][0]
        self.assertEqual(rust_row["valid_views"], 12)
        self.assertEqual(rust_row["repo_clicks"], 3)
        self.assertEqual(rust_row["demo_clicks"], 1)
        self.assertLess(rust_row["valid_views"], 100)


class Phase3OperationsAndExportTests(TestCase):
    def setUp(self):
        self.site, self.home, self.blog_idx, self.guide_idx, self.port_idx = _setup_tree()
        ensure_author_role_infrastructure()
        authors_group = Group.objects.get(name="Authors")
        self.admin = User.objects.create_superuser("op_admin", "opa@example.com", "Pass123456!")
        self.author_user = User.objects.create_user("op_author", "opau@example.com", "Pass123456!")
        self.other_user = User.objects.create_user("op_other", "opo@example.com", "Pass123456!")

        perm_view_page = Permission.objects.get(codename="view_page_analytics")
        perm_admin = Permission.objects.get(codename="access_admin")
        self.author_user.user_permissions.add(perm_view_page, perm_admin)
        self.other_user.user_permissions.add(perm_view_page, perm_admin)
        self.author_user.groups.add(authors_group)
        self.other_user.groups.add(authors_group)

        self.person_a = Person.objects.create(name="张作者", linked_user=self.author_user)
        self.person_b = Person.objects.create(name="李合著", linked_user=self.other_user)
        self.topic = GuideTopic.objects.create(name="Op Topic", slug=f"opt-{uuid4().hex[:6]}")

        # 合著文章（属 author_user，由 person_a 与 person_b 共同署名）
        self.co_blog = BlogPage(
            title="=CMD('calc') 危险公式标题",
            slug=f"cob-{uuid4().hex[:6]}",
            owner=self.author_user,
            body=[("markdown", {"markdown": "coauthored content"})],
        )
        self.blog_idx.add_child(instance=self.co_blog)
        BlogPagePerson.objects.create(page=self.co_blog, person=self.person_a, role="primary_author", sort_order=0)
        BlogPagePerson.objects.create(page=self.co_blog, person=self.person_b, role="co_author", sort_order=1)
        self.co_blog.save_revision().publish()
        self.co_blog.refresh_from_db()
        PageLogEntry.objects.create(
            page=self.co_blog,
            action="wagtail.publish",
            user=self.author_user,
            timestamp=timezone.now(),
            label=self.co_blog.title,
        )

        # 他人独立指南（逾期 400 天且未分配维护人）
        self.overdue_guide = GuidePage(
            title="逾期指南",
            slug=f"og-{uuid4().hex[:6]}",
            owner=self.other_user,
            topic=self.topic,
            last_reviewed_at=timezone.localdate() - timedelta(days=400),
            body=[("step", {"step_number": "1", "title": "步骤", "content": "<p>s</p>"})],
        )
        self.guide_idx.add_child(instance=self.overdue_guide)
        self.overdue_guide.save_revision().publish()
        self.overdue_guide.refresh_from_db()

        # 评论审核记录（1 条待审 + 1 条通过 + 1 条驳回）
        now = timezone.now()
        c_pending = Comment.objects.create(
            page=self.co_blog,
            page_document_uuid=self.co_blog.document_uuid,
            guest_name="访客A",
            status="pending",
            pending_submitted_at=now - timedelta(hours=30),
        )
        c_app = Comment.objects.create(
            page=self.co_blog,
            page_document_uuid=self.co_blog.document_uuid,
            guest_name="访客B",
            status="active",
            pending_submitted_at=now - timedelta(hours=10),
        )
        c_rej = Comment.objects.create(
            page=self.co_blog,
            page_document_uuid=self.co_blog.document_uuid,
            guest_name="访客C",
            status="rejected",
            pending_submitted_at=now - timedelta(hours=4),
        )
        CommentReviewAction.objects.create(
            comment=c_app, actor=self.admin, action="approve", previous_status="pending", new_status="active"
        )
        CommentReviewAction.objects.create(
            comment=c_rej, actor=self.admin, action="reject", previous_status="pending", new_status="rejected"
        )

        # 小样本评价（仅 3 票 < min_rating_votes=20），用于校验导出防反推
        ArticleRatingAggregate.objects.create(
            site=self.site,
            page=self.co_blog,
            live_revision_id=self.co_blog.live_revision_id,
            excellent_count=3,
        )

    def test_operations_dashboard_permissions_capacity_and_review_metrics(self):
        admin_ops = get_operations_dashboard_data(self.admin, site_id=self.site.id, days=30)
        self.assertTrue(admin_ops["is_site_manager"])
        self.assertTrue(admin_ops["can_view_governance"])
        by_person = {r["person"].id: r for r in admin_ops["authors"]["rows"]}
        self.assertEqual(by_person[self.person_a.id]["page_count"], 1)
        self.assertEqual(by_person[self.person_b.id]["page_count"], 1)
        self.assertIn("观察中", by_person[self.person_a.id]["rating_note"])
        self.assertIsNone(by_person[self.person_a.id]["favorable_pct"])

        # 逾期未分配指南可见
        og_ids = [g["page_id"] for g in admin_ops["authors"]["overdue_guides"]]
        self.assertIn(self.overdue_guide.id, og_ids)

        # 评论审核效率统计：1 条待审（1~3 天桶）、1 通过 / 1 驳回 -> 50.0% 驳回率
        cm = admin_ops["review_governance"]["comments"]
        self.assertEqual(cm["backlog"]["total"], 1)
        self.assertEqual(cm["backlog"]["d1_to_3"], 1)
        self.assertEqual(cm["approved_count"], 1)
        self.assertEqual(cm["rejected_count"], 1)
        self.assertEqual(cm["rejection_rate_pct"], 50.0)

        # 受限作者仅见本人绑定 Person 与本人有权页面，不暴露全站审核与他人数据
        author_ops = get_operations_dashboard_data(self.author_user, site_id=self.site.id, days=30)
        self.assertFalse(author_ops["is_site_manager"])
        self.assertFalse(author_ops["can_view_governance"])
        self.assertIsNone(author_ops["review_governance"])
        self.assertEqual(len(author_ops["authors"]["rows"]), 1)
        self.assertEqual(author_ops["authors"]["rows"][0]["person"].id, self.person_a.id)
        self.assertEqual([g["page_id"] for g in author_ops["authors"]["overdue_guides"]], [])

    def test_controlled_csv_export_formula_escape_small_sample_mask_and_audit(self):
        self.assertEqual(sanitize_csv_cell("=CMD('calc')"), "'=CMD('calc')")
        self.assertEqual(sanitize_csv_cell("+1+1"), "'+1+1")

        # 1. 全站作者产能导出：小样本评价（3 < 20）不得输出好评率数值
        resp_auth = export_analytics_csv(
            self.admin,
            export_kind="author_capacity",
            site=self.site,
            filters={"days": 30},
        )
        self.assertEqual(resp_auth.status_code, 200)
        csv_text = resp_auth.content.decode("utf-8")
        self.assertIn("观察中 (3/20)", csv_text)
        self.assertNotIn("100.0", csv_text)

        # 2. 受限作者导出 page_summary：仅导出本人页面，且危险公式前缀已被转义
        resp_page = export_analytics_csv(
            self.author_user,
            export_kind="page_summary",
            site=self.site,
            filters={"days": 30},
        )
        page_csv = resp_page.content.decode("utf-8")
        self.assertIn("'=CMD('calc') 危险公式标题", page_csv)
        self.assertNotIn("逾期指南", page_csv)

        # 3. 受限作者越权导出全站 author_capacity 必须拒绝并写审计日志
        with self.assertRaises(PermissionDenied):
            export_analytics_csv(self.author_user, export_kind="author_capacity", site=self.site)
        self.assertTrue(
            AnalyticsExportAudit.objects.filter(
                actor=self.author_user,
                export_kind="author_capacity",
                status="rejected",
                reason="permission_denied",
            ).exists()
        )

        # 4. 超出 MAX_EXPORT_ROWS 拒绝并写审计日志
        client = Client(HTTP_HOST="testserver")
        client.force_login(self.admin)
        r_ops = client.get("/admin/analytics/operations/", {"site_id": self.site.id})
        self.assertEqual(r_ops.status_code, 200)
        self.assertIn("作者产能、审核效率与治理看板", r_ops.content.decode("utf-8"))
        self.assertEqual(client.get("/admin/analytics/operations/", {"p": 101}).status_code, 422)

        with patch("analytics.exports.MAX_EXPORT_ROWS", 0):
            with self.assertRaises(ValidationError):
                export_analytics_csv(
                    self.admin,
                    export_kind="page_summary",
                    site=self.site,
                    filters={"days": 30, "sort": "latest"},
                )
        self.assertTrue(
            AnalyticsExportAudit.objects.filter(
                actor=self.admin,
                export_kind="page_summary",
                status="rejected",
                reason="max_rows_exceeded",
            ).exists()
        )
