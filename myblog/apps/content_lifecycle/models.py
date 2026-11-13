"""
全站统一业务页面抽象基类 (CustomBasePage)。

继承自 Wagtail 的 Page 模型，落实核心生命周期与回收安全契约：
1. document_uuid: 稳定的跨存储/跨版本业务文档唯一标识，与 Page ID 物理分离；
2. is_recycled: 原树占位回收状态标志，用于在所有视图与公共服务中隔离已回收页面；
3. publish() 守卫: 严格拦截自身或祖先处于已回收状态的非法发布尝试；
4. delete() 守卫: 拦截未经授权的直接物理删除，引导使用受控回收站服务；
5. copy() 守卫: 递归复制时排除已回收子节点，并在新副本中重新生成全新的 document_uuid；
6. update_aliases() 守卫: 排除已回收的 Alias 别名页面，防止源页面发布导致回收别名被动上线。
"""

import contextvars
import uuid
from contextlib import contextmanager
from django.db import models
from django.core.exceptions import PermissionDenied
from wagtail.models import Page


_PURGE_AUTHORIZED = contextvars.ContextVar("is_purge_authorized", default=False)


@contextmanager
def authorize_physical_purge():
    """
    上下文管理器：授权执行受控的物理清空（Purge）。
    在受控清理（RecycleService.purge_page_or_subtree）期间生效，
    允许 CustomBasePage 以及通过外键级联删除的相关模型（如 Comment）执行真实的物理删除，
    同时阻止未经授权的代码通过常规 ORM 操作绕过回收站直接物理删除。
    """
    token = _PURGE_AUTHORIZED.set(True)
    try:
        yield
    finally:
        _PURGE_AUTHORIZED.reset(token)


def is_purge_authorized() -> bool:
    """返回当前执行上下文是否已获得物理清空授权。"""
    return _PURGE_AUTHORIZED.get()


class CustomBasePage(Page):
    """
    全站统一抽象页面基类 (CustomBasePage)。
    所有业务页面（如 BlogPage、GuidePage、ProjectPage）均继承本基类。
    """

    # 1. 业务文档唯一身份标识 (document_uuid)
    # 【原理与作用】：
    # 为什么不能只用 Wagtail 自带的自增整数 id？
    # 因为在跨存储架构（PostgreSQL + MongoDB + S3 RustFS）中，自增 ID 会因跨库迁移、树重构或页面克隆而发生变化；
    # document_uuid 是文章与生俱来的“数字身份证号”，页面在树中改名、换父级或回收再恢复，该 UUID 永生不变，
    # 专门用于在 MongoDB 和媒体引用表中精确锚定属于本页面的历史版本。
    document_uuid = models.UUIDField(
        default=uuid.uuid4,  # 自动生成 128 位全局唯一的 UUIDv4 随机数
        editable=False,      # 页面出生即固定，绝不允许在管理后台人工篡改
        unique=True,         # 数据库级 UNIQUE 唯一性约束
        db_index=True,       # 建立 B-Tree 索引，毫秒级通过 UUID 反查页面
        verbose_name="业务文档唯一标识",
        help_text="跨 PostgreSQL、MongoDB 与媒体引用追踪使用的稳定不可变文档身份",
    )

    # 2. 原树占位回收标志 (is_recycled)
    # 【原理与作用】：
    # models.BooleanField 是 Django ORM 的布尔字段类，在 PostgreSQL 中映射为原生的 boolean 数据类型（TRUE/FALSE）。
    # 用于“原树软删除”状态机：当文章被删除时，不从数据表中物理擦除（物理擦除会破坏 treebeard 树结构），
    # 而是将本标记置为 True 原位占位。前台路由发现为 True 自动拦截为 404，管理后台树列表自动隐藏。
    is_recycled = models.BooleanField(
        default=False,       # 新建页面默认处于正常未回收状态 (False)
        editable=False,      # 禁止在编辑表单中人工勾选，必须通过回收站服务统一状态流转
        db_index=True,       # 建立 B-Tree 索引，极高频过滤条件（所有公开查询都要 WHERE is_recycled=false），加索引消除全表扫描
        verbose_name="是否已移入回收站",
        help_text="原树占位标记；已回收页面不可发布、对公网不可见且在管理后台普通目录隐藏",
    )

    # 3. 类级别评论准入策略开关 (allow_comments)
    # 【原理与作用】：
    # 遵循 RFC-020：抽象基类与聚合大厅默认不启用评论能力 (supports_comments=False)；
    # BlogPage、GuidePage、ProjectPage 显式声明 supports_comments = True。
    supports_comments = False
    allow_comments = True  # 向后兼容属性

    # 4. 声明式作者创作与子节点允许策略（零单页面硬编码）
    # - author_creatable: 是否允许受限作者（仅 add+publish，无 change）创建/编辑/发布/删除本人页面
    # - author_allow_child_creation: 是否允许受限作者在该页面下创建 author_creatable=True 的子页面
    author_creatable = False
    author_allow_child_creation = False

    @classmethod
    def get_edit_handler(cls):
        handler = super().get_edit_handler()
        if getattr(cls, "supports_comments", False):
            tab_names = [getattr(c, "heading", "") for c in getattr(handler, "children", [])]
            if "💬 评论与互动" not in tab_names:
                try:
                    from comments.panels import PageCommentGovernancePanel
                    from wagtail.admin.panels import ObjectList, TabbedInterface

                    comment_tab = ObjectList([PageCommentGovernancePanel()], heading="💬 评论与互动")
                    children = list(handler.children) + [comment_tab]
                    model_form_class = getattr(cls, "base_form_class", None)
                    handler = TabbedInterface(children, base_form_class=model_form_class).bind_to_model(cls)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception(
                        "Failed to attach PageCommentGovernancePanel to %s", cls.__name__
                    )
        return handler

    class Meta:
        abstract = True

    def permissions_for_user(self, user):
        from content_lifecycle.permissions import OwnershipPagePermissionTester
        return OwnershipPagePermissionTester(user, self)

    def collect_media_references(self) -> list:
        """
        通用媒体引用采集：
        自动扫描 featured_image、gallery_images 与所有 StreamField 积木提取 MediaReference。
        """
        from wagtail.fields import StreamField
        from content_blocks.contracts import MediaReference

        refs: list[MediaReference] = []
        featured_img = getattr(self, "featured_image", None)
        if featured_img is not None and getattr(featured_img, "public_id", None):
            refs.append(
                MediaReference(
                    asset_id=str(featured_img.public_id),
                    asset_type="image",
                    block_path="featured_image",
                    extra={"field_name": "featured_image"},
                )
            )

        if hasattr(self, "gallery_images"):
            try:
                for idx, item in enumerate(self.gallery_images.all()):
                    img = getattr(item, "image", None)
                    if img is not None and getattr(img, "public_id", None):
                        refs.append(
                            MediaReference(
                                asset_id=str(img.public_id),
                                asset_type="image",
                                block_path=f"gallery_images.{idx}",
                                extra={"field_name": "gallery_images"},
                            )
                        )
            except Exception:
                pass

        for field in self._meta.get_fields():
            if isinstance(field, StreamField):
                stream_val = getattr(self, field.name, None)
                if not stream_val:
                    continue
                for idx, child in enumerate(stream_val):
                    block = getattr(child, "block", None)
                    if block and hasattr(block, "extract_media_refs"):
                        extracted = block.extract_media_refs(child.value, path=f"{field.name}.{idx}")
                        for ref in extracted:
                            extra = dict(ref.extra) if getattr(ref, "extra", None) else {}
                            extra.setdefault("field_name", field.name)
                            refs.append(
                                MediaReference(
                                    asset_id=ref.asset_id,
                                    asset_type=ref.asset_type,
                                    block_path=ref.block_path,
                                    extra=extra,
                                )
                            )
        return refs

    def register_revision_media_references(self, revision) -> None:
        """在 Revision 生成时统一登记 MEDIA-REF-01 媒体引用记录。"""
        if not self.id or not getattr(revision, "id", None):
            return
        from media_library.references import MediaReferenceTracker

        MediaReferenceTracker.register_revision_references(
            page_id=self.id,
            revision_id=revision.id,
            references=self.collect_media_references(),
        )

    def prune_unpublished_draft_revisions(self, current_revision=None, keep: int = 20) -> None:
        """滑动窗口修剪从未发布过页面的多余草稿版本（白名单保护曾发布与排期页面）。"""
        if not self.id:
            return
        was_ever_published = bool(
            self.first_published_at
            or (self.live and getattr(self, "live_revision_id", None))
            or getattr(self, "approved_go_live_at", None)
        )
        if not was_ever_published:
            from django.db.models import Q
            was_ever_published = Page.objects.filter(id=self.id).filter(
                Q(first_published_at__isnull=False) | Q(live=True)
            ).exists()
        if not was_ever_published:
            from content_lifecycle.models import RecycleEntry
            if RecycleEntry.objects.filter(page_id=self.id, was_live=True).exists():
                was_ever_published = True
        if was_ever_published:
            return

        from wagtail.models import Revision
        from media_library.models import MediaReferenceRecord

        draft_revs = list(Revision.objects.for_instance(self).order_by("-created_at", "-id"))
        if len(draft_revs) <= keep:
            return
        current_id = getattr(current_revision, "id", None)
        for old_rev in draft_revs[keep:]:
            if old_rev.id != current_id and getattr(old_rev, "approved_go_live_at", None) is None:
                MediaReferenceRecord.objects.filter(page_id=self.id, revision_id=old_rev.id).delete()
                old_rev.delete()

    def validate_for_publish(self, user=None, revision=None) -> None:
        """
        全站统一发布前门禁校验（供 CustomBasePage.publish 与 before_publish_page 钩子共同调用）：
        1. 自身或祖先处于回收站则拒绝发布；
        2. 若指定了操作人，重查用户激活状态与对象级发布权限；
        3. 核验所引用的受限媒体：严禁发布引用了他人名下仍为 RESTRICTED 媒体的页面。
        """
        from django.core.exceptions import ValidationError

        if self.is_recycled:
            raise PermissionDenied(f"页面 '{self.title}' 当前处于回收站中，禁止发布！")
        ancestor_ids = list(self.get_ancestors().values_list("id", flat=True))
        if ancestor_ids:
            from content_lifecycle.models import RecycleEntry
            if RecycleEntry.objects.filter(page_id__in=ancestor_ids, status__in=["recycled", "restoring"]).exists():
                raise PermissionDenied("父级页面或祖先页面处于回收站中，禁止发布子页面！")
        for ancestor in self.get_ancestors():
            if getattr(ancestor, "is_recycled", False):
                raise PermissionDenied(f"父级页面 '{ancestor.title}' 处于回收站中，禁止发布子页面！")

        from django.contrib.auth import get_user_model
        UserModel = get_user_model()
        if user is not None:
            fresh_user = UserModel.objects.filter(pk=user.pk).first() if getattr(user, "pk", None) else user
            if fresh_user is None or not getattr(fresh_user, "is_active", False):
                raise PermissionDenied("当前账号已停用，禁止发布页面。")
            if not getattr(fresh_user, "is_superuser", False):
                if not self.permissions_for_user(fresh_user).can_publish():
                    raise PermissionDenied("当前用户无权发布此页面。")
        elif revision is not None and getattr(revision, "approved_go_live_at", None) is not None:
            rev_user = getattr(revision, "user", None)
            if rev_user is not None:
                fresh_rev_user = UserModel.objects.filter(pk=rev_user.pk).first() if getattr(rev_user, "pk", None) else rev_user
                if fresh_rev_user is None or not getattr(fresh_rev_user, "is_active", False):
                    raise PermissionDenied("排期发布人账号已停用，禁止自动发布。")
                if not getattr(fresh_rev_user, "is_superuser", False) and not self.permissions_for_user(fresh_rev_user).can_publish():
                    raise PermissionDenied("排期发布人已被撤权，禁止自动发布。")

        # 核查受限媒体引用归属：避免越权通过发布将他人受限媒体暴露给公众
        from media_library.models import (
            CustomDocument,
            CustomImage,
            MediaReferenceRecord,
            VisibilityChoices,
        )

        ref_pairs: list[tuple[str, str]] = []
        rev_id = getattr(revision, "id", None)
        if self.pk and rev_id:
            db_refs = list(
                MediaReferenceRecord.objects.filter(page_id=self.pk, revision_id=rev_id)
                .values_list("asset_type", "asset_id")
            )
            ref_pairs.extend((atype, str(aid)) for atype, aid in db_refs)
        if not ref_pairs:
            for ref in self.collect_media_references():
                if ref.asset_id:
                    ref_pairs.append((ref.asset_type, str(ref.asset_id)))

        image_ids = {aid for atype, aid in ref_pairs if atype == "image"}
        doc_ids = {aid for atype, aid in ref_pairs if atype == "document"}

        def _check_restricted_assets(model_cls, raw_ids):
            if not raw_ids:
                return
            uuid_vals = []
            pk_vals = []
            for raw in raw_ids:
                try:
                    uuid_vals.append(uuid.UUID(str(raw)))
                except Exception:
                    if str(raw).isdigit():
                        pk_vals.append(int(str(raw)))
            q = models.Q()
            if uuid_vals:
                q |= models.Q(public_id__in=uuid_vals)
            if pk_vals:
                q |= models.Q(pk__in=pk_vals)
            if not q:
                return
            for asset in model_cls.objects.filter(q).filter(visibility=VisibilityChoices.RESTRICTED):
                if asset.uploaded_by_user_id != self.owner_id:
                    if not (user is not None and getattr(user, "is_superuser", False)):
                        raise ValidationError(
                            f"页面引用了非本页面作者上传的受限媒体文件（{asset.title}），禁止直接发布！"
                        )

        _check_restricted_assets(CustomImage, image_ids)
        _check_restricted_assets(CustomDocument, doc_ids)

    def save_revision(self, *args, **kwargs):
        revision = super().save_revision(*args, **kwargs)
        self.register_revision_media_references(revision)
        return revision

    def publish(self, revision=None, user=None, changed=True, log_action=True, previous_revision=None, **kwargs):
        """发布守卫：统一调用 validate_for_publish 执行回收隔离、权限与媒体归属门禁"""
        target_obj = revision.as_object() if revision else self
        target_obj.validate_for_publish(user=user, revision=revision)
        return super().publish(
            revision=revision,
            user=user,
            changed=changed,
            log_action=log_action,
            previous_revision=previous_revision,
            **kwargs,
        )

    def delete(self, *args, **kwargs):
        """删除守卫：普通业务入口禁止直接物理删除，必须通过 RecycleService 回收"""
        allow_physical_purge = kwargs.pop("allow_physical_purge", False) or is_purge_authorized()
        if not allow_physical_purge:
            raise PermissionDenied(
                f"禁止对页面 '{self.title}' (id={self.id}) 执行直接物理删除！"
                "请调用 content_lifecycle.RecycleService 移入受控回收站。"
            )
        try:
            from media_library.models import MediaReferenceRecord
            MediaReferenceRecord.objects.filter(page_id=self.id).delete()
        except Exception:
            pass
        return super().delete(*args, **kwargs)

    def copy(self, *args, **kwargs):
        """复制守卫：排除已回收子页面，并确保新副本分配全新 document_uuid"""
        exclude_fields = kwargs.get("exclude_fields") or []
        if "document_uuid" not in exclude_fields:
            exclude_fields = list(exclude_fields) + ["document_uuid"]
            kwargs["exclude_fields"] = exclude_fields
        # 默认不复制历史 Revision 链，防止新副本与源页面共享 MongoDB 指针在永久删除时产生连坐误删
        kwargs.setdefault("copy_revisions", False)
        return super().copy(*args, **kwargs)

    def create_alias(self, *args, **kwargs):
        """别名守卫：已回收页面禁止创建别名（别名新 UUID 由 full_clean/save 自动分配）"""
        if self.is_recycled:
            raise PermissionDenied(f"页面 '{self.title}' 当前处于回收站中，禁止创建别名！")
        kwargs.pop("exclude_fields", None)
        return super().create_alias(*args, **kwargs)

    def full_clean(self, *args, **kwargs):
        if self.pk is None and getattr(self, "alias_of_id", None) is not None:
            if type(self).objects.filter(document_uuid=self.document_uuid).exists():
                self.document_uuid = uuid.uuid4()
        return super().full_clean(*args, **kwargs)

    def save(self, *args, **kwargs):
        if self.pk is None and getattr(self, "alias_of_id", None) is not None:
            if not self.document_uuid or type(self).objects.filter(document_uuid=self.document_uuid).exists():
                self.document_uuid = uuid.uuid4()
        return super().save(*args, **kwargs)

    def update_aliases(self, *, revision=None, _content=None, _updated_ids=None):
        """别名同步守卫：未上线页面（如未来排期）不唤醒别名；排除处于回收站中的别名"""
        if not self.live:
            return []
        from content_lifecycle.models import RecycleEntry
        recycled_alias_ids = set(
            RecycleEntry.objects.filter(status__in=["recycled", "restoring"]).values_list("page_id", flat=True)
        )
        if _updated_ids is None:
            _updated_ids = set()
        _updated_ids.update(recycled_alias_ids)
        return super().update_aliases(revision=revision, _content=_content, _updated_ids=_updated_ids)

    def with_content_json(self, content):
        """
        Wagtail 官方反序列化入口基类守卫：
        从快照还原时强制保护实例级关键状态字段，防止历史 Revision 覆盖掉当前页面的 document_uuid 或 is_recycled。
        """
        obj = super().with_content_json(content)
        obj.document_uuid = getattr(self, "document_uuid", getattr(obj, "document_uuid", None))
        obj.is_recycled = getattr(self, "is_recycled", getattr(obj, "is_recycled", False))
        return obj

    def serve(self, request, *args, **kwargs):
        """前台交付守卫：未发布草稿（非带权预览）与已回收页面统一返回 404"""
        is_preview = getattr(request, "is_preview", False) or getattr(request, "in_preview_panel", False)
        if (not self.live) and (not is_preview):
            from django.http import Http404
            raise Http404(f"页面 '{self.title}' 尚未发布或已被下架")
        if self.is_recycled:
            from django.http import Http404
            raise Http404(f"页面 '{self.title}' 当前处于回收站中")
        return super().serve(request, *args, **kwargs)


class ContentMutationGuard(models.Model):
    """
    PostgreSQL 单行协调排他锁模型。
    所有页面生命周期、发布与媒体权限写操作首先获取该记录的行级排他锁 (SELECT FOR UPDATE)，
    配合 lock_timeout = 3s 协调后台写操作串行排队，彻底消除死锁。
    """
    name = models.CharField(max_length=64, unique=True, default="global", verbose_name="锁标识")
    last_mutated_at = models.DateTimeField(auto_now=True, verbose_name="最后加锁时间")

    class Meta:
        verbose_name = "全局写协调锁"
        verbose_name_plural = "全局写协调锁"


class AliasPublicationBinding(models.Model):
    """
    别名页面 (Alias) 发布关联绑定表。
    记录别名页与源页面正式发布版本及发布序号的精准对应关系。
    """
    alias_page_id = models.IntegerField(unique=True, db_index=True, verbose_name="别名页面ID")
    source_page_id = models.IntegerField(db_index=True, verbose_name="源页面ID")
    source_revision_id = models.IntegerField(verbose_name="绑定的源发布Revision ID")
    source_document_uuid = models.UUIDField(verbose_name="绑定的源文档UUID")
    publication_generation = models.BigIntegerField(default=1, verbose_name="发布序号代次")
    synced_at = models.DateTimeField(auto_now=True, verbose_name="同步时间")

    class Meta:
        verbose_name = "别名发布绑定"
        verbose_name_plural = "别名发布绑定"


class RecycleBatch(models.Model):
    """
    回收操作批次记录表。
    当删除一个单页或整棵子树时，作为一个原子批次记录操作人、回收时间与批次状态。
    """
    batch_id = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True, verbose_name="批次ID")
    actor_id = models.IntegerField(null=True, blank=True, verbose_name="操作人ID")
    actor_username = models.CharField(max_length=150, blank=True, verbose_name="操作人账号")
    recycled_at = models.DateTimeField(auto_now_add=True, verbose_name="回收时间")
    root_page_id = models.IntegerField(db_index=True, verbose_name="回收子树根页面ID")
    root_page_title = models.CharField(max_length=255, verbose_name="根页面标题")
    total_pages = models.IntegerField(default=1, verbose_name="包含页面总数")
    status = models.CharField(
        max_length=16,
        choices=[("recycled", "已回收"), ("restored", "已恢复"), ("purged", "已永久删除")],
        default="recycled",
        db_index=True,
        verbose_name="批次状态",
    )

    class Meta:
        verbose_name = "回收批次"
        verbose_name_plural = "回收批次"
        ordering = ["-recycled_at"]

    def actions(self):
        """
        状态摘要兜底方法（管理列表真实操作按钮已由感知 request.user 的 RecycleBatchActionsColumn 渲染，
        防止对非超管渲染“彻底销毁”链接）。
        """
        from django.utils.safestring import mark_safe
        if self.status == "recycled":
            return mark_safe('<span style="color:#d97706;font-weight:600;">已回收待处理</span>')
        elif self.status == "restored":
            return mark_safe('<span style="color:#16a34a;font-weight:600;">已恢复为草稿</span>')
        else:
            return mark_safe('<span style="color:#94a3b8;">已彻底销毁</span>')

    actions.short_description = "回收站操作"

    def retention_status(self):
        """
        【30 天保留期倒计时计算与动态视觉预警】

        【教学点与变量说明】：
        - elapsed_days (int): 已在回收站中滞留的天数（当前时区感知时间 timezone.now() 减去回收时间 self.recycled_at 的天数）；
        - remaining_days (int): 距离 30 天最终物理硬删除还剩下的天数，通过 max(0, 30 - elapsed_days) 保证不出现负数；
        - 临界保护逻辑：当剩余天数 <= 3 天时，动态注入内联红色加粗警示标签，提醒管理员数据即将永久灭失。
        """
        if self.status != "recycled":
            return "-"
        from django.utils import timezone
        from django.utils.safestring import mark_safe
        elapsed_days = (timezone.now() - self.recycled_at).days
        remaining_days = max(0, 30 - elapsed_days)
        if remaining_days <= 3:
            return mark_safe(f'<span style="color:#dc2626;font-weight:bold;">已滞留 {elapsed_days} 天 (剩余 {remaining_days} 天)</span>')
        return f"已滞留 {elapsed_days} 天 (剩余 {remaining_days} 天)"

    retention_status.short_description = "保留倒计时(30天)"

    def __str__(self):
        return f"RecycleBatch {self.batch_id} (root: {self.root_page_title}, count: {self.total_pages})"


class RecycleEntry(models.Model):
    """
    单页面回收快照记录表 (RecycleEntry)。
    记录被回收页面当时的父节点、原兄弟顺序、原路径快照，为原树位置精准恢复提供数据支撑。
    """
    batch = models.ForeignKey(RecycleBatch, on_delete=models.CASCADE, related_name="entries", verbose_name="所属回收批次")
    page_id = models.IntegerField(db_index=True, verbose_name="页面ID")
    parent_id = models.IntegerField(db_index=True, verbose_name="原父页面ID")
    original_slug = models.CharField(max_length=255, verbose_name="原Slug")
    original_path = models.CharField(max_length=255, verbose_name="原树路径path")
    original_depth = models.IntegerField(verbose_name="原树深度depth")
    original_url_path = models.TextField(verbose_name="原URL路径")
    sibling_order = models.IntegerField(default=0, verbose_name="原同级顺序")
    was_live = models.BooleanField(default=False, verbose_name="回收前是否已发布")
    had_unpublished_changes = models.BooleanField(default=False, verbose_name="回收前是否有未发布草稿")
    snapshot_revision_id = models.IntegerField(null=True, blank=True, verbose_name="回收快照Revision ID")
    is_batch_root = models.BooleanField(default=False, verbose_name="是否为批次根节点")
    status = models.CharField(
        max_length=16,
        choices=[("recycled", "已回收"), ("restoring", "恢复中"), ("active", "正常活动")],
        default="recycled",
        db_index=True,
        verbose_name="条目状态",
    )

    class Meta:
        verbose_name = "回收页面条目"
        verbose_name_plural = "回收页面条目"
        indexes = [
            models.Index(fields=["page_id", "status"]),
            models.Index(fields=["parent_id", "status"]),
        ]

    def __str__(self):
        return f"RecycleEntry page={self.page_id} parent={self.parent_id} slug={self.original_slug}"
