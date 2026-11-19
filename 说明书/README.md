# myblog 项目工程与技术规范入口

更新日期：2026-11-19。**第一阶段核心能力、会员权限体系、P1 公共内容积木渲染增强（Mermaid / LaTeX / 复杂表格 / 代码复制）与 SEO 底座（Sitemap & Robots）已全部完成实现并实测验证**（隔离测试库全量 224 项测试通过、Django 系统检查 0 错误 0 警告、ManifestStaticFilesStorage 839 个静态资源收集无异常；`.env.local` 与 `.env.test` 均同步就绪）。

## 建议阅读顺序

0. [会员体系、多角色创作权限与双轨防刷评论架构方案](myblog_会员体系与多角色创作权限架构方案_RFC_2026-11-11.md)：**【Phase 1–5 已实现并通过 `gemini-4-argon` 审核、全量测试与真实浏览器验收】** 前台会员中心（`/account/*`）、`MemberProfile` 独立头像与 `AvatarPurgeTask`、会话代次守卫与账号硬删过户（Phase 1）、全站公共图片验证码 `captcha`（Phase 2）、双轨防刷评论配额与 5 分钟内联编辑（Phase 3），以及基于 `CustomBasePage` 与 Wagtail 通用生命周期的作者隔离、图片/文档网关鉴权与回收站冲突恢复（Phase 4–5）均已落地。
0. [第一阶段技术实现方案与架构规范](myblog_第一阶段技术方案_v1.0.md)：**【核心必读】** 系统完整落地实现蓝图，涵盖彻底脱水模式、受控回收站原树恢复、中文搜索与 GIN 索引、专栏体系、通用评论系统、全站多页面 RSS/Atom 订阅与时间线归档、RESTful JSON API 规范与架构拓扑图。
1. [全站多页面评论后台管理与多维治理系统 RFC](myblog_全站多页面评论管理与多维治理系统_RFC_2026-11-10.md)：**【RFC-020 最新】** 全局分类与溯源中控台、Page 编辑器【💬 评论与互动】Tab（100% 异步隔离 Wagtail 自动保存）、Purge 级联物理清空、作者自删占位、V1/V2 审核隔离与 24h 主体限制。
1. [全站层级列表与多维检索通用框架 RFC](myblog_全站层级列表与多维检索通用框架_RFC_2026-11-09.md)：通用 HierarchicalListingMixin、IndexListingService、Keyset 签名游标、双模同构与 Wagtail 8.1 后台预览守卫闭环。
2. [全站标签大厅与跨类型检索系统 RFC](myblog_全站标签大厅与跨类型检索系统_RFC_2026-11-09.md)：TagIndexPage 路由、A-Z 分组、多类型分栏 Tab、私有草稿标签防泄漏与 0 Mongo 访问闭环。
1. [全站订阅与时间线归档通用接口框架方案](myblog_全站订阅与时间线归档通用接口框架方案_2026-11-07.md)：通用多页面即插即用契约、弱 ETag 协商缓存与 Wagtail 8.1 刚性生命周期安全守卫。
2. [第一阶段从零起步开发路线与学习指引](myblog_第一阶段从零起步开发路线与学习指引.md)：面向刚接触 Django/Wagtail 学生的实战全景指引，解答“S1–S12 演进主线、每项需求设计思想与避坑指南”。
3. [第一阶段需求规格说明书](myblog_第一阶段需求规格说明书_v1.0.md)：确认功能范围、发布与草稿、媒体权限和回收规则。
4. [十项决策与页面恢复契约完善报告](myblog_十项决策与页面恢复契约完善报告_2026-11-04.md)：10 项确认决策落地（全部采用 A 方案）、页面恢复防冲突机制与 Wagtail 8.1 陷阱防御。
5. [统一搜索与组合筛选方案](myblog_统一搜索与组合筛选方案_SEARCH-V2-FILTER_2026-11-04.md)：搜索接口、过滤、排序、权限及 GIN 索引细节。
6. [RustFS 切换方案](myblog_RustFS切换方案_2026-11-03.md)：私有对象存储、逐次鉴权、历史引用保护与 ProtectedRustFSStorage 拦截。
7. [依赖核查与升级方案](myblog_依赖核查与升级方案_2026-11-04.md)：版本约束、升级顺序和依赖验证。
8. [前端公共 Vendor 资产管理与升级指南](前端公共Vendor资产管理与升级指南.md)：`myblog/static/vendor/` 不入库的锁定还原方案、`sync_vendor_assets.py` 同步校验与未来版本升级规范。

需要逐项查阅时，再看[第三方依赖清单](myblog_第三方依赖清单_2026-11-04.md)、[版本核查证据](myblog_依赖版本核查证据_2026-11-04.json)和[环境与代理准备记录](myblog_环境与代理准备记录_2026-11-03.md)。
基础服务的配置与操作说明在 [infra/README.md](../infra/README.md)。

## 当前实现与验证指标

- **存储与脱水**：PostgreSQL `blog_blogpage` 与 `guides_guidepage` 物理列 `body` 强制保持为 `[]`（2 字节），正文 StreamField 积木由 MongoDB 不可变集合存储并通过 `with_content_json()` 秒级水合。
- **回收站与恢复**：原树占位标记（`is_recycled=True`），前台 404 隔离；支持错删原位一键恢复为未发布草稿；支持 30 天保留倒计时与受控彻底物理销毁。
- **全站发现与搜索**：单列 GIN 倒排索引 + 256 字符重叠模式 A 原文；模式 A 命中少于 5 条自动提示展开模式 B；单快照 `REPEATABLE READ` 查询。
- **指南体系**：支持主题绑定、教程积木、365 天未复核过时提示与发布强校验门禁。
- **全站多页面评论与治理系统 (RFC-020)**：外键直连 `wagtailcore.Page`，两级扁平反嵌套，版本化审核与 CAS 防盲批，`is_publicly_visible` 布尔跃迁防翻倍计数；Page 彻底销毁级联清空（DO_NOTHING 版本指针彻底消除崩溃）；作者自删根占位条并保留回复；编辑待审 V2 期间前台保全展示 V1；Page 编辑器内嵌纯异步评论 Tab（B-03 隔离）；全站 24h 主体限制与跨页面批量治理；Wagtail Explorer 挂载快捷动作。
- **全站通用订阅与归档**：RSS 2.0 与 Atom 1.0 全维度订阅，RFC 4287 合规，弱 ETag 304 快速协商，PostgreSQL SARGable 时间线年月归档。
- **公共内容积木与高级排版渲染 (P1 最新)**：
  - **全站代码语法高亮与统一代码块外壳**：通过 `vendor.lock.json` 接入官方 `@highlightjs/cdn-assets@11.12.0`（GitHub Dark 主题 + 按需语言包懒加载），由公共 `code-highlighting.js/css` 统一覆盖 `CustomMarkdownBlock` 代码围栏、`CustomCodeBlock` 与 `CustomStepBlock`，自动补齐左侧行号、超长代码（>14 行）折叠/展开、一键复制及无 JS 时的 Pygments `.hl-*` 降级配色；
  - **Mermaid 流程图统一样式、受控内联 SVG 与左右分栏对照**：升级引入无已知高危漏洞的官方离线包 `Mermaid 11.17.2`；独立块与 Markdown ```` ```mermaid ```` 围栏（含缩进/CRLF/中文节点）输出完全统一的 DOM 契约；前端按需懒加载，渲染开启 `securityLevel: 'strict'` 与 SVG 白名单净化；工具栏支持「⇄ 分栏源码」切换为**左侧源码 + 右侧 SVG 实时对照**（移动端自动上下堆叠），并提供放大/缩小/100%/自适应宽度/深浅主题切换/复制源码/导出 SVG/全屏弹窗/折叠与错误回退；
  - **LaTeX 数学公式前置保护与 KaTeX 按需渲染**：引入官方离线包 `KaTeX 0.19.1`；在 Markdown 扩展（tables/nl2br）前预先提取公式，彻底消除公式下划线被转为 `<em>`、换行被转为 `<br>` 以及表格单元格内公式绝对值管道符被拆列的问题；严格规避货币价格误匹配；支持块级与行内公式；
  - **复杂表格（CustomTableBlock）逻辑网格、全能单元格与左右分栏**：新增逻辑占位网格校验（精确拦截越界、重叠与非正跨度）及 `<thead>`/`<tbody>` 表头递推，彻底解决 `rowspan` 跨行组被浏览器截断错位的问题；单元格支持 LaTeX 公式、行内反引号 `` `code` ``、围栏代码块 Highlight.js 高亮与 `asset://` 受控媒体；前端支持「⇄ 分栏对照」切换**左侧 JSON 源码高亮 + 右侧表格渲染**；
  - **全站媒体、嵌入与卡片公共前端收敛**：补齐 `CustomEmbedBlock` 白名单沙盒 `<iframe>`（B站/YouTube/Vimeo/CodePen）与安全外链降级；统一 `CustomImageBlock` 对齐排版与全站正文/图集公共无障碍灯箱；补充 `Audio`/`Video`/`Document` 响应式卡片样式；将 `Lead`/`Step`/`Tip`/`Faq`/`Reference` 卡片样式抽离至 `content_blocks/css/structured-blocks.css` 统一服务全站页面；
  - **全站代码复制统一**：规范 `code.py` 与 `structured.py` 的 URI 编码协议，统一由 `code-copy.js` 驱动并平滑回退，修复指南步骤卡代码复制乱码；优化 `guide_page.html` 侧栏 375px 移动端响应式断行。
- **全站 SEO 与爬虫收录协议**：
  - `/sitemap.xml`：集成 Wagtail 站点地图，严格排除回收站页面（`is_recycled=True`）与未发布草稿，仅收录公开发布的合法在线页面；
  - `/robots.txt`：阻断爬虫抓取 `/admin/`、`/django-admin/`、`/media-gateway/`、`/avatar-gateway/`、`/account/` 与 `/api/` 等私密网关，自动定位并引导抓取 `/sitemap.xml`。
- **测试与质量**：隔离测试库全站 **224 项自动化单元与集成测试全部通过**；Django 系统检查 0 错误 0 警告；`ManifestStaticFilesStorage` 静态产物收集 839 项全部通过；已通过 Headless Chrome 真实浏览器实测验收。
- **前端与交互闭环**：
  - **全局响应式导航与页脚**：绑定 `site_settings`，纯 CSS 移动端汉堡菜单自适应；
  - **首页综合门户**：Hero 标语导引 + 文章流 + 专栏推荐 + 开源项目 + 热门标签云；
  - **全站层级列表检索**：双模同构、中文输入法焦点保护、签名 Keyset 游标上一页/下一页；
  - **全站标签大厅**：RESTful 路由 `/tags/` 与 `/tags/<slug>/`，A-Z 首字母分组，跨类型 Tab 分栏；
  - **读者评论互动组件**：AJAX 免刷新提交、两层回复、CAS 审核提示、nh3 防 XSS 净化；
  - **时间线年月归档**：`/timeline/` 独立归档大厅，SARGable 只读快照秒级扫描。
- **目录架构**：全站 16 个业务子应用物理收敛于 `myblog/myblog/apps/*`（与 `settings/`、`static/`、`templates/` 同级），通过 `myblog.bootstrap` 注入 `sys.path[0]`，保证数据库表名与 `ContentType` 100% 零漂移。
- **基础设施拓扑**：PostgreSQL 18.6 + MongoDB 9.0.4 + RustFS 1.0.4 (S3) + Redis 8.10.3，端口统一发布至 `0.0.0.0`。
- **凭据安全**：所有生产凭据与私密 `.env` 严格由本地安全管理，禁止提交至版本库。
