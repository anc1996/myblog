# myblog 详情页三栏布局与侧栏互动后端方案（RFC）

- **状态**：**Phase A 与 Phase B 均已编码落地，开发库 `.env.local` 已完成 `site_settings.0003` 与 `people.0006` 迁移，并通过 50 项隔离库回归测试及 Playwright 多视口（1440px / 1280px / 1024px / 375px）浅深色真实浏览器核验**（注：「作者发言」Tab 已通过 5 组隔离反例测试，开发库默认保持 `show_public_comments=False` 未开启）。
- **日期**：2026-11-24
- **适用范围**：全站所有继承 `detail_shell.html` 的内容详情页（现有 `BlogPage`、`GuidePage`、`ProjectPage` 及未来新增的任意 `XxPage`）。
- **遵循规范**：严格遵守 `AGENTS.md` 中的四层解耦原则（共享层零具体页面模型导入）、YAGNI 克制原则（不加新库/队列/冗余缓存）与前端样式红线（零模板内联 `style=`、零业务 CSS 裸 `#hex`）。

---

## 一、 目标：要解决什么问题

当前详情页（如 `/tech/postgresql-18-wagtail-8-architecture/`）存在三个实际痛点：

1. **宽屏利用率低且顶部导航挤压换行**：全站导航与 `.detail-shell` 限制在 `1120px / 1180px` 居中，在 1366px/1440px+ 桌面屏两侧留白过大，导航项出现折行。
2. **详情页缺少通用阅读延续与侧栏互动能力**：缺少跨页面类型通用的「上一篇 / 下一篇」、右侧标签卡、站点公告 / 同栏推荐、以及作者名片卡（头像、核验标识、简介、本月创作热力图、`作者好文 / 作者发言` Tab）。
3. **后端共享层存在隐藏的数据与权限缺口**：现有标签索引查找存在跨站回退、搜索副本移动后 `site_id` 可能过期、隐藏根评论不会级联隐藏子回复、且缺少安全的只读公告配置模型。

本方案将上述前端排版与后端共享能力一次梳理清楚，拆分为两步小步交付：
- **Phase A（先做）**：宽屏三栏自适应布局 + 导航防换行 + 同父栏目「上一篇 / 下一篇」+ 右侧「本文标签卡」+「站点公告 / 同栏推荐」（含 1 张最小公告配置表）。
- **Phase B（后做）**：右侧「作者名片卡」（头像、核验署名、简介、本月创作热力图、`作者好文 / 作者发言` Tab，含 1 个默认关闭的公开评论聚合开关字段）。

---

## 二、 前端布局架构与响应式契约

已确认采用 **方案 A**：
- **中间正文区底部、评论区上方**：保留四档文章评价挂件（`{% analytics_rating_widget page %}`，全页严格唯一 DOM 实例，避免 JS 冲突）与「上一篇 / 下一篇」卡片；
- **左侧固定栏**：可折叠树形目录大纲（`detail-toc-aside`，宽 `240px`，收起态 `56px`）；
- **右侧固定栏（`310px`）**：依次放置「作者名片卡（Phase B）」「本文标签卡（Phase A）」「站点公告 / 同栏推荐（Phase A）」。

### 2.1 三栏与窄屏网格拓扑（解决移动端评论顺序陷阱）

为避免手机/平板下侧栏卡片被压到几十条评论之后，且不复制两份标签/作者 HTML，将 `.detail-comments-wrap` 从 `<article class="detail-main">` 内部提升为 `.detail-shell` 的独立 Grid 区域（`grid-template-areas`）：

```text
【桌面宽屏 >= 1280px】容器 max-width: min(1480px, calc(100vw - 40px))
+-------------+------------------------------+--------------------+
|  左栏 TOC   |  中间正文 (.detail-main)     |  右栏 (.detail-    |
|   240px     |  面包屑/标题/正文/四档评价/  |  right-aside)      |
|  (可收起    |  上一篇·下一篇               |   310px (sticky)   |
|   至 56px)  +------------------------------+  - 作者名片(Ph B)  |
|  (sticky)   |  评论区 (.detail-comments)   |  - 本文标签卡      |
|             |  (与正文左对齐、同宽)        |  - 公告/同栏推荐   |
+-------------+------------------------------+--------------------+

【中等屏 1025px – 1279px】两栏（左 TOC + 右主列）
  左栏 TOC (240px) | 正文+评价+上下篇 -> 右侧栏卡片横排/叠放 -> 评论区

【平板/手机 <= 1024px】单栏 + 顶部粘性目录条/<dialog>抽屉
  顶部粘性目录按钮 -> 正文+评价+上下篇 -> 右侧栏卡片 -> 评论区
```

### 2.2 四态网格矩阵（防短文章/无侧栏破版）

`detail_shell.html` 根节点按真实数据输出 `.has-toc` / `.no-toc` 与 `.has-right` / `.no-right`，在 `myblog/static/css/detail-layout.css` 明确覆盖全部状态：

| 状态组合 | `>= 1280px` 桌面列宽 | 说明 |
| :--- | :--- | :--- |
| `has-toc` + `has-right` | `240px minmax(0, 1fr) 310px` | 标准三栏；TOC 收起时变为 `56px minmax(0, 1fr) 310px` |
| `no-toc` + `has-right` | `minmax(0, 1fr) 310px` | 短文章无标题目录时自动变为优雅两栏 |
| `has-toc` + `no-right` | `240px minmax(0, 1fr)` | 预览态或无任何右侧卡片时保持两栏（收起态 `56px minmax(0, 1fr)`） |
| `no-toc` + `no-right` | 单栏居中 `max-width: var(--reading-width)` | 极简正文居中阅读 |

### 2.3 顶部导航与样式红线

- `myblog/static/css/myblog.css`：将 `.site-nav-container` 最大宽度同步放宽至 `min(1480px, calc(100vw - 40px))`，为 `.site-nav .nav-link`、`button` 与 `.site-brand` 增加 `white-space: nowrap; flex-shrink: 0;`，防止中等宽屏下导航文字竖向折行。
- 严禁在模板新增内联 `style=`；新增 CSS 全部引用 `myblog/static/css/themes/tokens.css` 语义变量（如 `--color-surface`、`--color-border`、`--color-primary-soft`、`--cal-heat-1..4`），并顺手把 `detail-layout.css`（`.detail-badge.is-topic` / `.is-status` / `.tutorial-lead-card .lead-title`）与 `myblog.css`（`.nav-badge`）中残留的旧裸 `#hex` 替换为语义变量。

---

## 三、 后端共享层设计与安全契约

所有共享逻辑统一按职责归口，**严禁在共享应用中导入 `BlogPage`、`GuidePage`、`ProjectPage` 或硬编码页面类名**（继续受 `tests/test_shared_layer_decoupling.py` AST 守卫强制约束）。

### 3.1 全站统一站点核验与 URL 解析基线（通用防线）

针对已查明的两个源码隐患（`projection.py` 移动子树不更新 `site_id` 且 `url_path` 硬截断 `/home/`；`eligibility.py` 不校验站点归属），所有新增查询统一遵循三条底线：

1. **站点一致性校验**：参考 `analytics_tags.py` 的 `_resolve_site_readonly(request, page)`，同时检查 `Site.find_for_request(request)` 与 `page.get_site()`；若两者不为空且 `site.id` 不一致，直接返回空结果，不跨站吐数据。
2. **以当前 `Site.root_page.path` 为准**：查询 `PublishedSearchDocument` 时，除经过 `apply_public_eligibility_filter`（已核验 `live=True`、`alias_of_id__isnull=True`、`live_revision_id`、回收站路径与 `PageViewRestriction` 路径）和 `authored_on__lte=site_today` 外，**必须显式附加 `root_path__startswith=site.root_page.path`**，杜绝跨站移动后旧 `site_id` 导致的泄漏。
3. **真实 URL 由 `wagtailcore.Page` 解析**：对最终筛出的极少量目标页（上下篇 ≤2、推荐 ≤4、作者好文 ≤5），批量通过 `Page.objects.filter(id__in=...)` 调用 Wagtail 原生 `p.get_url(request)` 生成链接（零 MongoDB 读取），不直接信任 `PublishedSearchDocument.url_path`。

### 3.2 Phase A 模块：上下篇、标签卡、站点公告与同栏推荐

#### A1. 「上一篇 / 下一篇」共享查询（零迁移）
- **归属模块**：`myblog/apps/discovery/detail_navigation.py`（新增纯只读共享模块，由 `content_blocks/rendering/detail.py` 调用）。
- **候选范围**：
  - 同一站点（`root_path__startswith=site.root_page.path`）；
  - **同一直接父栏目**：用 Treebeard 路径前缀（Wagtail 树每层固定 4 字符，`parent_path = current_path[:-4]`）限定 `root_path__startswith=parent_path` 且 `depth=current_depth`；
  - **只收录内容详情类型**：强制过滤 `type_key__in=get_archive_enabled_type_keys()`，彻底排除同级子栏目页（`BlogIndexPage`、`GuideIndexPage`、`PortfolioIndexPage`、`TagIndexPage`），同时支持同一父栏目下跨内容类型平滑衔接；
  - 当前页自身必须先命中与当前 `live_revision_id` 匹配的合格 `PublishedSearchDocument`（取权威 `(authored_on, page_id)` 键）；若当前页无合格投影，直接返回 `prev=None, next=None`，不回退到草稿或旧投影。
- **排序与平局消解**：按 `(authored_on, page_id)` 严格全序（旧 → 新）：
  - **上一篇（较旧）**：`Q(authored_on__lt=cur_date) | Q(authored_on=cur_date, page_id__lt=cur_id)`，按 `-authored_on, -page_id` 取第 1 条；
  - **下一篇（较新）**：`Q(authored_on__gt=cur_date) | Q(authored_on=cur_date, page_id__gt=cur_id)`，按 `authored_on, page_id` 取第 1 条；
  - 首尾缺一侧不循环，返回 `None`。

#### A2. 右侧「本文标签卡」（零迁移）
- **归属模块**：修复 `myblog/apps/content_blocks/rendering/detail.py` 中的 `_resolve_tag_index_page(request, site)` 与 `resolve_tag_links`。
- **规则修正**：
  - 取消原第 267 行的全局回退（`return qs.first()`）；
  - 仅查找当前站点子树（`descendant_of(site.root_page, inclusive=True)`）下 `live=True`、`alias_of_id__isnull=True` 且通过 `get_public_blocked_page_ids` 核验的 `TagIndexPage`；
  - 本站无合法公开 `TagIndexPage` 时，所有标签安全降级为纯文本 `#标签名`（`url=None`）；
  - 文章无标签时不渲染标签卡，不留空盒子。

#### A3. 「站点公告」与「同栏推荐」互斥槽位（新增 1 张最小设置表）
- **新模型 `SiteAnnouncementSettings`**（`myblog/apps/site_settings/models.py`，下一次迁移 `site_settings.0003`）：
  - **直接继承 Wagtail 原生 `wagtail.contrib.settings.models.BaseSiteSetting`**（按站点隔离，**绝不继承**项目里会递增 `ContentDiscoveryEpoch` 且全局共用的自定义 `BaseSiteSetting`）；
  - 最小字段：
    - `is_enabled`（`BooleanField(default=False)`，默认关闭）
    - `title`（`CharField(max_length=80, blank=True)`，纯文本标题）
    - `body`（`TextField(max_length=500, blank=True)`，纯文本正文，不接收 HTML）
    - `link_label`（`CharField(max_length=40, blank=True)`，可选链接按钮文案）
    - `target_page`（`ForeignKey("wagtailcore.Page", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")`，站内页选择器）
    - `external_url`（`URLField(blank=True)`，仅允许 `https://` 绝对外链，表单 `clean()` 强校验；同时填站内页与外链时优先站内页或在 `clean()` 提示二选一）
    - `starts_at` / `ends_at`（`DateTimeField(null=True, blank=True)`，展示窗口为左闭右开 `[starts_at, ends_at)`，`clean()` 校验 `starts_at < ends_at`）
    - `updated_at`（`DateTimeField(auto_now=True)`）
  - **只读读取契约**：提供 `SiteAnnouncementSettings.get_active_for_site_readonly(site, request=None, now=None) -> dict | None`：
    - 纯 `filter(site_id=site.id).first()`，空表或未开启或不在 `[starts_at, ends_at)` 内直接返回 `None`，**绝不写库**；
    - **严禁模板通过 `settings.site_settings.SiteAnnouncementSettings` 访问**（防止 `base.html` 中的 `{% get_settings %}` 触发隐式 `get_or_create`）；
    - 若配置了 `target_page`，展示前复核该页 `live=True`、`live_revision_id__isnull=False`、`alias_of_id__isnull=True`、归属当前 `site.root_page.path` 且不在 `get_public_blocked_page_ids` 黑名单内；校验不通过或外链非 `https://` 时，降级为无链接纯文本公告，不报错不泄露。
- **同栏推荐（公告未激活时自动补位，零迁移）**：
  - 规则：当 `announcement` 为 `None` 时触发，最多返回 **4 条**；
  - 优先级 1（同栏最新）：在同站点 + 同直接父栏目 + `type_key__in=get_archive_enabled_type_keys()` + 公开资格过滤下，排除当前页与已展示的上下篇，按 `-authored_on, -page_id` 取最新；
  - 优先级 2（同标签补齐）：若不足 4 条且当前页存在已发布标签（取自当前 `PublishedSearchDocument.search_tags`），在同站点 + `type_key__in=get_archive_enabled_type_keys()` + 公开资格过滤下按共享标签文章补齐去重，跨栏目补位条目标注 `source="tag"`（前端微标「同标签推荐」）；
  - 若最终仍为 0 条，不渲染推荐卡片。

### 3.3 Phase B 模块：作者名片卡、本月热力图与 `作者好文 / 作者发言` Tab

#### B1. 作者名片与多作者策略
- **归属模块**：`myblog/apps/people/sidebar.py`（新增纯只读聚合模块）。
- **署名口径**：基于 `detail.py` 现有公开署名解析（优先匹配当前 `live_revision_id` 的 `PublishedSearchAuthor`，去重后主笔 `primary` 优先，同级按 `sort_order, id`）。
- **系统佚名（`is_system_anonymous=True`）**：正文 meta 正常显示「佚名」，右侧栏**不渲染**作者名片卡、认证徽章、热力图和 Tab；GET 请求绝不创建佚名记录。
- **多作者极简展示（零新增 API）**：右侧栏完整展示第 1 位主笔的名片、热力图与 `作者好文 / 作者发言` Tab；若文章有其他合作作者（且非系统佚名），在名片底部以紧凑头像徽章列出并直链 `/authors/<slug>/`。（若后续需要页内点击切换第 2 位作者热力图，可平滑追加单个只读接口，无需改动主结构。）
- **核验徽章**：当且仅当 `person.linked_user_id` 非空、非系统佚名、且 `linked_user.is_active=True` 时，显示「站点核验署名」徽章；**严禁输出**后台 `username`、账号邮箱或权限角色。头像仅在 `avatar.visibility == PUBLIC` 时通过 `/media-gateway/image/<public_id>/` 输出，否则显示首字占位徽章。

#### B2. 本月创作热力图（零迁移、首版零缓存直查 SQL）
- **统计口径**：统计该作者在**当前站点**下、公开合格、且 `authored_on` 落在 `[本月1日, site_today]` 内的去重作品数（`COUNT(DISTINCT document_id)`），纳入角色为主笔与共同作者（排除仅审校/佚名）。
- **查询实现**：单条 ORM 聚合查询 `PublishedSearchAuthor` 关联 `PublishedSearchDocument`（施加 `apply_public_eligibility_filter`、`root_path__startswith=site.root_page.path`、`type_key__in=get_author_enabled_type_keys()`、`authored_on__gte=month_start`、`authored_on__lte=site_today`），按 `document__authored_on` 分组，最多返回 **31 行**。
- **为什么首版不做缓存（YAGNI）**：现有 `discovery/signals.py` 各监听器内部存在异常捕获降级，不能假设 Epoch 在所有失败路径下都强一致；单作者单月聚合本身极轻量，直查数据库最简单且无权限撤销延迟。前端复用 `tokens.css` 既有的 `--cal-heat-1..4` 令牌渲染当月小日历网格，无文章日期填 0 档，未来日期置灰留空。

#### B3. `作者好文` Tab（零迁移）
- **口径**：首版取该作者在当前站点下**最近 5 篇**公开作品（`authored_on DESC, page_id DESC`），排除当前正在阅读的文章，跨所有声明支持 `"author"` 维度的已注册类型；URL 由 `Page.get_url(request)` 统一解析。
- **克制边界**：首版不关联 `PageAnalyticsTotal` 或文章评分表，避免新站冷启动无数据或意外泄露未公开统计。

#### B4. `作者发言` Tab（`people.Person` 新增 1 个布尔字段）
- **最小模型改动**（`myblog/apps/people/models.py`，下一次迁移 `people.0006`）：
  - 新增 `show_public_comments = models.BooleanField(default=False, verbose_name="允许在作者名片公开聚合历史评论", help_text="仅对已绑定后台账号的非佚名作者生效；开启后将聚合该账号在公开页面下仍公开可见的历史评论。")`；
  - 权限与不变量（写入 `Person.clean()` 与 `Person.save()`）：
    1. 面板配置为 `FieldPanel("show_public_comments", permission="identity.change_user")`；
    2. 若 `is_system_anonymous=True` 或 `linked_user_id is None`，强制归零 `show_public_comments = False`；
    3. **防换绑越权**：在 `Person.save()` 中对比数据库旧 `linked_user_id`，只要发生绑定账号变更或解绑，自动把 `show_public_comments` 复位为 `False`，必须由管理员确认后重新开启。
- **严格 SQL 过滤契约**（`myblog/apps/comments/services.py` 新增只读查询方法 `get_public_comments_for_user(user_id, site, request, limit=5)`）：
  - 前置门禁：仅当 `not person.is_system_anonymous`、`person.linked_user_id` 非空且 `person.show_public_comments is True` 时才执行查询（绝不按昵称/访客名匹配）；
  - 评论自身条件：`author_id=person.linked_user_id`、`status="active"`、`is_publicly_visible=True`、`approved_version_id__isnull=False`；
  - **防止隐藏线程侧漏（核心 P0）**：必须叠加 `Q(parent__isnull=True) | Q(parent__status="active", parent__is_publicly_visible=True)`（管理员隐藏/删除根评论下的子回复 100% 屏蔽；作者自删根下的回复默认不计入聚合，保持口径干净）；
  - **所属页面公开资格（含祖先回收/权限/站点/别名）**：先取最近候选批次（例如有界 `[:25]` 按 `-created_at, -id`）关联 `page_id / page__path / page__live / page__live_revision_id / page__alias_of_id`，过滤 `page__live=True`、`page__live_revision_id__isnull=False`、`page__alias_of_id__isnull=True`、`page__path__startswith=site.root_page.path`，再对候选调用 `get_public_blocked_page_ids` 一次性剔除自身或祖先存在 `RecycleEntry` / `PageViewRestriction` 的页面；若对应页面在 `PublishedSearchDocument` 中存在未来日期（`authored_on > site_today`），一并排除；最终截取前 **5 条**；
  - **安全摘要**：严禁复用 `Comment.content_snippet`（会优先读待审原文）；必须批量读取 `approved_version.safe_html`，使用 HTML 文本提取去除标签、压缩空白、截断约 120 字符后交给模板自动转义输出；链接指向当前合格页面的 `page.get_url(request)` + `#comment-<id>`。
- **索引策略**：首版**零新增索引迁移**；后续若真实 PostgreSQL `EXPLAIN ANALYZE` 证明存在瓶颈，再补局部索引 `(author_id, created_at DESC, id DESC) WHERE status = 'active' AND is_publicly_visible = TRUE AND approved_version_id IS NOT NULL`。

### 3.4 预览态严格隔离（Phase A + B 通用）

当 `request.is_preview` 或 `request.in_preview_panel` 为真时：
- 保留正文、草稿自身标题目录、草稿标签与草稿署名文本；
- **跳过全部动态侧栏查询**：上下篇、站点公告、同栏推荐、作者热力图、`作者好文 / 作者发言` 统一返回空，避免后台草稿预览触发冗余查询或越权拉取数据。

---

## 四、 分批实施清单（供 `gemini-4-hydrogen` 执行）

### Phase A：三栏自适应布局 + 导航防换行 + 上下篇 + 标签卡 + 公告/同栏推荐
- **修改/新增文件**：
  1. `myblog/apps/site_settings/models.py` + `migrations/0003_siteannouncementsettings.py`（新增 `SiteAnnouncementSettings` 逐站只读设置模型）；
  2. `myblog/apps/discovery/detail_navigation.py`（新增：同父栏目且仅详情类型的上下篇与同栏/同标签推荐只读服务）；
  3. `myblog/apps/content_blocks/rendering/detail.py`（修复 `_resolve_tag_index_page` 站点与公开资格安检，组装 `prev_page / next_page / announcement / recommendations / has_right_sidebar`）；
  4. `myblog/templates/detail/detail_shell.html` 及新增片段 `_detail_prev_next.html`、`_detail_right_sidebar.html`（调整 `.detail-comments-wrap` 网格区域，保留唯一 `analytics_rating_widget` 实例）；
  5. `myblog/static/css/myblog.css`、`myblog/static/css/detail-layout.css`（放宽容器、导航防换行、四态三栏网格与窄屏顺序、清理旧裸 `#hex`）；
  6. `tests/test_detail_phase_a_sidebar.py`（新增定向回归测试）。
- **迁移授权**：生成 `site_settings.0003` 后，按 `AGENTS.md` 规范在开发库 `.env.local` 执行 `migrate` 前向用户确认。

### Phase B：作者名片卡 + 本月热力图 + `作者好文 / 作者发言` Tab
- **修改/新增文件**：
  1. `myblog/apps/people/models.py` + `migrations/0006_person_show_public_comments.py`（新增 `show_public_comments` 字段与换绑/解绑自动归零不变量）；
  2. `myblog/apps/comments/services.py`（新增 `get_public_comments_for_user` 严格线程/站点/祖先权限过滤与 `approved_version.safe_html` 纯文本截断）；
  3. `myblog/apps/people/sidebar.py`（新增：主笔名片、合作者徽章、本月 SQL 热力图与最近 5 篇跨类型好文）；
  4. `myblog/apps/content_blocks/rendering/detail.py`、`myblog/templates/detail/_detail_author_card.html`、`myblog/templates/detail/_detail_right_sidebar.html`、`myblog/static/css/detail-layout.css`（Tab 切换复用 Alpine.js 或极简无障碍按钮，零内联 `style=`、热力格复用 `--cal-heat-1..4`）；
  5. `tests/test_detail_phase_b_author_card.py`（新增定向回归测试）。

---

## 五、 怎么验证（验收标准）

### 5.1 自动化测试门禁（WSL2 `wagtailblog-test` 环境）
1. **架构与静态守卫**：
   - `tests/test_shared_layer_decoupling.py`（确认共享层 0 具体 Page 导入）；
   - `tests/test_theme_system_stage2.py`、`tests/test_theme_system_stage3.py`（确认零内联 `style=`、零裸 `#hex`）；
   - `MYBLOG_ENV_FILE=.env.test /root/anaconda3/envs/wagtailblog-test/bin/python -B manage.py check` 与 `makemigrations --check --dry-run`。
2. **Phase A 必测用例**：
   - 上下篇同日 `authored_on` 按 `page_id` 稳定打破平局，首尾缺项隐藏；
   - 同级存在 `BlogIndexPage` / `GuideIndexPage` / `TagIndexPage` 时**绝不**混入上下篇或推荐；
   - 跨站移动子树（即使 `PublishedSearchDocument.site_id` 未同步）、受 `PageViewRestriction` 限制、已回收或未来成文日期文章均被拦截；
   - 公告空表 GET **零写入**，起止时间 `[starts_at, ends_at)` 边界生效，失效站内页与非 `https://` 外链降级为纯文本，公告有效时隐藏推荐、关闭时展示去重后的同栏/同标签推荐；
   - 标签卡本站无公开 `TagIndexPage` 时降级纯文本且不跨站泄漏；预览态跳过全部动态侧栏查询。
3. **Phase B 必测用例**：
   - 佚名与未绑定账号表现正确；`linked_user` 变更/解绑后 `show_public_comments` 自动归零；停用账号隐藏核验徽章；
   - 本月热力图跨月界、排除审校与未来日期、同日多篇聚合准确；
   - `作者发言` 必测 4 类防漏反例：① 管理员隐藏根评论后其子回复**不可见**；② 待审编辑版本 V2 **绝不覆盖**已批准 V1 摘要（不读 `content_snippet`）；③ 祖先回收/受限页面评论**不可见**；④ 未开启 `show_public_comments` 时即使已绑定 `linked_user` 也不展示发言 Tab。

### 5.2 真实浏览器多视口核验（Playwright）
- 使用 `waitUntil: 'load'` 检查 `1440px`、`1280px`、`1024px`、`375px` 四档视口 × `mist-blue` / `moss-lab` 浅深主题：
  - 导航栏文字零折行、全页零横向溢出（`scrollWidth <= innerWidth`）；
  - 桌面三栏对齐、左栏目录收起平滑、无目录文章自动切两栏；
  - 手机/平板下顺序为「正文 + 评价 + 上下篇 → 右侧卡片 → 评论区」，四档评价挂件全页仅 1 个且点击/取消正常。

---

## 六、 还没解决什么与后续扩展

1. **历史跨站移动 `site_id` 同步**：本方案在查询层通过 `root_path__startswith=site.root_page.path` 兜底彻底阻断跨站泄漏；若未来扩展多站点运营，可顺手在 `projection.py` 的 `sync_subtree_tree_metadata` 补上同步重算 `site_id`。
2. **多作者页内切换动态热力图**：Phase B 首版采用“主笔完整名片 + 合作者直跳 `/authors/<slug>/`”，若后续确有高频多作者同页对比需求，再追加轻量只读 JSON 接口，不在当前版本提前加接口面。
3. **`作者好文` 按阅读量/口碑加权排序**：待站点积累足够阅读与四档评价样本后，再在 `people/sidebar.py` 引入可控门槛切换，不把零样本文章误判为低质量。
