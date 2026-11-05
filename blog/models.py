"""
BlogPage 与 BlogIndexPage 核心业务模型。

落实第一阶段需求：
1. 继承 CustomBasePage（具备 document_uuid 与 is_recycled 回收隔离守卫）；
2. 必填成文日期 authored_on (PAGE-DATE-01，新建预填当天，与发布时间独立)；
3. 多人贡献角色 BlogPagePerson (Orderable，支持主要作者、共同作者、审校等角色与排序)；
4. 全站统一标签词库 BlogPageTag (ClusterTaggableManager)；
5. 可选封面大图 featured_image (指向 CustomImage，无封面不留白框)；
6. 页面独立图集 BlogPageGalleryImage (固定在文末展示，支持独立图注与拖拽排序)；
7. 简介与正文外置 StreamField (BlogIntroBlocks 与 BlogBodyBlocks)；
8. 严密页面树层级约束 parent_page_types / subpage_types。
"""

from django.db import models
from django.utils import timezone
from modelcluster.fields import ParentalKey
from modelcluster.contrib.taggit import ClusterTaggableManager
from taggit.models import TaggedItemBase
from wagtail.models import Orderable, Page
from wagtail.fields import StreamField
from wagtail.admin.panels import FieldPanel, InlinePanel, MultiFieldPanel

from content_lifecycle.models import CustomBasePage
from media_library.models import CustomImage
from people.models import Person
from blog.blocks import BlogIntroBlocks, BlogBodyBlocks


CONTRIBUTION_ROLE_CHOICES = [
    ("primary_author", "主要作者"),
    ("co_author", "共同作者"),
    ("reviewer", "审校人员"),
    ("translator", "翻译贡献"),
    ("editor", "责任编辑"),
]


class BlogPagePerson(Orderable):
    """文章与人员的多对多贡献关系（支持排序与贡献角色）"""
    page = ParentalKey("blog.BlogPage", on_delete=models.CASCADE, related_name="authors")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="+", verbose_name="人员")
    role = models.CharField(
        max_length=32,
        choices=CONTRIBUTION_ROLE_CHOICES,
        default="primary_author",
        verbose_name="贡献角色",
    )

    panels = [
        FieldPanel("person"),
        FieldPanel("role"),
    ]

    class Meta(Orderable.Meta):
        verbose_name = "贡献人员"
        verbose_name_plural = "贡献人员"


class BlogPageTag(TaggedItemBase):
    """文章与全站统一标签词库的关联项"""
    content_object = ParentalKey("blog.BlogPage", on_delete=models.CASCADE, related_name="tagged_items")


class BlogPageGalleryImage(Orderable):
    """文章独立图集图片项（位于正文后，由 Page 独立维护排序与图注）"""
    page = ParentalKey("blog.BlogPage", on_delete=models.CASCADE, related_name="gallery_images")
    image = models.ForeignKey(
        CustomImage,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name="图集图片",
    )
    caption = models.CharField(max_length=255, blank=True, verbose_name="独立图注说明")

    panels = [
        FieldPanel("image"),
        FieldPanel("caption"),
    ]

    class Meta(Orderable.Meta):
        verbose_name = "图集图片"
        verbose_name_plural = "图集图片"


class BlogIndexPage(CustomBasePage):
    """博客栏目 / 目录页面，可递归多层嵌套"""
    authored_on = models.DateField(
        default=timezone.now,
        verbose_name="成文日期",
        help_text="必填成文日期，默认当天，与实际发布时间完全独立",
    )
    intro = models.TextField(blank=True, verbose_name="栏目简介")

    # 页面树层级约束：只能在根首页或父栏目下创建
    parent_page_types = ["home.HomePage", "blog.BlogIndexPage"]
    # 子页面允许自嵌套栏目，或包含具体文章
    subpage_types = ["blog.BlogIndexPage", "blog.BlogPage"]

    content_panels = Page.content_panels + [
        FieldPanel("authored_on"),
        FieldPanel("intro"),
    ]

    class Meta:
        verbose_name = "博客栏目"
        verbose_name_plural = "博客栏目"

    def get_context(self, request, *args, **kwargs):
        context = super().get_context(request, *args, **kwargs)
        # 真实有效面包屑导航 (过滤回收节点与根节点)
        ancestors = []
        for ancestor in self.get_ancestors():
            if ancestor.is_root():
                continue
            if getattr(ancestor, "is_recycled", False):
                continue
            ancestors.append(ancestor)
        context["breadcrumbs"] = ancestors

        # 下属已发布且未回收的直接子栏目与文章
        raw_children = self.get_children().live().specific()
        context["children_pages"] = [
            p for p in raw_children if not getattr(p, "is_recycled", False)
        ]
        return context


class BlogPage(CustomBasePage):
    """核心博客文章页面"""
    authored_on = models.DateField(
        default=timezone.now,
        verbose_name="成文日期",
        help_text="必填成文日期，默认当天，与实际发布时间完全独立",
    )
    featured_image = models.ForeignKey(
        CustomImage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="封面大图",
        help_text="可选封面，居中置于标题上方，无封面不留白框",
    )
    tags = ClusterTaggableManager(through=BlogPageTag, blank=True, verbose_name="全站标签")
    intro = StreamField(BlogIntroBlocks(), use_json_field=True, blank=True, verbose_name="文章简介")
    body = StreamField(BlogBodyBlocks(), use_json_field=True, blank=True, verbose_name="正文积木")

    # 页面树层级约束：可在首页、栏目页或作为子文章系列在其他文章下创建
    parent_page_types = ["home.HomePage", "blog.BlogIndexPage", "blog.BlogPage"]
    subpage_types = ["blog.BlogPage"]

    content_panels = Page.content_panels + [
        FieldPanel("authored_on"),
        FieldPanel("featured_image"),
        InlinePanel("authors", label="人员与贡献角色"),
        FieldPanel("tags"),
        FieldPanel("intro"),
        FieldPanel("body"),
        InlinePanel("gallery_images", label="页面独立图集（文末展示）"),
    ]

    class Meta:
        verbose_name = "博客文章"
        verbose_name_plural = "博客文章"

    def save_revision(self, user=None, approved_go_live_at=None, changed=True, log_action=True, previous_revision=None, clean=True):
        """
        保存修订版本时自动将 intro 与 body 积木外置持久化到 MongoDB：
        1. 在 MongoDB 集合 content_field_versions 中生成带有 SHA-256 哈希的不可变快照；
        2. 在当前 Revision content 中绑定 _external_field_pointers 版本指针。
        """
        revision = super().save_revision(
            user=user,
            approved_go_live_at=approved_go_live_at,
            changed=changed,
            log_action=log_action,
            previous_revision=previous_revision,
            clean=clean,
        )
        try:
            import json
            import uuid
            from external_content.store import MongoContentStore
            from external_content.revision import bind_pointers_to_revision_content

            store = MongoContentStore()
            doc_uuid = self.document_uuid
            pointers = []
            op_id = f"op_rev_{revision.id}_{uuid.uuid4().hex[:8]}"

            # 1. 外置保存 intro 到 MongoDB
            if self.intro:
                intro_val = self.intro.get_prep_value() if hasattr(self.intro, "get_prep_value") else list(self.intro)
                ptr_intro = store.save_field_version(
                    document_uuid=doc_uuid,
                    field_name="intro",
                    payload=intro_val,
                    model_label="blog.BlogPage",
                    operation_id=f"{op_id}_intro",
                )
                pointers.append(ptr_intro)

            # 2. 外置保存 body 到 MongoDB
            if self.body:
                body_val = self.body.get_prep_value() if hasattr(self.body, "get_prep_value") else list(self.body)
                ptr_body = store.save_field_version(
                    document_uuid=doc_uuid,
                    field_name="body",
                    payload=body_val,
                    model_label="blog.BlogPage",
                    operation_id=f"{op_id}_body",
                )
                pointers.append(ptr_body)

            # 3. 在 Revision 中记录外置版本指针
            if pointers:
                content_dict = revision.content if isinstance(revision.content, dict) else json.loads(revision.content)
                bind_pointers_to_revision_content(content_dict, pointers)
                revision.content = content_dict
                revision.save(update_fields=["content"])
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Failed to auto-externalize fields to MongoDB on save_revision: %s", exc)
        finally:
            try:
                store.close()
            except Exception:
                pass

        return revision

    def get_context(self, request, *args, **kwargs):
        context = super().get_context(request, *args, **kwargs)
        # 1. 真实有效面包屑导航 (过滤回收节点与根节点)
        ancestors = []
        for ancestor in self.get_ancestors():
            if ancestor.is_root():
                continue
            if getattr(ancestor, "is_recycled", False):
                continue
            ancestors.append(ancestor)
        context["breadcrumbs"] = ancestors

        # 2. 贡献人员列表 (按 sort_order 排序并加载关联 person)
        context["authors"] = self.authors.select_related("person").all()

        # 3. 独立图集 (文末展示，按 sort_order 排序并加载关联 image)
        context["gallery_images"] = self.gallery_images.select_related("image").all()

        # 4. 标签列表
        context["tags"] = self.tags.all()

        # 5. 草稿预览与版本判定
        in_preview = getattr(request, "is_preview", False) or getattr(request, "in_preview_panel", False)
        context["is_preview"] = in_preview
        return context
