"""
discovery.feeds
全站多页面通用订阅（RSS 2.0 / Atom 1.0）与时间线归档（Timeline Archive）核心服务。

落实架构契约与安全守卫：
1. 多页面零硬编码：基于 PublicListingCapabilities 动态读取已注册页面类型（BlogPage、GuidePage 及任意未来 xxPage）；
2. 刚性生命周期安全：排期过滤、别名排除、祖先回收防御与 PageViewRestriction 私有页面阻断；
3. RFC 4287 (Atom 1.0) / RFC 822 (RSS 2.0) 标准合规：UTC 时间戳、urn:uuid 稳定标识、XML 非法控制字符清洗、<summary type="text">；
4. 高性能条件请求：弱 ETag (W/"feed:...") + 主库极速核验 304，零 XML 与正文提取开销；
5. SARGable 时间线归档：PostgreSQL TruncMonth 聚合与左闭右开日期区间过滤。
"""

import hashlib
import html
import re
from datetime import date, datetime, time, timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.db import models
from django.db.models.functions import TruncMonth
from django.http import Http404, HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.cache import get_conditional_response
from django.utils.feedgenerator import Atom1Feed, Rss201rev2Feed
from wagtail.models import Site

from discovery.contracts import PublicListingCapabilities
from discovery.models import ContentDiscoveryEpoch, PublishedSearchDocument
from discovery.registry import (
    get_adapter_by_public_type_slug,
    get_archive_enabled_type_keys,
    get_feed_enabled_type_keys,
    get_public_type_slug_map,
    get_type_key_to_public_slug_map,
    list_type_adapters,
)
from content_lifecycle.public_access import get_public_blocked_page_ids

_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_xml_text(s: Any) -> str:
    """清洗 XML 1.0 非法控制字符，防止 RSS/Atom 解析器解析失败 (BLK-07)"""
    if not s:
        return ""
    text = str(s)
    return _XML_ILLEGAL_RE.sub("", text)


class SafeAtom1Feed(Atom1Feed):
    """
    安全合规的 Atom 1.0 生成器 (RFC 4287)：
    1. 重写 add_item_elements 将 <summary> 的 type 设为 "text"，杜绝 HTML 二次解析漏洞；
    2. 支持循环输出多个 <author><name>...</name></author> 节点；
    3. 确保顶层 author 节点存在。
    """

    def add_item_elements(self, handler, item):
        handler.addQuickElement("title", item["title"])
        handler.addQuickElement("link", "", {"href": item["link"], "rel": "alternate"})

        if item["pubdate"] is not None:
            handler.addQuickElement("published", item["pubdate"].isoformat())

        if item.get("updateddate") is not None:
            handler.addQuickElement("updated", item["updateddate"].isoformat())
        elif item["pubdate"] is not None:
            handler.addQuickElement("updated", item["pubdate"].isoformat())

        # 作者节点：支持多作者
        authors = item.get("authors") or []
        if authors:
            for author_info in authors:
                handler.startElement("author", {})
                handler.addQuickElement("name", _clean_xml_text(author_info.get("name", "")))
                if author_info.get("email"):
                    handler.addQuickElement("email", author_info["email"])
                if author_info.get("uri"):
                    handler.addQuickElement("uri", author_info["uri"])
                handler.endElement("author")
        elif item.get("author_name"):
            handler.startElement("author", {})
            handler.addQuickElement("name", _clean_xml_text(item["author_name"]))
            handler.endElement("author")

        if item.get("unique_id") is not None:
            handler.addQuickElement("id", item["unique_id"])

        if item.get("description") is not None:
            # 强制标记为 type="text"
            handler.addQuickElement("summary", _clean_xml_text(item["description"]), {"type": "text"})

        # 分类标签
        for cat in item.get("categories") or []:
            handler.addQuickElement("category", "", {"term": _clean_xml_text(cat)})


class SafeRss2Feed(Rss201rev2Feed):
    """安全强化的 RSS 2.0 生成器"""

    def add_item_elements(self, handler, item):
        super().add_item_elements(handler, item)


def get_public_listing_queryset(*, site_id: int | None = None, for_feed: bool = False, for_archive: bool = False):
    """
    构建全站统一公共合法候选集 (Eligible Set)：
    1. 过滤已正式上线且未移入回收站 (is_live=True, is_recycled=False)；
    2. 严格按能力声明白名单过滤允许进入订阅/归档的页面类型 (feed_enabled / archive_enabled)；
    3. 严格排期过滤：authored_on <= site_today，杜绝未来排期泄漏；
    4. 批量执行 PageViewRestriction 权限与祖先回收阻断 (content_lifecycle.public_access)。
    """
    site_today = timezone.localdate()
    qs = PublishedSearchDocument.objects.filter(
        is_live=True,
        is_recycled=False,
        authored_on__lte=site_today,
    )

    if site_id is not None:
        qs = qs.filter(site_id=site_id)

    if for_feed:
        feed_type_keys = get_feed_enabled_type_keys()
        qs = qs.filter(type_key__in=feed_type_keys)
    elif for_archive:
        archive_type_keys = get_archive_enabled_type_keys()
        qs = qs.filter(type_key__in=archive_type_keys)

    # 批量检查并排除权限受限或祖先被回收的页面
    candidate_records = list(qs.values_list("page_id", "root_path"))
    if candidate_records:
        blocked_ids = get_public_blocked_page_ids(candidate_records)
        if blocked_ids:
            qs = qs.exclude(page_id__in=blocked_ids)

    return qs


def public_feed_view(request: HttpRequest, dimension: str = "all", slug: str | None = None) -> HttpResponse:
    """
    全站统一多页面 RSS 2.0 / Atom 1.0 订阅入口。
    支持多维度：all, type, tag, author, topic。
    支持格式：RSS 2.0 (默认), Atom 1.0 (?format=atom 或 /atom/ 路由)。
    """
    valid_dimensions = ("all", "type", "tag", "author", "topic")
    if dimension not in valid_dimensions:
        raise Http404(f"Unsupported feed dimension: '{dimension}'")

    # 解析当前站点
    site = Site.find_for_request(request)
    site_id = site.id if site else None
    site_name = site.site_name if (site and site.site_name) else "技术博客"
    site_root_url = site.root_url if site else f"http://{request.get_host()}"

    # 确定格式
    fmt = request.GET.get("format", "").lower()
    if not fmt:
        fmt = "atom" if "/atom" in request.path else "rss"
    if fmt not in ("rss", "atom"):
        fmt = "rss"

    # 1. 主库极速核验弱 ETag（零 XML 拼接开销）
    epoch = ContentDiscoveryEpoch.get_current_epoch_readonly()
    site_today = timezone.localdate().isoformat()
    slug_token = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:12] if slug else "all"
    weak_etag = f'W/"feed:v1:s{site_id or 0}:e{epoch}:{dimension}:{slug_token}:{fmt}:{site_today}"'

    # 校验条件请求
    conditional_response = get_conditional_response(request, etag=weak_etag)
    if conditional_response is not None:
        conditional_response["Cache-Control"] = "public, max-age=60"
        conditional_response["ETag"] = weak_etag
        return conditional_response

    # 2. 构造基础合法候选集 (for_feed=True)
    qs = get_public_listing_queryset(site_id=site_id, for_feed=True)

    # 3. 多维度过滤
    channel_title = f"{site_name} - 全站订阅"
    channel_description = "全站最新技术文章与专题指南"

    if dimension == "type":
        if not slug:
            raise Http404("Missing type slug")
        type_map = get_public_type_slug_map()
        if slug not in type_map:
            raise Http404(f"Unknown or non-feedable content type: '{slug}'")
        target_type_key = type_map[slug]
        adapter = get_adapter_by_public_type_slug(slug)
        if not adapter or not getattr(getattr(adapter, "capabilities", None), "feed_enabled", False):
            raise Http404(f"Content type '{slug}' does not support feeds.")
        qs = qs.filter(type_key=target_type_key)
        channel_title = f"{site_name} - {slug.capitalize()} 专栏订阅"
        channel_description = f"{slug} 类型的最新发布内容"

    elif dimension == "tag":
        if not slug:
            raise Http404("Missing tag slug")
        qs = qs.filter(search_tags__tag_slug=slug).distinct()
        channel_title = f"{site_name} - 标签: #{slug}"
        channel_description = f"带有标签 #{slug} 的最新文章"

    elif dimension == "author":
        if not slug:
            raise Http404("Missing author slug")
        # 匹配 extra_data 中的 author_slugs
        slug_clean = slug.strip().lower()
        qs = qs.filter(extra_data__author_slugs__contains=slug_clean)
        channel_title = f"{site_name} - 作者: {slug}"
        channel_description = f"作者 {slug} 撰写的技术内容"

    elif dimension == "topic":
        if not slug:
            raise Http404("Missing topic slug")
        qs = qs.filter(extra_data__topic_slug=slug)
        channel_title = f"{site_name} - 专题主题: {slug}"
        channel_description = f"专题专栏 {slug} 的全部文章"

    # 排序并取最近 50 篇
    docs = list(qs.order_by("-authored_on", "-page_id")[:50])

    # 构建 Feed 实例
    feed_cls = SafeAtom1Feed if fmt == "atom" else SafeRss2Feed
    feed = feed_cls(
        title=_clean_xml_text(channel_title),
        link=f"{site_root_url}{request.path}",
        description=_clean_xml_text(channel_description),
        language="zh-cn",
        author_name=_clean_xml_text(site_name),
    )

    # 注入条目
    for doc in docs:
        item_title = _clean_xml_text(doc.title)
        item_summary = _clean_xml_text(doc.summary)
        item_url = doc.url_path
        if not item_url.startswith("http"):
            item_url = f"{site_root_url.rstrip('/')}/{item_url.lstrip('/')}"

        # UTC 发布时间 (RFC 4287 / RFC 822)
        pub_datetime = datetime.combine(doc.authored_on, time(0, 0, 0, tzinfo=dt_timezone.utc))

        # 标签列表
        categories = [t.tag_name for t in doc.search_tags.all()]

        # 作者信息
        extra = doc.extra_data or {}
        authors = extra.get("authors") or []
        primary_author = authors[0]["name"] if authors else site_name

        feed.add_item(
            title=item_title,
            link=item_url,
            description=item_summary,
            pubdate=pub_datetime,
            unique_id=f"urn:uuid:{doc.document_uuid}",
            unique_id_is_permalink=False,
            categories=categories,
            author_name=_clean_xml_text(primary_author),
            authors=authors,
        )

    # 序列化 XML
    content_type = "application/atom+xml; charset=utf-8" if fmt == "atom" else "application/rss+xml; charset=utf-8"
    response = HttpResponse(feed.writeString("utf-8"), content_type=content_type)
    response["ETag"] = weak_etag
    response["Cache-Control"] = "public, max-age=60"
    return response


def compute_public_timeline_archives(
    *,
    site_id: int | None = None,
    type_slug: str | None = None,
    tag_slug: str | None = None,
    topic_slug: str | None = None,
    year: int | None = None,
) -> dict[str, Any]:
    """
    全站统一时间线归档聚合计算引擎：
    1. 基于合法候选集 (for_archive=True) 确保安全隔离；
    2. 使用 PostgreSQL TruncMonth 聚合统计年月分桶；
    3. 使用 SARGable 范围过滤 [start_date, next_period_start)；
    4. 输出结构化统计数据与可选年份过滤结果。
    """
    qs = get_public_listing_queryset(site_id=site_id, for_archive=True)

    # 类型过滤
    if type_slug:
        type_map = get_public_type_slug_map()
        if type_slug not in type_map:
            return {"total_count": 0, "timeline": [], "years": []}
        qs = qs.filter(type_key=type_map[type_slug])

    # 标签过滤
    if tag_slug:
        qs = qs.filter(search_tags__tag_slug=tag_slug).distinct()

    # 专题过滤
    if topic_slug:
        qs = qs.filter(extra_data__topic_slug=topic_slug)

    # 年份区间过滤 (SARGable)
    if year is not None:
        start_date = date(year, 1, 1)
        next_year_start = date(year + 1, 1, 1)
        qs = qs.filter(authored_on__gte=start_date, authored_on__lt=next_year_start)

    # PostgreSQL TruncMonth 聚合
    # 关键：显式 order_by() 清除前序排序，避免原排序列被加入 GROUP BY 破坏聚合 (BLK-04.1)
    month_groups = (
        qs.order_by()
        .annotate(month_bucket=TruncMonth("authored_on", output_field=models.DateField()))
        .values("month_bucket")
        .annotate(doc_count=models.Count("id"))
        .order_by("-month_bucket")
    )

    # 组织年月层级结构
    timeline_by_year: dict[int, dict[str, Any]] = {}
    total_count = 0

    for row in month_groups:
        b_date = row["month_bucket"]
        if not b_date:
            continue
        cnt = row["doc_count"]
        total_count += cnt
        y = b_date.year
        m = b_date.month

        if y not in timeline_by_year:
            timeline_by_year[y] = {
                "year": y,
                "count": 0,
                "months": [],
            }
        timeline_by_year[y]["count"] += cnt
        timeline_by_year[y]["months"].append({
            "month": m,
            "month_label": f"{y}年{m}月",
            "count": cnt,
            "start_date": date(y, m, 1).isoformat(),
        })

    years_list = sorted(timeline_by_year.keys(), reverse=True)
    timeline_list = [timeline_by_year[y] for y in years_list]

    return {
        "total_count": total_count,
        "years": years_list,
        "timeline": timeline_list,
    }
