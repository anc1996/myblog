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

import uuid
from django.db import models
from django.core.exceptions import PermissionDenied
from wagtail.models import Page


class CustomBasePage(Page):
    document_uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        verbose_name="业务文档唯一标识",
        help_text="跨 PostgreSQL、MongoDB 与媒体引用追踪使用的稳定不可变文档身份",
    )
    is_recycled = models.BooleanField(
        default=False,
        editable=False,
        db_index=True,
        verbose_name="是否已移入回收站",
        help_text="原树占位标记；已回收页面不可发布、对公网不可见且在管理后台普通目录隐藏",
    )

    class Meta:
        abstract = True

    def publish(self, revision=None, user=None, changed=True, log_action=True, previous_revision=None, **kwargs):
        """发布守卫：自身或任一祖先处于回收状态时坚决拒绝发布"""
        if self.is_recycled:
            raise PermissionDenied(f"页面 '{self.title}' 当前处于回收站中，禁止发布！")
        for ancestor in self.get_ancestors():
            if getattr(ancestor, "is_recycled", False):
                raise PermissionDenied(f"父级页面 '{ancestor.title}' 处于回收站中，禁止发布子页面！")
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
        allow_physical_purge = kwargs.pop("allow_physical_purge", False)
        if not allow_physical_purge:
            raise PermissionDenied(
                f"禁止对页面 '{self.title}' (id={self.id}) 执行直接物理删除！"
                "请调用 content_lifecycle.RecycleService 移入受控回收站。"
            )
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
