# myblog 全站响应式前端共享组件与底层 StructBlock 协议架构方案

> **版本状态**：**已实现核心方案与隔离测试 / Headless 浏览器多视口验收（2026-11-22）**。已落地 `vendor.lock.json` 5 个公共前端 UI 组件库（Alpine.js / HTMX / Tom Select / Flatpickr / Splide）、`myblog.css` 语义令牌与防溢出规则、`detail_shell.html` 共享详情骨架与单次服务端分级 TOC 提取器（已接入 `BlogPage`、`GuidePage`、`ProjectPage`）；真实物理 iPad Safari 与生产代理尚未真机实测。

---

## 一、 目标

**保留现有 Django/Wagtail 服务端渲染，用一套设计令牌、一个详情页壳层、一套内容块协议服务全站。** Blog、Guide、Project 和未来 `XxPage` 只需提供各自差异内容，不再重复编写左侧大纲目录、面包屑、作者/标签栏和手机端自适应规则；关闭 JavaScript 时，正文、目录链接、图片链接与原生音视频控件依然可用。

---

## 二、 做法与全站协议规范

### 2.1 集中设计令牌与三档响应式断点（`myblog.css`）

- **统一 CSS 变量**：逐步迁移至语义令牌（`--color-bg/surface/text/muted/border/link/focus`、状态色、`--space-*`、`--reading-width`、`--header-offset`、`--touch-target: 44px`），并支持系统及 `data-theme="light|dark"` 浅深色切换与 `:focus-visible`。
- **流体排版与三档宽度适配**：
  - **手机（`≤640px`）**：单栏铺满；目录按钮吸附在站点导航下方，点击打开 `<dialog>` 抽屉（站点自控交互按钮不小于 `44×44` CSS 像素）。
  - **iPad（`641px～1024px`）**：默认收为抽屉；仅当壳层实际可用宽度足够容纳 `9rem` 目录 + 间距 + `36rem` 阅读区时才展示可折叠窄栏（不按设备 UA 判断）。
  - **桌面（`≥1025px`）**：左侧 `sticky` 可折叠分级大纲目录 + 右侧 `minmax(0, 1fr)` 阅读主栏；无目录时不留空栏。
- **防横向溢出合同**：所有公共块根容器统一具备 `[data-cb-block] { min-width: 0; max-width: 100%; overflow-wrap: anywhere; }`，长表格/长代码/块级公式仅在自身局部区域横向滚动，禁止全页 `overflow-x: hidden`。

### 2.2 全站详情页共享壳层与局部组件（`myblog/templates/detail/`）

新增全站共享详情页骨架 `detail_shell.html` 与子片段（`_detail_breadcrumbs.html`、`_detail_meta.html`、`_detail_toc.html`、`_detail_gallery.html`、`_detail_read_next.html`）：
- 由公共展示函数统一整理面包屑、`<time>` 日期、作者列表、标签（统一反解 `TagIndexPage` `/tags/<slug>/` 详情路由，避免跳去普通搜索页）、封面图、图集与关联导航，**不绕过页面访问权限与 Revision 版本边界**；
- 各业务页模板（`blog_page.html`、`guide_page.html`、`project_page.html` 及未来 `XxPage`）仅继承 `detail_shell.html` 填入专属元信息卡片，并唯一挂载一次 `_comment_section.html`。

### 2.3 服务端一次渲染生成的分级 TOC 大纲目录

- **后端单次提取与稳定锚点**：按绑定正文块渲染一次并解析 HTML 标题（`h1`～`h4`）及 `GuidePage` `step` 根标题，跳过代码块/工具栏/提示卡片里的无关标题；用稳定块 ID + 块内顺序生成唯一锚点（兼容旧 `#step-<编号>`），保证目录与正文严格一致且无 JS 也能跳转。
- **共享交互**：桌面左侧目录与手机/iPad `<dialog>` 抽屉复用同一份目录 HTML；预览页内锚点显式设置 `target="_self"` 防止受 `<base target="_blank">` 干扰。

### 2.4 底层 `StructBlock` 后端契约与前端按需加载规范

- **后端接口**：统一在 `content_blocks/contracts.py` 对齐 `validate_value`、`render_clean`、`extract_text`、`extract_media_refs`（不在渲染时触发存储网络请求，媒体权限仍走受控网关）。
- **HTML 清理边界**：所有块统一输出受控根属性 `data-cb-block="<type>"`；作者输入经 `sanitize_html` 净化后，再由可信渲染器写入框架属性和目录锚点。
- **单一按需 JS 注册表**：收敛 `_block_assets_css.html` / `_block_assets_js.html`，JS 按 `mount(root)` / `unmount(root)` 登记重型依赖（Mermaid、Highlight.js、KaTeX、Video.js），正文存在对应块时才按需加载，避免纯文本页面拉取无关脚本。
- **新块 / 新页面 `XxPage` 维护五步清单**：① 定块/页面输入与 `get_common_stream_blocks()` 复用 → ② 接校验、渲染、搜索与 `MediaReferenceRecord` → ③ 继承 `detail_shell` 与共享令牌（禁止模板内联 `<style>`）→ ④ 优先原生 HTML 并接按需注册表 → ⑤ 补充双主题、窄屏不溢出与权限回归测试。

---

## 三、 分批实施与验收计划

1. **阶段 1**：沉淀 `myblog.css` 令牌与公共容器防溢出规则；
2. **阶段 2**：新增 `detail_shell` 与服务端 TOC 提取器，先行接入 `GuidePage` 对照验证；
3. **阶段 3**：将 `BlogPage`、`ProjectPage` 迁入 `detail_shell` 并统一标签与列表/评论片段样式；
4. **阶段 4**：收敛按需 JS 注册表并在真机/仿真视口（`320 / 375 / 640 / 768 / 1024 / 1440px`、iPad Safari 横竖屏、浅深色、无 JS 降级）完成验收。
