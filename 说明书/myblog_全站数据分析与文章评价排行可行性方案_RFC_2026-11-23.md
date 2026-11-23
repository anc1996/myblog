# myblog 全站数据分析与文章评价排行可行性方案（务实定稿版）

**日期：2026-11-23｜状态：已吸收审查反例并完成务实瘦身，待用户最终确认；尚未编写业务代码或执行迁移。**
对应需求规格：[myblog_全站数据分析指标与排行精选需求规格说明书_2026-11-23.md](myblog_全站数据分析指标与排行精选需求规格说明书_2026-11-23.md)（`D1–D6` 全选 A，含四档文章评价与高浏览降噪）。

---

## 1. 目标、设计原则与系统边界

### 1.1 目标
为 `myblog` 建立一套**不绑定具体博客模型、不拖慢公开文章访问、适配本地 WSL2 非 24 小时开机环境**的后台数据分析、四档读者评价、榜单降噪与编辑精选系统，覆盖 `BlogPage`、`GuidePage`、`ProjectPage` 以及未来新增的 `XxPage`。

### 1.2 核心设计原则（为什么这样设计）
1. **统计是近似运营数据，公开权限与精选才是强一致红线**：不为了消除几十毫秒内多记 1 次 PV 的极小概率去深度魔改 Wagtail 8.1 十几个原生写视图或加全站大锁；公开投放和权限靠“**读侧按 PostgreSQL 主库实时复核**”守死底线。
2. **页面渲染不写统计 SQL**：浏览器拿到文章 HTML 后再发轻量异步请求上报浏览与阅读深度；Redis 挂了或慢了，文章照常秒开，绝不回退到逐请求写 PostgreSQL。
3. **支持多进程、支持随时开关机补跑**：不依赖单进程内存状态，也不死卡凌晨 02:00 才允许结算；WSL2 笔记本休眠后次日开机，仍可在 48 小时 Redis 窗口内安全补结前一日数据。
4. **高浏览不等于高质量**：用“四档评价（优秀/良好/及格/差）+ 贝叶斯平滑与 Wilson 下界 + 独立低完读/高短读预警”共同识别标题党和水文，同时把冷启动阈值做成后台可配，避免小站初期规则永远不触发。

### 1.3 应用与存储边界
新增独立 Django App：`myblog/apps/analytics/`（不写进 `blog`，也不让 `discovery` 反向依赖 `analytics`）。

| 模块 / 存储 | 负责什么 | 明确不负责什么 |
| --- | --- | --- |
| `analytics` + PostgreSQL | 保存站点配置、日聚合、累计总表、四档当前票与分档统计、精选候选与审批、榜单快照、异常告警 | 不修改 `Page.live`、`live_revision_id`、`Page.owner` 或 Wagtail 审批工作流 |
| Redis（独立前缀 `analytics:`） | 短期阅读会话、当日 UV 去重集合（TTL ≤48h）、PV/阅读待结算桶、评价防刷滑动窗口限流、短期展示 ID 缓存 | 不作为投票真值来源，不作为公开权限或精选审批依据 |
| `discovery` | 在 `TypeAdapterContract` 扩展只读的 `AnalyticsCapabilities` 声明，标识某类页面支持哪些指标 | 搜索投影和 `IndexListingService` 不替代主库公开资格校验 |
| `content_lifecycle` / `identity` | 决定页面公开、回收、彻底销毁（Purge）和账号生命周期；提供 `get_public_blocked_page_ids()` 公开校验和销户事务入口 | 不在普通发布事务内同步调用 Redis 网络 IO 或重算全站榜单 |
| `home` / 各栏目页 | 首页轮播、精选指南、精选项目和栏目精选位统一调用 `RecommendationService` 读取编辑批准池 | 不再把 `-authored_on` / `-last_published_at` 最新切片伪装成精选 |
| MongoDB / RustFS | 继续存不可变正文版本与私有媒体文件 | 不写入任何访问、评价或排行数据；SQL 回滚不删外部对象 |

---

## 2. 多页面统一接入契约

### 2.1 `AnalyticsCapabilities` 能力声明
在 `myblog/apps/discovery/contracts.py` 增加纯数据结构 `AnalyticsCapabilities`（无数据库和网络 IO），各业务 App 在自己的 `TypeAdapter` 中按需声明 `analytics_capabilities`：

```python
@dataclass(frozen=True)
class AnalyticsCapabilities:
    kind: str  # "detail"（内容详情）或 "entry"（聚合入口）
    metric_keys: tuple[str, ...] = ()          # 如 ("pv", "uv", "read_depth", "code_copy")
    conversion_keys: tuple[str, ...] = ()      # 如 ("step_complete", "doc_download", "repo_click")
    rating_enabled: bool = False               # 是否支持四档文章评价（entry 必须为 False）
    rank_eligible: bool = False                # 是否参与榜单（entry 必须为 False）
    curation_eligible: bool = False            # 是否参与精选候选（entry 必须为 False）
```

- **详情页（`BlogPage`、`GuidePage`、`ProjectPage` 及未来 `XxPage`）**：只要继承 `CustomBasePage`、使用 `detail_shell.html` 并声明 `AnalyticsCapabilities(kind="detail", ...)`，即可自动接入流量、阅读深度、四档评价与精选，公共代码里零 `if isinstance(..., BlogPage)` 分支。
- **聚合入口（`HomePage`、`BlogIndexPage`、`GuideIndexPage`、`PortfolioIndexPage`、`TagIndexPage`、作者专栏、站内搜索）**：`HomePage` 继续直接继承 Wagtail `Page`（不改继承关系），通过入口注册表按固定 `entry_kind`（如 `home`、`blog_index`、`tag_index`、`author_detail`、`search`）记录入口 PV、日 UV、卡片曝光与进入详情导流，不计完读率，也不和详情页竞争榜位。

### 2.2 统一正文阅读边界与四档评价挂载点
在共享详情外壳 `myblog/templates/detail/detail_shell.html` 中：
- **阅读进度与完读区间**：固定为 `#article-start`（已有 `<section id="article-start" class="detail-body-section">`）到紧接 `detail_body` 末尾新增的零高度锚点 `<div id="article-end" aria-hidden="true"></div>`。
  - 计入范围：正文 StreamField 中的段落、代码块、Mermaid、图片和指南步骤；
  - 不计入范围：博客顶部的 `detail_intro`、指南摘要、`detail_after_body` 中的独立图集/相关指南、标签和评论区；
  - 边界兜底：短正文（首屏即可见 `#article-end`）按停留时长判定，不按“打开即完读”；缺少 `#article-start` 或 `#article-end` 标记的自定义模板只记合格 PV，不计完读、不发评价阅读凭证。
- **四档评价挂件位置**：放在 `#article-end` 之后、`detail_after_body` 与评论区之上（独立于 `allow_comments` 开关）：
  - 按钮为带清晰中文文字的表情项：**🌟 优秀**、**👍 良好**、**👌 及格**、**👎 差**，使用 `aria-pressed` 支持无障碍与键盘操作；
  - 非公开页、后台预览页、回收站提示页不渲染；
  - 首发面向有效登录会员，未登录游客看到按钮禁用态与登录引导；页面首次加载不公开显示总票数和口碑分（只通过私有接口回显登录者本人当前所选档位）。
- **课件下载归因**：复用现有 `/media-gateway/document/<uuid>/` 授权交付成功分支，结合当前 `page_id`、`live_revision_id`、附件 ID 与一次性的下载动作 ID 去重，自动合并断点续传（`Range`）与重试；鉴权失败、`HEAD` 和未授权请求不计下载。

---

## 3. PostgreSQL 数据模型（收敛为 10 张核心表）

为避免表结构过度碎片化，全部需求收敛为 **第 1 期 5 张核心表 + 第 2 期 5 张核心表**（均用非负整数计数，带 `CheckConstraint` 防止负数漂移；所有页面级表使用真实 `ForeignKey(Page, on_delete=models.CASCADE)`）。

### 第 1 期：流量采集与基础报表（5 张表）

| 模型名 | 核心字段与唯一约束 | 作用说明 |
| --- | --- | --- |
| `AnalyticsSettings` | Wagtail `BaseSiteSetting`（每站点 1 行）：`collection_enabled`、`public_pv_display_enabled`（默认 False）、`rating_enabled`、`ranking_enabled`、`min_rating_votes`（默认 20）、`observe_rating_votes`（默认 5）、`high_pv_min`（默认 300）、`min_healthy_days_in_30d`（默认 25） | 站点级总开关与冷启动可调参数，避免写死常数 |
| `PageAnalyticsDaily` | 唯一 `(site, page, stat_date, live_revision_id)`：`type_key`、`column_page_id`（记录当时所属栏目）、`pv`、`daily_uv`、`measurable_views`、`valid_views`、`completed_views`、`short_bounce_views`、`total_active_seconds`、`organic_pv`（剔除精选/榜单导流）、`conversions_json`（有限类型转化计数）、`health_status`（`normal / partial / missing`） | 单页按日按版本明细（保留 13 个月）；`page` 用 `CASCADE` 随 Purge 自动清理 |
| `PageAnalyticsTotal` | 唯一 `(site, page)`：`total_pv`、`total_visitor_days`（每日 UV 之和，明确不是跨日独立人数）、`total_valid_views`、`total_completed_views`、`last_settled_date`、`updated_at` | 页面累计总表（保留至 Purge）；查询卡片直接读此表，零 N+1 |
| `ScopeAnalyticsDaily` | 唯一 `(site, stat_date, scope_kind, scope_key)`（`scope_kind` 为 `site / type / entry / column / safe_search_term`）：`pv`、`daily_uv`、`entry_clicks`、`health_status` | 存放全站、分类型、入口导流和命中安全词表的热门/无结果搜索词计数；**不含 `page_id`**，历史站点总量不会泄露已 Purge 页面 |
| `AnalyticsBatchReceipt` | 唯一 `batch_id`：`payload_hash`、`batch_kind`（`pv_reading / daily_uv`）、`settled_at`、`expires_at`（保留 7 天） | 批次幂等收据表（防止 worker 重试或 Redis ACK 丢失导致重复记账） |

### 第 2 期：四档评价、降噪告警与精选投放（5 张表）

| 模型名 | 核心字段与唯一约束 | 作用说明 |
| --- | --- | --- |
| `ArticleRating` | 唯一 `(page, user)`：`site`、`page`（`CASCADE`）、`user`（**`PROTECT`**）、`live_revision_id`、`grade`（`excellent / good / pass / poor / null`）、`trust_tier`（`member / new_member`）、`first_eligible_day`（首次有效投票日期，切换/撤票重投不刷新）、`version`（单调递增版本号）、`updated_at` | 评价真值表；撤票置 `grade=null` 保留行以防重投刷日期；`user` 用 `PROTECT` 防止误删账号漏扣票 |
| `ArticleRatingAggregate` | 唯一 `(site, page, live_revision_id)`：`excellent_count`、`good_count`、`pass_count`、`poor_count`（均带 `>= 0` 约束）、`rating_evidence_version`、`updated_at` | **仅代表当前仍指向该版本的现存有效票分布**（跨版改投或销户会同步扣减旧版计数，不冒充不可变历史快照） |
| `ArticleRatingReceipt` | 唯一 `(page, user, request_id)`：`request_hash`、`result_version`、`created_at`（定期清理 >24h 记录） | 评价写请求幂等表，防止前端双击或网络重试重复处理 |
| `CuratedRecommendation` + `CuratedRecommendationAction` | 推荐表唯一 `(site, page, live_revision_id, position)`：`status`（`pending / approved / rejected / withdrawn`）、`priority`、`is_pinned`、`starts_at`、`expires_at`、`rejected_cooldown_until`（拒绝后 14 天冷却期）、`quality_score`、`evidence_json`（计算时的无身份样本/分值/规则版本快照）、`version`；动作表追加记录 `actor`、`action`、`reason`、`created_at` | 精选候选、主编审批与投放位管理；审批证据以 JSON 快照留存，不受后续改票回写影响 |
| `AnalyticsAlert` + `RankingSnapshot` | 告警表唯一 `(site, page, live_revision_id, alert_kind)`：`alert_kind`（`LOW_REPUTATION / HIGH_TRAFFIC_LOW_REPUTATION / HIGH_TRAFFIC_LOW_ENGAGEMENT / TRAFFIC_ANOMALY`）、`status`（`observing / active / exempted / confirmed_demoted / resolved`）、`consecutive_hit_days`、`consecutive_clear_days`、`exempt_until`、`evidence_json`；榜单快照表按 `(site, board_kind, type_key, stat_date)` 保存每日/实时榜单结果 JSON 与规则版本 | 管理降噪状态、滞回天数、主编限时豁免（最长 14 天）与可追溯榜单快照（保留 30 天） |

---

## 4. 适配 WSL2 与多进程的 Redis → PostgreSQL 采集结算

为了兼容**多 worker 无状态部署**以及**本地 WSL2 笔记本非 24 小时开机**，本方案**不使用**每分钟预登记桶或单进程内存清单，改用标准的“Redis 无状态原子写入 + 随时可补跑的幂等批处理”：

### 4.1 前端采样与上下文签发
1. **公开页输出轻量元数据**：规范公开详情页（经 `get_public_blocked_page_ids` 排除私有/回收/别名页）在 HTML 仅输出公开的 `site_id`、`page_id`、`live_revision_id` 与一个短期 HMAC 签名描述，不塞入个人身份或票态。
2. **异步签发阅读令牌**：浏览器页面实际可见（`document.visibilityState === "visible"`）后，异步调用 `POST /api/v1/analytics/context/`：
   - 服务端重新核对当前页面公开资格与 `live_revision_id`（拦截预览、缓存旧页、已回收/设私有页）；
   - 为本次浏览签发 30 分钟有效的 `visit_token`（绑定随机 `visit_id`、当日轮换密钥生成的短期浏览器 HMAC 标识、`site_id`、`page_id`、`live_revision_id` 和进入来源大类）。该接口只读不写 SQL。
3. **阅读心跳与收尾**：页面在可见且有交互时每 10 秒左右上报一次绝对进度（累计活跃秒数上限 1800s、最大滚动百分比、是否触达 `#article-end`），切换标签或离开时通过 `navigator.sendBeacon` / `fetch(..., { keepalive: true })` 补尾。服务端只信任服务器时间，按单调递增序号防重放。

### 4.2 Redis 无状态原子写入（多进程安全）
- `POST /api/v1/analytics/events/` 使用单次 **Redis Lua 脚本**完成检查与写入（任何 Django worker 均可无状态处理）：
  1. 30 秒同站/同页/同访客刷新合并为 1 次 PV；
  2. 更新当前 `visit_id` 的最大滚动、活跃秒数与完读标记（只取更大值，不重复叠加）；
  3. 将增量累加到当日活动 Hash（`analytics:active:pv:{site}:{YYYY-MM-DD}` 等）；
  4. 将短期访客 HMAC 写入当日 UV 去重集合（`analytics:uv:page:{site}:{date}:{page_id}`、`analytics:uv:site:{site}:{date}`、`analytics:uv:type:{site}:{date}:{type_key}` 等），所有 UV 与会话键统一设置 **固定 48 小时过期**（不续期）。
- **来源与搜索词脱敏（D5-A）**：外链 Referrer 只保留主域名或分类，丢弃路径与参数；站内搜索只在查询成功返回后检查是否命中安全技术词表或已发布标签/主题，未命中的只计入 `unknown` 总数，绝不保存原词或可枚举哈希。

### 4.3 随时可补跑的幂等结算（`manage.py settle_analytics`）
结算逻辑全部封装在管理命令 `python manage.py settle_analytics`（生产可用 systemd timer / cron 每 5 分钟跑一次；本地 WSL2 可按需手动或开机触发，并带短冷却锁防止重复并发）：

1. **PV 与阅读滚动结算（随时可跑）**：
   - 用 Redis Lua 原子把当前活动 Hash 重命名为带唯一 `batch_id` 的冻结键（`analytics:sealed:{batch_id}`），新请求继续写入新活动键，零丢数；
   - Worker 查 PostgreSQL 批量核验：取出当前仍存在且通过 `get_public_blocked_page_ids()` 的 `page_id`（已 Purge 或非公开的条目直接丢弃，不复活页面）；
   - 在**单个短 SQL 事务**内：先插 `AnalyticsBatchReceipt(batch_id, payload_hash)`，再按 `page_id` 升序增量更新 `PageAnalyticsDaily` 与 `PageAnalyticsTotal`（不锁全站，只更新本批涉及的行）；
   - **SQL 事务提交成功后**，才删除 Redis 中的冻结键 `analytics:sealed:{batch_id}`。如果提交后删除失败，下次重跑会因 `AnalyticsBatchReceipt` 命中而直接跳过并补删 Redis 键，保证精确一次入库。
2. **日 UV 补跑封账（48 小时窗口内任意时间可跑）**：
   - 对每一个已结束的自然日（`stat_date < today`）且尚未封账的日期，只要仍在 Redis 48 小时 TTL 内（无论是凌晨 02:00 定时任务，还是你第二天早上 09:00 打开笔记本开机触发），读取对应范围的 Redis UV 集合基数（同一页面同日多版本用 `SUNION` 合并，绝不把 V1 UV + V2 UV 相加），一次性写入 `PageAnalyticsDaily.daily_uv` 和 `ScopeAnalyticsDaily.daily_uv`，并按差值 `new_uv - old_uv` 同步更新 `PageAnalyticsTotal.visitor_days`。
3. **故障与停机容错（不卡死 30 天精选）**：
   - Redis 故障时，前台页面与已有精选照常访问，接口快速降级返回，绝不写 SQL；
   - 若因长期关机（超过 48 小时未开机）或 Redis 清空导致某日 UV 丢失，将该日标为 `health_status="missing"`（部分丢失标 `partial`），后台报表清晰注明“当日缺测”，绝不用 `0` 覆盖；
   - 30 天精选候选窗口默认允许最多 5 天缺测（即至少 `min_healthy_days_in_30d = 25` 个健康日，后台可配），计算完读率等比率时只统计健康日，避免笔记本偶发停机导致整月精选停摆。

---

## 5. 四档文章评价与“高浏览勿扰”降噪方案

### 5.1 接口契约、防刷与并发安全
- **读取本人状态**：`GET /api/v1/analytics/pages/{page_id}/rating/`（`Cache-Control: private, no-store`），只返回当前登录用户在这篇文章的 `grade`、`version` 和是否已满足阅读门槛，不写 SQL，不泄露全站票数。
- **提交/切换/撤销**：`POST /api/v1/analytics/pages/{page_id}/rating/`，参数为 `target_grade`（`excellent / good / pass / poor / null`）、`expected_revision_id`、`expected_version`（首次未投传 `0`）、`request_id`、`reading_proof`（撤销可免）。
- **三重防刷门禁**：
  1. **阅读门槛（Reading Gate）**：服务端依据本次登录用户的短阅读会话签发 10 分钟有效的 `reading_proof`，要求**活跃可见停留 ≥ 15 秒，且正文滚动 ≥ 50% 或触达 `#article-end`**（短正文仅需停留 ≥ 15 秒）；
  2. **作者自评拦截**：检查 `Page.owner_id == user.id` 或当前正式版 `PublishedAuthorRef` 关联的 `Person.linked_user_id == user.id`，命中直接拒绝；
  3. **独立 Redis Lua 滑动窗口限流（Fail-Closed）**：不复用评论限流键（避免复用 `comments/rate_limit.py:189` 的明文 IP 键），按 `user_id`（10 次/分）、`user_id + page_id`（4 次/分）与 `HMAC(可信代理IP)`（60 次/分）原子限流；Redis 不可用时评价写接口直接返回 `503`，不降级放行。
- **局部行锁与并发幂等（不抢全站锁）**：
  1. 事务内先查 `ArticleRatingReceipt(page, user, request_id)`：同摘要直接返回原 `result_version`，异摘要返回 `409`；
  2. 锁定当前用户的 `ArticleRating` 行（若不存在且 `expected_version == 0`，在内层 `transaction.atomic()` 保存点执行 `INSERT`，并发首票触发唯一冲突时回滚保存点并重读，返回 `409` 引导前端刷新，绝不把失败的首票误变成切换）；
  3. 核对 `expected_version` 一致后，锁定目标 `ArticleRatingAggregate`（若跨版改投，按 `id` 升序同时锁新旧两行聚合），按净差值更新四档计数（新增新档 `+1`、切换旧档 `-1` 新档 `+1`、撤销旧档 `-1`、相同档位不动），`version` 递增并写入 `ArticleRatingReceipt`；
  4. 账号停用/删除联动：在 `myblog/apps/identity/account_lifecycle.py` 现有 `User -> MemberProfile -> content_mutation_guard()` 事务内、调用 `target.delete()` 之前，按 `page_id` 升序锁定并扣减该用户的全部有效 `ArticleRatingAggregate` 计数，随后删除其 `ArticleRating` 与 `ArticleRatingReceipt`。因外键为 `PROTECT`，任何绕过该服务的删号操作都会被数据库直接拦截。

### 5.2 口碑平滑与“高浏览勿扰”降噪算法

| 档位 | 🌟 优秀 | 👍 良好 | 👌 及格 | 👎 差 |
| --- | :---: | :---: | :---: | :---: |
| 分值 `s` | `1.00` | `0.75` | `0.45` | `0.00` |

1. **分级权重与贝叶斯平滑**：
   - 注册满 7 天的正常会员权重 `w = 1.0`（计入可信样本数 `n`）；注册不足 7 天的新会员权重 `w = 0.5`（仅计入后台观察口碑，不触发也不解除低口碑告警，防批量新小号刷差评或洗好评）；
   - 可信平滑口碑分：`R_trusted = (20 × 0.65 + Σs) / (20 + n)`（加入 20 个 `0.65` 先验虚拟样本，彻底杜绝 1–2 个好评或差评引起剧烈抖动）。
2. **Wilson 下界与低口碑判定**：
   - 设可信样本数 `n` 中“差”票数为 `d`，差评比例 `p = d / n`，95% 置信度（`z = 1.96`）的 Wilson 下界为：
     `W = (p + z²/(2n) - z * sqrt(p(1-p)/n + z²/(4n²))) / (1 + z²/n)`
   - **低口碑条件**（阈值可在 `AnalyticsSettings` 调整，默认保守值）：`n >= 20` 且 `d >= 8` 且 `W >= 0.20` 且 `R_trusted < 0.55`；
   - **高 PV 定义**：近 30 个健康日内，自然流量 `PV >= max(300, 同类型组 P90)`（组内可比公开页 `< 20` 篇时，直接采用 `PV >= 500`）。
3. **热榜降权与滞回防抖**：
   - **滞回规则**：防止 `n=19, d=7 → n=20, d=8` 一票跨线导致榜单天天反复横跳。满足低口碑或高 PV 低口碑条件第 1 天先标 `observing`（后台观察），**连续 2 个健康日**满足才转 `active` 生效；生效后须**连续 3 个健康日**满足恢复条件（`n >= 20` 且 `W < 0.15` 或 `R_trusted >= 0.60`，或高 PV 降至阈值 80% 以下）才自动解除；缺测日不解除活动告警。
   - **热榜降权**：近 7/30 日人气榜和飙升榜基础分乘以惩罚系数 `factor`：
     - 正常文章：`factor = 1.0`；
     - 活动低口碑：`factor = 0.60`；
     - 活动高 PV 低口碑 + 完读率差（完读样本 ≥60 且完读率 `< 35%`，或可判短读样本 ≥60 且短读率 `≥ 55%`）：`factor = 0.35`。
   - **精选阻断**：活动 `HIGH_TRAFFIC_LOW_REPUTATION` 告警直接阻断该版本自动进入精选候选池，并暂停该版本已投放的精选位，交由主编核查（主编可给同版本最长 14 天的限时豁免，或确认永久下榜）。
4. **中小站冷启动防空转机制（两道补充）**：
   - **早期人工观察预警**：当 `n >= observe_rating_votes`（默认 5 票）且差评占比 `>= 40%` 时，虽然不自动降权，但会在后台单篇分析与大盘标出“早期口碑偏低（待观察）”；
   - **独立行为标题党预警（`HIGH_TRAFFIC_LOW_ENGAGEMENT`）**：即使还没有人点评价，只要文章满足高 PV 且有 `>= 60` 个可测阅读样本、同时完读率 `< 35%` 或短读离开率 `>= 55%`，立即触发后台人工预警，提醒主编核查是否为标题党或勿扰流量。

---

## 6. 精选工作台与首页/栏目联动

1. **质量分与自动候选（D3-A）**：
   - `Quality Score = 100 × (0.35C + 0.20T + 0.15L + 0.20I + 0.10F)`：其中 `C` 为平滑完读率，`T` 为按同组参考时长归一化的活跃时长，`L` 为 `1 - 平滑短读率`，`I` 为评论率与可信评价口碑的加权融合（`n < 20` 时评价权重为 `0`），`F` 为指南复核时效分（逾期指南不自动入围）；
   - 自动入围门槛：近 30 日内健康日 ≥25 天、≥100 访客日、≥60 可测访问、覆盖率 ≥60%、≥60 可测完读样本、完读率 ≥50%、`Quality Score >= 70`，且无活动低口碑/违规告警、未处于 14 天拒绝冷却期（`rejected_cooldown_until`）；冷门佳作支持主编手工提名。
2. **首页与栏目逐区替换合同**（对照 `myblog/apps/home/models.py` 与 `home_page.html`）：
   - `featured_carousel_items`（首页轮播，最多 8 条）：改为从 `RecommendationService.for_placement(site, "home_carousel")` 读取跨类型已批准池（有效置顶优先，按优先级+分数补位）；
   - `featured_guides`（最多 4 条）与 `featured_projects`（最多 3 条）：分别读取 `home_guides`、`home_projects` 已批准池；若尚无已批准内容则隐藏该精选块或明确显示“暂无精选”，绝不用未审批最新列表冒充精选；
   - `latest_articles`（最新博客前 6 条）、`columns`、`popular_tags`：保持各自独立语义，但全部补齐**当前站点 + 祖先公开状态（`get_public_blocked_page_ids`）**过滤，修复现有首页查询漏查祖先私有与跨站的问题。

---

## 7. Wagtail 8.1 生命周期、缓存与 Purge 清理（务实安全边界）

本方案**不再**侵入重写 Wagtail 原生十几个写入口或加全站 Advisory Lock，改用分层防御确保零数据泄露、零复活、零死锁：

| 场景 | 具体处理方式 | 安全保障原理 |
| --- | --- | --- |
| **草稿、预览、未生效排期** | 预览和未发布页不输出采集上下文、不渲染评价按钮；已有公开版继续按原 `live_revision_id` 独立统计 | 采集令牌与评价都强绑定 `live_revision_id`，草稿修改不会污染线上数据 |
| **正式发布换版 / 排期上线** | 通过 `page_published` 信号在 `transaction.on_commit()` 清理该页及首页/榜单推荐 ID 缓存；新 `live_revision_id` 自动开启新一轮评价与候选评分 | 公开读侧始终按主库最新 `live_revision_id` 比对，旧版推荐即使缓存未清也会在读侧被瞬间过滤掉 |
| **设私有 / 下架 / 移动 / 回收** | 监听 `page_unpublished`、回收动作及 `PageViewRestriction` 的 `post_save`/`post_delete` 信号在 `on_commit` 清缓存；保留已结算历史统计 | 所有公开投放、榜单与采集签发都在返回前调用 `get_public_blocked_page_ids()` 核验当前站点和整条祖先链，绝不泄露私有或回收页 |
| **别名与复制** | 别名页（`alias_of__isnull=False`）拒绝采集、评价与入榜；复制产生的新 `page_id` 与 `document_uuid` 从零开始 | 不复制任何分析表、评价表或推荐记录 |
| **Purge（彻底销毁）** | 在 `RecycleService.purge_page_or_subtree()` 现有 `content_mutation_guard()` 事务内，所有带 `Page` 外键的分析/评价/推荐/告警表随页面删除一并清空；`transaction.on_commit()` 删除对应 `page_id` 的 Redis 键并失效首页/榜单缓存 | 迟到的 Redis 批次在结算前核验 `Page.objects.filter(id__in=...)`，不存在的页直接丢弃（并捕获并发外键错误），**不可能复活已删页面**；原有 `OutboxEvent` 的 MongoDB 清理合同保持不变 |
| **缓存防脏回填** | Redis 缓存只存候选 `page_id` 列表（不缓存拼好的公开 HTML），且命中后仍须批量查主库核对公开资格与 `live_revision_id` | 即使有旧 worker 在删除缓存后误回填旧 ID，下一次公开读依然会在主库复核阶段将其挡掉 |

---

## 8. 权限隔离、隐私留存与分期实施计划

### 8.1 权限与隐私
- **权限分级**：新增 `analytics.view_site_analytics`（主编/超管）、`analytics.view_page_analytics`（受限作者）、`analytics.approve_curatedrecommendation`、`analytics.resolve_analyticsalert`。受限作者的所有单页/汇总/导出接口一律在服务端复用 `OwnershipPagePermissionTester`，绝不靠 `is_staff` 或 `Person` 同名授权；回收站历史数据还须同时具备既有回收查看权限。
- **数据留存（D6-A）**：Redis 会话与日 UV 去重键 `≤ 48 小时`；评价幂等收据 `≤ 24 小时`；批次收据与无身份运维日志 `≤ 7 天`；榜单快照 `30 天`；`PageAnalyticsDaily` 保留 `13 个月`；`PageAnalyticsTotal` 保留至 Purge。绝不存储原始 IP、UA、Cookie、`session_key` 到分析或评价业务表。

### 8.2 分期落地与验收矩阵

| 阶段 | 交付内容 | 上线前必须通过的验证 |
| --- | --- | --- |
| **第 1 期：可信看数底座** | `analytics` App 骨架、5 张第 1 期表、`AnalyticsCapabilities` 注册、`#article-end` 标记、异步上下文/心跳采集、Redis Lua 接收、`settle_analytics` 补跑命令、后台大盘与单篇数据抽屉、权限隔离与 Purge 联动 | 1. 新 `XxPage` 零分支接入单测；<br>2. 同日跨三页站点 UV=1、同日换版 UV 不重复叠加、48h 内开机补跑日 UV、缺测日不写 0；<br>3. Redis 停机时文章页正常返回且无同步写 SQL；<br>4. 真实 PostgreSQL 并发结算不重算、Purge 后迟到批次不复活；<br>5. 受限作者越权查他人 `page_id` 被拒绝 |
| **第 2 期：四档评价与精选投放** | 5 张第 2 期表、详情页四档表情组件与防刷 API、`account_lifecycle.py` 销户扣票联动、贝叶斯/Wilson 降噪与滞回状态机、独立高 PV 低完读预警、精选工作台与 `HomePage` 逐区替换 | 1. 四档新增/切换/撤销/双击幂等/并发首票保存点/作者自评拦截/销户扣票；<br>2. 反例计算验证（2 差评不降权、19→20 票进观察不立即下榜、连续 2 日生效/3 日解除、14 天拒绝冷却期）；<br>3. 首页三区精选、最新文章与祖先私有/跨站/Purge 缓存隔离；<br>4. Playwright 桌面+移动端交互验收 |
| **第 3 期：类型专属转化与运营深化** | 博客代码复制/Mermaid 查看、指南步骤完成度与 `/media-gateway/` 课件下载归因、项目仓库/Demo 点击、作者产能与审核效率看板、权限过滤后的 CSV 导出 | 按类型专属转化去重（含 `Range` 分段下载合并）、审核积压与耗时口径核对、CSV 导出防越权与防小样本反推 |

**性能压测基线（隔离环境实测通过后再开前台功能开关）**：100 公共详情请求/秒持续压测下，HTML 响应无同步分析 SQL 写入，P95 延迟较关闭采集增幅 `≤ 5%`；后台 30 日聚合查询 P95 `≤ 2 秒`；单热门页 20 次评价写/秒 POST P95 `≤ 250 ms` 且四档总和与真值表 100% 一致。

---

## 9. 审查与修订记录

- **历史审查背景**：初稿由 `sol`（`gpt-6.2-sol`）起草；原计划调度 `argon`（`gemini-4-neon`）审查，但 `argon` 等待超 16 分钟无返回已关闭（未获得也未虚构其意见）；随后由独立只读代理 `terra`（`gpt-6.1-terra`）对照仓库源码和本机 Wagtail 8.1 源码提出 4 项 P0、5 项 P1 及 2 项 P2 反例。
- **本次务实瘦身修订（保留安全底线、砍掉过度设计）**：
  1. **去掉每分钟 SQL 预登记桶与单进程内存清单**：改用多进程无状态 Redis 活动桶 + 48 小时内随时可补跑的日 UV 封账命令，彻底适配非 24 小时开机的 WSL2 环境，并用“30 日中至少 25 个健康日”避免单日停机锁死整月精选；
  2. **去掉全站单一 Advisory Lock 与侵入式包裹十几个原生 Wagtail 写入口**：公开隔离靠读侧 `get_public_blocked_page_ids()` + `live_revision_id` 实时核验，Purge 防复活靠真实 `Page` 外键 + 结算前存在性校验，评价并发只用单页局部行锁，销户扣票并入现有 `account_lifecycle.py` 锁序；
  3. **模型从 20+ 张收敛至 10 张核心表**，补齐需求书中“暂不入选 14 天冷却期（`rejected_cooldown_until`）”；
  4. **新增冷启动友好机制**：阈值全部放进 `AnalyticsSettings` 可配，并新增不依赖投票数的“高 PV + 低完读/高短读”独立标题党预警。

> 注：本轮仅修订设计文档，未编写应用代码、未执行 `migrate`、未进行真实服务压测或 Git 提交。
