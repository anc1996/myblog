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
from identity.account_lifecycle import AccountLifecycleService
from identity.models import MemberProfile, User
from captcha.service import _get_redis_client
from comments.models import Comment, CommentDailyQuota, CommunitySettings, PageCommentSettings
from comments.services import CommentService
from content_lifecycle.recycle import RecycleService


class CommentFrontendIntegrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.home = HomePage.objects.first()
        cls.site = Site.objects.filter(is_default_site=True).first()
        cls.admin_user = User.objects.filter(is_superuser=True, is_active=True).first()
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
        self._cleanup_quota_and_rate_limits()

    def tearDown(self):
        for p in reversed(self.created_pages):
            try:
                if p.id:
                    RecycleService.recycle_page_or_subtree(p, actor=self.admin_user)
                    RecycleService.purge_page_or_subtree(p.id, actor=self.admin_user)
            except Exception:
                pass
        self._cleanup_quota_and_rate_limits()

    def _cleanup_quota_and_rate_limits(self):
        CommentDailyQuota.objects.filter(
            subject_key__in=[
                f"user:{self.reader_user.id}",
                f"user:{self.admin_user.id}",
                "ip:127.0.0.1",
                "ip:192.0.2.10",
                "ip:192.0.2.11",
            ]
        ).delete()
        try:
            r = _get_redis_client(write=True)
            if r is not None:
                from comments.rate_limit import _resolve_prefix
                p = _resolve_prefix()
                for pattern in (
                    f"{p}:ratelimit:comment:user:{self.reader_user.id}:*",
                    f"{p}:ratelimit:comment:user:{self.admin_user.id}:*",
                    f"{p}:ratelimit:comment:ip:127.0.0.1:*",
                ):
                    for key in r.scan_iter(match=pattern):
                        r.delete(key)
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
            self.client.logout()
            AccountLifecycleService.hard_delete(
                actor=self.admin_user,
                target_user_id=other.id,
                confirmed=True,
            )

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

    def test_api_author_presentation_distinguishes_member_deleted_guest_and_unknown(self):
        """评论列表与回复 API 正确区分会员、已注销用户、访客与空主体未知用户，且不暴露 author_subject_key"""
        blog = self._create_blog_page()
        member_user = User.objects.create_user(
            username=f"fe_mem_{uuid4().hex[:6]}",
            password="password123",
        )
        deleted_user = User.objects.create_user(
            username=f"fe_del_{uuid4().hex[:6]}",
            password="password123",
        )
        community = CommunitySettings._get_or_create()
        prev_mode = community.comment_mode
        try:
            community.comment_mode = "controlled_guests"
            community.save(update_fields=["comment_mode"])
            PageCommentSettings.objects.update_or_create(
                page=blog,
                defaults={"allow_comments": True, "allow_guest_comments": True},
            )
            profile = MemberProfile.get_or_create_for_user(member_user)
            profile.nickname = "前端专栏昵称"
            profile.save(update_fields=["nickname"])

            c_member = CommentService.post_comment(
                page=blog,
                author=member_user,
                raw_content="在任会员评论",
                auto_approve=True,
            )
            c_deleted = CommentService.post_comment(
                page=blog,
                author=deleted_user,
                raw_content="待注销会员评论",
                auto_approve=True,
            )
            c_guest = CommentService.post_comment(
                page=blog,
                author=None,
                guest_name="",
                raw_content="匿名访客评论",
                ip_address="192.0.2.10",
                auto_approve=False,
            )
            CommentService.moderate_comment(c_guest.id, action="approve", reviewer=self.admin_user)
            c_unknown = CommentService.post_comment(
                page=blog,
                author=None,
                guest_name="",
                raw_content="历史空主体评论",
                ip_address="192.0.2.11",
                auto_approve=False,
            )
            CommentService.moderate_comment(c_unknown.id, action="approve", reviewer=self.admin_user)
            Comment.objects.filter(id=c_unknown.id).update(
                author=None,
                author_subject_key="",
                guest_name="",
            )

            CommentService.post_comment(
                page=blog,
                author=self.reader_user,
                raw_content="回复专栏作者",
                parent_id=c_member.id,
                reply_to_comment_id=c_member.id,
                auto_approve=True,
            )

            AccountLifecycleService.hard_delete(
                actor=self.admin_user,
                target_user_id=deleted_user.id,
                confirmed=True,
            )

            list_resp = self.client.get(f"/api/v1/pages/{blog.id}/comments/")
            self.assertEqual(list_resp.status_code, 200)
            by_id = {item["id"]: item for item in list_resp.json()["items"]}

            self.assertEqual(
                by_id[c_member.id]["author"],
                {
                    "id": member_user.id,
                    "username": member_user.username,
                    "display_name": "前端专栏昵称",
                    "kind": "member",
                    "avatar_url": "",
                },
            )
            self.assertEqual(
                by_id[c_deleted.id]["author"],
                {
                    "id": None,
                    "username": "已注销用户",
                    "display_name": "已注销用户",
                    "kind": "deleted_member",
                    "avatar_url": "",
                },
            )
            self.assertEqual(
                by_id[c_guest.id]["author"],
                {
                    "id": None,
                    "username": "访客",
                    "display_name": "访客",
                    "kind": "guest",
                    "avatar_url": "",
                },
            )
            self.assertEqual(
                by_id[c_unknown.id]["author"],
                {
                    "id": None,
                    "username": "未知用户",
                    "display_name": "未知用户",
                    "kind": "unknown",
                    "avatar_url": "",
                },
            )
            for item in by_id.values():
                self.assertNotIn("author_subject_key", item)
                self.assertNotIn("author_subject_key", item["author"])

            replies_resp = self.client.get(f"/api/v1/comments/{c_member.id}/replies/")
            self.assertEqual(replies_resp.status_code, 200)
            reply_item = replies_resp.json()["items"][0]
            self.assertEqual(
                reply_item["replied_to_user"],
                {
                    "id": member_user.id,
                    "username": member_user.username,
                    "display_name": "前端专栏昵称",
                },
            )
            self.assertEqual(reply_item["author"]["kind"], "member")
            self.assertNotIn("author_subject_key", reply_item["author"])
        finally:
            community.comment_mode = prev_mode
            community.save(update_fields=["comment_mode"])
            if User.objects.filter(id=member_user.id).exists():
                AccountLifecycleService.hard_delete(
                    actor=self.admin_user,
                    target_user_id=member_user.id,
                    confirmed=True,
                )
            if User.objects.filter(id=deleted_user.id).exists():
                AccountLifecycleService.hard_delete(
                    actor=self.admin_user,
                    target_user_id=deleted_user.id,
                    confirmed=True,
                )

    def test_comment_rich_markdown_preview_api_budgets_and_inspect_render(self):
        """验证评论区工具栏/表情/公式/Mermaid/表格安全渲染、只读预览接口、隐私防缓存头及后台检查页富预览"""
        from django.contrib.auth import get_user_model
        from django.core.exceptions import ValidationError
        from comments.models import Comment, CommentDailyQuota, CommentVersion, CommunitySettings, PageCommentSettings
        from comments.sanitizer import sanitize_comment_for_tier
        from comments.services import CommentService
        from identity.account_lifecycle import AccountLifecycleService
        from identity.models import MemberProfile

        User = get_user_model()
        member = User.objects.create_user(username=f"rich_m_{uuid4().hex[:6]}", password="StrongPass123!")
        prof = MemberProfile.get_or_create_for_user(member)
        prof.nickname = "技术评论员"
        prof.save(update_fields=["nickname"])

        community = CommunitySettings._get_or_create()
        prev_mode = community.comment_mode
        blog = None
        try:
            community.comment_mode = "controlled_guests"
            community.save(update_fields=["comment_mode"])
            blog = BlogPage(
                title=f"富格式评论测试页-{uuid4().hex[:6]}",
                slug=f"rich-comment-test-{uuid4().hex[:6]}",
                body=[("markdown", "评论富格式与预览测试正文。")],
            )
            self.blog_index.add_child(instance=blog)
            blog.save_revision(user=self.admin_user).publish(user=self.admin_user)
            self.created_pages.append(blog)
            PageCommentSettings.objects.update_or_create(
                page=blog,
                defaults={"allow_comments": True, "allow_guest_comments": True},
            )

            # 1. 前台模板输出新限制与用户显示昵称而非登录账号名
            self.client.force_login(member)
            page_resp = self.client.get(blog.url)
            self.assertEqual(page_resp.status_code, 200)
            page_html = page_resp.content.decode("utf-8")
            self.assertIn("技术评论员", page_html)
            self.assertNotIn(member.username, page_html)
            self.assertIn('data-max-chars="3000"', page_html)
            self.assertIn('data-max-external-links="5"', page_html)
            self.assertIn('data-allow-rich-embeds="true"', page_html)
            self.assertIn('data-preview-url=', page_html)

            # 2. 会员富格式渲染：Emoji + 表格 + 代码块 + 公式 + Mermaid + 货币不误判
            rich_md = (
                "太棒了 🚀！价格是 $100，行内公式 $E = mc^2$。\n\n"
                "| 项目 | 状态 |\n| --- | --- |\n| KaTeX | ✅ |\n\n"
                "```python\nprice = '$200'\nprint('hi')\n```\n\n"
                "```mermaid\n%%{init:{'theme':'dark'}}%%\nflowchart LR\n  A[开始] --> B[完成]\nclick A href \"https://evil.com\"\n```\n"
                "<script>alert(1)</script><img src=x onerror=alert(1)>"
            )
            preview_resp = self.client.post(
                f"/api/v1/pages/{blog.id}/comments/preview/",
                {"content": rich_md},
            )
            self.assertEqual(preview_resp.status_code, 200)
            self.assertIn("no-store", preview_resp["Cache-Control"])
            pdata = preview_resp.json()
            self.assertEqual(pdata["status"], "success")
            safe_html = pdata["safe_html"]
            self.assertIn("🚀", safe_html)
            self.assertIn("$100", safe_html)
            self.assertIn("cb-math-inline", safe_html)
            self.assertIn("language-python", safe_html)
            self.assertIn('<table class="custom-table-block">', safe_html)
            self.assertIn('<div class="responsive-table-wrapper">', safe_html)
            self.assertIn("data-cb-mermaid", safe_html)
            self.assertNotIn("<script>", safe_html)
            self.assertNotIn("<img", safe_html)
            self.assertNotIn("%%{init", safe_html)
            self.assertNotIn("evil.com", safe_html)
            # 预览接口绝不写库、不消耗日配额
            self.assertEqual(Comment.objects.filter(page=blog).count(), 0)
            self.assertEqual(CommentDailyQuota.objects.filter(subject_key=f"user:{member.id}").count(), 0)

            # 3. Mermaid 超预算拦截（多图 / 超长）
            two_mermaids = "```mermaid\nflowchart LR\nA-->B\n```\n\n```mermaid\nflowchart LR\nC-->D\n```"
            bad_preview = self.client.post(
                f"/api/v1/pages/{blog.id}/comments/preview/",
                {"content": two_mermaids},
            )
            self.assertEqual(bad_preview.status_code, 400)

            # 4. 游客降级：不支持公式与 Mermaid 图表（仅作普通代码块/文本），仍禁外链
            guest_html, g_links, _ = sanitize_comment_for_tier(
                "你好 😀 $a+b$\n\n```mermaid\nflowchart LR\nA-->B\n```",
                tier="guest",
            )
            self.assertEqual(g_links, 0)
            self.assertIn("😀", guest_html)
            self.assertNotIn("cb-math", guest_html)
            self.assertNotIn("data-cb-mermaid", guest_html)
            with self.assertRaises(ValidationError):
                sanitize_comment_for_tier("看这里 https://example.com", tier="guest")

            # 5. 列表隐私防缓存头、点踩计数 & inspect 后台双版本富渲染对照
            c = CommentService.post_comment(
                page=blog,
                author=member,
                raw_content="初始发布版本 👍\n\n$$\n\\sum_{i=1}^n i\n$$",
                auto_approve=True,
            )
            edit_ver = CommentService.edit_comment(
                c.id,
                member,
                "待审修订版\n\n```mermaid\nflowchart LR\nX-->Y\n```",
            )
            c.refresh_from_db()
            self.assertEqual(c.pending_version_id, edit_ver.id)

            # 他人点赞/点踩与列表隐私头校验
            self.client.force_login(self.admin_user)
            react_resp = self.client.post(f"/api/v1/comments/{c.id}/react/", {"reaction": "-1"})
            self.assertEqual(react_resp.status_code, 200)
            self.assertEqual(react_resp.json()["data"]["dislike_count"], 1)

            list_resp = self.client.get(f"/api/v1/pages/{blog.id}/comments/")
            self.assertEqual(list_resp.status_code, 200)
            self.assertIn("no-store", list_resp["Cache-Control"])
            self.assertIn("Cookie", list_resp["Vary"])
            list_json = list_resp.json()
            self.assertEqual(list_json["comment_settings"]["max_chars"], 3000)
            self.assertEqual(list_json["items"][0]["dislike_count"], 1)
            self.assertEqual(list_json["items"][0]["user_reaction"], -1)

            # 后台检查页同步展示当前已公开版与待审渲染预览
            insp_resp = self.client.get(f"/admin/comments/inspect/{c.id}/")
            self.assertEqual(insp_resp.status_code, 200)
            insp_html = insp_resp.content.decode("utf-8")
            self.assertIn("当前已公开生效版本", insp_html)
            self.assertIn("渲染预览", insp_html)
            self.assertIn("data-cb-mermaid", insp_html)
            self.assertIn("cb-math-block", insp_html)

            # 6. 验证 Typora 2空格嵌套列表、混合有/无序列表及引用下列表在评论区同样正确嵌套
            nested_md = (
                "- 分类依据:\n"
                "  - ③④⑤为一笔画\n"
                "  - ①②⑥为两笔画（含T型交点）\n"
                "- 识别技巧: 注意T型交点的存在会增加笔画数\n\n"
                "> 引用提示:\n"
                "> 1. 第一步\n"
                ">    - 引用内子项\n"
                "> 2. 第二步"
            )
            nested_preview = self.client.post(
                f"/api/v1/pages/{blog.id}/comments/preview/",
                {"content": nested_md},
            )
            self.assertEqual(nested_preview.status_code, 200)
            nested_html = nested_preview.json()["safe_html"]
            self.assertIn("<ul>\n<li>分类依据:\n<ul>\n<li>③④⑤为一笔画</li>", nested_html)
            self.assertIn("<blockquote>\n<p>引用提示:</p>\n<ol>\n<li>第一步\n<ul>\n<li>引用内子项</li>", nested_html)
        finally:
            community.comment_mode = prev_mode
            community.save(update_fields=["comment_mode"])
            if User.objects.filter(id=member.id).exists():
                AccountLifecycleService.hard_delete(
                    actor=self.admin_user,
                    target_user_id=member.id,
                    confirmed=True,
                )
