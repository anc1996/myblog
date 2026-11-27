"""
/tags/ 全站标签大厅方案 B 分层索引、60/页 SQL 下推分页、零写库只读与视觉规范测试集。
"""

from datetime import date
from pathlib import Path
import re
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from taggit.models import Tag
from wagtail.models import Page, PageViewRestriction, Site

from blog.models import BlogIndexPage, BlogPage
from content_lifecycle.recycle import RecycleService
from home.models import HomePage
from taxonomy.models import TagDirectoryMetadata, TagIndexPage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
User = get_user_model()


class TagDirectoryPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.get_first_root_node() or Page.add_root(title="Root")
        cls.home = HomePage.objects.first()
        if not cls.home:
            cls.home = HomePage(title="Home", slug=f"home-{uuid4().hex[:6]}")
            root.add_child(instance=cls.home)
            cls.home.save_revision().publish()

        site = Site.objects.filter(is_default_site=True).first()
        if not site:
            Site.objects.create(hostname="localhost", port=80, root_page=cls.home, is_default_site=True)
        elif site.root_page_id != cls.home.id:
            site.root_page = cls.home
            site.save()
        Site.clear_site_root_paths_cache()

        cls.owner = User.objects.create_superuser(
            username=f"tag_pg_su_{uuid4().hex[:6]}",
            email="tag_pg@example.com",
            password="StrongPassword!123",
        )
        cls.tag_index = TagIndexPage.objects.live().filter(is_recycled=False).first()
        if not cls.tag_index:
            cls.tag_index = TagIndexPage(
                title="全站标签大厅",
                slug=f"tags-{uuid4().hex[:6]}",
                intro="方案 B 分层标签索引",
            )
            cls.home.add_child(instance=cls.tag_index)
            cls.tag_index.save_revision(user=cls.owner).publish(user=cls.owner)
        cls.tag_index.refresh_from_db()
        cls.base_url = cls.tag_index.get_url() or "/tags/"

    def _publish_blog(self, title: str, tags: list[str], *, parent=None) -> BlogPage:
        parent_page = parent or self.home
        page = BlogPage(
            title=title,
            slug=f"b-{uuid4().hex[:8]}",
            owner=self.owner,
            authored_on=date(2026, 11, 20),
            body=[("markdown", f"{title} content")],
        )
        parent_page.add_child(instance=page)
        for tn in tags:
            page.tags.add(tn)
        page.save_revision(user=self.owner).publish(user=self.owner)
        return page

    def test_a_plan_b_overview_caps_each_letter_at_20_and_renders_view_all_link(self):
        """方案 B 默认总览：单字母 25 个标签仅展示前 20 个，并保留真实总数与查看全部链接"""
        prefix = f"QTag{uuid4().hex[:4]}"
        tag_names = [f"{prefix}-{i:02d}" for i in range(1, 26)]

        # 让 01..05 引用次数为 2，其余为 1，确保前 20 可稳定排序
        self._publish_blog("热门前排文章", tag_names[:5])
        self._publish_blog("全量覆盖文章", tag_names)

        resp = self.client.get(self.base_url)
        self.assertEqual(resp.status_code, 200)
        vm = resp.context["view_model"]
        self.assertEqual(vm.view_mode, "overview")
        self.assertGreaterEqual(vm.group_totals["Q"], 25)
        self.assertEqual(len(vm.grouped_tags["Q"]), 20)

        html = resp.content.decode("utf-8")
        q_total = vm.group_totals["Q"]
        self.assertIn(f"查看 Q 开头全部 {q_total} 个标签", html)
        self.assertIn(f"{self.base_url}?letter=Q", html)

    def test_b_single_letter_60_per_page_pagination_sort_and_clamp(self):
        """单字母 60/页分页：65 个 Z 标签第 1 页 60 个、第 2 页 5 个，支持 sort 与页码安全钳位"""
        prefix = f"ZTag{uuid4().hex[:4]}"
        z_tags = [f"{prefix}-{i:02d}" for i in range(1, 66)]
        # 01..03 拥有 2 篇文档，确保按热门排序置顶
        self._publish_blog("Z 热门文章", z_tags[:3])
        self._publish_blog("Z 全集文章", z_tags)

        r_p1 = self.client.get(f"{self.base_url}?letter=Z&page=1")
        self.assertEqual(r_p1.status_code, 200)
        vm1 = r_p1.context["view_model"]
        self.assertEqual(vm1.view_mode, "letter")
        self.assertEqual(vm1.pagination["page_size"], 60)
        self.assertEqual(vm1.pagination["current_page"], 1)
        self.assertEqual(vm1.pagination["total_pages"], 2)
        self.assertEqual(len(vm1.page_items), 60)
        self.assertTrue(vm1.pagination["has_next"])
        self.assertFalse(vm1.pagination["has_previous"])
        # 默认 popular 排序，count=2 的标签排在最前面
        self.assertEqual(vm1.page_items[0].count, 2)

        r_p2 = self.client.get(f"{self.base_url}?letter=Z&page=2")
        self.assertEqual(r_p2.status_code, 200)
        vm2 = r_p2.context["view_model"]
        self.assertEqual(vm2.pagination["current_page"], 2)
        self.assertEqual(len(vm2.page_items), 5)
        self.assertTrue(vm2.pagination["has_previous"])
        self.assertFalse(vm2.pagination["has_next"])

        # 按名称 A-Z 排序
        r_name = self.client.get(f"{self.base_url}?letter=Z&sort=name")
        self.assertEqual(r_name.status_code, 200)
        vm_name = r_name.context["view_model"]
        names_returned = [it.name for it in vm_name.page_items]
        self.assertEqual(names_returned, sorted(names_returned, key=str.lower))

        # 越界页码安全钳位至最大页，绝不 500
        for bad_page in ("9999", "-5", "abc"):
            r_bad = self.client.get(f"{self.base_url}?letter=Z&page={bad_page}")
            self.assertEqual(r_bad.status_code, 200)
            vm_bad = r_bad.context["view_model"]
            if bad_page == "9999":
                self.assertEqual(vm_bad.pagination["current_page"], 2)
                self.assertEqual(len(vm_bad.page_items), 5)
            else:
                self.assertEqual(vm_bad.pagination["current_page"], 1)

        # AJAX 分片请求返回完整分页 HTML
        r_ajax = self.client.get(
            f"{self.base_url}?letter=Z&page=2",
            HTTP_X_LISTING_RESPONSE="fragment-v1",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(r_ajax.status_code, 200)
        payload = r_ajax.json()
        self.assertIn("html", payload)
        self.assertEqual(payload["view_mode"], "letter")

    def test_c_server_search_and_grid_find_cold_tags_and_never_leak_private_tags(self):
        """服务端搜索与分页网格能命中超出前 20 预览的冷门标签，且严禁泄漏草稿/回收/受限标签"""
        prefix = f"MTag{uuid4().hex[:4]}"
        m_tags = [f"{prefix}-{i:02d}" for i in range(1, 26)]
        cold_tag = f"{prefix}-ColdHidden99"
        self._publish_blog("M 热门文章", m_tags[:20])
        self._publish_blog("M 冷门文章", m_tags[20:] + [cold_tag])

        # 验证 q 与 view=grid 均能命中超出前 20 预览的冷门标签
        r_search = self.client.get(f"{self.base_url}?q=ColdHidden99")
        self.assertEqual(r_search.status_code, 200)
        vm_s = r_search.context["view_model"]
        self.assertEqual(vm_s.view_mode, "search")
        self.assertIn(cold_tag, [it.name for it in vm_s.page_items])

        r_grid = self.client.get(f"{self.base_url}?view=grid&sort=name")
        self.assertEqual(r_grid.status_code, 200)
        vm_g = r_grid.context["view_model"]
        self.assertEqual(vm_g.view_mode, "grid")
        self.assertIn(cold_tag, [it.name for it in vm_g.page_items])

        # 草稿页、回收站页、受限栏目下页面、嵌套子站页面的标签绝不泄漏
        draft_tag = f"SecretDraftTag_{uuid4().hex[:6]}"
        draft_page = BlogPage(
            title="草稿文章",
            slug=f"draft-{uuid4().hex[:6]}",
            owner=self.owner,
            live=False,
            body=[("markdown", "draft")],
        )
        self.home.add_child(instance=draft_page)
        draft_page.tags.add(draft_tag)
        draft_page.save_revision(user=self.owner)

        rec_tag = f"SecretRecycledTag_{uuid4().hex[:6]}"
        rec_page = self._publish_blog("即将回收文章", [rec_tag])
        RecycleService.recycle_page_or_subtree(rec_page, actor=self.owner)

        res_col = BlogIndexPage(title="受限栏目", slug=f"res-col-{uuid4().hex[:6]}")
        self.home.add_child(instance=res_col)
        res_col.save_revision(user=self.owner).publish(user=self.owner)
        PageViewRestriction.objects.create(page=res_col, restriction_type=PageViewRestriction.LOGIN)
        restricted_tag = f"SecretRestrictedTag_{uuid4().hex[:6]}"
        self._publish_blog("受限栏目内文章", [restricted_tag], parent=res_col)

        sub_home = HomePage(title="嵌套子站", slug=f"sub-home-{uuid4().hex[:6]}")
        self.home.add_child(instance=sub_home)
        sub_home.save_revision(user=self.owner).publish(user=self.owner)
        Site.objects.create(hostname="nested-tag.localhost", port=80, root_page=sub_home, is_default_site=False)
        Site.clear_site_root_paths_cache()
        nested_tag = f"SecretNestedSiteTag_{uuid4().hex[:6]}"
        self._publish_blog("嵌套子站文章", [nested_tag], parent=sub_home)

        resp_prefix = self.client.get(f"{self.base_url}?q=Secret")
        self.assertEqual(resp_prefix.status_code, 200)
        prefix_html = resp_prefix.content.decode("utf-8")
        prefix_names = {it.name for it in resp_prefix.context["view_model"].page_items}

        resp_grid_all = self.client.get(f"{self.base_url}?view=grid")
        self.assertEqual(resp_grid_all.status_code, 200)
        grid_html = resp_grid_all.content.decode("utf-8")

        for secret in (draft_tag, rec_tag, restricted_tag, nested_tag):
            self.assertNotIn(secret, prefix_names)
            self.assertNotIn(secret, prefix_html)
            self.assertNotIn(secret, grid_html)
            resp_exact = self.client.get(f"{self.base_url}?q={secret}")
            self.assertEqual(resp_exact.status_code, 200)
            self.assertEqual(resp_exact.context["view_model"].filtered_count, 0)
            self.assertEqual(resp_exact.context["view_model"].page_items, [])

    def test_d_public_get_tags_performs_zero_db_writes_and_signal_syncs_new_tags(self):
        """Tag.post_save 自动同步元数据；即使缺失元数据，GET /tags/ 与 /tags/<slug>/ 也 100% 零写库"""
        tag_name = f"ZeroWrite_{uuid4().hex[:6]}"
        self._publish_blog("零写库验证文章", [tag_name])
        tag_obj = Tag.objects.get(name=tag_name)

        # 1. 验证 post_save 信号已自动创建 TagDirectoryMetadata
        self.assertTrue(TagDirectoryMetadata.objects.filter(tag=tag_obj).exists())

        # 2. 模拟历史缺失元数据行，验证 GET /tags/ 与 /tags/<slug>/ 绝不执行 INSERT/UPDATE
        TagDirectoryMetadata.objects.filter(tag=tag_obj).delete()

        write_sql_pattern = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
        with CaptureQueriesContext(connection) as ctx:
            r_dir = self.client.get(self.base_url)
            r_grid = self.client.get(f"{self.base_url}?view=grid")
            r_detail = self.client.get(f"{self.base_url}{tag_obj.slug}/")

        self.assertEqual(r_dir.status_code, 200)
        self.assertEqual(r_grid.status_code, 200)
        self.assertEqual(r_detail.status_code, 200)

        write_queries = [q["sql"] for q in ctx.captured_queries if write_sql_pattern.match(q["sql"])]
        self.assertEqual(write_queries, [], f"GET 请求期间检测到非法写库 SQL: {write_queries}")
        self.assertFalse(TagDirectoryMetadata.objects.filter(tag=tag_obj).exists())

    def test_e_tags_css_and_templates_hygiene(self):
        """tags.css 零裸 #hex、零 transition: all；模板零 Emoji、零旧 Bootstrap 白卡与内联 style"""
        css_path = PROJECT_ROOT / "myblog/static/css/pages/tags.css"
        self.assertTrue(css_path.exists())
        raw_css = css_path.read_text(encoding="utf-8")
        cleaned = re.sub(r"/\*.*?\*/", "", raw_css, flags=re.S)

        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", cleaned), [])
        self.assertNotRegex(cleaned, r"transition\s*:\s*all\b")
        self.assertIn("var(--", cleaned)
        self.assertIn("prefers-reduced-motion", cleaned)

        tpl_paths = [
            PROJECT_ROOT / "myblog/templates/taxonomy/tag_index_page.html",
            PROJECT_ROOT / "myblog/templates/taxonomy/partials/_tag_directory.html",
            PROJECT_ROOT / "myblog/templates/taxonomy/partials/_tag_detail.html",
            PROJECT_ROOT / "myblog/templates/taxonomy/partials/_tag_missing.html",
        ]
        for p in tpl_paths:
            text = p.read_text(encoding="utf-8")
            for bad_emoji in ("🔥", "🔍", "📡", "🏷️"):
                self.assertNotIn(bad_emoji, text, f"{p.name} 不得包含 Emoji {bad_emoji}")
            self.assertNotIn("bg-white rounded border shadow-sm", text)
            self.assertNotIn("badge bg-danger", text)
            self.assertNotIn("badge bg-primary", text)
            self.assertNotRegex(text, r"\sstyle\s*=")
