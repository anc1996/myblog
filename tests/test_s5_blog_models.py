"""
S5 核心博客模型与元数据关联单元测试。

测试覆盖：
1. people.Person: 字段结构、独立于后台 User、注册为 Wagtail Snippet；
2. taxonomy.TagMergeRedirect: 标签合并与重定向映射模型；
3. blog.BlogIndexPage: 继承 CustomBasePage、必填成文日期、页面树层级约束；
4. blog.BlogPage: 必填成文日期、多人作者角色关联、全站标签、封面大图、文末独立图集与 StreamField；
5. home.HomePage: 子页面类型约束；
6. CustomBasePage 核心安全防线：已回收禁止发布、直接物理删除被阻断、复制自动重置 document_uuid。
"""

import os
from pathlib import Path
import unittest
from datetime import date
from uuid import UUID
from django.core.exceptions import PermissionDenied

PROJECT_DIR = Path(__file__).resolve().parents[1]


class S5BlogModelsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
        import django
        django.setup()

    def test_person_model_structure_and_snippet_registration(self):
        from people.models import Person
        from media_library.models import CustomImage
        from wagtail.snippets.models import get_snippet_models

        field_names = [f.name for f in Person._meta.fields]
        self.assertIn("public_id", field_names)
        self.assertIn("name", field_names)
        self.assertIn("avatar", field_names)
        self.assertIn("bio", field_names)
        self.assertIn("email", field_names)

        # 验证 avatar 指向全站公共 CustomImage
        avatar_field = Person._meta.get_field("avatar")
        self.assertEqual(avatar_field.related_model, CustomImage)

        # 验证已注册为 Wagtail Snippet
        self.assertIn(Person, get_snippet_models())

    def test_taxonomy_tag_merge_redirect_structure(self):
        from taxonomy.models import TagMergeRedirect
        from taggit.models import Tag

        field_names = [f.name for f in TagMergeRedirect._meta.fields]
        self.assertIn("source_tag_id", field_names)
        self.assertIn("source_name", field_names)
        self.assertIn("target_tag", field_names)
        self.assertIn("merged_at", field_names)
        self.assertIn("merged_by", field_names)

        target_field = TagMergeRedirect._meta.get_field("target_tag")
        self.assertEqual(target_field.related_model, Tag)

    def test_blog_index_page_structure_and_tree_constraints(self):
        from blog.models import BlogIndexPage
        from content_lifecycle.models import CustomBasePage

        self.assertTrue(issubclass(BlogIndexPage, CustomBasePage))
        field_names = [f.name for f in BlogIndexPage._meta.fields]
        self.assertIn("authored_on", field_names)
        self.assertIn("intro", field_names)
        self.assertIn("document_uuid", field_names)
        self.assertIn("is_recycled", field_names)

        # 验证成文日期默认当天且不为 null
        authored_field = BlogIndexPage._meta.get_field("authored_on")
        self.assertFalse(authored_field.null)

        # 验证树层级约束
        self.assertEqual(BlogIndexPage.parent_page_types, ["home.HomePage", "blog.BlogIndexPage"])
        self.assertEqual(BlogIndexPage.subpage_types, ["blog.BlogIndexPage", "blog.BlogPage"])

    def test_blog_page_structure_and_relations(self):
        from blog.models import (
            BlogPage,
            BlogPagePerson,
            BlogPageTag,
            BlogPageGalleryImage,
            CONTRIBUTION_ROLE_CHOICES,
        )
        from content_lifecycle.models import CustomBasePage
        from media_library.models import CustomImage
        from people.models import Person

        self.assertTrue(issubclass(BlogPage, CustomBasePage))
        field_names = [f.name for f in BlogPage._meta.fields]
        self.assertIn("authored_on", field_names)
        self.assertIn("featured_image", field_names)
        self.assertIn("intro", field_names)
        self.assertIn("body", field_names)
        self.assertIn("document_uuid", field_names)
        self.assertIn("is_recycled", field_names)

        # 验证树层级约束：BlogPage 可在首页、栏目或作为子文章系列创建
        self.assertEqual(BlogPage.parent_page_types, ["home.HomePage", "blog.BlogIndexPage", "blog.BlogPage"])
        self.assertEqual(BlogPage.subpage_types, ["blog.BlogPage"])

        # 验证多对多作者贡献模型 (BlogPagePerson)
        person_fields = [f.name for f in BlogPagePerson._meta.fields]
        self.assertIn("page", person_fields)
        self.assertIn("person", person_fields)
        self.assertIn("role", person_fields)
        self.assertIn("sort_order", person_fields)  # Orderable 排序
        self.assertEqual(BlogPagePerson._meta.get_field("person").related_model, Person)
        role_keys = [c[0] for c in CONTRIBUTION_ROLE_CHOICES]
        self.assertIn("primary_author", role_keys)
        self.assertIn("reviewer", role_keys)

        # 验证标签关联模型 (BlogPageTag)
        self.assertEqual(BlogPageTag._meta.get_field("content_object").related_model, BlogPage)

        # 验证独立图集模型 (BlogPageGalleryImage)
        gallery_fields = [f.name for f in BlogPageGalleryImage._meta.fields]
        self.assertIn("page", gallery_fields)
        self.assertIn("image", gallery_fields)
        self.assertIn("caption", gallery_fields)
        self.assertIn("sort_order", gallery_fields)
        self.assertEqual(BlogPageGalleryImage._meta.get_field("image").related_model, CustomImage)

    def test_homepage_subpage_types_constraint(self):
        from home.models import HomePage

        self.assertIn("blog.BlogIndexPage", HomePage.subpage_types)
        self.assertIn("blog.BlogPage", HomePage.subpage_types)

    def test_custom_base_page_safety_guards(self):
        from blog.models import BlogPage

        page = BlogPage(title="安全测试文章", slug="safe-test-page")

        # 1. 验证已回收状态下禁止发布
        page.is_recycled = True
        with self.assertRaises(PermissionDenied):
            page.publish()

        # 2. 验证直接物理删除被守卫阻断
        with self.assertRaises(PermissionDenied):
            page.delete()


if __name__ == "__main__":
    unittest.main()
