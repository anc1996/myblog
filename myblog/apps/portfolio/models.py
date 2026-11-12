"""
portfolio.models
作品集大厅 (PortfolioIndexPage) 与项目详情 (ProjectPage) 核心模型。

落实架构契约：
1. 继承 CustomBasePage：获得稳定的 document_uuid、原树回收 (is_recycled) 与通用评论 (allow_comments=True)；
2. 方案 A【彻底脱水模式】：实现 ExternalStreamPageMixin，物理列 body 始终为 [] (2字节)，不可变正文持久化至 MongoDB；
3. 规范树形拓扑：PortfolioIndexPage 挂载于 HomePage 下，ProjectPage 严格作为 PortfolioIndexPage 的子页面；
4. 专属项目元数据：涵盖项目状态、起止日期、成文日期、代码仓库、Star 数与开源协议。
"""
import json
import logging
import uuid
from datetime import date
from typing import Any
from django.db import models
from django.utils import timezone
from modelcluster.fields import ParentalKey
from modelcluster.contrib.taggit import ClusterTaggableManager
from taggit.models import TaggedItemBase
from wagtail.models import Page, Orderable
from wagtail.fields import StreamField
from wagtail.admin.panels import FieldPanel, MultiFieldPanel, InlinePanel

from content_lifecycle.models import CustomBasePage
from external_content.store import MongoContentStore
from discovery.listing import HierarchicalListingMixin, ListingPolicy
from portfolio.blocks import PortfolioStreamBlock

logger = logging.getLogger(__name__)

PROJECT_STATUS_CHOICES = [
    ("planning", "规划筹备中"),
    ("in_development", "敏捷研发中"),
    ("maintained", "生产稳定维护"),
    ("archived", "归档与封存"),
]


class ExternalStreamPageMixin:
    """
    方案 A【彻底脱水模式】通用页面混入类 (H-08)。
    1. save() 前置脱水：将 body 替换为 [] 后调用父类写入 PostgreSQL，并在 finally 中还原内存 raw_body；
    2. validate_constraints()：在校验表级 CHECK(body=[]) 约束时在 try 块内临时置空，避免内存真实正文导致校验失败；
    3. save_revision()：在序列化前将正文保存至 MongoDB，在 serializable_data() 中将 body 置空并注入指针；
    4. with_content_json()：从 Revision 提取不可变指针并毫秒级水合正文。
    """

    def validate_constraints(self, exclude=None):
        raw_body = getattr(self, "body", None)
        try:
            self.body = []
            super().validate_constraints(exclude=exclude)
        finally:
            self.body = raw_body

    def save(self, clean=True, *args, **kwargs):
        raw_body = getattr(self, "body", None)
        if clean:
            if getattr(self, "live", False):
                self.full_clean()
            else:
                self.minimal_clean()
        try:
            self.body = []
            return super().save(clean=False, *args, **kwargs)
        finally:
            self.body = raw_body

    def serializable_data(self):
        data = super().serializable_data()
        data["body"] = []
        pending = getattr(self, "_pending_external_pointers", None)
        if pending:
            data["_external_field_pointers"] = pending
        return data

    def save_revision(self, user=None, approved_go_live_at=None, changed=True, log_action=True, previous_revision=None, clean=True, overwrite_revision=None, **kwargs):
        from external_content.store import MongoContentStore
        from external_content.contracts import FieldVersionPointer
        from external_content.revision import bind_pointers_to_revision_content

        raw_body = getattr(self, "body", None)
        if clean:
            if getattr(self, "live", False):
                self.full_clean()
            else:
                self.minimal_clean()

        store = None
        pointers = []
        try:
            store = MongoContentStore()
            doc_uuid = getattr(self, "document_uuid", None) or uuid.uuid4()
            op_id = f"op_proj_{uuid.uuid4().hex[:8]}"

            if raw_body:
                body_val = raw_body.get_prep_value() if hasattr(raw_body, "get_prep_value") else list(raw_body)
                ptr_body = store.save_field_version(
                    document_uuid=doc_uuid,
                    field_name="body",
                    payload=body_val,
                    model_label="portfolio.ProjectPage",
                    operation_id=f"{op_id}_body",
                )
                pointers.append(ptr_body)
        finally:
            if store:
                store.close()

        pointers_dict_list = [
            {
                "field_name": p.field_name,
                "version_id": p.version_id,
                "document_uuid": str(p.document_uuid),
                "content_hash": p.content_hash,
            }
            for p in pointers
        ]
        self._pending_external_pointers = pointers_dict_list

        try:
            revision = super().save_revision(
                user=user,
                approved_go_live_at=approved_go_live_at,
                changed=changed,
                log_action=log_action,
                previous_revision=previous_revision,
                clean=False,
                overwrite_revision=overwrite_revision,
                **kwargs,
            )
            content_dict = revision.content if isinstance(revision.content, dict) else json.loads(revision.content)
            bind_pointers_to_revision_content(content_dict, pointers)
            content_dict["body"] = []
            revision.content = content_dict
            revision.save(update_fields=["content"])
            return revision
        finally:
            self._pending_external_pointers = None

    def with_content_json(self, content):
        import json
        from uuid import UUID
        from external_content.store import MongoContentStore
        from portfolio.blocks import PortfolioStreamBlock

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
                    if res and hasattr(res, "payload"):
                        if field_name == "body":
                            block = PortfolioStreamBlock()
                            obj.body = block.to_python(res.payload)
            except Exception as exc:
                logger.error("Failed to hydrate project body from MongoDB: %s", exc)
            finally:
                if store:
                    store.close()
        return obj

    def hydrate_from_mongo(self, access_context=None):
        """内存对象按需水合正文"""
        if getattr(self, "body", None) and len(self.body) > 0:
            return
        from external_content.revision import get_effective_page_content
        from portfolio.blocks import PortfolioStreamBlock
        from external_content.store import MongoContentStore
        store = None
        try:
            store = MongoContentStore()
            data = get_effective_page_content(self, access_context=access_context, store=store)
            if data and not data.get("error"):
                if data.get("body"):
                    block = PortfolioStreamBlock()
                    self.body = block.to_python(data["body"])
        except Exception as exc:
            logger.warning("Failed on-demand hydration for project page %s: %s", self.id, exc)
        finally:
            if store:
                store.close()


class PortfolioIndexPage(HierarchicalListingMixin, CustomBasePage):
    """作品集与开源项目大厅聚合页"""
    parent_page_types = ["home.HomePage"]
    subpage_types = ["portfolio.ProjectPage"]
    allow_comments = False
    listing_policy = ListingPolicy(
        type_keys=("project",),
        facet_keys=("tags",),
        navigation_type_keys=("portfolio_index",),
    )

    intro = models.TextField(blank=True, verbose_name="大厅介绍导语")

    content_panels = Page.content_panels + [
        FieldPanel("intro"),
    ]

    class Meta:
        verbose_name = "作品集大厅页"
        verbose_name_plural = "作品集大厅页"


class ProjectPagePerson(Orderable):
    """项目贡献人员关联子表 (H-01: 采用 CASCADE 避免 modelcluster 反序列化崩溃)"""
    page = ParentalKey("portfolio.ProjectPage", on_delete=models.CASCADE, related_name="authors")
    person = models.ForeignKey("people.Person", on_delete=models.CASCADE, related_name="+", verbose_name="核心研发/负责人")
    role = models.CharField(max_length=64, default="架构设计与主研发", verbose_name="项目分工角色")

    panels = [
        FieldPanel("person"),
        FieldPanel("role"),
    ]

    class Meta:
        verbose_name = "项目贡献人员"
        verbose_name_plural = "项目贡献人员"


class ProjectPageTag(TaggedItemBase):
    """项目技术标签关联关系"""
    content_object = ParentalKey("portfolio.ProjectPage", related_name="tagged_items", on_delete=models.CASCADE)


class ProjectPage(ExternalStreamPageMixin, CustomBasePage):
    """作品集项目详情页 (严格脱水 + 原树回收 + 通用评论)"""
    parent_page_types = ["portfolio.PortfolioIndexPage"]
    subpage_types = []
    supports_comments = True
    allow_comments = True

    project_status = models.CharField(
        max_length=32,
        choices=PROJECT_STATUS_CHOICES,
        default="maintained",
        db_index=True,
        verbose_name="项目生命周期状态",
    )
    summary = models.TextField(blank=True, verbose_name="项目摘要说明")
    started_on = models.DateField(null=True, blank=True, verbose_name="项目立项日期")
    completed_on = models.DateField(null=True, blank=True, verbose_name="稳定/交付日期")
    authored_on = models.DateField(default=timezone.localdate, db_index=True, verbose_name="公开展示成文日期")

    featured_image = models.ForeignKey(
        "media_library.CustomImage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="项目封面主图",
    )
    repo_url = models.URLField(blank=True, verbose_name="开源代码仓库地址")
    demo_url = models.URLField(blank=True, verbose_name="在线演示体验地址")
    primary_language = models.CharField(max_length=64, default="Python", blank=True, verbose_name="主要技术语言")
    star_count = models.PositiveIntegerField(default=0, verbose_name="Star 徽标数量")
    license_name = models.CharField(max_length=64, default="MIT", blank=True, verbose_name="开源协议")

    # 方案 A 核心脱水列：落库 PostgreSQL 必须保持为 []
    body = StreamField(PortfolioStreamBlock(), blank=True, use_json_field=True, verbose_name="项目详情积木流")

    tags = ClusterTaggableManager(through=ProjectPageTag, blank=True, verbose_name="技术标签")

    content_panels = Page.content_panels + [
        MultiFieldPanel(
            [
                FieldPanel("project_status"),
                FieldPanel("summary"),
                FieldPanel("authored_on"),
                FieldPanel("started_on"),
                FieldPanel("completed_on"),
            ],
            heading="项目基本生命周期",
        ),
        MultiFieldPanel(
            [
                FieldPanel("featured_image"),
                FieldPanel("repo_url"),
                FieldPanel("demo_url"),
                FieldPanel("primary_language"),
                FieldPanel("star_count"),
                FieldPanel("license_name"),
            ],
            heading="开源元数据与仓库链接",
        ),
        InlinePanel("authors", label="核心研发与架构贡献人员"),
        FieldPanel("tags"),
        FieldPanel("body"),
    ]

    class Meta:
        verbose_name = "作品集项目详情页"
        verbose_name_plural = "作品集项目详情页"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(body=[]),
                name="portfolio_projectpage_body_dehydrated",
            ),
        ]

    def get_context(self, request, *args, **kwargs):
        if not getattr(self, "_hydrated_from_revision", False) and (not getattr(self, "body", None) or len(getattr(self, "body", [])) == 0):
            from content_lifecycle.access import AccessContext, AccessMode
            is_preview = getattr(request, "is_preview", False) or getattr(request, "in_preview_panel", False)
            mode = AccessMode.PREVIEW_DRAFT if is_preview else AccessMode.PUBLIC
            self.hydrate_from_mongo(access_context=AccessContext(user=getattr(request, "user", None), mode=mode))

        context = super().get_context(request, *args, **kwargs)
        context["breadcrumbs"] = [
            ancestor for ancestor in self.get_ancestors()
            if not ancestor.is_root() and not getattr(ancestor, "is_recycled", False)
        ]
        context["authors"] = self.authors.select_related("person").all()
        context["tags"] = self.tags.all()
        return context
