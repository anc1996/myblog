"""
guides.models
专题技术专栏与指南数据模型。

落实业务契约：
1. GuideTopic: 指南技术主题 Snippet，带 models.PROTECT 防级联删除；
2. GuideIndexPage: 专题指南聚合大厅，继承 CustomBasePage（30 天回收站保护），支持主题与难度过滤；
3. GuidePage: 专题指南详情页，继承 CustomBasePage，全面贯彻方案 A【彻底脱水模式】：
   - 正文 body 外置于 MongoDB，PostgreSQL 主表列保持 2 字节 []；
   - 决策一：超过 1 年未技术复核（last_reviewed_at >= 365 天）的文章顶部轻提示；
   - 决策二：存草稿宽松；正式发布时强校验必须绑定 Topic 且至少包含 1 个 step 步骤块（配备 GuidePageAdminForm 与 publish 守卫）；
   - 支持多作者 (people.Person)、统一标签 (taxonomy.Tag)、有序关联指南 (GuideRelatedPage)；
   - 包含 MEDIA-REF-01 媒体引用注册与步骤目录 steps_toc。
"""

from datetime import datetime, date, timedelta
import json
import logging
from uuid import UUID
from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from modelcluster.fields import ParentalKey
from modelcluster.contrib.taggit import ClusterTaggableManager
from taggit.models import TaggedItemBase
from wagtail.models import Page, Orderable
from wagtail.fields import StreamField
from wagtail.admin.panels import FieldPanel, InlinePanel, MultiFieldPanel
from wagtail.admin.forms import WagtailAdminPageForm

from content_lifecycle.models import CustomBasePage
from content_lifecycle.access import AccessContext, AccessMode
from media_library.models import CustomImage
from people.models import Person
from external_content.store import MongoContentStore
from external_content.revision import bind_pointers_to_revision_content
from .blocks import GuideStreamBlock

logger = logging.getLogger(__name__)

CONTRIBUTION_ROLE_CHOICES = [
    ("primary_author", "主笔作者"),
    ("co_author", "共同作者"),
    ("reviewer", "技术审校"),
    ("translator", "翻译"),
]

DIFFICULTY_CHOICES = [
    ("beginner", "🟢 入门初阶"),
    ("intermediate", "🟡 进阶实战"),
    ("advanced", "🔴 深入调优"),
]


class GuidePageAdminForm(WagtailAdminPageForm):
    """
    Wagtail 8.1 专栏定制后台表单。
    精细识别“保存草稿/预览”与“公开发布/提交审核”动作，解决原生 Page.live 默认 True 的校验冲突。
    """
    def defer_required_fields(self):
        super().defer_required_fields()
        # Wagtail 8.1 在点击“保存草稿”及表单预览验证时自动调用此方法
        self.instance._is_deferred_draft_validation = True

    def _post_clean(self):
        if not self.is_deferred_validation:
            self.instance._is_publishing_validation = True
        super()._post_clean()


class GuideTopic(models.Model):
    """技术专栏专题主题（Wagtail Snippet）"""
    name = models.CharField(max_length=100, verbose_name="主题名称")
    slug = models.SlugField(max_length=100, unique=True, allow_unicode=True, verbose_name="URL 标识符")
    description = models.TextField(blank=True, verbose_name="主题适用场景与介绍")
    order = models.PositiveIntegerField(default=0, verbose_name="展示排序权重")

    panels = [
        FieldPanel("name"),
        FieldPanel("slug"),
        FieldPanel("description"),
        FieldPanel("order"),
    ]

    class Meta:
        verbose_name = "专栏主题"
        verbose_name_plural = "专栏主题"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class GuidePageAuthor(Orderable):
    """指南与核心作者/审校人员的关联项"""
    page = ParentalKey("guides.GuidePage", on_delete=models.CASCADE, related_name="authors")
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


class GuidePageTag(TaggedItemBase):
    """指南与全站统一标签库的关联项"""
    content_object = ParentalKey("guides.GuidePage", on_delete=models.CASCADE, related_name="tagged_items")


class GuideRelatedPage(Orderable):
    """指南有序相关文章/前后篇步骤外链"""
    page = ParentalKey("guides.GuidePage", on_delete=models.CASCADE, related_name="related_guides")
    related_page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="+", verbose_name="关联页面")

    panels = [
        FieldPanel("related_page"),
    ]

    class Meta(Orderable.Meta):
        verbose_name = "关联技术指南"
        verbose_name_plural = "关联技术指南"


class GuideIndexPage(CustomBasePage):
    """专题指南聚合大厅页面（支持主题与难度聚合筛选）"""
    allow_comments = False
    intro = models.TextField(blank=True, verbose_name="专栏聚合大厅简介")
    featured_topic = models.ForeignKey(
        GuideTopic,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="置顶精选主题",
    )

    parent_page_types = ["home.HomePage", "guides.GuideIndexPage"]
    subpage_types = ["guides.GuidePage", "guides.GuideIndexPage"]

    content_panels = Page.content_panels + [
        FieldPanel("featured_topic"),
        FieldPanel("intro"),
    ]

    class Meta:
        verbose_name = "技术专栏聚合页"
        verbose_name_plural = "技术专栏聚合页"

    def get_context(self, request, *args, **kwargs):
        context = super().get_context(request, *args, **kwargs)
        # 获取有效上线的直接子页面（过滤已回收页面）
        qs = [
            p for p in self.get_children().live().specific()
            if not getattr(p, "is_recycled", False) and isinstance(p, GuidePage)
        ]
        topic_slug = request.GET.get("topic")
        difficulty = request.GET.get("difficulty")
        if topic_slug:
            qs = [p for p in qs if getattr(p, "topic", None) and p.topic.slug == topic_slug]
        if difficulty:
            qs = [p for p in qs if getattr(p, "difficulty", None) == difficulty]

        context["guides"] = qs
        context["all_topics"] = GuideTopic.objects.all()
        context["selected_topic"] = topic_slug
        context["selected_difficulty"] = difficulty
        return context


class GuidePage(CustomBasePage):
    """
    技术专题指南详情页面。
    继承 CustomBasePage（自动享有 30 天原树受控回收站保护与防误删门禁）。
    正文 body 彻底脱水外置于 MongoDB。
    """
    base_form_class = GuidePageAdminForm

    topic = models.ForeignKey(
        GuideTopic,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="guides",
        verbose_name="所属技术专栏主题",
        help_text="关联的专栏主题；发布时为必填项",
    )
    difficulty = models.CharField(
        max_length=16,
        choices=DIFFICULTY_CHOICES,
        default="intermediate",
        verbose_name="难度等级",
    )
    estimated_minutes = models.PositiveIntegerField(
        default=15,
        verbose_name="预估实操耗时(分钟)",
    )
    authored_on = models.DateField(
        default=timezone.now,
        verbose_name="初次成文日期",
    )
    last_reviewed_at = models.DateField(
        default=timezone.now,
        verbose_name="最后技术复核日期",
        help_text="最后核对技术有效性的日期；若超过 1 年未复核，前台自动展示时效提示",
    )
    summary = models.TextField(
        blank=True,
        verbose_name="摘要导语",
        help_text="简要概括本指南解决的核心问题",
    )
    featured_image = models.ForeignKey(
        CustomImage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="封面/架构图",
    )
    body = StreamField(
        GuideStreamBlock(),
        blank=True,
        use_json_field=True,
        verbose_name="教程正文积木",
    )

    tags = ClusterTaggableManager(through=GuidePageTag, blank=True, verbose_name="技术标签")

    parent_page_types = ["guides.GuideIndexPage"]
    subpage_types = []

    content_panels = Page.content_panels + [
        MultiFieldPanel(
            [
                FieldPanel("topic"),
                FieldPanel("difficulty"),
                FieldPanel("estimated_minutes"),
                FieldPanel("authored_on"),
                FieldPanel("last_reviewed_at"),
            ],
            heading="专栏与复核元数据",
        ),
        FieldPanel("summary"),
        FieldPanel("featured_image"),
        InlinePanel("authors", label="核心作者与技术审校"),
        FieldPanel("tags"),
        FieldPanel("body"),
        InlinePanel("related_guides", label="前后篇/相关专栏外链"),
    ]

    class Meta:
        verbose_name = "专题指南详情"
        verbose_name_plural = "专题指南详情"

    def _validate_publish_gate(self):
        """决策二核心发布门禁校验"""
        errors = {}
        if not self.topic:
            errors["topic"] = "公开发布前必须绑定所属技术专栏主题 (Topic)！"
        has_step = any(b.block_type == "step" for b in self.body) if self.body else False
        if not has_step:
            errors["body"] = "专题指南在正式发布前必须包含至少一个【操作步骤】(step) 积木！"
        if errors:
            raise ValidationError(errors)

    def clean(self):
        super().clean()
        # 若处于草稿延迟校验模式（点击“保存草稿”或“预览”），永远宽容放行
        if getattr(self, "_is_deferred_draft_validation", False):
            return
        # 若在后台点击发布（Publish / Submit），或非后台表单调用且已处于上线态
        if getattr(self, "_is_publishing_validation", False) or (not hasattr(self, "_is_deferred_draft_validation") and self.live):
            self._validate_publish_gate()

    def publish(self, revision=None, user=None, changed=True, log_action=True, previous_revision=None, **kwargs):
        """编程接口发布守卫：防止跳过后台表单直接通过 revision.publish() 上线空壳文章"""
        target_obj = revision.as_object() if revision else self
        target_obj._validate_publish_gate()
        return super().publish(
            revision=revision,
            user=user,
            changed=changed,
            log_action=log_action,
            previous_revision=previous_revision,
            **kwargs,
        )

    def is_outdated(self) -> tuple[bool, int, str]:
        """决策一：技术复核时效检查（容忍 datetime 与 date）"""
        if self.last_reviewed_at:
            rev_date = self.last_reviewed_at.date() if isinstance(self.last_reviewed_at, datetime) else self.last_reviewed_at
            now_date = timezone.now().date()
            diff_days = (now_date - rev_date).days
            if diff_days >= 365:
                years = max(1, diff_days // 365)
                return True, diff_days, f"⚠️ 本指南最后技术复核于 {years} 年前，部分依赖包版本可能有更新，请结合官方文档参考。"
        return False, 0, ""

    def get_steps_toc(self) -> list[dict[str, str]]:
        """从 body 积木中提取操作步骤目录 (TOC)"""
        toc = []
        if self.body:
            for b in self.body:
                if b.block_type == "step":
                    val = b.value if isinstance(b.value, dict) else (b.block.get_prep_value(b.value) if hasattr(b.block, "get_prep_value") else {})
                    num = str(val.get("step_number") or len(toc) + 1)
                    toc.append({
                        "step_number": num,
                        "title": str(val.get("title") or f"步骤 {num}"),
                        "anchor": f"step-{num}",
                    })
        return toc

    def get_admin_display_title(self):
        """后台列表与版本差异对比 (RevisionsCompareView) 时动态水合正文"""
        if not getattr(self, "_hydrated_from_revision", False) and (not getattr(self, "body", None) or len(getattr(self, "body", [])) == 0):
            try:
                self.hydrate_from_mongo(access_context=AccessContext(mode=AccessMode.PUBLIC))
            except Exception:
                pass
        return self.draft_title or self.title

    def hydrate_from_mongo(self, access_context=None):
        """方案 A【彻底脱水模式】：从 MongoDB 按生效版本指针精准水合恢复正文积木"""
        from external_content.revision import get_effective_page_content
        from .blocks import GuideStreamBlock
        if access_context is None:
            mode = AccessMode.PUBLIC if (self.live and self.live_revision_id) else AccessMode.PREVIEW_DRAFT
            access_context = AccessContext(mode=mode)
        store = None
        try:
            store = MongoContentStore()
            data = get_effective_page_content(self, access_context=access_context, store=store)
            if data and not data.get("error"):
                if data.get("body"):
                    body_block = GuideStreamBlock()
                    self.body = body_block.to_python(data["body"])
        except Exception as exc:
            logger.warning("Failed to hydrate GuidePage %s from MongoDB: %s", self.id, exc)
        finally:
            if store:
                store.close()

    def get_latest_revision_as_object(self):
        """后台编辑或预览时，从最新草稿版本指针中水合正文"""
        page = super().get_latest_revision_as_object()
        if not getattr(page, "_hydrated_from_revision", False) and hasattr(page, "hydrate_from_mongo"):
            page.hydrate_from_mongo(access_context=AccessContext(mode=AccessMode.PREVIEW_DRAFT))
            page._hydrated_from_revision = True
        return page

    def save(self, *args, **kwargs):
        """落库物理脱水拦截：PostgreSQL 主表列始终清空为 []"""
        raw_body = self.body
        super().save(*args, **kwargs)
        if self.pk:
            GuidePage.objects.filter(pk=self.pk).update(body=[])
        self.body = raw_body

    def with_content_json(self, content):
        """Wagtail 8.1 官方反序列化入口：按指针精准从 MongoDB 还原正文（严格抛出哈希错误）"""
        content_dict = content if isinstance(content, dict) else json.loads(content)
        obj = super().with_content_json(content)

        pointers_list = content_dict.get("_external_field_pointers", [])
        if pointers_list:
            store = None
            try:
                store = MongoContentStore()
                for item in pointers_list:
                    if item.get("field_name") == "body":
                        doc_uuid = UUID(item["document_uuid"])
                        version_id = item["version_id"]
                        expected_hash = item.get("content_hash")
                        res = store.get_field_version(
                            document_uuid=doc_uuid,
                            field_name="body",
                            version_id=version_id,
                            expected_hash=expected_hash,
                        )
                        if res and res.payload:
                            obj.body = GuideStreamBlock().to_python(res.payload)
            finally:
                if store:
                    store.close()

        obj._hydrated_from_revision = True
        return obj

    def save_revision(
        self,
        user=None,
        approved_go_live_at=None,
        changed=True,
        log_action=True,
        previous_revision=None,
        clean=True,
        overwrite_revision=None,
        **kwargs,
    ):
        """保存修订版本：外置 MongoDB 正文 + 脱水 Revision + MEDIA-REF-01 注册 + 草稿滑动窗口修剪"""
        raw_body = self.body
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

                import uuid
                store = MongoContentStore()
                doc_uuid = self.document_uuid
                pointers = []
                op_id = f"op_guide_rev_{revision.id}_{uuid.uuid4().hex[:8]}"

                if raw_body:
                    body_val = raw_body.get_prep_value() if hasattr(raw_body, "get_prep_value") else list(raw_body)
                    ptr_body = store.save_field_version(
                        document_uuid=doc_uuid,
                        field_name="body",
                        payload=body_val,
                        model_label="guides.GuidePage",
                        operation_id=f"{op_id}_body",
                    )
                    pointers.append(ptr_body)

                content_dict = revision.content if isinstance(revision.content, dict) else json.loads(revision.content)
                bind_pointers_to_revision_content(content_dict, pointers)
                content_dict["body"] = []
                revision.content = content_dict
                revision.save(update_fields=["content"])

                GuidePage.objects.filter(id=self.id).update(body=[])
                self.body = raw_body

                # 登记媒体引用到 MEDIA-REF-01 (封面图与步骤配图)
                try:
                    from media_library.references import MediaReferenceTracker
                    from content_blocks.contracts import MediaReference
                    refs = []
                    if self.featured_image:
                        refs.append(MediaReference(
                            asset_id=str(self.featured_image.public_id),
                            asset_type="image",
                            block_path="featured_image",
                        ))
                    if raw_body:
                        for i, child in enumerate(raw_body):
                            block = getattr(child, "block", None)
                            if block and hasattr(block, "extract_media_refs"):
                                refs.extend(block.extract_media_refs(child.value, path=f"body.{i}"))
                    MediaReferenceTracker.register_revision_references(
                        page_id=self.id,
                        revision_id=revision.id,
                        references=refs,
                    )
                except Exception as ref_exc:
                    logger.debug("Failed to register media refs for GuidePage %s: %s", self.id, ref_exc)

                # 草稿修剪契约 (RECYCLE-RETENTION-03)
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
        if not getattr(self, "_hydrated_from_revision", False) and (not getattr(self, "body", None) or len(getattr(self, "body", [])) == 0):
            is_preview = getattr(request, "is_preview", False) or getattr(request, "in_preview_panel", False)
            mode = AccessMode.PREVIEW_DRAFT if is_preview else AccessMode.PUBLIC
            self.hydrate_from_mongo(access_context=AccessContext(user=getattr(request, "user", None), mode=mode))

        context = super().get_context(request, *args, **kwargs)
        is_outdated, days, warning_msg = self.is_outdated()
        context["is_outdated"] = is_outdated
        context["outdated_warning"] = warning_msg
        context["steps_toc"] = self.get_steps_toc()
        # 过滤未发布或已回收的关联指南
        context["active_related_guides"] = [
            r for r in self.related_guides.select_related("related_page").all()
            if r.related_page.live and not getattr(r.related_page.specific, "is_recycled", False)
        ]
        return context
