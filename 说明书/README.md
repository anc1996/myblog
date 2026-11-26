# myblog 项目工程与技术规范入口

更新日期：2026-11-25。**全站 19 个业务子应用、57 份自动化测试文件及核心架构已落地；自 2026-11-24 起项目遵循《AGENTS.md》中借鉴 WordPress 四层解耦思想的防写死/防过度设计铁律（不再为个人站点叠加多库/大并发子系统）**。

## 建议阅读顺序

1. [详情页三栏布局与侧栏互动后端方案（RFC）](myblog_详情页三栏布局与侧栏互动后端方案_RFC_2026-11-24.md)：**【已实施完成，`.env.local` 已迁移并通过多视口浏览器核验】** 定义宽屏（最高 1800px）三栏与窄屏防评论压底的网格布局、导航防换行、同父栏目且仅详情类型的上下篇与同栏/同标签推荐、逐站只读 `SiteAnnouncementSettings`、以及作者名片卡（本月 SQL 热力图、`作者好文 / 作者发言` 防侧漏与换绑归零契约）。
2. [全站主题设计系统与昼夜跟随架构方案（RFC）](myblog_全站主题设计系统与昼夜跟随架构方案_RFC_2026-11-24.md)：**【阶段 1–3 已实施，12 份全站/积木 CSS 已纳入零裸 `#hex` 静态护栏】** 以 `tokens.css` 为单一设计令牌真相源，实现 `mist-blue`/`moss-lab` 双色板、`system`/`light`/`dark` 三态同步、`SiteThemeSettings` 只读隔离、全站白块收敛及 Highlight.js/Mermaid `follow-site`/Vendor 控件全联动。
3. [全站数据分析与文章评价排行可行性方案（RFC）](myblog_全站数据分析与文章评价排行可行性方案_RFC_2026-11-23.md) 与 [需求规格](myblog_全站数据分析指标与排行精选需求规格说明书_2026-11-23.md)：**【第 1–3 期已落地（13 模型），已完成共享层注册表解耦（通过 AST 守卫），冻结新表扩展；待真实多日流量与批量告警/榜单页验收】** 覆盖统计上报、四档评价、降噪告警与运营看板。
4. [全站作者专栏与按作者搜索方案（RFC）](myblog_全站作者专栏与按作者搜索方案_RFC_2026-11-23.md)：**【已落地】** `/authors/` 大厅与 `/authors/<slug>/` 专栏、匿名/佚名归因及按作者组合检索。
5. [会员体系、多角色创作权限与双轨防刷评论架构方案](myblog_会员体系与多角色创作权限架构方案_RFC_2026-11-11.md)：**【Phase 1–5 及音视频作者隔离已全部落地】** 前台会员中心（`/account/*`）、`MemberProfile` 独立头像与 `AvatarPurgeTask`、会话代次守卫与账号硬删过户（Phase 1）、公共图片验证码 `captcha`（Phase 2）、双轨防刷评论配额与 5 分钟内联编辑（Phase 3），以及基于 `CustomBasePage` 与 `AuthorIsolatedSnippetPermissionPolicy` 的页面、图片、文档及音视频作者隔离、媒体网关鉴权与回收站恢复（Phase 4–5）。
6. [课件附件下载增强与通用多页面 Markdown 导入系统架构方案](myblog_课件附件下载与通用多页面Markdown导入架构方案_RFC_2026-11-22.md)：**【已实现并通过 Terra 独立审查加固与 110 项隔离回归测试】** 区分 `.md` 附件下载与多页面导入入口，含 RFC 5987 下载头修复、公共 `CustomAttachmentListBlock`、三类页面写前脱水加固、`markdown_import` 子应用与接口、10 万字四区采样 AI 引擎与本地 CLI/GUI 客户端。
7. [全站响应式前端共享组件与底层 StructBlock 协议架构方案](myblog_全站响应式前端共享组件与StructBlock协议方案_RFC_2026-11-22.md)：**【已实现并演进至宽屏三栏体系】** 接入 `Alpine.js / HTMX / Tom Select / Flatpickr / Splide` 离线前端公共组件（`vendor.lock.json`），统一 `tokens.css` 与 `myblog.css` 语义令牌规则，落地 `detail_shell.html` 共享详情骨架与单次服务端分级 TOC 提取器（现已扩展为 2026-11-24 宽屏三栏与右栏互动体系）。
8. [第一阶段技术实现方案与架构规范](myblog_第一阶段技术方案_v1.0.md)：**【核心必读】** 系统完整落地实现蓝图，涵盖彻底脱水模式、受控回收站原树恢复、中文搜索与 GIN 索引、专栏体系、通用评论系统、全站多页面 RSS/Atom 订阅与时间线归档、RESTful JSON API 规范与架构拓扑图。
9. [全站多页面评论后台管理与多维治理系统 RFC](myblog_全站多页面评论管理与多维治理系统_RFC_2026-11-10.md)：**【RFC-020 已实施】** 全局分类与溯源中控台（动态注册表 Tab）、Page 编辑器【💬 评论与互动】Tab（100% 异步隔离 Wagtail 自动保存）、Purge 级联物理清空、作者自删占位、V1/V2 审核隔离与 24h 主体限制。
10. [全站层级列表与多维检索通用框架 RFC](myblog_全站层级列表与多维检索通用框架_RFC_2026-11-09.md)：**【已实施】** 通用 `HierarchicalListingMixin`、`IndexListingService`、混合分页与签名游标、双模同构与 Wagtail 8.1 后台预览守卫闭环。
11. [全站标签大厅与跨类型检索系统 RFC](myblog_全站标签大厅与跨类型检索系统_RFC_2026-11-09.md)：**【已实施】** `TagIndexPage` 路由、A-Z 分组、注册表驱动的多类型分栏 Tab、私有草稿标签防泄漏与 0 Mongo 访问闭环。
12. [全站订阅与时间线归档通用接口框架方案](myblog_全站订阅与时间线归档通用接口框架方案_2026-11-07.md)：**【已实施】** `/feeds/` 通用多页面即插即用契约、弱 ETag 协商缓存与 Wagtail 8.1 刚性生命周期安全守卫。
13. [portfolio 与 site_settings 架构与实现方案 RFC](myblog_portfolio与site_settings架构与实现方案_RFC_2026-11-07.md)：**【已实施】** `ProjectPage` 作品集、`NavigationSettings / SocialMediaSettings / FooterSettings / SiteThemeSettings / SiteAnnouncementSettings` 与安全降级。
14. [子应用目录架构收敛与重构方案 RFC](myblog_子应用目录架构收敛与重构方案_RFC_2026-11-09.md)：**【已完成】** 将全部业务应用统一收敛至 `myblog/myblog/apps/*` 与 `myblog.bootstrap` 零表名漂移方案。
15. [统一搜索与组合筛选方案](myblog_统一搜索与组合筛选方案_SEARCH-V2-FILTER_2026-11-04.md)：**【已实施】** 搜索接口、过滤、排序、权限及 GIN 索引细节。
16. [RustFS 切换方案](myblog_RustFS切换方案_2026-11-03.md)：**【已实施】** 私有对象存储、逐次鉴权、历史引用保护与 `ProtectedRustFSStorage` 拦截。
17. [前端公共 Vendor 资产管理与升级指南](前端公共Vendor资产管理与升级指南.md)：`myblog/static/vendor/` 不入库的 10 包锁定还原方案、`sync_vendor_assets.py` 同步校验与未来版本升级规范。
18. 站内音视频播放器与分栏预览：[需求规格](myblog_站内音视频播放器与分栏预览需求规格_RFC.md)、[技术设计](myblog_站内音视频播放器与分栏预览技术设计.md)。已接入本地 Video.js 10.5.0、前台公共积木和后台分栏，跨浏览器/代理验收范围及待测项见技术设计。

早期历史过渡文档（MySQL/MinIO 切换、初版任务书、2026-11-04 依赖快照、六项技术审查、十项决策报告、第一阶段学习指引与 `SEARCH-V2-FILTER` 原始需求附件）已统一归档至 `archive/` 目录；当前直接第三方依赖以根目录 `requirements.txt` 为准。
基础服务的配置与操作说明在 [infra/README.md](../infra/README.md)。

## 当前实现与验证指标

- **存储与脱水**：PostgreSQL `blog_blogpage` 与 `guides_guidepage` 物理列 `body` 强制保持为 `[]`（2 字节），正文 StreamField 积木由 MongoDB 不可变集合存储并通过 `with_content_json()` 秒级水合。
- **回收站与恢复**：原树占位标记（`is_recycled=True`），前台 404 隔离；支持错删原位一键恢复为未发布草稿；支持 30 天保留倒计时与受控彻底物理销毁。
- **全站发现与搜索**：单列 GIN 倒排索引 + 256 字符重叠模式 A 原文；模式 A 命中少于 5 条自动提示展开模式 B；单快照 `REPEATABLE READ` 查询。
- **指南体系**：支持主题绑定、教程积木、365 天未复核过时提示与发布强校验门禁。
- **全站多页面评论与治理系统 (RFC-020)**：外键直连 `wagtailcore.Page`，两级扁平反嵌套，版本化审核与 CAS 防盲批，`is_publicly_visible` 布尔跃迁防翻倍计数；Page 彻底销毁级联清空（DO_NOTHING 版本指针彻底消除崩溃）；作者自删根占位条并保留回复；编辑待审 V2 期间前台保全展示 V1；Page 编辑器内嵌纯异步评论 Tab（B-03 隔离）；全站 24h 主体限制与跨页面批量治理；Wagtail Explorer 挂载快捷动作。
- **全站通用订阅与归档**：RSS 2.0 与 Atom 1.0 全维度订阅，RFC 4287 合规，弱 ETag 304 快速协商，PostgreSQL SARGable 时间线年月归档。
- **公共内容积木与高级排版渲染 (P1 最新)**：
  - **Wagtail 8.1 后台左右分栏实时编辑器（第二批 / 选项 A）**：通过 `vendor.lock.json` 接入离线 `vditor@3.11.2`，结合 Telepath 适配器（零数据库迁移）为 `CustomMarkdownBlock` 提供 `Vditor` 分屏编辑 + 截图粘贴直传受控 `CustomImage`（默认 `RESTRICTED` 并归属当前作者工作区，返回 `asset://image/<uuid>`，预览窗格自动重写为 `/media-gateway/`，同步触发 Wagtail 8.1 原生自动保存）；同时为 `CustomMermaidBlock` 与 `CustomTableBlock` 提供后台左右分栏实时预览控件（Mermaid 实时 SVG + 复杂表格服务端无落库预览含 KaTeX 与 Highlight.js）；
  - **全站代码语法高亮与统一代码块外壳**：通过 `vendor.lock.json` 接入官方 `@highlightjs/cdn-assets@11.12.0`（GitHub Dark 主题 + 按需语言包懒加载），由公共 `code-highlighting.js/css` 统一覆盖 `CustomMarkdownBlock` 代码围栏、`CustomCodeBlock` 与 `CustomStepBlock`，自动补齐左侧行号、超长代码（>14 行）折叠/展开、一键复制及无 JS 时的 Pygments `.hl-*` 降级配色；
  - **Mermaid 流程图统一样式、受控内联 SVG 与左右分栏对照**：升级引入无已知高危漏洞的官方离线包 `Mermaid 11.17.2`；独立块与 Markdown ```` ```mermaid ```` 围栏（含缩进/CRLF/中文节点）输出完全统一的 DOM 契约；前端按需懒加载，渲染开启 `securityLevel: 'strict'` 与 SVG 白名单净化；工具栏支持「⇄ 分栏源码」切换为**左侧源码 + 右侧 SVG 实时对照**（移动端自动上下堆叠），并提供放大/缩小/100%/自适应宽度/深浅主题切换/复制源码/导出 SVG/全屏弹窗/折叠与错误回退；
  - **LaTeX 数学公式前置保护与 KaTeX 按需渲染**：引入官方离线包 `KaTeX 0.19.1`；在 Markdown 扩展（tables/nl2br）前预先提取公式，彻底消除公式下划线被转为 `<em>`、换行被转为 `<br>` 以及表格单元格内公式绝对值管道符被拆列的问题；严格规避货币价格误匹配；支持块级与行内公式；
  - **复杂表格（CustomTableBlock）逻辑网格、全能单元格与左右分栏**：新增逻辑占位网格校验（精确拦截越界、重叠与非正跨度）及 `<thead>`/`<tbody>` 表头递推，彻底解决 `rowspan` 跨行组被浏览器截断错位的问题；单元格支持 LaTeX 公式、行内反引号 `` `code` ``、围栏代码块 Highlight.js 高亮与 `asset://` 受控媒体；前端支持「⇄ 分栏对照」切换**左侧 JSON 源码高亮 + 右侧表格渲染**；
  - **外部媒体**：独立嵌入块和富文本共用 `content_blocks.embeds` 平台规则。Vimeo 保留非公开列出视频的 `h` 参数，点击加载后等播放器就绪再展示；加载失败时可走源链接。腾讯视频需要有效视频链接，旧演示的占位 ID 不启动播放器。网易云和 Spotify 使用站内播放器；Spotify 可粘贴 `open.spotify.com` 的内容链接，不需要粘贴 iframe 代码，播放受 Spotify 授权与可用性限制，始终保留源链接。酷狗和咪咕使用外链卡片，酷我用普通链接。QQ 音乐不再支持嵌入；既有内容不改写，旧卡片不展示，可在编辑时移除旧块或改用普通链接。平台徽标与标题不重复显示同一个前缀。
- **RichText 与 Markdown 统一上传、生命周期与图文混排框架（2026-11-25 Phase 1–3 已落地并迁移）**：
  - **生命周期兜底**：结构化卡片（导读/步骤/Tip/FAQ）嵌套富文本统一委托 `CustomRichTextBlock` 净化渲染并提取 `MediaReferenceRecord`；公开页发布前阻断不会被自动转正的他人/未授权受限资产；公开派生图缓存头收敛为 `public, max-age=300, must-revalidate`；
  - **统一上传体验**：Draftail 新增剪贴板/拖拽图片直传受控 `CustomImage`（零字段硬编码）；Vditor 新增工具栏复用 Wagtail 原生图库/文档库 Chooser（含附件上传）并通过受控解析接口写入 `asset://image/<uuid>` / `[标题](asset://document/<uuid>)`；两者共用表单级上传并发锁 `CBEditorUploadCoordinator`、防降级集合隔离与同集合标题防重；
  - **文本对齐、图片修饰与三层防留白排版**：Draftail 注册 `align-left / align-center / align-right` 段落特性，Markdown 支持 `::: align-center / align-right` 块指令（经 `nh3` 严格类名白名单净化，禁内联 `style`）；`image_formats.py` 统一注册 `centered / fullwidth / left / right` 格式并向灯箱注入 `data-full-src` 原图网关；Markdown 图片改用有限 6 档宽度语义类（`cb-img-w-25..100`）与独立 `<figure><figcaption>` 升级；`content-images.css` 收敛至 `tokens.css` 语义令牌（零裸 `#hex`），落地容器宽度流式环绕、标题/代码块/表格/卡片自动 `clear: both` 以及全新 `CustomMediaTextBlock`（图文并排 Grid 积木，已完成 `blog`/`guides`/`portfolio` `0005` 迁移）。
- **站内媒体与卡片**：图片排版与灯箱、音视频和文档卡片、教程和提示卡片使用公共样式；
  - **全站代码复制统一**：规范 `code.py` 与 `structured.py` 的 URI 编码协议，统一由 `code-copy.js` 驱动并平滑回退，修复指南步骤卡代码复制乱码；优化 `guide_page.html` 侧栏 375px 移动端响应式断行。
- **全站 SEO 与爬虫收录协议**：
  - `/sitemap.xml`：集成 Wagtail 站点地图，严格排除回收站页面（`is_recycled=True`）与未发布草稿，仅收录公开发布的合法在线页面；
  - `/robots.txt`：阻断爬虫抓取 `/admin/`、`/django-admin/`、`/media-gateway/`、`/avatar-gateway/`、`/account/` 与 `/api/` 等私密网关，自动定位并引导抓取 `/sitemap.xml`。
- **测试与质量**：`tests/` 目录现含 **57 份自动化测试文件**；各新增功能均需通过 `MYBLOG_ENV_FILE=.env.test python -B manage.py check`、`makemigrations --check --dry-run` 及定向回归测试。
- **前端与交互闭环**：
  - **全局响应式导航、浅深色主题与公共 UI 组件**：绑定 `site_settings`，支持 `data-theme="light|dark"` 切换与防闪烁恢复；通过 `vendor.lock.json` 统一挂载 `Alpine.js`、`HTMX`、`Tom Select`、`Flatpickr` 与 `Splide`；
  - **全站共享详情壳层、三栏互动与单次服务端 TOC 目录 (`detail_shell.html`)**：统一服务 `BlogPage`、`GuidePage`、`ProjectPage` 及后续 `XxPage`；后端单遍提取 `h1`–`h4` 与 `step` 锚点并注入 `[data-cb-block]` 防溢出容器，桌面宽屏（`>=1280px`，最高 `1800px` 居中适配全屏）采用「可折叠左侧 sticky 目录 + 主文/评价/上下篇/评论区 + 右侧作者名片/标签/公告或推荐」三栏布局，手机/iPad（`<=1024px`）使用粘性触发栏 + 原生 `<dialog>` 抽屉；标签统一反解本站 `TagIndexPage` 详情路由；
  - **详情页四级树形目录与动态定位**：`build_detail_presentation()` 构建 `h2→h3→h4`（兼容 `h1` 最多 4 级）可折叠树形目录；`detail-toc.js` 通过 `ResizeObserver` 动态测量顶部遮挡高度驱动 `--detail-anchor-offset`，提供「回到顶部」「正文开头」与精确阅读线高亮，窄屏无 JS 自动降级为原生 `<details>` 目录；
  - **首页综合门户**：Hero 标语导引 + Splide 精选轮播 + 文章流 + 专栏推荐 + 开源项目 + 热门标签云；
  - **有界计数混合分页与部分复合索引**：`discovery.pagination` 支持浅页直跳窗口（`1…4 5 [6] 7 8…100`，超出返回 HTTP 422 引导）、双向 Keyset 游标（`v2:` 上下翻页）、`seek_date` / `seek_direction=oldest` 日期定位及 `LIMIT 5001` 有界计数探测（精确 / 汇总 / 下限明标）；`PublishedSearchDocument` 挂载条件部分 B-tree 索引；
  - **全站标签大厅**：RESTful 路由 `/tags/` 与 `/tags/<slug>/`，A-Z 首字母分组，跨类型 Tab 分栏；
  - **读者评论互动组件与 Typora 级 Markdown 引擎**：正文 `CustomMarkdownBlock` 与评论系统共享基于 `markdown-it-py 3.0.0` 的 `markdown_engine.py`（全面兼容 2 空格嵌套列表、引用内嵌套列表、混合有序/无序与中文标点加粗）；评论主评/回复/5 分钟内联编辑共用 Markdown 工具栏、Emoji 面板与只读实时分栏预览接口（`/api/v1/pages/<id>/comments/preview/`），支持点赞/点踩（本人禁用防误报）、`MemberProfile` 昵称头像、会员平等（3000 字 / 5 外链）与受限 `rowspan`/`colspan` 复合表格（禁图防 XSS），后台 `/admin/comments/inspect/<id>/` 同步富格式预览；
  - **月历归档与预聚合汇总表**：`/timeline/` 与 `/api/v1/archives/calendar/` 按 `authored_on` 提供按月日历网格与 12 月快速切换；只读 `ArchiveDailyCount`（单月最多 31 行），通过 `ArchiveContribution` 事务 +/-1 增量维护、既有 `ArchiveSyncOutbox`（早期既有实现，现行 `AGENTS.md` 规范不再为新需求新增 Outbox 表）与 `reconcile_archive_counts` 管理命令保障计数一致，点击日/月下钻走 SARGable 区间与混合分页。
- **目录架构**：全站 **19 个业务子应用**物理收敛于 `myblog/myblog/apps/*`（与 `settings/`、`static/`、`templates/` 同级），通过 `myblog.bootstrap` 注入 `sys.path[0]`，保证数据库表名与 `ContentType` 100% 零漂移。
- **基础设施拓扑**：PostgreSQL 18.6 + MongoDB 9.0.4 + RustFS 1.0.4 (S3) + Redis 8.10.3，端口统一发布至 `0.0.0.0`。
- **凭据安全**：所有生产凭据与私密 `.env` 严格由本地安全管理，禁止提交至版本库。
