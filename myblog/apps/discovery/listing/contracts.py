"""
全站层级列表与多维检索通用框架数据合同与策略规范 (Listing Contracts)。

落实 RFC 规范与 D1~D4 决策：
1. ListingPolicy: 页面声明能力策略（类型白名单、递归开关、专属筛选器、分桶统计键等）；
2. ListingFilters: 从 HTTP 请求规范化提取的筛选与排序条件；
3. ListingViewModel: 展现层同构视图模型（支持 SSR 直出与 AJAX 局部更新）。
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class ListingPolicy:
    """页面接入全站层级列表与多维检索的能力声明策略"""
    type_keys: tuple[str, ...]                   # 允许列出的业务内容类型白名单，如 ("blog_page",)
    descendants_default: bool = True             # 默认是否递归全部后代（包含子栏目）
    descendants_toggle: bool = True              # 前台是否允许用户切换“包含子栏目”开关
    filter_keys: tuple[str, ...] = ()            # 专属筛选器注册键，如 ("guides.topic", "guides.difficulty")
    facet_keys: tuple[str, ...] = ("tags",)      # 允许展示的统计分桶键
    navigation_type_keys: tuple[str, ...] = ()   # 允许作为直接子栏目导航展示的类型白名单
    page_size: int = 15                          # 默认每页展示数量
    max_page_size: int = 50                      # 每页硬上限


@dataclass(frozen=True)
class ListingFilters:
    """从 HTTP GET 请求解析出的规范化筛选与排序条件"""
    q: str = ""
    title_only: bool = False
    include_descendants: bool = True
    date_from: date | None = None
    date_to: date | None = None
    sort: str = "authored_desc"                  # "authored_desc" | "authored_asc" | "relevance"
    cursor: str | None = None
    navigation_cursor: str | None = None
    page_size: int = 15
    extra_filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ListingViewModel:
    """提供给模板渲染或 AJAX 序列化的统一视图模型数据结构"""
    items: list[Any]
    total_count: int
    facets: dict[str, list[dict[str, Any]]]
    next_cursor: str | None = None
    previous_cursor: str | None = None
    navigation_items: list[dict[str, Any]] = field(default_factory=list)
    navigation_total_count: int = 0
    canonical_url: str = ""
    active_filters: dict[str, Any] = field(default_factory=dict)
    query_string: str = ""
    is_empty: bool = False
    http_status: int = 200
    error_message: str | None = None
    cursor_reset: bool = False
    is_preview: bool = False
    corpus_epoch: int = 1
