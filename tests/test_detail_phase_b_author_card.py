"""
Phase B 详情页右侧作者名片卡、本月 SQL 热力图与「作者好文 / 作者发言」防漏回归测试。
"""

from datetime import date, timedelta
from pathlib import Path
import re
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.utils import timezone
from wagtail.models import Page, PageViewRestriction, Site

from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from comments.services import CommentService
from content_blocks.rendering.detail import build_detail_presentation
from content_lifecycle.recycle import RecycleService
from guides.models import GuideIndexPage, GuidePage, GuidePageAuthor, GuideTopic
from home.models import HomePage
from people.models import Person, get_or_create_anonymous_person
from portfolio.models import PortfolioIndexPage, ProjectPage, ProjectPagePerson

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DetailPhaseBAuthorCardTests(TestCase):
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

    def test_phase_b_template_and_css_hygiene(self):
        """作者名片模板零内联 style= / <style，detail-layout.css 保持零裸 #hex"""
        tpl = (PROJECT_ROOT / "myblog/templates/detail/_detail_author_card.html").read_text(encoding="utf-8")
        self.assertNotIn("style=", tpl)
        self.assertNotIn("<style", tpl.lower())

        detail_css = (PROJECT_ROOT / "myblog/static/css/detail-layout.css").read_text(encoding="utf-8")
        cleaned = re.sub(r"/\*.*?\*/", "", detail_css, flags=re.DOTALL)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", cleaned), [])

    def test_person_rebind_unbind_resets_show_public_comments_and_clean_validation(self):
        """换绑或解绑 linked_user 时自动复位 show_public_comments=False，匿名与未绑定禁止开启"""
        User = get_user_model()
        u1 = User.objects.create_user(username=f"au1_{uuid4().hex[:6]}", password="StrongPass123!")
        u2 = User.objects.create_user(username=f"au2_{uuid4().hex[:6]}", password="StrongPass123!")

        anon = get_or_create_anonymous_person()
        anon.show_public_comments = True
        with self.assertRaises(ValidationError):
            anon.full_clean()
        anon.save()
        anon.refresh_from_db()
        self.assertFalse(anon.show_public_comments)

        unbound = Person.objects.create(name="未绑定作者")
        unbound.show_public_comments = True
        with self.assertRaises(ValidationError):
            unbound.full_clean()
        unbound.save()
        unbound.refresh_from_db()
        self.assertFalse(unbound.show_public_comments)

        p = Person.objects.create(name="绑定作者", linked_user=u1, show_public_comments=True)
        self.assertTrue(p.show_public_comments)

        # 换绑账号：强制复位为 False
        p.linked_user = u2
        p.save()
        p.refresh_from_db()
        self.assertFalse(p.show_public_comments)

        # 重新开启后解绑：强制复位为 False
        p.show_public_comments = True
        p.save()
        p.refresh_from_db()
        self.assertTrue(p.show_public_comments)
        p.linked_user = None
        p.save()
        p.refresh_from_db()
        self.assertFalse(p.show_public_comments)

    def test_anonymous_unlinked_suspended_and_preview_author_card_boundaries(self):
        """系统佚名不展示名片卡；未绑定或停用账号不展示核验徽章和发言 Tab；预览态不查名片"""
        User = get_user_model()
        u_suspended = User.objects.create_user(
            username=f"susp_{uuid4().hex[:6]}",
            password="StrongPass123!",
            is_active=False,
        )
        p_anon_post = BlogPage(title="佚名文章", slug=f"anon-{uuid4().hex[:6]}")
        self.home.add_child(instance=p_anon_post)
        p_anon_post.save_revision().publish()
        p_anon_post.refresh_from_db()
        self.created_pages.append(p_anon_post)

        d_anon = build_detail_presentation(p_anon_post, self.factory.get(p_anon_post.url_path))
        self.assertIsNone(d_anon["author_card"])

        # 未绑定作者与停用账号作者均不展示核验与发言 Tab
        person_susp = Person.objects.create(
            name="停用绑定作者",
            bio="架构师",
            linked_user=u_suspended,
            show_public_comments=True,
        )
        post = BlogPage(title="停用作者文章", slug=f"susp-post-{uuid4().hex[:6]}")
        post.authors = [BlogPagePerson(person=person_susp, role="primary_author")]
        self.home.add_child(instance=post)
        post.save_revision().publish()
        post.refresh_from_db()
        self.created_pages.append(post)

        d_susp = build_detail_presentation(post, self.factory.get(post.url_path))
        self.assertIsNotNone(d_susp["author_card"])
        self.assertFalse(d_susp["author_card"]["is_verified"])
        self.assertFalse(d_susp["author_card"]["show_comments_tab"])

        prev_req = self.factory.get(post.url_path)
        prev_req.is_preview = True
        self.assertIsNone(build_detail_presentation(post, prev_req)["author_card"])

    def test_multi_author_heatmap_and_cross_type_recent_articles(self):
        """多作者主笔优先去重、本月热力图排除审校/未来/上月，作者好文支持跨类型且排除当前页"""
        today = timezone.localdate()
        prev_month_day = today.replace(day=1) - timedelta(days=2)
        future_day = today + timedelta(days=5)

        lead = Person.objects.create(name="主笔沈括", bio="写数据库和架构")
        co = Person.objects.create(name="共同作者祖冲之")
        rev = Person.objects.create(name="审校刘徽")

        col = BlogIndexPage(title="作者测试专栏", slug=f"acol-{uuid4().hex[:6]}")
        self.home.add_child(instance=col)
        col.save_revision().publish()
        self.created_pages.append(col)

        # 当前阅读文章：故意把审校放 sort_order=0，主笔放第 2 位，验证主笔仍胜出
        cur_post = BlogPage(
            title="当前主读博文",
            slug=f"cur-{uuid4().hex[:6]}",
            authored_on=today,
        )
        cur_post.authors = [
            BlogPagePerson(person=rev, role="reviewer", sort_order=0),
            BlogPagePerson(person=lead, role="primary_author", sort_order=1),
            BlogPagePerson(person=co, role="co_author", sort_order=2),
        ]
        col.add_child(instance=cur_post)
        cur_post.save_revision().publish()
        cur_post.refresh_from_db()
        self.created_pages.append(cur_post)

        # 同月同日第 2 篇（指南类型），主笔为 co_author -> 计入热力图 & 作者好文
        guide_idx = GuideIndexPage(title="指南", slug=f"agi-{uuid4().hex[:6]}")
        self.home.add_child(instance=guide_idx)
        guide_idx.save_revision().publish()
        self.created_pages.append(guide_idx)
        topic = GuideTopic.objects.create(name=f"AT-{uuid4().hex[:6]}", slug=f"at-{uuid4().hex[:6]}")
        self.created_topics.append(topic)

        g_same_day = GuidePage(
            title="同日跨类型指南",
            slug=f"gsd-{uuid4().hex[:6]}",
            topic=topic,
            authored_on=today,
            body=[("step", {"step_number": "1", "title": "S1", "content": "<p>ok</p>"})],
        )
        g_same_day.authors = [GuidePageAuthor(person=lead, role="co_author")]
        guide_idx.add_child(instance=g_same_day)
        g_same_day.save_revision().publish()
        self.created_pages.append(g_same_day)

        # 同月仅担任审校的文章（项目类型）-> 不计入热力图，但计入全部参与作品（作者好文）
        port_idx = PortfolioIndexPage(title="项目", slug=f"api-{uuid4().hex[:6]}")
        self.home.add_child(instance=port_idx)
        port_idx.save_revision().publish()
        self.created_pages.append(port_idx)
        p_review_only = ProjectPage(
            title="仅审校开源项目",
            slug=f"prev-{uuid4().hex[:6]}",
            authored_on=today,
        )
        p_review_only.authors = [ProjectPagePerson(person=lead, role="reviewer")]
        port_idx.add_child(instance=p_review_only)
        p_review_only.save_revision().publish()
        self.created_pages.append(p_review_only)

        # 上月文章与未来文章 -> 不计入本月热力图（未来文章也不进作者好文）
        p_old = BlogPage(title="上月旧博文", slug=f"old-{uuid4().hex[:6]}", authored_on=prev_month_day)
        p_old.authors = [BlogPagePerson(person=lead, role="primary_author")]
        col.add_child(instance=p_old)
        p_old.save_revision().publish()
        self.created_pages.append(p_old)

        p_fut = BlogPage(title="未来排期博文", slug=f"fut-{uuid4().hex[:6]}", authored_on=future_day)
        p_fut.authors = [BlogPagePerson(person=lead, role="primary_author")]
        col.add_child(instance=p_fut)
        p_fut.save_revision().publish()
        self.created_pages.append(p_fut)

        resp = self.client.get(cur_post.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        detail = build_detail_presentation(cur_post, self.factory.get(cur_post.url_path))
        ac = detail["author_card"]
        self.assertIsNotNone(ac)
        self.assertEqual(ac["name"], "主笔沈括")
        self.assertEqual([o["name"] for o in ac["other_authors"]], ["共同作者祖冲之", "审校刘徽"])
        # 本月仅 cur_post + g_same_day 共 2 篇（排除 reviewer-only 的 p_review_only、上月与未来）
        self.assertEqual(ac["heatmap"]["month_total"], 2)
        # 作者好文排除当前页与未来页，包含指南、项目、上月旧文
        art_titles = [a["title"] for a in ac["recent_articles"]]
        self.assertNotIn("当前主读博文", art_titles)
        self.assertNotIn("未来排期博文", art_titles)
        self.assertIn("同日跨类型指南", art_titles)
        self.assertIn("仅审校开源项目", art_titles)
        self.assertIn("上月旧博文", art_titles)
        self.assertIn("author-heatmap-grid", html)
        self.assertIn("主笔沈括", html)

    def test_author_comments_security_anti_leakage(self):
        """作者发言防漏：拦截隐藏根评论下的回复、待审V2不覆盖V1、祖先回收/受限拦截、未授权拦截"""
        User = get_user_model()
        admin_user = User.objects.create_superuser(
            username=f"mod_{uuid4().hex[:6]}",
            email="mod@example.com",
            password="AdminPass123!",
        )
        author_user = User.objects.create_user(
            username=f"uauth_{uuid4().hex[:6]}",
            password="StrongPass123!",
        )
        other_user = User.objects.create_user(
            username=f"uoth_{uuid4().hex[:6]}",
            password="StrongPass123!",
        )

        person = Person.objects.create(
            name="发言作者",
            linked_user=author_user,
            show_public_comments=False,
        )
        col = BlogIndexPage(title="正常专栏", slug=f"ccol-{uuid4().hex[:6]}")
        self.home.add_child(instance=col)
        col.save_revision().publish()
        self.created_pages.append(col)

        article = BlogPage(title="作者文章", slug=f"ca-{uuid4().hex[:6]}", authored_on=date(2026, 11, 20))
        article.authors = [BlogPagePerson(person=person, role="primary_author")]
        col.add_child(instance=article)
        article.save_revision().publish()
        article.refresh_from_db()
        self.created_pages.append(article)

        # 1. 公开评论 V1 已批准，随后在 5 分钟内提交待审 V2
        c_public = CommentService.post_comment(
            article,
            author_user,
            "已批准公开安全内容",
            auto_approve=True,
        )
        CommentService.edit_comment(
            c_public.id,
            author_user,
            "未过审敏感草稿内容",
        )

        # 2. 在另一根评论下发一条已批准回复，然后由管理员隐藏该根评论（子回复仍 active 且 is_publicly_visible=True）
        hidden_root = CommentService.post_comment(article, other_user, "即将被隐藏的根评论", auto_approve=True)
        reply_under_hidden = CommentService.post_comment(
            article,
            author_user,
            "隐藏线程下的作者回复不应泄漏",
            parent_id=hidden_root.id,
            auto_approve=True,
        )
        CommentService.moderate_comment(hidden_root.id, action="hide", reviewer=admin_user, reason="违规")

        # 3. 在被父级限制 / 被回收的页面下的评论均不可出现
        secret_col = BlogIndexPage(title="受限专栏", slug=f"scol-{uuid4().hex[:6]}")
        self.home.add_child(instance=secret_col)
        secret_col.save_revision().publish()
        self.created_pages.append(secret_col)
        secret_post = BlogPage(title="受限文章", slug=f"sp-{uuid4().hex[:6]}")
        secret_col.add_child(instance=secret_post)
        secret_post.save_revision().publish()
        secret_post.refresh_from_db()
        self.created_pages.append(secret_post)
        CommentService.post_comment(secret_post, author_user, "祖先受限页面里的评论", auto_approve=True)
        PageViewRestriction.objects.create(page=secret_col, restriction_type=PageViewRestriction.LOGIN)

        rec_col = BlogIndexPage(title="回收专栏", slug=f"rcol-{uuid4().hex[:6]}")
        self.home.add_child(instance=rec_col)
        rec_col.save_revision().publish()
        self.created_pages.append(rec_col)
        rec_post = BlogPage(title="回收文章", slug=f"rp-{uuid4().hex[:6]}")
        rec_col.add_child(instance=rec_post)
        rec_post.save_revision().publish()
        rec_post.refresh_from_db()
        self.created_pages.append(rec_post)
        CommentService.post_comment(rec_post, author_user, "祖先已回收页面里的评论", auto_approve=True)
        RecycleService.recycle_page_or_subtree(rec_col, system_authorized=True)

        # A) 未开启 show_public_comments 时，即使已绑定账号也绝不显示发言 Tab
        d_off = build_detail_presentation(article, self.factory.get(article.url_path))
        self.assertTrue(d_off["author_card"]["is_verified"])
        self.assertFalse(d_off["author_card"]["show_comments_tab"])
        self.assertEqual(d_off["author_card"]["recent_comments"], [])

        # B) 开启 show_public_comments 后：只见 V1 安全摘要，不见 V2/隐藏根子回复/祖先受限/祖先回收
        person.show_public_comments = True
        person.save()
        d_on = build_detail_presentation(article, self.factory.get(article.url_path))
        ac = d_on["author_card"]
        self.assertTrue(ac["show_comments_tab"])
        excerpts = [c["excerpt"] for c in ac["recent_comments"]]
        self.assertEqual(len(excerpts), 1)
        self.assertIn("已批准公开安全内容", excerpts[0])
        self.assertNotIn("未过审敏感草稿内容", excerpts[0])
        self.assertNotIn(reply_under_hidden.id, [c["comment_id"] for c in ac["recent_comments"]])
