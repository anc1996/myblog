"""
tests.test_comment_frontend
前端详情页（BlogPage / GuidePage / ProjectPage）评论区渲染与接口回归测试。

覆盖验证：
1. BlogPage、GuidePage、ProjectPage 详情页均正确渲染评论组件且 data-list-url/data-post-url 与真实路由一致；
2. 表单包含 csrfmiddlewaretoken，JS/CSS 静态资源引用完整；
3. PageCommentSettings 关闭评论时保留历史评论可读、隐藏投稿表单并展示 close_reason，禁止新增互动但允许取消；
4. 草稿预览模式下不发出注定 403 的前台评论加载请求；
5. 开启 enforce_csrf_checks=True 验证带 CSRF Token 的端到端发表、二级回复、点赞、软删全流程。
"""

import os
import unittest
from datetime import date
from uuid import uuid4

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
import django
django.setup()

from django.test import Client, RequestFactory
from wagtail.models import Site
from home.models import HomePage
from blog.models import BlogIndexPage, BlogPage
from guides.models import GuideIndexPage, GuidePage, GuideTopic
from portfolio.models import PortfolioIndexPage, ProjectPage
from identity.models import User
from comments.models import Comment, PageCommentSettings
from comments.services import CommentService
from content_lifecycle.recycle import RecycleService


class CommentFrontendIntegrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.home = HomePage.objects.first()
        cls.site = Site.objects.filter(is_default_site=True).first()
        cls.admin_user = User.objects.filter(is_superuser=True).first()
        if not cls.admin_user:
            cls.admin_user = User.objects.create_superuser(
                username=f"fe_admin_{uuid4().hex[:6]}",
                email="fe_admin@test.com",
                password="password123",
            )
        cls.reader_user, _ = User.objects.get_or_create(
            username="fe_reader",
            defaults={"email": "fe_reader@test.com"},
        )

        cls.blog_index = BlogIndexPage.objects.first()
        if not cls.blog_index:
            cls.blog_index = BlogIndexPage(title="FE Blog Index", slug=f"fe-blog-idx-{uuid4().hex[:6]}")
            cls.home.add_child(instance=cls.blog_index)
            cls.blog_index.save_revision().publish()

        cls.topic = GuideTopic.objects.create(name=f"FE Topic {uuid4().hex[:6]}", slug=f"fe-topic-{uuid4().hex[:6]}")
        cls.guide_index = GuideIndexPage(title="FE Guide Index", slug=f"fe-guide-idx-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.guide_index)
        cls.guide_index.save_revision().publish()

        cls.portfolio_index = PortfolioIndexPage(title="FE Portfolio Index", slug=f"fe-proj-idx-{uuid4().hex[:6]}")
        cls.home.add_child(instance=cls.portfolio_index)
        cls.portfolio_index.save_revision().publish()

    @classmethod
    def tearDownClass(cls):
        for idx in (cls.guide_index, cls.portfolio_index):
            try:
                if idx and idx.id:
                    RecycleService.recycle_page_or_subtree(idx, actor=cls.admin_user)
                    RecycleService.purge_page_or_subtree(idx.id, actor=cls.admin_user)
            except Exception:
                pass
        try:
            cls.topic.delete()
        except Exception:
            pass
        try:
            from external_content.outbox import OutboxProcessor
            while OutboxProcessor.process_pending_events(batch_size=100) > 0:
                pass
        except Exception:
            pass

    def setUp(self):
        self.client = Client()
        self.created_pages = []

    def tearDown(self):
        for p in reversed(self.created_pages):
            try:
                if p.id:
                    RecycleService.recycle_page_or_subtree(p, actor=self.admin_user)
                    RecycleService.purge_page_or_subtree(p.id, actor=self.admin_user)
            except Exception:
                pass

    def _create_blog_page(self, publish=True):
        page = BlogPage(
            title=f"前端评论博客-{uuid4().hex[:6]}",
            slug=f"fe-blog-{uuid4().hex[:6]}",
            authored_on=date(2026, 11, 10),
            body=[("markdown", "博客正文测试段落")],
        )
        self.blog_index.add_child(instance=page)
        rev = page.save_revision()
        if publish:
            rev.publish()
        page.refresh_from_db()
        self.created_pages.append(page)
        return page

    def _create_guide_page(self):
        page = GuidePage(
            title=f"前端评论指南-{uuid4().hex[:6]}",
            slug=f"fe-guide-{uuid4().hex[:6]}",
            topic=self.topic,
            authored_on=date(2026, 11, 10),
            last_reviewed_at=date(2026, 11, 10),
            body=[("step", {"step_number": "1", "title": "环境准备", "content": "准备环境内容"})],
        )
        self.guide_index.add_child(instance=page)
        page.save_revision().publish()
        page.refresh_from_db()
        self.created_pages.append(page)
        return page

    def _create_project_page(self):
        page = ProjectPage(
            title=f"前端评论项目-{uuid4().hex[:6]}",
            slug=f"fe-proj-{uuid4().hex[:6]}",
            summary="开源项目摘要说明",
            authored_on=date(2026, 11, 10),
            body=[("markdown", "项目架构详细介绍")],
        )
        self.portfolio_index.add_child(instance=page)
        page.save_revision().publish()
        page.refresh_from_db()
        self.created_pages.append(page)
        return page

    def test_blog_guide_project_detail_pages_render_valid_comment_widget(self):
        """BlogPage / GuidePage / ProjectPage 详情页均应正常渲染且使用正确的 API URL 与 CSRF"""
        blog = self._create_blog_page()
        guide = self._create_guide_page()
        project = self._create_project_page()

        for page, expected_text in [
            (blog, "博客正文测试段落"),
            (guide, "准备环境内容"),
            (project, "项目架构详细介绍"),
        ]:
            resp = self.client.get(page.url)
            self.assertEqual(resp.status_code, 200, f"{page.__class__.__name__} should return 200")
            html_text = resp.content.decode("utf-8")
            self.assertIn(expected_text, html_text)
            self.assertIn(f'data-list-url="/api/v1/pages/{page.id}/comments/"', html_text)
            self.assertIn(f'data-post-url="/api/v1/pages/{page.id}/comments/post/"', html_text)
            self.assertNotIn("/api/v1/comments/pages/", html_text)
            self.assertIn("csrfmiddlewaretoken", html_text)
            self.assertIn("comments_widget.css", html_text)
            self.assertIn("comments_widget.js", html_text)

    def test_closed_page_comment_settings_keeps_comments_readable_and_blocks_new_writes(self):
        """PageCommentSettings 关闭评论后，详情页隐藏发评表单并展示公告，仍可读历史评论"""
        blog = self._create_blog_page()
        c = CommentService.post_comment(
            page=blog,
            author=self.reader_user,
            raw_content="关评前的历史合规评论",
            auto_approve=True,
        )
        CommentService.react_comment(c.id, self.admin_user, 1)

        PageCommentSettings.objects.create(
            page=blog,
            allow_comments=False,
            close_reason="专题归档，暂不再接收新评论",
        )

        resp = self.client.get(blog.url)
        self.assertEqual(resp.status_code, 200)
        html_text = resp.content.decode("utf-8")
        self.assertIn("专题归档，暂不再接收新评论", html_text)
        self.assertNotIn('id="comment-post-form"', html_text)
        self.assertIn('id="comments-items-list"', html_text)

        # API 读取依然返回历史评论，并附带 comment_settings 状态
        api_res = self.client.get(f"/api/v1/pages/{blog.id}/comments/")
        self.assertEqual(api_res.status_code, 200)
        payload = api_res.json()
        self.assertEqual(len(payload["items"]), 1)
        self.assertFalse(payload["comment_settings"]["allow_comments"])
        self.assertEqual(payload["comment_settings"]["close_reason"], "专题归档，暂不再接收新评论")

        # 关评后禁止新增点赞，但允许取消已有点赞
        self.client.force_login(self.admin_user)
        add_react = self.client.post(f"/api/v1/comments/{c.id}/react/", {"reaction": "1"})
        # reader_user 未赞过，尝试点赞必须被拒绝
        self.client.force_login(self.reader_user)
        # reader_user 是作者本来就不能自赞，换一个第三用户测试
        other = User.objects.create_user(username=f"fe_other_{uuid4().hex[:6]}", password="password123")
        try:
            self.client.force_login(other)
            blocked_react = self.client.post(f"/api/v1/comments/{c.id}/react/", {"reaction": "1"})
            self.assertEqual(blocked_react.status_code, 400)
            # admin 取消自己关评前点过的赞 -> 允许
            self.client.force_login(self.admin_user)
            cancel_react = self.client.post(f"/api/v1/comments/{c.id}/react/", {"reaction": "0"})
            self.assertEqual(cancel_react.status_code, 200)
            self.assertEqual(cancel_react.json()["data"]["like_count"], 0)
        finally:
            other.delete()

    def test_draft_preview_renders_notice_instead_of_failing_widget(self):
        """草稿预览模式不加载注定 403 的公开评论 API"""
        draft_blog = self._create_blog_page(publish=False)
        rf = RequestFactory()
        req = rf.get(f"/admin/pages/{draft_blog.id}/view_draft/")
        req.user = self.admin_user
        req.is_preview = True
        resp = draft_blog.serve(req)
        resp.render()
        html_text = resp.content.decode("utf-8")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('id="comment-post-form"', html_text)
        self.assertIn("预览模式", html_text)

    def test_csrf_enforced_comment_post_reply_react_delete_flow(self):
        """开启 enforce_csrf_checks=True 验证完整前端 CSRF AJAX 提交流程"""
        blog = self._create_blog_page()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin_user)

        # 获取页面以取得 csrftoken cookie
        page_resp = csrf_client.get(blog.url)
        self.assertEqual(page_resp.status_code, 200)
        csrf_token = csrf_client.cookies["csrftoken"].value

        # 1. 发表根评论 (携带 X-CSRFToken)
        post_resp = csrf_client.post(
            f"/api/v1/pages/{blog.id}/comments/post/",
            data={"content": "CSRF 实测根评论"},
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(post_resp.status_code, 201)
        root_id = post_resp.json()["data"]["id"]

        # 2. 读者回复根评论 (携带 X-CSRFToken)
        reader_client = Client(enforce_csrf_checks=True)
        reader_client.force_login(self.reader_user)
        r_page = reader_client.get(blog.url)
        r_csrf = reader_client.cookies["csrftoken"].value
        reply_resp = reader_client.post(
            f"/api/v1/pages/{blog.id}/comments/post/",
            data={"content": "CSRF 实测二级回复", "parent_id": str(root_id), "reply_to_comment_id": str(root_id)},
            HTTP_X_CSRFTOKEN=r_csrf,
        )
        self.assertEqual(reply_resp.status_code, 201)
        reply_id = reply_resp.json()["data"]["id"]
        CommentService.moderate_comment(reply_id, action="approve", reviewer=self.admin_user)

        # 3. 读者给根评论点赞 (携带 X-CSRFToken 与 canonical reaction 参数)
        react_resp = reader_client.post(
            f"/api/v1/comments/{root_id}/react/",
            data={"reaction": "1"},
            HTTP_X_CSRFTOKEN=r_csrf,
        )
        self.assertEqual(react_resp.status_code, 200)
        self.assertEqual(react_resp.json()["data"]["like_count"], 1)

        # 4. 获取二级回复列表
        replies_resp = reader_client.get(f"/api/v1/comments/{root_id}/replies/")
        self.assertEqual(replies_resp.status_code, 200)
        self.assertEqual(len(replies_resp.json()["items"]), 1)

        # 5. 根评论作者自删后仍保留二级回复占位
        del_resp = csrf_client.post(
            f"/api/v1/comments/{root_id}/delete/",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(del_resp.status_code, 200)
        list_resp = reader_client.get(f"/api/v1/pages/{blog.id}/comments/")
        self.assertEqual(list_resp.json()["items"][0]["kind"], "author_deleted")
        self.assertEqual(reader_client.get(f"/api/v1/comments/{root_id}/replies/").status_code, 200)
