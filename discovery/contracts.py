"""
全站内容发现（ContentQuery）公共服务协议与数据合同。

所有内容发现入口（全站搜索、Tag 页、栏目后代、年月归档、类型列表）统一遵循本协议：
1. 输入通过 QuerySpec 统一表达过滤条件、搜索模式与排序规则；
2. 所有 Page 类型通过 TypeAdapter 注册协议接入，公共模块绝不反向依赖具体的 BlogPage / GuidePage 业务模型；
3. 输出通过 QueryResult 统一返回同快照下的列表、精确总数与分桶统计；
4. 支持 SEARCH-MODE-02 规则：模式 A 命中少于 5 条时提供 manual expansion 按钮；
5. 支持 RECYCLE-DISCOVERY-02 规则：游标失效重置标识。

本模块为纯接口契约与数据结构，导入时绝对无外部数据库或网络 IO。
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class TextSegment:
    """单个逻辑文本段数据结构（标题、简介或正文）"""
    field_name: str  # 'title' | 'intro' | 'body'
    text: str
    char_offset: int = 0


@dataclass(frozen=True)
class PublishedPayload:
    """页面类型适配器从有效发布快照中提取的统一发布载荷"""
    page_id: int
    document_uuid: str
    type_key: str
    title: str
    url_path: str
    authored_on: date
    summary: str
    text_segments: list[TextSegment] = field(default_factory=list)
    tag_ids: list[int] = field(default_factory=list)
    tags_data: list[dict[str, Any]] = field(default_factory=list)
    extra_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QuerySpec:
    """公共内容查询参数规范"""
    q: str | None = None
    type_keys: list[str] = field(default_factory=list)
    site_id: int | None = None
    root_page_id: int | None = None
    tree_scope: str = "descendants"  # 'descendants' | 'children'
    include_root: bool = False
    page_id: int | None = None
    tag_ids: list[int] = field(default_factory=list)
    tag_mode: str = "ANY"  # 'ANY' | 'ALL'
    year: int | None = None
    month: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    sort: str = "relevance"  # 'relevance' | 'authored_desc' | 'authored_asc'
    expand: str = "auto"     # 'auto' | 'off' | 'on' (控制模式 C)
    secondary: str = "none"  # 'none' | 'words' (SEARCH-MODE-02: 手动展开模式 B)
    facets: list[str] = field(default_factory=list)
    page_size: int = 20
    cursor: str | None = None
    extra_filters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchResultItem:
    """单条内容展示结构"""
    page_id: int
    effective_revision_id: int
    type_key: str
    title: str
    url: str
    authored_on: date
    summary: str
    highlights: list[dict[str, Any]] = field(default_factory=list)
    match_mode: str = "FILTER"  # 'A' | 'B' | 'C' | 'FILTER'
    score_key: int = 0
    tags: list[dict[str, Any]] = field(default_factory=list)
    column_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryResult:
    """公共查询服务返回结果结构"""
    items: list[SearchResultItem]
    total_count: int
    facets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    archive_groups: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None
    match_policy: str = "default"
    expansion_used: bool = False
    secondary_used: bool = False
    corpus_epoch: int = 1
    cursor_reset: bool = False  # RECYCLE-DISCOVERY-02: 游标失效自动重置首页轻提示


@runtime_checkable
class TypeAdapterContract(Protocol):
    """业务 Page 类型接入公共内容发现的适配器协议"""
    type_key: str
    model_label: str
    schema_version: int
    extractor_version: int
    allowed_filters: list[str]
    weight_profile: dict[str, float]

    def extract(self, published_snapshot: Any) -> Any:
        """从已核验的有效发布版本快照中提取统一的发布载荷"""
        ...
