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
                    pass
        return handler

    class Meta:
        abstract = True

    def publish(self, revision=None, user=None, changed=True, log_action=True, previous_revision=None, **kwargs):
        """发布守卫：自身或任一祖先处于回收状态时坚决拒绝发布"""
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
        """别名守卫：已回收页面禁止创建别名，且为别名排除 document_uuid 避免唯一约束冲突"""
        if self.is_recycled:
            raise PermissionDenied(f"页面 '{self.title}' 当前处于回收站中，禁止创建别名！")
        exclude_fields = kwargs.get("exclude_fields") or []
        if "document_uuid" not in exclude_fields:
            exclude_fields = list(exclude_fields) + ["document_uuid"]
            kwargs["exclude_fields"] = exclude_fields
        return super().create_alias(*args, **kwargs)

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
        【Wagtail 管理后台操作列动态按钮生成】

        【教学点】：
        在 Wagtail Snippet/ModelAdmin 列表页中，通过在模型中定义方法并指定 short_description，
        可以直接为每一行动态渲染交互按钮。

        【状态分支与变量说明】：
        - restore_url (str): 解析一键恢复视图的 URL（路由指向 recycle_bin:restore，带当前批次主键 self.pk）；
        - purge_url (str): 解析彻底销毁视图的 URL（路由指向 recycle_bin:purge，带当前批次主键 self.pk）；
        - mark_safe: Django 安全模块，将拼接的 HTML 标记为安全，避免被模板引擎转义为纯文本。
        """
        from django.utils.safestring import mark_safe
        from django.urls import reverse
        if self.status == "recycled":
            try:
                restore_url = reverse("recycle_bin:restore", args=[self.pk])
                purge_url = reverse("recycle_bin:purge", args=[self.pk])
                return mark_safe(
                    f'<a href="{restore_url}" class="button button-small button-secondary" style="margin-right:8px;" onclick="return confirm(\'确认将该页面/子树原位恢复为未发布草稿？\')">一键恢复</a>'
                    f'<a href="{purge_url}" class="button button-small button-danger" style="background:#dc2626;color:#fff;" onclick="return confirm(\'警告：永久删除将彻底物理销毁该页面及 MongoDB 中的全部历史版本，不可撤销！确认删除？\')">彻底销毁</a>'
                )
            except Exception:
                return "待操作"
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
