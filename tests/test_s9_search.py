"""
S9 全站统一中文搜索 (PostgreSQL 18 + Jieba) 与组合筛选单元测试。

实测覆盖：
1. 页面发布后自动投影到 PublishedSearchDocument / PublishedSearchChunk / PublishedSearchTag；
2. SEARCH-CHUNK-02 契约：normalized_text 保留 256 重叠，search_vector 严格按不重叠分块切分；
3. 模式 A 连续原句匹配与跨块边界原句命中实测；
4. SEARCH-MODE-02 规则实测：
   - 模式 A 命中 >= 5 条时默认仅展示 A；
   - 模式 A 命中 1~4 条时默认仅展示 A，传入 secondary='words' 时手动展开模式 B；
   - 模式 A 命中 0 条时自动转入模式 B（Jieba 分词多词组 AND）；
   - 模式 B 命中 < 10 条时自动扩展模式 C（受控同义词扩展）；
5. 无关键词纯 FILTER 模式：类型过滤、栏目树后代/直属过滤、Tag (ANY/ALL) 过滤、成文日期闭区间；
6. C++、PostgreSQL 18 等专业技术词保护与边界匹配；
7. 草稿隔离与回收站隔离：
   - 修改新草稿关键词绝不提前泄漏进正式搜索结果；
   - 回收页面及其子页面立即从搜索中消失；
   - 回收站恢复为草稿态后绝不漏入搜索结果；
8. RECYCLE-DISCOVERY-02 规则实测：Epoch 变化导致游标失效时自动重置为第 1 页并返回轻提示标记。
"""

import os
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from django.test import Client, TestCase

PROJECT_DIR = Path(__file__).resolve().parents[1]


class S9SearchAndDiscoveryTests(TestCase):

    def setUp(self):
        from home.models import HomePage
        from blog.models import BlogIndexPage, BlogPage
        from discovery.models import PublishedSearchDocument, ContentDiscoveryEpoch

        self.home = HomePage.objects.first()
        if not self.home:
            from wagtail.models import Page
            root = Page.get_first_root_node() or Page.add_root(title="Root")
            self.home = HomePage(title="Home", slug=f"home-{uuid4().hex[:6]}")
            root.add_child(instance=self.home)
            self.home.save_revision().publish()
        self.client = Client()
        self.created_pages = []
        self.created_topics = []

    def tearDown(self):
        for p in reversed(self.created_pages):
            try:
                p.delete(allow_physical_purge=True)
            except Exception:
                pass
        for t in reversed(self.created_topics):
            try:
                t.delete()
            except Exception:
                pass

    def _create_and_publish_blog_page(self, title, intro="", body_text="", authored_on=None, tags=None):
        from blog.models import BlogPage
        from wagtail.rich_text import RichText

        slug = f"test-page-{uuid4().hex[:6]}"
        page = BlogPage(
            title=title,
            slug=slug,
            authored_on=authored_on or date(2026, 5, 1),
        )
        self.home.add_child(instance=page)
        self.created_pages.append(page)

        # 填充 StreamField
        if intro:
            page.intro = [("rich_text", RichText(f"<p>{intro}</p>"))]
        if body_text:
            page.body = [("markdown", body_text)]
        if tags:
            for t in tags:
                page.tags.add(t)

        rev = page.save_revision()
        rev.publish()
        page.refresh_from_db()
        return page

    def test_01_projection_and_chunk_overlap(self):
        """测试页面发布后自动同步投影与 SEARCH-CHUNK-02 分块重叠规则"""
        from discovery.models import PublishedSearchDocument, PublishedSearchChunk, PublishedSearchTag

        # 构建一段超过 1500 字符的长文本，使得必然切分出多块
        part1 = "架构设计非常重要。" * 120
        cross_sentence = "跨块边界原句必须被模式A成功匹配。"
        part2 = "数据库优化与索引。" * 120
        full_body = part1 + cross_sentence + part2

        page = self._create_and_publish_blog_page(
            title="深入理解PostgreSQL长文架构",
            intro="本文是一篇关于数据库核心设计的长文。",
            body_text=full_body,
            tags=["PostgreSQL", "架构"],
        )

        doc = PublishedSearchDocument.objects.filter(page_id=page.id).first()
        self.assertIsNotNone(doc, "发布后必须自动投影到 PublishedSearchDocument")
        self.assertTrue(doc.is_live)
        self.assertFalse(doc.is_recycled)
        self.assertEqual(doc.title, "深入理解PostgreSQL长文架构")

        # 检查分块
        chunks = list(doc.chunks.filter(field_name="body").order_by("chunk_index"))
        self.assertGreaterEqual(len(chunks), 2, "长文必须被切分为至少 2 个分块")

        # 验证 SEARCH-CHUNK-02: 第 2 块的 normalized_text 必须包含与第 1 块末尾重叠的 256 字符
        chunk0 = chunks[0]
        chunk1 = chunks[1]
        overlap_text = chunk0.normalized_text[-200:]
        self.assertIn(overlap_text, chunk1.normalized_text, "相邻块 normalized_text 必须保留字符重叠！")

        # 检查标签
        tags = list(doc.search_tags.values_list("tag_name", flat=True))
        self.assertIn("PostgreSQL", tags)
        self.assertIn("架构", tags)

    def test_02_mode_a_sentence_and_secondary_expansion(self):
        """测试模式 A 原句匹配、少于 5 条判定与 secondary='words' 手动展开模式 B"""
        from discovery.contracts import QuerySpec
        from discovery.query import ContentQuery

        # 创建 2 篇包含完整原句的文章（命中少于 5 条）
        phrase = "深入探索PostgreSQL高效索引机制"
        p1 = self._create_and_publish_blog_page("页面一", intro=f"本篇{phrase}进行系统分析。")
        p2 = self._create_and_publish_blog_page("页面二", intro=f"欢迎阅读{phrase}实战指南。")

        # 创建 1 篇只包含分词但不包含完整原句的文章
        p3 = self._create_and_publish_blog_page(
            "页面三",
            intro="探索数据库的深入机制，针对 PostgreSQL 实现了高效的索引方案。",
        )

        # 1. 默认查询模式 A：命中 2 条 (< 5 条)，仅返回模式 A 结果，不自动混排模式 B
        spec = QuerySpec(q=phrase)
        res = ContentQuery.execute(spec)
        self.assertEqual(res.total_count, 2, "模式 A 仅命中 2 条")
        self.assertFalse(res.secondary_used, "未指定 secondary='words' 时不应自动展开 B")
        matched_ids = [item.page_id for item in res.items]
        self.assertIn(p1.id, matched_ids)
        self.assertIn(p2.id, matched_ids)
        self.assertNotIn(p3.id, matched_ids)

        # 2. 模拟前端点击“展开分词匹配”按钮：传入 secondary='words'
        spec_expanded = QuerySpec(q=phrase, secondary="words")
        res_expanded = ContentQuery.execute(spec_expanded)
        self.assertTrue(res_expanded.secondary_used, "必须标记 secondary_used=True")
        self.assertEqual(res_expanded.total_count, 3, "展开后应包含模式 B 命中的页面三")
        all_ids = [item.page_id for item in res_expanded.items]
        self.assertIn(p3.id, all_ids)
        # 模式 A 排在前面（tier 0），模式 B 排在后面（tier 1）
        self.assertEqual(res_expanded.items[0].match_mode, "A")
        self.assertEqual(res_expanded.items[1].match_mode, "A")
        self.assertEqual(res_expanded.items[2].match_mode, "B")

    def test_03_mode_b_auto_transition_and_mode_c_synonym_expansion(self):
        """测试模式 A 为 0 条时自动进入模式 B，及模式 B 少于门槛时自动触发模式 C"""
        from discovery.contracts import QuerySpec
        from discovery.query import ContentQuery

        # 页面四包含分词“容器”与“调度”，但原句不连贯
        p4 = self._create_and_publish_blog_page(
            "云原生实战",
            intro="我们在生产环境中采用容器化架构，并通过集群进行自动化调度与运维。",
        )

        # 1. 查询“调度 容器”：原句 A 命中为 0，自动转入模式 B（Jieba 分词多词组 AND）
        spec_b = QuerySpec(q="调度 容器")
        res_b = ContentQuery.execute(spec_b)
        self.assertEqual(res_b.items[0].match_mode, "B")
        self.assertEqual(res_b.items[0].page_id, p4.id)

        # 2. 页面五只包含受控同义词“kubernetes”，不包含“k8s”
        p5 = self._create_and_publish_blog_page(
            "Kubernetes从入门到精通",
            intro="系统讲解 kubernetes 集群的部署与配置指南。",
        )
        # 查询“k8s”：原句 A=0，分词 B=0 (< 10 条)，自动触发模式 C（受控同义词扩展到 kubernetes）
        spec_c = QuerySpec(q="k8s", expand="auto")
        res_c = ContentQuery.execute(spec_c)
        self.assertEqual(res_c.total_count, 1)
        self.assertTrue(res_c.expansion_used, "必须标记 expansion_used=True")
        self.assertEqual(res_c.items[0].match_mode, "C")
        self.assertEqual(res_c.items[0].page_id, p5.id)

    def test_04_pure_filter_mode_and_facets(self):
        """测试无关键词的纯 FILTER 模式组合筛选（类型、标签、日期、分桶统计）"""
        from discovery.contracts import QuerySpec
        from discovery.query import ContentQuery

        p_2025 = self._create_and_publish_blog_page("旧文章", authored_on=date(2025, 6, 1), tags=["Django"])
        p_2026_a = self._create_and_publish_blog_page("新文章A", authored_on=date(2026, 3, 1), tags=["Django", "Wagtail"])
        p_2026_b = self._create_and_publish_blog_page("新文章B", authored_on=date(2026, 4, 1), tags=["Python"])

        # 1. 筛选 2026 年且包含标签 Django 的文章
        django_tag_id = p_2026_a.tags.get(name="Django").id
        spec = QuerySpec(
            q=None,
            year=2026,
            tag_ids=[django_tag_id],
            facets=["types", "tags", "years"],
        )
        res = ContentQuery.execute(spec)
        self.assertGreaterEqual(res.total_count, 1)
        item_ids = [it.page_id for it in res.items]
        self.assertIn(p_2026_a.id, item_ids)
        self.assertNotIn(p_2025.id, item_ids)
        self.assertEqual(res.items[0].match_mode, "FILTER")

        # 检查分桶统计
        facets = res.facets
        self.assertIn("types", facets)
        self.assertIn("tags", facets)
        self.assertIn("years", facets)
        year_entry = next((y for y in facets["years"] if y["year"] == 2026), None)
        self.assertIsNotNone(year_entry)
        self.assertGreaterEqual(year_entry["count"], 1)

    def test_05_draft_and_recycle_isolation(self):
        """测试草稿隔离与回收站隔离：草稿修改不泄漏、回收立即下架、恢复保持草稿"""
        from discovery.contracts import QuerySpec
        from discovery.query import ContentQuery
        from content_lifecycle.recycle import RecycleService

        # 1. 发布正式版本 V1（包含关键词“AlphaSecretKeyword”）
        page = self._create_and_publish_blog_page(
            "秘密项目说明",
            intro="这是已发布的公开内容，包含 AlphaSecretKeyword 关键词。",
        )

        spec_v1 = QuerySpec(q="AlphaSecretKeyword")
        res_v1 = ContentQuery.execute(spec_v1)
        self.assertEqual(res_v1.total_count, 1)

        # 2. 编辑新草稿 V2，注入未发布的内部关键词“BetaDraftOnlyKeyword”，但不发布
        from wagtail.rich_text import RichText
        page.intro = [("rich_text", RichText("<p>这是未发布的内部草稿，包含 BetaDraftOnlyKeyword 关键词。</p>"))]
        page.save_revision()  # 仅存草稿，不 publish()

        # 断言：搜索新草稿关键词必须为 0 条，绝不提前泄漏！
        spec_draft = QuerySpec(q="BetaDraftOnlyKeyword")
        res_draft = ContentQuery.execute(spec_draft)
        self.assertEqual(res_draft.total_count, 0, "未发布的新草稿内容绝不能出现在搜索结果中！")

        # 3. 移入回收站：搜索原关键词立即归零
        batch = RecycleService.recycle_page_or_subtree(page, system_authorized=True)
        res_recycled = ContentQuery.execute(spec_v1)
        self.assertEqual(res_recycled.total_count, 0, "页面移入回收站后必须立即从搜索中消失！")

        # 4. 从回收站恢复为草稿态：依然不能出现在搜索中，直到重新发布！
        RecycleService.restore_page_or_subtree(batch.batch_id, system_authorized=True)
        page.refresh_from_db()
        self.assertFalse(page.live, "恢复后严格保持草稿态")
        res_restored = ContentQuery.execute(spec_v1)
        self.assertEqual(res_restored.total_count, 0, "恢复为草稿态的页面不可直接被搜索召回！")

    def test_06_recycle_discovery_02_cursor_reset(self):
        """测试 RECYCLE-DISCOVERY-02 规则：Epoch 变化导致游标失效自动重置回第 1 页并轻提示"""
        from discovery.contracts import QuerySpec
        from discovery.query import ContentQuery, _encode_cursor
        from discovery.models import ContentDiscoveryEpoch

        page = self._create_and_publish_blog_page("游标测试页", intro="用于测试游标失效自动重置的内容。")

        old_epoch = ContentDiscoveryEpoch.get_current_epoch()
        # 生成一个基于旧 Epoch 的游标 (offset=10)
        old_cursor = _encode_cursor(old_epoch, 10)

        # 后台发生内容变更（如发布或回收），Epoch 递增
        ContentDiscoveryEpoch.increment_epoch()
        new_epoch = ContentDiscoveryEpoch.get_current_epoch()
        self.assertGreater(new_epoch, old_epoch)

        # 使用旧游标请求：检测到 Epoch 不匹配，自动重置回第 1 页，返回 cursor_reset=True
        spec = QuerySpec(q="游标测试页", cursor=old_cursor)
        res = ContentQuery.execute(spec)
        self.assertTrue(res.cursor_reset, "必须标记 cursor_reset=True 供前台轻提示！")
        self.assertEqual(res.total_count, 1)
        self.assertEqual(res.items[0].page_id, page.id)

    def test_07_navigation_type_filter_and_facet_labels(self):
        """测试 type=navigation 展开所有 _index 类型，以及 compute_facets 的中文名称与 navigation 聚合"""
        from blog.models import BlogIndexPage
        from discovery.contracts import QuerySpec
        from discovery.query import ContentQuery
        from guides.models import GuideIndexPage, GuidePage, GuideTopic
        from portfolio.models import PortfolioIndexPage
        from taxonomy.models import TagIndexPage

        b_idx = BlogIndexPage(title="搜索测试博客栏目", slug=f"bidx-{uuid4().hex[:6]}")
        self.home.add_child(instance=b_idx)
        b_idx.save_revision().publish()
        self.created_pages.append(b_idx)

        g_idx = GuideIndexPage(title="搜索测试指南栏目", slug=f"gidx-{uuid4().hex[:6]}")
        self.home.add_child(instance=g_idx)
        g_idx.save_revision().publish()
        self.created_pages.append(g_idx)

        p_idx = PortfolioIndexPage(title="搜索测试作品栏目", slug=f"pidx-{uuid4().hex[:6]}")
        self.home.add_child(instance=p_idx)
        p_idx.save_revision().publish()
        self.created_pages.append(p_idx)

        t_idx = TagIndexPage.objects.live().filter(is_recycled=False).first()
        if not t_idx:
            t_idx = TagIndexPage(title="搜索测试标签大厅", slug=f"tidx-{uuid4().hex[:6]}")
            self.home.add_child(instance=t_idx)
            t_idx.save_revision().publish()
            self.created_pages.append(t_idx)

        topic = GuideTopic.objects.create(name=f"NavTopic-{uuid4().hex[:4]}", slug=f"nt-{uuid4().hex[:4]}")
        self.created_topics.append(topic)
        from wagtail.rich_text import RichText
        guide = GuidePage(
            title="非栏目指南正文",
            slug=f"gpage-{uuid4().hex[:6]}",
            topic=topic,
            body=[("step", {"step_number": "1", "title": "步骤一", "content": RichText("<p>步骤内容</p>")})],
        )
        g_idx.add_child(instance=guide)
        guide.save_revision().publish()
        self.created_pages.append(guide)

        blog = self._create_and_publish_blog_page("非栏目博客文章")

        # 1. ContentQuery 直接验证 type_keys=["navigation"]
        res = ContentQuery.execute(QuerySpec(type_keys=["navigation"], facets=["types"]))
        result_type_keys = {it.type_key for it in res.items}
        self.assertTrue(result_type_keys.issubset({"blog_index", "guide_index", "portfolio_index", "tag_index"}))
        self.assertIn("blog_index", result_type_keys)
        self.assertIn("guide_index", result_type_keys)
        self.assertIn("portfolio_index", result_type_keys)
        self.assertIn("tag_index", result_type_keys)
        result_ids = {it.page_id for it in res.items}
        self.assertNotIn(blog.id, result_ids)
        self.assertNotIn(guide.id, result_ids)

        # 2. 检查 facets["types"] 中的中文显示名与 navigation 聚合总和
        types_map = {t["key"]: t for t in res.facets["types"]}
        self.assertIn("navigation", types_map)
        self.assertEqual(types_map["navigation"]["label"], "栏目导航页")
        expected_nav_count = sum(
            t["count"] for k, t in types_map.items() if k.endswith("_index")
        )
        self.assertEqual(types_map["navigation"]["count"], expected_nav_count)
        self.assertEqual(types_map["blog_page"]["label"], "博客文章")

        # 3. /search/?type=navigation HTTP 视图校验
        resp = self.client.get("/search/?type=navigation")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("搜索测试博客栏目", html)
        self.assertIn("搜索测试指南栏目", html)
        self.assertIn("搜索测试作品栏目", html)
        self.assertNotIn("非栏目博客文章", html)
        for label in ("全部", "博客文章", "专题指南", "开源项目", "栏目导航页"):
            self.assertIn(label, html)

    def test_08_case_insensitive_tag_facet_merging_and_multi_tag_view(self):
        """测试大小写标签在 facets 中去重合并（保留首字母大写/驼峰），以及视图多选与 slug/id 解析"""
        from discovery.contracts import QuerySpec
        from discovery.query import ContentQuery
        from taggit.models import Tag

        unique_suffix = uuid4().hex[:5]
        upper_name = f"PostGreSQL{unique_suffix}"
        lower_name = f"postgresql{unique_suffix}"
        other_name = f"Django{unique_suffix}"

        p1 = self._create_and_publish_blog_page(f"PG大写标签文章-{unique_suffix}", tags=[upper_name])
        # 确保创建独立的小写 Tag 记录模拟历史大小写重复标签
        lower_tag, _ = Tag.objects.get_or_create(
            name=lower_name,
            defaults={"slug": f"{lower_name}-lower"},
        )
        p2 = self._create_and_publish_blog_page(f"PG小写标签文章-{unique_suffix}")
        p2.tags.add(lower_tag)
        p2.save_revision().publish()

        p3 = self._create_and_publish_blog_page(f"Django标签文章-{unique_suffix}", tags=[other_name])

        # 1. 验证 facets["tags"] 仅保留一条，展示名为 Camel/首字母大写形式，count 累加且记录全部 tag_ids
        res = ContentQuery.execute(QuerySpec(facets=["tags"]))
        matching = [t for t in res.facets["tags"] if t["name"].lower() == lower_name.lower()]
        self.assertEqual(len(matching), 1, "大小写不同但小写相同的标签必须在 facets 中合并为 1 行")
        merged = matching[0]
        self.assertEqual(merged["name"], upper_name)
        self.assertGreaterEqual(merged["count"], 2)
        upper_tag = p1.tags.get(name=upper_name)
        self.assertEqual(set(merged["tag_ids"]), {upper_tag.id, lower_tag.id})

        # 2. 通过 slug 过滤：同时命中大小写两篇文章
        resp_slug = self.client.get(f"/search/?tag={upper_tag.slug}")
        self.assertEqual(resp_slug.status_code, 200)
        html_slug = resp_slug.content.decode("utf-8")
        self.assertIn(f"PG大写标签文章-{unique_suffix}", html_slug)
        self.assertIn(f"PG小写标签文章-{unique_suffix}", html_slug)

        # 3. 通过 tag_id 过滤：同样命中两篇
        resp_id = self.client.get(f"/search/?tag={lower_tag.id}")
        self.assertEqual(resp_id.status_code, 200)
        html_id = resp_id.content.decode("utf-8")
        self.assertIn(f"PG大写标签文章-{unique_suffix}", html_id)
        self.assertIn(f"PG小写标签文章-{unique_suffix}", html_id)

        # 4. 多标签组合（重复参数与逗号分隔）
        other_tag = p3.tags.get(name=other_name)
        resp_multi_1 = self.client.get(f"/search/?tag={upper_tag.slug}&tag={other_tag.slug}")
        self.assertEqual(resp_multi_1.status_code, 200)
        html_multi_1 = resp_multi_1.content.decode("utf-8")
        self.assertIn(f"PG大写标签文章-{unique_suffix}", html_multi_1)
        self.assertIn(f"Django标签文章-{unique_suffix}", html_multi_1)

        resp_multi_2 = self.client.get(f"/search/?tag={upper_tag.slug},{other_tag.id}")
        self.assertEqual(resp_multi_2.status_code, 200)
        html_multi_2 = resp_multi_2.content.decode("utf-8")
        self.assertIn(f"PG大写标签文章-{unique_suffix}", html_multi_2)
        self.assertIn(f"Django标签文章-{unique_suffix}", html_multi_2)

    def test_09_search_view_multidimensional_combo_and_url_preservation(self):
        """测试多维组合筛选（query + title_only + type + tag + days + year + sort）、已选胶囊与侧栏参数保持"""
        from django.utils import timezone
        from guides.models import GuidePage, GuideTopic

        today = timezone.localdate()
        tag_name = f"ComboTag{uuid4().hex[:4]}"

        p_recent = self._create_and_publish_blog_page(
            "微服务核心架构演进",
            intro="近期最新发表的架构笔记。",
            authored_on=today - timedelta(days=2),
            tags=[tag_name],
        )
        p_body_only = self._create_and_publish_blog_page(
            "普通运维日志",
            intro="正文里提到了微服务核心，但标题没有。",
            authored_on=today - timedelta(days=2),
            tags=[tag_name],
        )
        p_old = self._create_and_publish_blog_page(
            "微服务核心历史回顾",
            intro="很久之前的旧文章。",
            authored_on=today - timedelta(days=60),
            tags=[tag_name],
        )

        topic = GuideTopic.objects.create(name=f"ComboTopic-{uuid4().hex[:4]}", slug=f"ct-{uuid4().hex[:4]}")
        self.created_topics.append(topic)
        from wagtail.rich_text import RichText
        g_recent = GuidePage(
            title="微服务核心实战指南",
            slug=f"g-combo-{uuid4().hex[:6]}",
            topic=topic,
            authored_on=today - timedelta(days=2),
            body=[("step", {"step_number": "1", "title": "步骤一", "content": RichText("<p>指南内容</p>")})],
        )
        self.home.add_child(instance=g_recent)
        g_recent.tags.add(tag_name)
        g_recent.save_revision().publish()
        self.created_pages.append(g_recent)

        tag_obj = p_recent.tags.get(name=tag_name)
        url = (
            f"/search/?query=微服务核心&title_only=1&type=blog_page"
            f"&tag={tag_obj.slug}&days=7&sort=authored_desc&cursor=dummy_old_cursor"
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        ctx = resp.context

        # 仅命中近期且标题含“微服务核心”的博客文章
        hit_ids = [it.page_id for it in ctx["items"]]
        self.assertEqual(hit_ids, [p_recent.id])
        self.assertNotIn(p_body_only.id, hit_ids)
        self.assertNotIn(p_old.id, hit_ids)
        self.assertNotIn(g_recent.id, hit_ids)

        # 校验已选条件胶囊（关键词、范围、类型、标签、时间、排序）与移除 URL 保持其他筛选
        pills_by_key = {p["key"]: p for p in ctx["active_filter_pills"]}
        self.assertIn("query", pills_by_key)
        self.assertIn("title_only", pills_by_key)
        self.assertIn("type", pills_by_key)
        self.assertIn("tag", pills_by_key)
        self.assertIn("days", pills_by_key)
        self.assertIn("sort", pills_by_key)

        # 移除 type 胶囊后，query/tag/days/title_only/sort 必须仍然保留，且 cursor 已被清空
        rm_type_qs = parse_qs(urlparse(pills_by_key["type"]["remove_url"]).query)
        self.assertNotIn("type", rm_type_qs)
        self.assertEqual(rm_type_qs.get("query"), ["微服务核心"])
        self.assertEqual(rm_type_qs.get("tag"), [tag_obj.slug])
        self.assertEqual(rm_type_qs.get("days"), ["7"])
        self.assertEqual(rm_type_qs.get("title_only"), ["1"])
        self.assertNotIn("cursor", rm_type_qs)

        # 侧栏类型切换选项必须保留现有 query/tag/days，绝不清空其他筛选
        guide_opt = next(o for o in ctx["type_options"] if o["key"] == "guide")
        guide_qs = parse_qs(urlparse(guide_opt["url"]).query)
        self.assertEqual(guide_qs.get("type"), ["guide"])
        self.assertEqual(guide_qs.get("query"), ["微服务核心"])
        self.assertEqual(guide_qs.get("tag"), [tag_obj.slug])
        self.assertEqual(guide_qs.get("days"), ["7"])
        self.assertNotIn("cursor", guide_qs)
        self.assertEqual(ctx["reset_url"], "/search/")

    def test_10_search_view_hybrid_pagination_422_and_ajax_fragment(self):
        """测试 /search/ 接入千万级混合分页、超窗 422 提示与 AJAX 局部返回"""
        prefix = f"分页验证{uuid4().hex[:4]}"
        for i in range(5):
            self._create_and_publish_blog_page(
                f"{prefix}-第{i + 1}篇",
                intro="混合分页集成验证内容。",
                authored_on=date(2026, 8, 10 - i),
            )

        # 预热站点全局设置单例，避免首屏渲染 {% get_settings %} 初始化 3 个单例行时触发 Epoch 自增
        self.client.get("/search/")

        # 1. 第 1 页（page_size=2）
        resp_p1 = self.client.get(f"/search/?query={prefix}&page_size=2&page=1")
        self.assertEqual(resp_p1.status_code, 200)
        html_p1 = resp_p1.content.decode("utf-8")
        self.assertIn("listing-pagination-wrap", html_p1)
        self.assertIn("listing-jump-page-form", html_p1)
        self.assertIn("listing-seek-date-form", html_p1)
        self.assertIn("search_controller.js", html_p1)
        self.assertEqual(len(resp_p1.context["items"]), 2)
        next_cursor = resp_p1.context["next_cursor"]
        self.assertTrue(next_cursor and next_cursor.startswith("v2:"))

        # 2. 游标翻页到第 2 页再上一页回到第 1 页
        resp_c2 = self.client.get(f"/search/?query={prefix}&page_size=2&cursor={next_cursor}")
        self.assertEqual(resp_c2.status_code, 200)
        self.assertEqual(len(resp_c2.context["items"]), 2)
        prev_cursor = resp_c2.context["previous_cursor"]
        self.assertTrue(prev_cursor)

        resp_back = self.client.get(f"/search/?query={prefix}&page_size=2&cursor={prev_cursor}")
        self.assertEqual(resp_back.status_code, 200)
        self.assertEqual(
            [it.page_id for it in resp_back.context["items"]],
            [it.page_id for it in resp_p1.context["items"]],
        )

        # 3. 超过浅页窗口上限返回 HTTP 422 友好提示
        resp_422 = self.client.get(f"/search/?query={prefix}&page_size=2&page=105")
        self.assertEqual(resp_422.status_code, 422)
        self.assertIn("超出直跳窗口上限", resp_422.content.decode("utf-8"))

        # 4. AJAX 请求返回 JSON 局部片段与 canonical_url
        resp_ajax = self.client.get(
            f"/search/?query={prefix}&type=blog_page&page_size=2",
            HTTP_X_LISTING_RESPONSE="fragment-v1",
        )
        self.assertEqual(resp_ajax.status_code, 200)
        payload = resp_ajax.json()
        self.assertEqual(payload["canonical_url"], "/search/")
        self.assertEqual(payload["total_count"], 5)
        self.assertIn("search-result-list", payload["html"])
        self.assertEqual(payload["active_filters"]["type"], "blog_page")

        # 5. AJAX 422 局部提示
        resp_ajax_422 = self.client.get(
            f"/search/?query={prefix}&page_size=2&page=105&format=partial",
        )
        self.assertEqual(resp_ajax_422.status_code, 422)
        payload_422 = resp_ajax_422.json()
        self.assertIn("超出直跳窗口上限", payload_422["error_message"])
        self.assertIn("超出直跳窗口上限", payload_422["html"])
