"""
全站内容发现与发布副本数据模型 (PublishedSearchDocument & PublishedSearchChunk)。

落实第一阶段统一搜索架构与契约：
1. ContentDiscoveryEpoch: 全站发现版本号，发布/下架/回收时原子自增，驱动游标失效 (RECYCLE-DISCOVERY-02)；
2. PublishedSearchDocument: 正式发布页面的搜索副本元数据，仅索引当前有效发布版本，草稿隔离；
3. PublishedSearchChunk (SEARCH-CHUNK-02):
   - 仅在归一化原文列 normalized_text 上保留相邻分块间 256 字符重叠供模式 A 原句匹配；
   - 全文检索列 search_vector 单列不重叠分块切分，挂载 PostgreSQL GinIndex，节省 50% 索引空间且不重复计分；
4. PublishedSearchTag: 关联当前正式发布的标签集合。
"""

import uuid
from django.db import models, transaction
from django.db.models import Q
from django.contrib.postgres.search import SearchVectorField
from django.contrib.postgres.indexes import GinIndex

from people.author_identity import CANONICAL_AUTHOR_ROLES


class ContentDiscoveryEpoch(models.Model):
    """全站内容发现版本号表：任何影响前台检索与统计的写操作均递增此版本"""
    name = models.CharField(max_length=64, unique=True, default="global", verbose_name="作用域")
    epoch = models.BigIntegerField(default=1, verbose_name="当前Epoch版本号")
    last_mutated_at = models.DateTimeField(auto_now=True, verbose_name="最后更新时间")

    class Meta:
        verbose_name = "内容发现Epoch"
        verbose_name_plural = "内容发现Epoch"

    @classmethod
    def get_current_epoch(cls, name: str = "global") -> int:
        """获取指定作用域的当前版本号"""
        obj, _ = cls.objects.get_or_create(name=name, defaults={"epoch": 1})
        return obj.epoch

    @classmethod
    def get_current_epoch_readonly(cls, name: str = "global") -> int:
        """在只读事务内安全读取当前版本号，绝不触发写操作"""
        val = cls.objects.filter(name=name).values_list("epoch", flat=True).first()
        return val if val is not None else 1

    @classmethod
    def increment_epoch(cls, name: str = "global") -> int:
        """原子递增版本号并返回新版本（确保首次递增至少从默认 1 递增到 2）"""
        with transaction.atomic():
            obj, created = cls.objects.select_for_update().get_or_create(name=name, defaults={"epoch": 2})
            if not created:
                obj.epoch += 1
                obj.save(update_fields=["epoch", "last_mutated_at"])
            return obj.epoch

    def __str__(self):
        return f"DiscoveryEpoch({self.name}={self.epoch})"


class PublishedSearchDocument(models.Model):
    """正式发布页面的搜索只读副本模型"""
    page_id = models.IntegerField(db_index=True, verbose_name="关联页面ID")
    document_uuid = models.UUIDField(db_index=True, verbose_name="业务文档唯一标识")
    effective_revision_id = models.IntegerField(verbose_name="生效Revision ID")
    publication_generation = models.BigIntegerField(default=1, verbose_name="发布代次序号")
    type_key = models.CharField(max_length=64, db_index=True, verbose_name="页面类型标识")
    title = models.CharField(max_length=255, verbose_name="页面标题")
    url_path = models.TextField(verbose_name="URL路径")
    authored_on = models.DateField(db_index=True, verbose_name="成文日期")
    summary = models.TextField(blank=True, verbose_name="内容摘要")
    is_live = models.BooleanField(default=True, db_index=True, verbose_name="是否已正式发布")
    is_recycled = models.BooleanField(default=False, db_index=True, verbose_name="是否已移入回收站")
    site_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="站点ID")
    root_path = models.CharField(max_length=255, db_index=True, verbose_name="树路径path")
    depth = models.IntegerField(default=1, verbose_name="树深度depth")
    index_generation = models.IntegerField(default=1, verbose_name="索引代次")
    synced_at = models.DateTimeField(auto_now=True, verbose_name="同步时间")
    extra_data = models.JSONField(default=dict, blank=True, verbose_name="额外元数据")

    class Meta:
        verbose_name = "发布搜索文档"
        verbose_name_plural = "发布搜索文档"
        constraints = [
            models.UniqueConstraint(fields=["page_id", "index_generation"], name="unique_page_index_gen"),
        ]
        indexes = [
            models.Index(fields=["type_key", "authored_on", "page_id"], name="disc_doc_type_date_idx"),
            models.Index(fields=["is_live", "is_recycled", "authored_on"], name="disc_doc_live_date_idx"),
            models.Index(fields=["root_path", "depth"], name="disc_doc_tree_path_idx"),
            models.Index(
                fields=["site_id", "-authored_on", "-page_id"],
                name="disc_doc_live_site_date_idx",
                condition=Q(is_live=True, is_recycled=False, index_generation=1),
            ),
            models.Index(
                fields=["site_id", "type_key", "-authored_on", "-page_id"],
                name="disc_doc_live_type_date_idx",
                condition=Q(is_live=True, is_recycled=False, index_generation=1),
            ),
        ]

    def __str__(self):
        return f"PublishedDoc page={self.page_id} title='{self.title}' type={self.type_key}"


class PublishedSearchChunk(models.Model):
    """
    搜索文本分块表 (SEARCH-CHUNK-02)。
    1. normalized_text 保留 256 字符重叠供模式 A 原句匹配；
    2. search_vector 严格按不重叠分块切分，挂载 GIN 索引，防止重复计分并节省 50% 空间。
    """
    document = models.ForeignKey(
        PublishedSearchDocument,
        on_delete=models.CASCADE,
        related_name="chunks",
        verbose_name="所属搜索文档",
    )
    field_name = models.CharField(max_length=32, db_index=True, verbose_name="字段名称")  # 'title' | 'intro' | 'body'
    chunk_index = models.IntegerField(default=0, verbose_name="分块序号")
    normalized_text = models.TextField(verbose_name="归一化原文（含256重叠）")
    search_vector = SearchVectorField(null=True, blank=True, verbose_name="全文检索词向量（不重叠）")
    char_start = models.IntegerField(default=0, verbose_name="起始字符偏移")
    char_end = models.IntegerField(default=0, verbose_name="结束字符偏移")

    class Meta:
        verbose_name = "搜索文本分块"
        verbose_name_plural = "搜索文本分块"
        indexes = [
            GinIndex(fields=["search_vector"], name="disc_chunk_vec_gin"),
            models.Index(fields=["document", "field_name", "chunk_index"], name="disc_chunk_doc_field_idx"),
        ]

    def __str__(self):
        return f"Chunk doc={self.document_id} field={self.field_name} chunk={self.chunk_index}"


class PublishedSearchTag(models.Model):
    """搜索文档与标签的关联只读关系"""
    document = models.ForeignKey(
        PublishedSearchDocument,
        on_delete=models.CASCADE,
        related_name="search_tags",
        verbose_name="所属搜索文档",
    )
    tag_id = models.IntegerField(db_index=True, verbose_name="标签ID")
    tag_name = models.CharField(max_length=100, verbose_name="标签名称")
    tag_slug = models.CharField(max_length=100, verbose_name="标签Slug")

    class Meta:
        verbose_name = "搜索文档标签"
        verbose_name_plural = "搜索文档标签"
        constraints = [
            models.UniqueConstraint(fields=["document", "tag_id"], name="unique_doc_tag"),
        ]
        indexes = [
            models.Index(fields=["tag_id", "document"], name="disc_tag_doc_idx"),
        ]

    def __str__(self):
        return f"SearchTag doc={self.document_id} tag={self.tag_name}(id={self.tag_id})"


class PublishedSearchAuthor(models.Model):
    """搜索文档与规范作者的归一化索引关系（支撑作者专栏、按角色筛选与全站作者分面）"""
    document = models.ForeignKey(
        PublishedSearchDocument,
        on_delete=models.CASCADE,
        related_name="search_authors",
        verbose_name="所属搜索文档",
    )
    person_id = models.BigIntegerField(db_index=True, verbose_name="公开人物主键")
    author_name = models.CharField(max_length=120, verbose_name="发布时作者姓名快照")
    author_slug = models.CharField(max_length=96, db_index=True, verbose_name="规范作者Slug")
    role = models.CharField(max_length=32, db_index=True, verbose_name="归一化贡献角色")
    sort_order = models.SmallIntegerField(default=0, verbose_name="署名排序")

    class Meta:
        verbose_name = "搜索文档作者"
        verbose_name_plural = "搜索文档作者"
        constraints = [
            models.UniqueConstraint(fields=["document", "author_slug"], name="uniq_disc_doc_author_slug"),
            models.CheckConstraint(
                condition=Q(role__in=CANONICAL_AUTHOR_ROLES),
                name="disc_author_role_enum_chk",
            ),
            models.CheckConstraint(
                condition=Q(sort_order__gte=0),
                name="disc_author_sort_nonneg_chk",
            ),
            models.CheckConstraint(
                condition=(
                    Q(role="anonymous", author_slug="anonymous")
                    | (~Q(role="anonymous") & ~Q(author_slug="anonymous"))
                ),
                name="disc_author_anon_pair_chk",
            ),
        ]
        indexes = [
            models.Index(fields=["author_slug", "document"], name="disc_author_slug_doc_idx"),
            models.Index(fields=["author_slug", "role", "document"], name="disc_author_role_doc_idx"),
        ]

    def __str__(self):
        return f"SearchAuthor doc={self.document_id} slug={self.author_slug} role={self.role}"


class ArchiveCountState(models.Model):
    """日历归档预聚合汇总表的当前读指针与脏状态标记"""
    name = models.CharField(max_length=64, unique=True, default="global", verbose_name="作用域")
    active_generation = models.BigIntegerField(default=1, verbose_name="当前生效汇总代次")
    is_stale = models.BooleanField(default=False, verbose_name="是否正在对账刷新")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "归档汇总代次状态"
        verbose_name_plural = "归档汇总代次状态"

    @classmethod
    def get_active_generation_readonly(cls, name: str = "global") -> tuple[int, bool]:
        row = cls.objects.filter(name=name).values_list("active_generation", "is_stale").first()
        if row is None:
            return 1, False
        return int(row[0]), bool(row[1])


class ArchiveDailyCount(models.Model):
    """按自然日(authored_on)预聚合的公开文章计数表，打开月历最多只读当月 31 行"""
    generation = models.BigIntegerField(default=1, db_index=True, verbose_name="汇总代次")
    site_id = models.IntegerField(default=0, verbose_name="站点ID(0=未绑定站点)")
    type_key = models.CharField(max_length=64, verbose_name="页面类型标识")
    bucket_date = models.DateField(verbose_name="成文日期日桶")
    article_count = models.BigIntegerField(default=0, verbose_name="公开文章数")

    class Meta:
        verbose_name = "按日归档计数"
        verbose_name_plural = "按日归档计数"
        constraints = [
            models.UniqueConstraint(
                fields=["generation", "site_id", "type_key", "bucket_date"],
                name="uniq_arch_daily_gen_site_type_date",
            ),
            models.CheckConstraint(
                condition=Q(article_count__gte=0),
                name="chk_arch_daily_nonneg",
            ),
        ]
        indexes = [
            models.Index(
                fields=["generation", "site_id", "bucket_date"],
                name="arch_daily_gen_site_date_idx",
            ),
        ]

    def __str__(self):
        return f"ArchiveDaily(gen={self.generation}, site={self.site_id}, type={self.type_key}, date={self.bucket_date}, count={self.article_count})"


class ArchiveContribution(models.Model):
    """单篇文章在归档日历中的当前计入凭证，用于幂等 +/-1 增量维护防重算"""
    generation = models.BigIntegerField(default=1, verbose_name="汇总代次")
    site_id = models.IntegerField(default=0, verbose_name="站点ID")
    page_id = models.IntegerField(verbose_name="页面ID")
    type_key = models.CharField(max_length=64, verbose_name="已计入类型")
    bucket_date = models.DateField(verbose_name="已计入日期桶")
    effective_revision_id = models.IntegerField(default=0, verbose_name="计入时生效Revision")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "归档计数贡献记录"
        verbose_name_plural = "归档计数贡献记录"
        constraints = [
            models.UniqueConstraint(
                fields=["generation", "site_id", "page_id"],
                name="uniq_arch_contrib_gen_site_page",
            ),
        ]


class ArchiveSyncOutbox(models.Model):
    """与业务数据同事务落库的归档汇总待处理任务表 (Transactional Outbox)"""
    event_type = models.CharField(max_length=64, verbose_name="事件类型")
    page_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="页面ID")
    root_path = models.CharField(max_length=255, blank=True, verbose_name="子树路径")
    status = models.CharField(max_length=24, default="pending", db_index=True, verbose_name="处理状态")
    attempts = models.IntegerField(default=0, verbose_name="重试次数")
    last_error = models.TextField(blank=True, verbose_name="最后错误")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    processed_at = models.DateTimeField(null=True, blank=True, verbose_name="完成时间")

    class Meta:
        verbose_name = "归档同步Outbox"
        verbose_name_plural = "归档同步Outbox"
        indexes = [
            models.Index(fields=["status", "created_at"], name="arch_outbox_status_idx"),
        ]
