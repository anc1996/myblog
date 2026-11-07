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
    allow_comments = False
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

    def hydrate_from_mongo(self, access_context=None):
        """
        方案 A【彻底脱水模式】：从 MongoDB 按生效版本指针精准水合恢复 intro 与 body 积木。
        PostgreSQL 物理行仅存 [] 空占位（2字节），彻底免除千万级长文导致的 TOAST 表膨胀！
        """
        from external_content.revision import get_effective_page_content
        from blog.blocks import BlogIntroBlocks, BlogBodyBlocks
        from external_content.store import MongoContentStore
        store = None
        try:
            store = MongoContentStore()
            data = get_effective_page_content(self, access_context=access_context, store=store)
            if data and not data.get("error"):
                if data.get("intro"):
                    intro_block = BlogIntroBlocks()
                    self.intro = intro_block.to_python(data["intro"])
                if data.get("body"):
                    body_block = BlogBodyBlocks()
                    self.body = body_block.to_python(data["body"])
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Failed to hydrate page %s from MongoDB: %s", self.id, exc)
        finally:
            if store:
                store.close()

    def get_latest_revision_as_object(self):
        """后台编辑或预览时，从最新草稿版本指针中水合正文"""
        page = super().get_latest_revision_as_object()
        if not getattr(page, "_hydrated_from_revision", False) and hasattr(page, "hydrate_from_mongo"):
            from content_lifecycle.access import AccessContext, AccessMode
            page.hydrate_from_mongo(access_context=AccessContext(mode=AccessMode.PREVIEW_DRAFT))
            page._hydrated_from_revision = True
        return page

    def save(self, *args, **kwargs):
        """
        保存页面模型物理行时的脱水守卫：
        即使 Wagtail PublishRevisionAction 将水合后的正文写回，也在此拦截将 PostgreSQL 物理列清空为 []，
        同时保留内存中的 self.intro 与 self.body 供信号监听器与搜索投影直接使用。
        """
        raw_intro = self.intro
        raw_body = self.body
        super().save(*args, **kwargs)
        if self.pk:
            BlogPage.objects.filter(pk=self.pk).update(intro=[], body=[])
        self.intro = raw_intro
        self.body = raw_body

    def with_content_json(self, content):
        """
        Wagtail 8.1 官方反序列化入口重写：
        从 Revision content 字典中解析 _external_field_pointers，精准从 MongoDB 还原正文积木。
        完美兼容 Wagtail 原生的历史预览 (RevisionsView)、历史对比 (RevisionsCompareView) 与版本还原 (RevisionsRevertView)！
        """
        import json
        from uuid import UUID
        from external_content.store import MongoContentStore
        from blog.blocks import BlogIntroBlocks, BlogBodyBlocks

        content_dict = content if isinstance(content, dict) else json.loads(content)
        obj = super().with_content_json(content)

        pointers_list = content_dict.get("_external_field_pointers", [])
        if pointers_list:
            store = None
            try:
                store = MongoContentStore()
                for item in pointers_list:
                    doc_uuid = UUID(item["document_uuid"])
                    field_name = item["field_name"]
                    version_id = item["version_id"]
                    expected_hash = item.get("content_hash")

                    res = store.get_field_version(
                        document_uuid=doc_uuid,
                        field_name=field_name,
                        version_id=version_id,
                        expected_hash=expected_hash,
                    )
                    if field_name == "intro" and res.payload:
                        obj.intro = BlogIntroBlocks().to_python(res.payload)
                    elif field_name == "body" and res.payload:
                        obj.body = BlogBodyBlocks().to_python(res.payload)
            finally:
                if store:
                    store.close()

        obj._hydrated_from_revision = True
        return obj

    def get_admin_display_title(self):
        title = super().get_admin_display_title()
        # 修复 Wagtail 8.1 RevisionsCompareView 在比较 "live" 正式版时直接拿 self.object 的盲区
        if self.live and (not getattr(self, "body", None) or len(getattr(self, "body", [])) == 0):
            from content_lifecycle.access import AccessContext, AccessMode
            self.hydrate_from_mongo(access_context=AccessContext(mode=AccessMode.PUBLIC))
        return title

    def save_revision(self, user=None, approved_go_live_at=None, changed=True, log_action=True, previous_revision=None, clean=True, overwrite_revision=None, **kwargs):
        """
        方案 A【彻底脱水模式】保存管线：
        1. 在 MongoDB 集合 content_field_versions 中生成带有 SHA-256 哈希的不可变快照；
        2. 在当前 Revision content 中绑定 _external_field_pointers 版本指针；
        3. 彻底清空 PostgreSQL 的 Revision 与 blog_blogpage 中的正文字符串，仅留 [] 占位。
        """
        raw_intro = self.intro
        raw_body = self.body

        # 安全防线：严禁原地覆写已上线的正式版本或排期发布版本
        if overwrite_revision:
            if overwrite_revision.id == self.live_revision_id or getattr(overwrite_revision, "approved_go_live_at", None) is not None:
                overwrite_revision = None

        from django.db import transaction
        with transaction.atomic():
            old_latest_rev = getattr(self, "latest_revision", None)
            old_latest_rev_id = getattr(self, "latest_revision_id", None)
            old_latest_rev_time = getattr(self, "latest_revision_created_at", None)
            old_has_changes = getattr(self, "has_unpublished_changes", False)
            old_draft_title = getattr(self, "draft_title", None)
            store = None

            try:
                revision = super().save_revision(
                    user=user,
                    approved_go_live_at=approved_go_live_at,
                    changed=changed,
                    log_action=log_action,
                    previous_revision=previous_revision,
                    clean=clean,
                    overwrite_revision=overwrite_revision,
                    **kwargs,
                )
                import json
                import uuid
                from external_content.store import MongoContentStore
                from external_content.revision import bind_pointers_to_revision_content

                store = MongoContentStore()
                doc_uuid = self.document_uuid
                pointers = []
                op_id = f"op_rev_{revision.id}_{uuid.uuid4().hex[:8]}"

                # 1. 将 intro 积木流全量外置保存到 MongoDB
                if raw_intro:
                    intro_val = raw_intro.get_prep_value() if hasattr(raw_intro, "get_prep_value") else list(raw_intro)
                    ptr_intro = store.save_field_version(
                        document_uuid=doc_uuid,
                        field_name="intro",
                        payload=intro_val,
                        model_label="blog.BlogPage",
                        operation_id=f"{op_id}_intro",
                    )
                    pointers.append(ptr_intro)

                # 2. 将 body 积木流全量外置保存到 MongoDB
                if raw_body:
                    body_val = raw_body.get_prep_value() if hasattr(raw_body, "get_prep_value") else list(raw_body)
                    ptr_body = store.save_field_version(
                        document_uuid=doc_uuid,
                        field_name="body",
                        payload=body_val,
                        model_label="blog.BlogPage",
                        operation_id=f"{op_id}_body",
                    )
                    pointers.append(ptr_body)

                # 3. 在 Revision 中无条件记录外置版本指针，并彻底清空 Revision 字典中的正文大文本
                content_dict = revision.content if isinstance(revision.content, dict) else json.loads(revision.content)
                bind_pointers_to_revision_content(content_dict, pointers)
                # 彻底脱水：PostgreSQL Revision 仅存指针，不存大 JSON
                content_dict["intro"] = []
                content_dict["body"] = []
                revision.content = content_dict
                revision.save(update_fields=["content"])

                # 4. 彻底脱水：将 PostgreSQL 主表 blog_blogpage 的物理列清空为 []
                BlogPage.objects.filter(id=self.id).update(intro=[], body=[])

                # 保持内存对象状态，供后续信号与搜索投影直接使用
                self.intro = raw_intro
                self.body = raw_body

                # 5. 草稿版本滑动窗口修剪契约 (RECYCLE-RETENTION-03)
                was_ever_published = bool(
                    self.first_published_at
                    or (self.live and getattr(self, "live_revision_id", None))
                    or getattr(self, "approved_go_live_at", None)
                )
                if not was_ever_published and self.id:
                    from wagtail.models import Page
                    from django.db.models import Q
                    was_ever_published = Page.objects.filter(id=self.id).filter(
                        Q(first_published_at__isnull=False) | Q(live=True)
                    ).exists()

                if not was_ever_published:
                    from content_lifecycle.models import RecycleEntry
                    if RecycleEntry.objects.filter(page_id=self.id, was_live=True).exists():
                        was_ever_published = True

                if not was_ever_published:
                    from wagtail.models import Revision
                    draft_revs = list(Revision.objects.filter(object_id=str(self.id)).order_by("-created_at"))
                    if len(draft_revs) > 20:
                        to_delete = draft_revs[20:]
                        for old_rev in to_delete:
                            if old_rev.id != revision.id:
                                old_rev.delete()

            except Exception:
                self.latest_revision = old_latest_rev
                self.latest_revision_id = old_latest_rev_id
                self.latest_revision_created_at = old_latest_rev_time
                self.has_unpublished_changes = old_has_changes
                self.draft_title = old_draft_title
                raise
            finally:
                if store:
                    try:
                        store.close()
                    except Exception:
                        pass

        return revision

    def get_context(self, request, *args, **kwargs):
        # 方案 A【彻底脱水模式】：仅在尚未从特定 Revision 水合时，从 MongoDB 精准水合生效版本的正文积木
        if not getattr(self, "_hydrated_from_revision", False) and (not getattr(self, "body", None) or len(getattr(self, "body", [])) == 0):
            from content_lifecycle.access import AccessContext, AccessMode
            is_preview = getattr(request, "is_preview", False) or getattr(request, "in_preview_panel", False)
            mode = AccessMode.PREVIEW_DRAFT if is_preview else AccessMode.PUBLIC
            ctx = AccessContext(user=getattr(request, "user", None), mode=mode)
            self.hydrate_from_mongo(access_context=ctx)

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
