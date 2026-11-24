"""
全站主题设计系统（阶段 2：破除“白块”与全站 CSS/模板收敛）回归测试。

核验范围：
1. templates/identity/ 4 个前台身份模板（login/register/force_password_change/profile）抽离内联 <style>，统一加载 identity-auth.css；
2. 后台 user_manage.html 归口 admin-workbench.css，且不混入前台色板样式；
3. comments_widget.css、author_directory.css、article.css、article-rating.css、detail-layout.css 收敛至语义令牌；
4. guides/blocks/*.html 清除硬编码浅色内联样式；标签与子栏目微章不强行叠加 text-white。
"""

from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from identity.models import MemberProfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Stage2IdentityAndAdminTemplateTests(TestCase):
    def test_identity_frontend_templates_use_external_stylesheet_without_inline_style(self):
        """前台 4 个身份页面必须使用 identity-auth.css 且不再包含内联 <style> 块"""
        User = get_user_model()
        user = User.objects.create_user(username=f"u_{uuid4().hex[:8]}", password="StrongPass123!")
        profile = MemberProfile.get_or_create_for_user(user)
        profile.must_change_password = True
        profile.save(update_fields=["must_change_password"])

        # 未登录：登录与注册页
        for url_name in ("identity:login", "identity:register"):
            resp = self.client.get(reverse(url_name))
            self.assertEqual(resp.status_code, 200)
            html = resp.content.decode("utf-8")
            self.assertIn("css/identity-auth.css", html)
            self.assertNotIn("<style>", html)

        # 已登录但 must_change_password=True：强制改密页
        self.client.force_login(user)
        resp_pw = self.client.get(reverse("identity:force_password_change"))
        self.assertEqual(resp_pw.status_code, 200)
        html_pw = resp_pw.content.decode("utf-8")
        self.assertIn("css/identity-auth.css", html_pw)
        self.assertNotIn("<style>", html_pw)

        # 解除强制改密后：个人中心页
        profile.must_change_password = False
        profile.save(update_fields=["must_change_password"])
        resp_prof = self.client.get(reverse("identity:profile"))
        self.assertEqual(resp_prof.status_code, 200)
        html_prof = resp_prof.content.decode("utf-8")
        self.assertIn("css/identity-auth.css", html_prof)
        self.assertNotIn("<style>", html_prof)

    def test_admin_user_manage_uses_admin_workbench_css_isolated_from_frontend_tokens(self):
        """后台账号治理页归口 admin-workbench.css，绝不引入前台 tokens.css 或内联白卡样式"""
        User = get_user_model()
        admin_user = User.objects.create_superuser(
            username=f"adm_{uuid4().hex[:8]}",
            email="adm@example.com",
            password="AdminPass123!",
        )
        target = User.objects.create_user(username=f"tgt_{uuid4().hex[:8]}", password="UserPass123!")
        self.client.force_login(admin_user)
        resp = self.client.get(reverse("identity_admin_user_manage", args=[target.id]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("css/admin-workbench.css", html)
        self.assertIn("admin-wb-shell", html)
        self.assertNotIn("css/themes/tokens.css", html)
        self.assertNotIn("background:#fff", html)


class Stage2CssConvergenceContractTests(TestCase):
    def test_stage2_css_files_converged_to_semantic_tokens(self):
        """Stage 2 目标 CSS 不应残留硬编码浅色孤岛或未定义旧变量别名"""
        forbidden_light_islands = [
            "#fffbeb",
            "#fef2f2",
            "#f0fdf4",
            "#f8fafc",
            "#f1f5f9",
            "--card-bg",
            "--surface-muted",
            "--text-color",
        ]
        target_css_files = [
            "myblog/static/css/identity-auth.css",
            "myblog/static/comments/css/comments_widget.css",
            "myblog/static/people/css/author_directory.css",
            "myblog/apps/analytics/static/analytics/css/article-rating.css",
            "myblog/static/css/detail-layout.css",
        ]
        for rel_path in target_css_files:
            text = (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
            self.assertIn("--color-", text, f"{rel_path} 应使用设计令牌")
            for bad in forbidden_light_islands:
                self.assertNotIn(bad, text, f"{rel_path} 不应残留硬编码或废弃变量 {bad}")

        # article.css 允许灯箱暗底文案使用 #f8fafc / #ffffff，但正文卡片与标题必须用令牌
        article_css = (PROJECT_ROOT / "myblog/apps/blog/static/blog/css/article.css").read_text(encoding="utf-8")
        self.assertIn("var(--color-surface)", article_css)
        self.assertIn("var(--color-on-primary)", article_css)
        self.assertNotIn("#fffbeb", article_css)
        self.assertNotIn("#f1f5f9", article_css)

    def test_guide_block_templates_and_badges_free_of_inline_light_islands(self):
        """Guide 积木模板不再硬编码浅色内联 style，微章不再强加 text-white"""
        guide_blocks_dir = PROJECT_ROOT / "myblog/apps/guides/templates/guides/blocks"
        for tpl_path in guide_blocks_dir.glob("*.html"):
            text = tpl_path.read_text(encoding="utf-8")
            self.assertNotIn("style=", text, f"{tpl_path.name} 不应残留内联 style=")

        for rel_tpl in [
            "myblog/templates/discovery/listing/_listing_subcategories.html",
            "myblog/templates/taxonomy/partials/_tag_detail.html",
            "myblog/templates/taxonomy/partials/_tag_directory.html",
            "myblog/templates/taxonomy/partials/_tag_missing.html",
        ]:
            text = (PROJECT_ROOT / rel_tpl).read_text(encoding="utf-8")
            self.assertNotIn("bg-primary text-white", text, f"{rel_tpl} 不应使用 bg-primary text-white")
            self.assertNotIn("bg-danger text-white", text, f"{rel_tpl} 不应使用 bg-danger text-white")
