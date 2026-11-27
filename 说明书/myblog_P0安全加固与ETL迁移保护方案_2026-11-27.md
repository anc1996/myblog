# myblog P0 安全加固与 ETL 迁移保护方案（2026-11-27）

> 状态：**已确认待实施**（由 `sol` 与 `neon` 联合核查源码并经需求确认；尚未改代码）。
> 关联路线图：[上线前待办梳理与分步推进计划（2026-11-27）](myblog_上线前待办梳理与分步推进计划_2026-11-27.md) 第 1 步（P0-1）与第 2 步（P0-2）。

---

## 一、已确认的业务与安全决策（Q1–Q4）

1. **Q1（公开 API 与推荐块的访问边界）**：公开 `/api/v1/...` 接口和页面内推荐块**只输出当前站点的完全公开内容**。哪怕请求者已经登录或输入过页面密码，也不通过公开 JSON API 或推荐列表返回受限页面；有权限的读者仍通过 Wagtail 正常网页访问受限内容。
2. **Q2（多站点与重名 `slug` 规则）**：未知域名请求一律拒绝（返回为空/404）；嵌套子站点归属路径最近的站点根；若同一站点内同类型存在多个同名公开页面，宁可返回 404，不随机取数据库第一条。
3. **Q3（旧站 `wagtailblog3` 迁移的公开与权限原则 — 关键业务口径）**：
   - **旧站本来已发布（`live=True`）且无访问限制的公开文章，迁入新站后直接保持已发布、所有人都可以看**，**绝不**额外给它们加上“仅注册会员可见”等限制；
   - 仅当旧站某页面**本来就是草稿（`live=False`）**时，迁入后保持草稿不发布；
   - 仅当旧站某页面**本来就带有 `PageViewRestriction`（访问限制）**时，才按“能确认权限不放宽才发布（如核准组映射、重设口令），确认不了则先留草稿人工复核”处理。
4. **Q4（ETL 写入目标与历史批次）**：正式命令行入口移除通用的 `--allow-dev` 豁免，仅允许写入明确配置的暂存/生产目标（如 `.env.staging`），演练时使用新快照验证；此前已跑过的历史批次仅做只读对账，不覆盖也不删除。

---

## 二、P0-1：堵住公开 API 与推荐块的受限页权限漏洞

### 1. 目标与现状问题

- **现状漏洞**：
  1. `myblog/apps/api/views.py` 的 `_serve_adapter_detail_api` 仅按 `slug` 和 `live=True` 查表，未检查站点归属、`alias_of_id`、自身/祖先 `PageViewRestriction` 或 `live=False` 祖先，还把结果写进不区分 `site_id` 的 Redis 缓存；`_serve_adapter_list_api` 及 3 个适配器（`blog/adapters.py`、`guides/adapters.py`、`portfolio/adapters.py`）也只检查 `live=True, is_recycled=False`；`search_api` 未给 `QuerySpec` 和缓存键传入 `site_id`。
  2. `myblog/apps/discovery/eligibility.py:37` 误写了 `RecycleEntry.objects.filter(batch__status__in=["recycled", "restoring"])`（`"restoring"` 实际在 `RecycleEntry.status`，不在 `RecycleBatch.status`），且漏了 `live=False` 祖先校验；而 `content_lifecycle/public_access.py` 漏了 `live_revision_id__isnull=False` 校验与别名源祖先链写回。
  3. `myblog/apps/content_blocks/rendering/detail.py:523` 调用 `bound_block.render()` 时没有透传 `context`，导致 `FeaturedPostsBlock`（`portfolio/blocks.py:231`）拿不到 `request` 和 `is_preview`；`FeaturedPostsBlock.extract_text` 还会把关联文章标题写进宿主页搜索索引，目标文章日后转私密时可能从宿主页摘要漏出；`guides/models.py:501` 与 `guides/adapters.py:214` 的 `related_guides` 存在循环 `.specific` N+1 查询且漏检祖先限制。

### 2. 做法（零页面硬编码，单点事实源）

1. **统一收口到 `content_lifecycle/public_access.py`**：
   - 抽取内部原语 `get_public_blocked_path_prefixes(*, site=None)`：合并去重三类阻断前缀（站点范围及根祖先的 `PageViewRestriction`、`RecycleEntry.status in ("recycled", "restoring")`、以及 `Page.objects.filter(live=False, numchild__gt=0)` 非叶子草稿路径）；
   - 修复 `get_public_blocked_page_ids`（补齐别名源祖先映射、修复 `O(N^2)` 查表），并新增两个标准入口：
     - `filter_public_page_queryset(qs, *, site=None, request=None, require_live_revision=True, exclude_aliases=True)`：用 SQL 过滤 `live=True`、`live_revision_id__isnull=False`、`alias_of_id__isnull=True`、站点根路径前缀（排除嵌套子站点）及上述阻断前缀；无合法站点时返回 `qs.none()`；
     - `filter_public_pages(pages, *, site=None, request=None, ...)`：供块级/关联列表小批量对象复用，固定 $O(1)$ 次 SQL，不在循环内调用 `.specific` 或 `.get_ancestors()`。
   - 让 `discovery/eligibility.py` 复用同一阻断前缀逻辑，消除搜索与 API/订阅之间的规则分裂。
2. **收紧 `myblog/apps/api/views.py`**：
   - 在 `_serve_adapter_detail_api` 与 `_serve_adapter_list_api` 统一调用 `filter_public_page_queryset(..., request=request)`；同站同 `slug` 存在多个公开候选时按安全失败原则返回 404；
   - `search_api` 给 `QuerySpec` 和缓存键补上 `site_id`；`ContentQuery` 内部再结合当前页面树复核站点归属；
   - **首版缓存策略**：由于 `discovery/signals.py` 中权限变更刷新代次时可能捕获异常、且跨节点缓存撤权需严格闭环，受影响的公开详情与列表 API 首版停用共享响应缓存并下发 `Cache-Control: no-store`（保留限流）；后续若恢复缓存，须使用包含 `site_id + epoch + 参数哈希` 的 `v2` 键并保证权限变更可靠失效。
3. **收紧推荐块与关联指南输出**：
   - `detail.py` 提前计算 `is_preview` 并透传 `bound_block.render(context={"request": request, "page": page, "is_preview": is_preview})`；
   - `FeaturedPostsBlock`（`render_clean` / `render_basic`）与 `GuidePage.related_guides`（网页与 `guides/adapters.py`）：公开态严格过 `filter_public_pages(..., request=request)` 并用 `get_url(request)`（缺站点或无公开目标时不输出空壳）；后台预览态保留通用“非公开项在公开页隐藏”提示，不暴露私密标题、ID 或 URL；
   - `ProjectPageAdapter.serialize_api_detail` 在结构化 `blocks.value` 中同步剔除不合格推荐文章 ID；
   - `FeaturedPostsBlock.extract_text` 仅提取自身 `heading + description`，不再索引目标文章标题；修改落地后对受影响的既有项目页重跑一次搜索投影，清理旧摘要残留。

### 3. 怎么验证

- 新增/补充测试矩阵：
  - 参数化覆盖旧版与通用 `/api/v1/content/<type_slug>/` 详情、列表、搜索：自身受限、祖先栏目受限、祖先未发布、祖先回收、别名页均返回 404 且不计入总数/分面，匿名与已登录结果一致；
  - 双站同 `slug`、嵌套子站点、未知域名、以及预填旧缓存后给祖先加限制的实时撤权验证；
  - `FeaturedPostsBlock` 与 `GuidePage.related_guides` 同时验证网页 HTML、API `blocks.value` 和搜索全文均不泄漏受限目标标题/ID/URL。

---

## 三、P0-2：补齐旧站 `wagtailblog3` ETL 迁移保护

### 1. 目标与现状问题

- **现状缺口**：
  1. `scripts/etl_wagtailblog3/remote_export.py` 未导出 `PageViewRestriction`、`has_unpublished_changes` 与正式发布版本号，且两次 SSH 调用写死 `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null`；
  2. `scripts/etl_wagtailblog3/loader.py` 无视源页 `live` 状态一律执行 `rev.publish()`，且未在发布前恢复 `PageViewRestriction`（会导致 `media_library/signals.py` 把受限页媒体永久公开）；`reconcile_target()` 直接拿全部文章比对 `live=True` 并把草稿/受限页图片误判为漏转正；
  3. `assert_target_isolation()` 仅检查 `DATABASES["default"]["NAME"] == "myblog_dev"`，未把 PostgreSQL、MongoDB、RustFS 三端成套锁定，且存在被进程环境变量半覆盖的风险。

### 2. 做法

1. **快照版本化与 SSH 身份校验（`remote_export.py`）**：
   - 导出字段新增快照版本号、页面 `live` / `has_unpublished_changes` / 源发布版本标识，以及全站 `PageViewRestriction`（仅含元信息与组名，不导出明文口令）；缺少限制段的旧快照拒绝用于自动发布导入；
   - 发布正文只取源站正式发布版本对应内容，禁止回退读取“Mongo 最新草稿”；
   - SSH 改用 `StrictHostKeyChecking=yes` + 专用 `known_hosts`（按主机与端口核验，支持管理员显式提供路径），远端临时目录改用 `mktemp -d` 独立创建并立即清理。
2. **Pass 1 → Pass 1.5 → Pass 2 严格生命周期时序（`loader.py`）**：
   - **Pass 1**：按树路径拓扑建立 `live=False` 骨架，保留原始父子层级；
   - **Pass 1.5（在任何 `rev.publish()` 之前执行）**：按 `page_id_map` 幂等恢复页面自身与祖先 `PageViewRestriction`（无源限制的公开文章保持完全无限制）；若祖先限制无法映射到目标首页或用户组，整支保留草稿等待人工处理；
   - **Pass 2**：写入确定性 MongoDB 版本与 `save_revision()`：
     - 源站 `live=False` 草稿：**绝不调用 `rev.publish()`**，仅在曾发布过时保留历史 `first_published_at`，媒体引用保持 `RESTRICTED`；
     - 源站 `live=True` 且无限制的公开文章：**直接发布并保持所有人公开可读（不附加任何会员权限）**；对正式公开版本实际引用且获本批迁入批准的跨作者媒体，在同一 SQL 事务内逐项设为 `PUBLIC` 后发布（避免被 `validate_for_publish` 的 `uploaded_by_user_id != owner_id` 拦截）；
     - 源站 `live=True` 但本身或祖先有 `PageViewRestriction` 的页面：在确认限制已落库且不放宽权限后才发布，媒体保持 `RESTRICTED`（注：当前媒体网关对 `RESTRICTED` 文件默认仅放行后台作者/管理员，不放行普通密码访客）；
   - **对账口径修正（`reconcile_target`）**：按本批映射拆分核对 `live`、`draft`、`restricted` 数量；只要求**真正公开页**引用的媒体已转正，同时反向断言 `leaked_restricted_only_assets == 0`。
3. **三端成套目标隔离（`assert_target_isolation`）**：
   - 校验实际生效的 PostgreSQL（`HOST`/`PORT`/`NAME`）、MongoDB（`HOST`/`PORT`/`DATABASE`）、RustFS（`endpoint_url`/`bucket_name`/私有存储后端）三端指纹；
   - 禁止任意一端指向空值、开发库（`myblog_dev`）、开发桶或旧站库，禁止生产导入混用本地文件存储；
   - 移除正式 CLI 默认的 `--allow-dev` 放行；自动化测试（`tests/test_phase2_etl_migration.py`）改为显式隔离测试配置，兼容 `override_settings` 局部覆盖且不影响真实 CLI 安全门禁。

### 3. 怎么验证

- 单元测试覆盖：公开文章迁入保持 `live=True` 且无多余权限、草稿保持 `live=False` 不触发发布、受限页先建 `PageViewRestriction` 后发布且媒体保持 `RESTRICTED`、缺少组映射/口令/旧快照降级留草稿、三端任意一端指向 dev 在首次写入前拦截、以及 `StrictHostKeyChecking=yes` 对未知/变更主机密钥拒绝连接。

---

## 四、实施顺序（待你看完 Demo 确认后按此推进）

1. 先完成并验收「右侧栏折叠 + 全屏阅读」（待你确认下方 HTML Demo 效果后正式改 5 个共享文件）；
2. 接着按本方案实施 **P0-1（公开 API 与推荐块权限）** 并跑通定向测试；
3. 再按本方案实施 **P0-2（ETL 迁移保护与三端锁定）** 并跑通定向测试。
