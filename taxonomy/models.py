"""
全站统一分类与标签词库 (Taxonomy)。

落实核心架构原则：
1. 全站共用单一 Tag 标签词库，不建独立 Category 模型；
2. TagMergeRedirect: 记录标签合并映射与重定向关系，确保合并后旧关联与搜索投影不失效。
"""

from django.db import models
from taggit.models import Tag


class TagMergeRedirect(models.Model):
    """标签合并映射表：记录被合并的旧标签到新规范标签的重定向"""
    source_tag_id = models.IntegerField(db_index=True, verbose_name="原标签ID")
    source_name = models.CharField(max_length=100, db_index=True, verbose_name="原标签名称")
    target_tag = models.ForeignKey(
        Tag,
        on_delete=models.CASCADE,
        related_name="merged_from_records",
        verbose_name="合并后目标标签",
    )
    merged_at = models.DateTimeField(auto_now_add=True, verbose_name="合并时间")
    merged_by = models.CharField(max_length=150, blank=True, verbose_name="操作人")

    class Meta:
        verbose_name = "标签合并重定向"
        verbose_name_plural = "标签合并重定向"
        ordering = ["-merged_at"]

    def __str__(self):
        return f"Tag '{self.source_name}' -> '{self.target_tag.name}'"
