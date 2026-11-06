# -*- coding: utf-8 -*-
"""
QA 自动化测试数据集构建脚本。

在 Home 页面下生成：
1. 1 个专用的 BlogIndexPage（“QA 自动化综合测试专栏” / qa-test-blog）
2. 4 个 Person 模型实例（张衡、沈括、毕昇、祖冲之）
3. 20 篇高覆盖度 BlogPage（严格按照 QA 测试用例矩阵设计，覆盖 V1/V2/V3、全积木组件、复杂表格、代码高亮、多作者、标签、长文与回收恢复）
"""

import os
import sys
import json
from pathlib import Path
from datetime import date, timedelta
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import django
from django.utils import timezone

os.environ["DJANGO_SETTINGS_MODULE"] = "myblog.settings.dev"
django.setup()

from home.models import HomePage
from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
from people.models import Person
from content_lifecycle.recycle import RecycleService


def create_or_clean_dataset():
    print("==================================================")
    print("开始构建 QA 自动化综合测试数据集（20 篇全场景博客）")
    print("==================================================")

    home = HomePage.objects.first()
    if not home:
        raise RuntimeError("未找到根节点 HomePage！")

    # 1. 清理已有测试目录（若存在）
    existing_index = BlogIndexPage.objects.filter(slug="qa-test-blog").first()
    if existing_index:
        print(f"[*] 清理旧测试专栏 (ID={existing_index.id}, Title={existing_index.title})...")
        RecycleService.purge_page_or_subtree(existing_index.id)
        home.refresh_from_db()

    # 2. 创建全新的 QA 测试索引页
    print("[*] 创建测试专栏: BlogIndexPage(title='QA 自动化综合测试专栏', slug='qa-test-blog')...")
    index_page = BlogIndexPage(
        title="QA 自动化综合测试专栏",
        slug="qa-test-blog",
        authored_on="2026-11-05",
        intro="本专栏由自动化 QA 测试套件构建，专门用于演练 Wagtail 8.1 + PostgreSQL 18.6 + MongoDB 9.0 架构下的多版本演进（V1/V2/V3）、全组件积木渲染、版本对比、版本回滚及回收站恢复能力。",
    )
    home.add_child(instance=index_page)
    index_page.save_revision().publish()
    index_page.refresh_from_db()
    print(f"[✓] 测试专栏创建并发布成功 (ID={index_page.id}, URL={index_page.url})")

    # 3. 创建测试人员 (Person)
    print("[*] 准备测试人员数据 (Person)...\n")
    p_arch, _ = Person.objects.get_or_create(name="沈括", defaults={"bio": "北宋科学家、主架构师"})
    p_dev, _ = Person.objects.get_or_create(name="张衡", defaults={"bio": "东汉天文学家、核心开发"})
    p_full, _ = Person.objects.get_or_create(name="毕昇", defaults={"bio": "活字印刷术发明人、全栈工程师"})
    p_qa, _ = Person.objects.get_or_create(name="祖冲之", defaults={"bio": "南北朝数学家、QA负责人"})

    created_summary = []

    # 辅助构造 Table JSON
    perf_table_json = json.dumps({
        "rows": [
            [
                {"value": "数据库引擎", "header": True, "align": "center"},
                {"value": "事务隔离级别", "header": True, "align": "center"},
                {"value": "并发连接数", "header": True, "align": "center"},
                {"value": "P99 响应耗时", "header": True, "align": "center"},
                {"value": "吞吐量 (QPS)", "header": True, "align": "center"},
            ],
            ["PostgreSQL 18.6", "REPEATABLE READ", "100", "1.82 ms", "18,450"],
            ["PostgreSQL 18.6", "READ COMMITTED", "100", "1.25 ms", "22,300"],
            ["MongoDB 9.0.4", "Local Snapshot", "200", "0.65 ms", "45,120"],
            ["Redis 8.10.3", "In-Memory Cache", "500", "0.12 ms", "128,000"],
        ]
    })

    # ==========================================
    # Group A: 版本演进与生命周期组 (Page 01 - 05)
    # ==========================================
    print("--- 正在生成 Group A: 版本演进与生命周期组 (Page 01 - 05) ---")

    # Page 01: V1 Live
    p01 = BlogPage(title="PostgreSQL 18 存储引擎与事务隔离全景解析", slug="tc-ver-01-v1-live", authored_on="2026-11-01")
    index_page.add_child(instance=p01)
    p01.body = [
        ("rich_text", "<p><strong>PostgreSQL 18</strong> 是关系数据库领域的标杆之作，引入了更强大的并发控制与快照隔离机制。</p>"),
        ("code", {"title": "isolation_test.sql", "language": "sql", "code": "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;\nSELECT * FROM blog_blogpage WHERE id = 1;\nCOMMIT;", "mode": "display"}),
        ("markdown", "### 核心收益\n- 严格的快照隔离保证读取一致性\n- 消除脏读与不可重复读\n- 支持并发无锁分析"),
    ]
    r1 = p01.save_revision()
    r1.publish()
    created_summary.append((p01.id, "TC-VER-01", p01.title, "V1 (Live)", "基准单版本正式发布"))
    print(f"  [1/20] ID={p01.id} {p01.title} -> V1 Live")

    # Page 02: V1 Live -> V2 Live
    p02 = BlogPage(title="Wagtail 8.1 官方 StreamField 积木开发最佳实践", slug="tc-ver-02-v2-live", authored_on="2026-11-02")
    index_page.add_child(instance=p02)
    p02.body = [
        ("markdown", "## V1 初始构建\nWagtail StreamField 允许编辑自由组合不同类型的内容块。\n下面是一个基础结构示例。"),
        ("code", {"title": "blocks.py", "language": "python", "code": "from wagtail import blocks\nclass MyBlock(blocks.CharBlock):\n    pass", "mode": "display"}),
    ]
    r1 = p02.save_revision()
    r1.publish()
    # 升级为 V2 并再次发布
    p02.refresh_from_db()
    p02.body = [
        ("markdown", "## V2 演进增强版\nWagtail StreamField 进阶：通过自定义 StructBlock 与装饰器增强前端渲染。\n新增了对 Mermaid 架构图的原生支持。"),
        ("code", {"title": "advanced_blocks.py", "language": "python", "code": "from wagtail import blocks\nclass AdvBlock(blocks.StructBlock):\n    title = blocks.CharBlock()\n    code = blocks.TextBlock()", "mode": "display"}),
        ("mermaid", {"title": "积木流转图", "diagram_code": "graph TD;\n  A[Editor Form]-->B[StreamField];\n  B-->C[MongoDB Immutable Version];\n  C-->D[Render HTML];", "theme": "default"}),
    ]
    r2 = p02.save_revision()
    r2.publish()
    created_summary.append((p02.id, "TC-VER-02", p02.title, "V2 (Live)", "两版本连续发布演练"))
    print(f"  [2/20] ID={p02.id} {p02.title} -> V1 Live -> V2 Live")

    # Page 03: V1 Live -> V2 Live -> V3 Draft (未发布草稿)
    p03 = BlogPage(title="跨关系数据库与 NoSQL 混合存储架构的一致性契约", slug="tc-ver-03-v3-draft", authored_on="2026-11-03")
    index_page.add_child(instance=p03)
    p03.body = [("markdown", "# V1: 初始方案探讨\n关系库负责结构化数据，MongoDB 负责富文本正文。")]
    p03.save_revision().publish()
    # V2 发布
    p03.refresh_from_db()
    p03.body = [
        ("markdown", "# V2: 正式生产架构（线上展示版）\nPostgreSQL 是发布状态与 Revision 唯一权威，MongoDB 仅存储不可变正文快照。"),
        ("table", {"caption": "双存储核心权责表", "has_header_row": True, "has_header_col": False, "alignment": "center", "table_data": perf_table_json}),
    ]
    p03.save_revision().publish()
    # V3 仅保存草稿（未发布）
    p03.refresh_from_db()
    p03.body = [
        ("markdown", "# V3: 待评审草案（未发布草稿！）\n【机密草稿】计划在 S12 阶段引入向量检索与语义混合分词通道。\n新增了分布式事务补偿机制与自动修复管道。"),
        ("code", {"title": "reconcile.py", "language": "python", "code": "def auto_heal():\n    # 自动对账补齐孤儿文档\n    pass", "mode": "runner"}),
    ]
    r3 = p03.save_revision()  # 不调用 publish()
    created_summary.append((p03.id, "TC-VER-03", p03.title, "V3 (Draft)", "线上展示 V2，后台编辑 V3，支持版本对比 Diff"))
    print(f"  [3/20] ID={p03.id} {p03.title} -> V1 Live -> V2 Live -> V3 Draft")

    # Page 04: V1 Draft -> V2 Draft -> V3 Draft (纯草稿)
    p04 = BlogPage(title="未上线内测草案：现代微前端与无头 CMS 探索", slug="tc-ver-04-pure-draft", authored_on="2026-11-04", live=False, has_unpublished_changes=True)
    index_page.add_child(instance=p04)
    p04.body = [("markdown", "草案初稿：评估 Next.js 15 App Router 与 Wagtail 8.1 API 通信。")]
    p04.save_revision()
    p04.body = [("markdown", "草案二稿：增加 Server Components 与缓存流式水合架构。")]
    p04.save_revision()
    p04.body = [
        ("markdown", "草案三稿（最新草稿，从未上线）：\n1. GraphQL vs REST API v3 选型对比\n2. 媒体私有鉴权代理通道集成\n3. 边缘 Edge 渲染缓存规划"),
        ("code", {"title": "fetch_page.ts", "language": "python", "code": "async function getPage(slug: string) {\n  return await fetch(`/api/v3/pages/?slug=${slug}`);\n}", "mode": "display"}),
    ]
    p04.save_revision()
    created_summary.append((p04.id, "TC-VER-04", p04.title, "V3 (Pure Draft)", "纯草稿链条，从未发布，前台 404"))
    print(f"  [4/20] ID={p04.id} {p04.title} -> V1 Draft -> V2 Draft -> V3 Draft")

    # Page 05: V1 Live -> V2 Live -> Revert to V1 (V3 Draft)
    p05 = BlogPage(title="版本回滚实战：从 V2 回滚至 V1 的安全演练", slug="tc-ver-05-revert-demo", authored_on="2026-11-05")
    index_page.add_child(instance=p05)
    p05.body = [("markdown", "### V1: 初始黄金稳定版正文\n系统运行极度稳定，无任何缺陷。")]
    r1 = p05.save_revision()
    r1.publish()
    # V2 引入破坏性改动
    p05.refresh_from_db()
    p05.body = [("markdown", "### V2: 引入实验性特性的测试版\n【警告】本版本正文包含未经验证的实验代码。")]
    r2 = p05.save_revision()
    r2.publish()
    # 模拟从历史记录中点击“恢复 V1 版本”
    p05.refresh_from_db()
    v1_obj = r1.as_object()
    p05.body = v1_obj.body
    r3 = p05.save_revision(log_action="wagtail.revert")
    created_summary.append((p05.id, "TC-VER-05", p05.title, "V3 (Revert V1 Draft)", "V1发布 -> V2发布 -> 还原至V1产生V3草稿"))
    print(f"  [5/20] ID={p05.id} {p05.title} -> V1 Live -> V2 Live -> Revert to V1")

    # ==========================================
    # Group B: 积木组件深度与排版组 (Page 06 - 10)
    # ==========================================
    print("\n--- 正在生成 Group B: 积木组件深度与排版组 (Page 06 - 10) ---")

    # Page 06: 代码块高亮
    p06 = BlogPage(title="全语言代码块语法高亮与行号极限测试", slug="tc-blk-06-code-matrix", authored_on="2026-10-28")
    index_page.add_child(instance=p06)
    p06.body = [
        ("markdown", "## 多语言代码块高亮测试\n本页集成了 Python、Rust、SQL、Go 等主流语言，包含展示模式与在线代码运行器原型。"),
        ("code", {"title": "concurrency_pool.py", "language": "python", "code": "import asyncio\n\nasync def worker(queue):\n    while True:\n        item = await queue.get()\n        print(f'Processing {item}')\n        queue.task_done()\n\nasync def main():\n    queue = asyncio.Queue()\n    for i in range(10):\n        queue.put_nowait(i)\n    tasks = [asyncio.create_task(worker(queue)) for _ in range(3)]\n    await queue.join()\n    for t in tasks: t.cancel()\n\nasyncio.run(main())", "mode": "display"}),
        ("code", {"title": "memory_safety.rs", "language": "python", "code": "fn main() {\n    let mut data = vec![1, 2, 3];\n    let ref1 = &data;\n    println!(\"Ref: {:?}\", ref1);\n    data.push(4);\n    println!(\"Updated: {:?}\", data);\n}", "mode": "display"}),
        ("code", {"title": "interactive_sandbox.py", "language": "python", "code": "# 交互执行模式原型 (AC-13)\ndef compute_fib(n):\n    return 1 if n <= 2 else compute_fib(n-1) + compute_fib(n-2)\nprint(compute_fib(10))", "mode": "runner"}),
    ]
    p06.save_revision().publish()
    created_summary.append((p06.id, "TC-BLK-06", p06.title, "V1 (Live)", "Python / Rust / Runner 代码块高亮"))
    print(f"  [6/20] ID={p06.id} {p06.title} -> CodeBlock 矩阵")

    # Page 07: 复杂数据表格
    p07 = BlogPage(title="分布式高并发场景数据库性能压测基准数据表", slug="tc-blk-07-complex-table", authored_on="2026-10-29")
    index_page.add_child(instance=p07)
    p07.body = [
        ("rich_text", "<p>下表展示了在 1000 万并发请求下，各存储系统的性能对比实测数据：</p>"),
        ("table", {"caption": "主流数据库并发基准测试指标 (8GB 内存环境)", "has_header_row": True, "has_header_col": False, "alignment": "center", "table_data": perf_table_json}),
    ]
    p07.save_revision().publish()
    created_summary.append((p07.id, "TC-BLK-07", p07.title, "V1 (Live)", "复杂结构化数据表格 TableBlock"))
    print(f"  [7/20] ID={p07.id} {p07.title} -> TableBlock 矩阵")

    # Page 08: Mermaid 流程图
    p08 = BlogPage(title="电商微服务架构状态机与订单流转时序图", slug="tc-blk-08-mermaid-charts", authored_on="2026-10-30")
    index_page.add_child(instance=p08)
    p08.body = [
        ("markdown", "## 微服务分布式链路时序图\n展示客户端、API 网关、订单服务与支付中台的交互流程。"),
        ("mermaid", {"title": "支付流转时序图", "diagram_code": "sequenceDiagram\n  autonumber\n  actor User as 读者\n  participant Web as Wagtail 前台\n  participant PG as PostgreSQL 18\n  participant MG as MongoDB 9\n  User->>Web: 访问 /blog/post-1/\n  Web->>PG: 获取 live_revision_id\n  PG-->>Web: 返回指针 intro_v1 & body_v1\n  Web->>MG: 按指针提取正文快照\n  MG-->>Web: 返回纯净结构化积木数据\n  Web-->>User: 渲染完整详情页面", "theme": "default"}),
        ("mermaid", {"title": "页面生命周期状态机", "diagram_code": "stateDiagram-v2\n  [*] --> 草稿态\n  草稿态 --> 审批中: 提交审核\n  审批中 --> 已发布: 终审通过\n  审批中 --> 草稿态: 审批驳回\n  已发布 --> 已回收: 移入回收站\n  已回收 --> 草稿态: 错删精准还原\n  已回收 --> [*]: 物理受控销毁", "theme": "forest"}),
    ]
    p08.save_revision().publish()
    created_summary.append((p08.id, "TC-BLK-08", p08.title, "V1 (Live)", "双 MermaidBlock 时序图与状态机"))
    print(f"  [8/20] ID={p08.id} {p08.title} -> Mermaid 流程图")

    # Page 09: Markdown 深度排版
    p09 = BlogPage(title="现代技术文档写作规范：Markdown 深度排版指南", slug="tc-blk-09-deep-markdown", authored_on="2026-10-31")
    index_page.add_child(instance=p09)
    p09.body = [
        ("markdown", """# 一级标题：系统设计总览
## 二级标题：核心模块划分
### 三级标题：通信协议定义

技术文档写作应遵循简洁、清晰、严密的工程原则。

> **架构师名言**：好的架构不是一蹴而就的，而是在持续重构与解耦中演进出来的。

#### 任务清单
- [x] 关系数据库切换至 PostgreSQL 18.6
- [x] 对象存储统一接入私有 RustFS
- [x] 验证 MongoDB 不可变版本水合机制
- [ ] 开展端到端浏览器自动化全量回归

#### 格式化代码围栏
行内代码 `SELECT * FROM pg_stat_activity` 可以快速排查当前活跃锁。

$$ E = mc^2 $$
"""),
    ]
    p09.save_revision().publish()
    created_summary.append((p09.id, "TC-BLK-09", p09.title, "V1 (Live)", "Markdown 深度排版（标题/公式/引用/任务）"))
    print(f"  [9/20] ID={p09.id} {p09.title} -> Markdown 深度排版")

    # Page 10: 全积木大综合混排
    p10 = BlogPage(title="全积木大融合：架构方案技术评审综合演练", slug="tc-blk-10-full-blocks-mix", authored_on="2026-11-01")
    index_page.add_child(instance=p10)
    p10.body = [
        ("rich_text", "<p>本文聚合了 <strong>RichText 富文本</strong>、<strong>Markdown 排版</strong>、<strong>高亮代码块</strong>、<strong>结构化表格</strong> 与 <strong>Mermaid 流程图</strong>，全面验证全栈积木协同。</p>"),
        ("markdown", "### 架构核心准则\n1. **数据权威归属**：PostgreSQL 决定发布状态\n2. **存储隔离**：MongoDB 保存不可变字段快照"),
        ("code", {"title": "server.py", "language": "python", "code": "def start_server():\n    print('Starting Wagtail 8.1 on port 8000...')", "mode": "display"}),
        ("table", {"caption": "架构特性检查表", "has_header_row": True, "has_header_col": False, "alignment": "center", "table_data": perf_table_json}),
        ("mermaid", {"title": "数据流架构图", "diagram_code": "graph LR;\n  PG[(PostgreSQL)]-->Core;\n  MG[(MongoDB)]-->Core;\n  Core[Wagtail 8.1 Engine]-->Client[浏览器客户端];", "theme": "default"}),
    ]
    p10.save_revision().publish()
    created_summary.append((p10.id, "TC-BLK-10", p10.title, "V1 (Live)", "全积木类型综合混排演练"))
    print(f"  [10/20] ID={p10.id} {p10.title} -> 全积木大混排")

    # ==========================================
    # Group C: 元数据与关联关系组 (Page 11 - 14)
    # ==========================================
    print("\n--- 正在生成 Group C: 元数据与关联关系组 (Page 11 - 14) ---")

    # Page 11: 多作者协作
    p11 = BlogPage(title="多团队跨域协同：核心作者、审校与协作角色分配", slug="tc-meta-11-multi-authors", authored_on="2026-10-25")
    index_page.add_child(instance=p11)
    p11.body = [("markdown", "本文由张衡主笔撰写，沈括负责技术审校，祖冲之参与实验复核，展示全站多作者角色分配能力。")]
    p11.save()
    BlogPagePerson.objects.create(page=p11, person=p_dev, role="主要作者", sort_order=0)
    BlogPagePerson.objects.create(page=p11, person=p_arch, role="技术审校", sort_order=1)
    BlogPagePerson.objects.create(page=p11, person=p_qa, role="测试复核", sort_order=2)
    p11.save_revision().publish()
    created_summary.append((p11.id, "TC-META-11", p11.title, "V1 (Live)", "多作者关联与角色定义（主要作者/审校/复核）"))
    print(f"  [11/20] ID={p11.id} {p11.title} -> 多作者协作")

    # Page 12: 多标签交叉
    p12 = BlogPage(title="技术标签多维交叉检索与知识图谱聚类分析", slug="tc-meta-12-multi-tags", authored_on="2026-10-26")
    index_page.add_child(instance=p12)
    p12.body = [("markdown", "本篇文章打上了 5 个全站公共技术标签：PostgreSQL、Wagtail、Django、架构设计、Python，用于组合过滤测试。")]
    p12.tags.add("PostgreSQL", "Wagtail", "Django", "架构设计", "Python")
    p12.save_revision().publish()
    created_summary.append((p12.id, "TC-META-12", p12.title, "V1 (Live)", "5 项公共技术标签多维覆盖"))
    print(f"  [12/20] ID={p12.id} {p12.title} -> 多标签交叉")

    # Page 13: 历史归档日期
    p13 = BlogPage(title="历史档案回顾：2020 年系统架构演进复盘", slug="tc-meta-13-archive-date", authored_on="2020-05-20")
    index_page.add_child(instance=p13)
    p13.body = [("markdown", "成文日期为 2020-05-20，用于验证年月归档服务按照 authored_on 过滤，而非原生发布时间。")]
    p13.save_revision().publish()
    created_summary.append((p13.id, "TC-META-13", p13.title, "V1 (Live)", "历史成文日期 (2020-05-20) 归档测试"))
    print(f"  [13/20] ID={p13.id} {p13.title} -> 历史归档日期")

    # Page 14: 高频修订演练
    p14 = BlogPage(title="高频次迭代演练：单日内四次修订记录追踪", slug="tc-meta-14-rapid-revisions", authored_on="2026-11-04")
    index_page.add_child(instance=p14)
    p14.body = [("markdown", "版本 1：草案初定")]
    p14.save_revision().publish()
    p14.refresh_from_db()
    p14.body = [("markdown", "版本 2：修正错别字并扩充内容")]
    p14.save_revision().publish()
    p14.refresh_from_db()
    p14.body = [("markdown", "版本 3：加入性能分析章节")]
    p14.save_revision().publish()
    p14.refresh_from_db()
    p14.body = [("markdown", "版本 4：终审定稿，全量发布上线")]
    p14.save_revision().publish()
    created_summary.append((p14.id, "TC-META-14", p14.title, "V4 (Live)", "连续 4 次发布演进，历史版本链条完整"))
    print(f"  [14/20] ID={p14.id} {p14.title} -> 4次高频迭代")

    # ==========================================
    # Group D: 边界值与极端输入组 (Page 15 - 17)
    # ==========================================
    print("\n--- 正在生成 Group D: 边界值与极端输入组 (Page 15 - 17) ---")

    # Page 15: 万字长文压力测试
    p15 = BlogPage(title="超长篇幅架构设计说明书（万字长文压力测试）", slug="tc-edge-15-ultra-long", authored_on="2026-11-02")
    index_page.add_child(instance=p15)
    long_paragraphs = []
    for i in range(1, 26):
        long_paragraphs.append(f"#### 第 {i} 章节：大型分布式系统的韧性架构原则\n在第 {i} 个服务分片中，我们实施了严格的断路器模式、隔离舱模式与超时降级机制。每次数据库交互均受 3 秒语句超时保护。在高负载冲击下，缓存层自动预热，Outbox 事件队列保持异步恒速消费，确保全站系统在峰值 QPS 超过 50,000 时依然平稳运行，P99 响应延迟严格控制在 5 毫秒以内。\n")
    p15.body = [
        ("markdown", "\n".join(long_paragraphs)),
        ("code", {"title": "benchmark.py", "language": "python", "code": "# 压力测试脚本\nimport time\ndef run_load():\n    start = time.time()\n    # simulate 100k ops\n    return time.time() - start", "mode": "display"}),
    ]
    p15.save_revision().publish()
    created_summary.append((p15.id, "TC-EDGE-15", p15.title, "V1 (Live)", "万字长文，验证长正文 Mongo 分块与水合"))
    print(f"  [15/20] ID={p15.id} {p15.title} -> 万字长文")

    # Page 16: 特殊字符与 XSS 安全
    p16 = BlogPage(title="Web 安全边界与特殊字符编码注入防御测试", slug="tc-edge-16-xss-security", authored_on="2026-11-03")
    index_page.add_child(instance=p16)
    p16.body = [
        ("rich_text", "<p>测试富文本安全净化：<script>alert('XSS')</script> 应该被彻底剥离！保留 <strong>合法粗体</strong> 与 <em>斜体</em>。</p>"),
        ("markdown", "### 极端字符与 Emoji 测试\n- 包含 Emoji: 🚀 🔥 💡 🎉 🛡️ ⚙️ 📦 ⚡\n- 引号与符号: \"双引号\", '单引号', & 和号, <尖括号>, \\反斜杠, /斜杠\n- SQL 注入特征串: `' OR 1=1; DROP TABLE users; --`"),
    ]
    p16.save_revision().publish()
    created_summary.append((p16.id, "TC-EDGE-16", p16.title, "V1 (Live)", "XSS 注入剥离与 Emoji/特殊符号安全验证"))
    print(f"  [16/20] ID={p16.id} {p16.title} -> 特殊字符与XSS")

    # Page 17: 增量结构差异
    p17 = BlogPage(title="积木增量差异演化测试：结构增删对比", slug="tc-edge-17-structure-diff", authored_on="2026-11-04")
    index_page.add_child(instance=p17)
    # V1: 只有富文本
    p17.body = [("rich_text", "<p>V1 版本只包含一段基础富文本介绍内容。</p>")]
    p17.save_revision().publish()
    # V2: 增加一段代码
    p17.refresh_from_db()
    p17.body = [
        ("rich_text", "<p>V2 版本增加了代码块展示：</p>"),
        ("code", {"title": "demo.py", "language": "python", "code": "print('Hello V2')", "mode": "display"}),
    ]
    p17.save_revision().publish()
    # V3: 删除代码，替换为表格与流程图
    p17.refresh_from_db()
    p17.body = [
        ("rich_text", "<p>V3 版本删除了代码块，替换为结构化表格与流程图：</p>"),
        ("table", {"caption": "V3 变更特征", "has_header_row": True, "has_header_col": False, "alignment": "center", "table_data": perf_table_json}),
        ("mermaid", {"title": "结构演进", "diagram_code": "graph LR; A[RichText]-->B[CodeBlock]; B-->C[Table & Mermaid];", "theme": "default"}),
    ]
    p17.save_revision().publish()
    created_summary.append((p17.id, "TC-EDGE-17", p17.title, "V3 (Live)", "组件增删演化，用于版本对比 Diff 专项测试"))
    print(f"  [17/20] ID={p17.id} {p17.title} -> 结构增删演变")

    # ==========================================
    # Group E: 回收站与安全生命周期组 (Page 18 - 20)
    # ==========================================
    print("\n--- 正在生成 Group E: 回收站与安全生命周期组 (Page 18 - 20) ---")

    # Page 18: 排期发布
    p18 = BlogPage(title="排期自动上线测试：未来的发布任务调度", slug="tc-sec-18-scheduled", authored_on="2026-11-05", live=False, has_unpublished_changes=True)
    index_page.add_child(instance=p18)
    p18.body = [("markdown", "本页面被安排在未来 7 天后自动发布上线，测试排期与历史记录联动机制。")]
    rev_sched = p18.save_revision()
    future_time = timezone.now() + timedelta(days=7)
    rev_sched.approved_go_live_at = future_time
    rev_sched.save(update_fields=["approved_go_live_at"])
    p18.go_live_at = future_time
    p18.save(update_fields=["go_live_at"])
    created_summary.append((p18.id, "TC-SEC-18", p18.title, "V1 (Scheduled)", "定时发布排期，测试未来时间调度"))
    print(f"  [18/20] ID={p18.id} {p18.title} -> 定时发布排期")

    # Page 19: 覆写防篡改演练
    p19 = BlogPage(title="系统完整性防御：覆写保护机制在线演练", slug="tc-sec-19-overwrite-guard", authored_on="2026-11-05")
    index_page.add_child(instance=p19)
    p19.body = [("markdown", "正式版初始正文：本版本发布后受到不可变保护。")]
    rev_live = p19.save_revision()
    rev_live.publish()
    p19.refresh_from_db()
    # 模拟以 overwrite_revision 传入已发布版本
    p19.body = [("markdown", "试图原地篡改已发布版本的新内容")]
    rev_guard = p19.save_revision(overwrite_revision=rev_live)
    if rev_guard.id != rev_live.id:
        p19.refresh_from_db()
        rev_guard.publish()
    created_summary.append((p19.id, "TC-SEC-19", p19.title, "V2 (Live)", "覆写保护：已发布版本禁止原地篡改，自动转新建版本"))
    print(f"  [19/20] ID={p19.id} {p19.title} -> 覆写保护演练")

    # Page 20: 受控回收站与还原演练
    p20 = BlogPage(title="受控回收站演练：误删安全还原与状态重置", slug="tc-sec-20-recycle-restore", authored_on="2026-11-05")
    index_page.add_child(instance=p20)
    p20.body = [("markdown", "本文初始发布后被误移入回收站，随后通过 RecycleService 精确还原至原树位置，保持未发布草稿态。")]
    p20.save_revision().publish()
    p20.refresh_from_db()
    # 1. 回收
    batch = RecycleService.recycle_page_or_subtree(p20)
    p20.refresh_from_db()
    print(f"    [p20] 移入回收站成功 (is_recycled={p20.is_recycled}, live={p20.live})")
    # 2. 还原
    restore_res = RecycleService.restore_page_or_subtree(batch.batch_id)
    p20.refresh_from_db()
    print(f"    [p20] 错删还原成功 (is_recycled={p20.is_recycled}, live={p20.live}, has_unpublished_changes={p20.has_unpublished_changes})")
    created_summary.append((p20.id, "TC-SEC-20", p20.title, "V2 (Restored Draft)", "回收站完整生命周期：发布V1 -> 回收下架 -> 还原为未发布草稿V2"))
    print(f"  [20/20] ID={p20.id} {p20.title} -> 回收站误删还原")

    print("\n==================================================")
    print("QA 自动化综合测试数据集创建完毕！")
    print(f"测试专栏: ID={index_page.id}, Slug={index_page.slug}, URL={index_page.url}")
    print("==================================================")
    print(f"共成功生成 20 篇高覆盖度测试博客文章，明细如下：")
    for pid, tcid, title, vstate, note in created_summary:
        print(f"- [{tcid}] (ID={pid:3d}) {vstate:22s} | {title} ({note})")

if __name__ == "__main__":
    create_or_clean_dataset()
