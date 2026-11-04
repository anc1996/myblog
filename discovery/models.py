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
from django.contrib.postgres.search import SearchVectorField
from django.contrib.postgres.indexes import GinIndex


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
    def increment_epoch(cls, name: str = "global") -> int:
        """原子递增版本号并返回新版本"""
        with transaction.atomic():
            obj, created = cls.objects.select_for_update().get_or_create(name=name, defaults={"epoch": 1})
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
