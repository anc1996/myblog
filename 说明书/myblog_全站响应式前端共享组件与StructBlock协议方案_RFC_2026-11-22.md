# myblog 全站响应式前端共享组件与底层 StructBlock 协议架构方案

> **版本状态**：**已实现核心方案并完成主题令牌拆分与三栏详情布局演进（2026-11-22 起草落地，2026-11-24 持续演进）**。已落地 `vendor.lock.json` 公共前端 UI 组件库（Alpine.js / HTMX / Tom Select / Flatpickr / Splide 等共 10 个包）、独立设计令牌 `tokens.css`、详情三栏样式 `detail-layout.css`、`detail_shell.html` 共享详情骨架与单次服务端分级 TOC 提取器（已接入 `BlogPage`、`GuidePage`、`ProjectPage`）；真实物理 iPad Safari 与生产代理尚未真机实测。

---

## 一、 目标

**保留现有 Django/Wagtail 服务端渲染，用一套设计令牌、一个详情页壳层、一套内容块协议服务全站。** Blog、Guide、Project 和未来 `XxPage` 只需提供各自差异内容，不再重复编写左侧大纲目录、面包屑、作者/标签栏和手机端自适应规则；关闭 JavaScript 时，正文、目录链接、图片链接与原生音视频控件依然可用。

---

## 二、 做法与全站协议规范

### 2.1 集中设计令牌与三档响应式断点（`tokens.css` / `myblog.css` / `detail-layout.css`）

- **统一 CSS 变量**：三层设计令牌已独立沉淀至 `myblog/static/css/themes/tokens.css`（`--color-bg/surface/text/muted/border/link/focus`、状态色、`--space-*`、`--reading-width`、`--header-offset`、`--touch-target: 44px` 等），支持「跟随系统 / 白天 / 黑夜」三态切换、`data-theme` / `data-palette` 双维度色板与 `:focus-visible`。
- **流体排版与三档/四态详情宽度适配（详情布局已迁入 `myblog/static/css/detail-layout.css`）**：
  - 最初以“左栏 TOC + 右阅读主栏”两栏落地；2026-11-24 已按[详情页三栏布局与侧栏互动后端方案](myblog_详情页三栏布局与侧栏互动后端方案_RFC_2026-11-24.md)演进为自适应三栏（`≥1280px` 桌面为左 TOC + 中正文/评论 + 右侧栏卡片、`1025px～1279px` 两栏、`≤1024px` 单栏 + 顶部粘性目录 `<dialog>` 抽屉，无目录/无右栏时自动退化，不留空栏）；站点自控交互按钮不小于 `44×44` CSS 像素。
- **防横向溢出合同**：所有公共块根容器统一具备 `[data-cb-block] { min-width: 0; max-width: 100%; overflow-wrap: anywhere; }`，长表格/长代码/块级公式仅在自身局部区域横向滚动，禁止全页 `overflow-x: hidden`。

### 2.2 全站详情页共享壳层与局部组件（`myblog/templates/detail/`）

新增全站共享详情页骨架 `detail_shell.html` 与真实子片段（`_detail_breadcrumbs.html`、`_detail_meta.html`、`_detail_toc.html`、`_detail_toc_branch.html`、`_detail_gallery.html`、`_detail_prev_next.html`、`_detail_right_sidebar.html`、`_detail_author_card.html`、`_detail_tags.html`）：
- 由公共展示函数统一整理面包屑、`<time>` 日期、作者列表、标签（统一反解 `TagIndexPage` `/tags/<slug>/` 详情路由，避免跳去普通搜索页）、封面图、图集与关联导航，**不绕过页面访问权限与 Revision 版本边界**；
- 各业务页模板（`blog_page.html`、`guide_page.html`、`project_page.html` 及未来 `XxPage`）仅继承 `detail_shell.html` 填入专属元信息卡片，并唯一挂载一次 `_comment_section.html`。

### 2.3 服务端一次渲染生成的分级 TOC 大纲目录

- **后端单次提取与稳定锚点**：按绑定正文块渲染一次并解析 HTML 标题（`h1`～`h4`）及 `GuidePage` `step` 根标题，跳过代码块/工具栏/提示卡片里的无关标题；用稳定块 ID + 块内顺序生成唯一锚点（兼容旧 `#step-<编号>`），保证目录与正文严格一致且无 JS 也能跳转。
- **共享交互**：桌面左侧目录与手机/iPad `<dialog>` 抽屉复用同一份目录 HTML；预览页内锚点显式设置 `target="_self"` 防止受 `<base target="_blank">` 干扰。

### 2.4 底层 `StructBlock` 后端契约与前端按需加载规范

- **后端接口**：`content_blocks/contracts.py` 定义通用契约方法 `validate`、`render`、`extract_text`、`extract_media_refs`；各 `blocks/*.py` 具体块为避让 Wagtail 原生 `StructBlock.render()` 签名，实际分别实现 `validate_value`、`render_clean`（供 `render_basic()` 与统一管线调用）以及 `extract_text`、`extract_media_refs`（不在渲染时触发存储网络请求，媒体权限仍走受控网关）。
- **HTML 清理边界**：所有块统一输出受控根属性 `data-cb-block="<type>"`；作者输入经 `sanitize_html` 净化后，再由可信渲染器写入框架属性和目录锚点。
- **分模块按需加载入口**：统一由 `_block_assets_css.html` / `_block_assets_js.html` 引入按需轻量模块（`mermaid-renderer.js` + `mermaid-block.js`、`code-highlighting.js`、`math-render.js`、`media-player.js` 等），正文存在对应块时才动态请求重型 vendor 资产，纯文本页面零下载；其中 `media-player.js` 导出 `window.ContentBlocksMediaPlayers.{init, destroy}` 供前台与 Wagtail 后台分栏复用。
- **新块 / 新页面 `XxPage` 维护五步清单**：① 定块/页面输入与 `get_common_stream_blocks()` 复用 → ② 接校验、渲染、搜索与 `MediaReferenceRecord` → ③ 继承 `detail_shell` 与共享令牌（禁止模板内联 `<style>`）→ ④ 优先原生 HTML 并接按需注册表 → ⑤ 补充双主题、窄屏不溢出与权限回归测试。

---

## 三、 已完成记录与待真机验收清单

### 3.1 已完成落地项
1. **阶段 1（已完成）**：沉淀语义令牌与公共容器防溢出规则（令牌现已独立至 `tokens.css`，零裸 `#hex` 守卫已纳入静态测试）。
2. **阶段 2（已完成）**：落地 `detail_shell.html`、共享展示呈现层与服务端单次分级 TOC 提取器，率先接入 `GuidePage` 并通过 `test_detail_shell_and_toc.py`。
3. **阶段 3（已完成）**：将 `BlogPage`、`ProjectPage` 全部迁入 `detail_shell.html`，统一标签路线、列表与评论挂载（后续并已演进为三栏布局与 `detail-layout.css`）。
4. **阶段 4 部分完成（已完成按需脚本收敛与仿真视口验证）**：收敛 `_block_assets_*` 分模块按需加载与 `ui-components.js` 挂载，并通过多视口 Playwright（含 `1440 / 1280 / 1024 / 375px` 等）、浅深色与无 JS 降级自动化回归。

### 3.2 待真机验收边界
- 真实物理 iPad Safari（含横竖屏切换与 `1024px` 附近断点）实机交互；
- 真实生产 Nginx 代理下的静态资源缓存哈希与媒体分段交付联调。
