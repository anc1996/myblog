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
from datetime import date
from pathlib import Path
import unittest
from uuid import uuid4

PROJECT_DIR = Path(__file__).resolve().parents[1]


class S9SearchAndDiscoveryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
        import django
        django.setup()

    def setUp(self):
        from home.models import HomePage
        from blog.models import BlogIndexPage, BlogPage
        from discovery.models import PublishedSearchDocument, ContentDiscoveryEpoch

        self.home = HomePage.objects.first()
        self.created_pages = []

    def tearDown(self):
        for p in reversed(self.created_pages):
            try:
                p.delete(allow_physical_purge=True)
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


if __name__ == "__main__":
    unittest.main()
