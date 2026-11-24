# myblog 全站主题设计系统与昼夜跟随架构方案（RFC）

> **文档状态**：**阶段 1（`694aff5`）与阶段 2（`a6be3ee`）已全部实施、通过隔离测试及 Playwright 真实浏览器核验并推送至 `origin/main`；阶段 3–4 待本轮推进完成。**

---

## 一、 目标与为什么需要这套框架

### 1.1 我们要解决什么问题
1. **白天画面太“白”、缺乏视觉层次**：目前浅色画布 `#f8fafc` 与白色卡片 `#ffffff` 的亮度反差只有约 `1.05:1`，读者第一眼只能看到大片白底，导航、正文、工具区没有明显的色彩主次。
2. **暗黑模式与“跟随系统”不完整**：
   - 浏览器只在首次打开时读取一次操作系统明暗偏好；只要用户点过一次切换按钮，就会一直锁定 `light` 或 `dark`，既无法在系统日落切换暗色时自动同步，也缺少“恢复跟随系统”的入口，多个已打开的标签页之间也不同步。
   - 多个页面和模块在写死颜色或使用未定义变量名，导致夜间模式下出现“刺眼白块”甚至“白底浅字看不见输入内容”的故障。
3. **未来改一套主题色工作量巨大**：各子应用 CSS 分散定义或硬编码色值，想把主色调换成另一种风格，往往需要翻改十几个 CSS 文件，重复劳动极多。

### 1.2 核心建设目标
- **改色零重复工**：建立集中式 **Design Tokens（设计令牌，即按用途命名的 CSS 变量集合）**，以后新增或替换一套全站配色，只需在一处新增几十行令牌声明，**不需要修改各业务 App 的 CSS 或 HTML 模板**。
- **白天有层次、黑夜不刺眼、系统变化实时跟**：区分“页面画布、正文阅读面、次级工具面、浮层弹窗”四层表面，提供 **「跟随系统 / 白天 / 黑夜」三态切换**，支持操作系统昼夜变化时无需刷新实时同步。
- **一套规则覆盖全站与第三方插件**：前台门户、详情页、列表时间线、评论区、作者专栏、会员登录/注册表单，以及 `Highlight.js`、`Mermaid`、`Tom Select`、`Flatpickr`、`Splide`、`Video.js`、`KaTeX` 全部遵循同一套底层协议。

---

## 二、 现状核查清单（必须一并清理的真实缺陷）

对照当前代码（基线 `a069440`），以下问题将在实施阶段纳入迁移清单逐一修复：

| 问题编号 | 位置与现象 | 根因说明 |
| --- | --- | --- |
| **TH-01** | `myblog/templates/identity/` 全部 4 个前台模板（`login.html`、`register.html`、`profile.html`、`force_password_change.html` 第 6 行起） | 4 个模板各自内联复制了 `<style>` 块，固定深色标签与白色输入框底色，但输入文字继承 `--text-main`；夜间模式下变成**白底近白字、输入内容几乎看不见**；`profile.html` 另有多处硬编码 `#f8fafc`/`#334155`。 |
| **TH-02** | `myblog/static/css/myblog.css`（第 46、408 行） | 深色模式主色 `--color-primary: #38bdf8`，但主按钮仍写死白色文字，两者对比度仅约 **`2.14:1`**（远低于 WCAG AA 标准的 `4.5:1`）。 |
| **TH-03** | `myblog/static/js/ui-components.js`（第 14–56 行）与 `myblog/templates/base.html`（第 22 行） | 首屏脚本与运行期脚本各自写了一遍主题判断，存储异常时行为不一致；且无 `system` 模式、无 `matchMedia` `change` 事件监听、无跨标签页 `storage` 同步。 |
| **TH-04** | `myblog/static/comments/css/comments_widget.css` | 评论标题、表单背景、回复提示等多处写死 `#ffffff` / `#f8fafc` / `#fffbeb`，深色模式下整块评论区表现为亮白孤岛。 |
| **TH-05** | `myblog/static/people/css/author_directory.css` | 使用未在全局定义的 `--card-bg`、`--surface-muted`、`--text-color`，全局定义的是 `--bg-card` / `--color-surface`，导致夜间模式退回浅色回退值。 |
| **TH-06** | `blog/css/article.css` 与 `content_blocks/css/{table,structured-blocks,mermaid-block,embed-block,media-blocks,code-highlighting}.css` | 大量硬编码 `#ffffff` / `#f8fafc` / `#f1f5f9`；`code-highlighting.css`（第 15、110–115、166 行）写死 `#0d1117` 背景、`!important` 与折叠渐变遮罩，第 132 行行号 `#484f58` 对比度仅 **`2.28:1`**，第 178–188 行无 JS Pygments `.hl-*` 仅适配暗底；前台 `_block_assets_css.html` 固定引入 `github-dark.min.css`。 |
| **TH-07** | `myblog/apps/content_blocks/blocks/markdown.py:141`、`rendering/mermaid.py:11,28`、`mermaid-renderer.js:15`、`admin-mermaid-split.js:12,241,609` | Markdown ` ```mermaid ``` ` 围栏固定传入 `theme="default"`；且前后台 4 处白名单均仅允许 `default/dark/forest/neutral`，不支持跟随站点昼夜。 |
| **TH-08** | `myblog/templates/base.html`（第 32–33、161 行） | `myblog.css` 加载在 `_ui_assets_css.html`（`tom-select`、`flatpickr`、`splide`）之前，导致同权重插件适配规则易被后加载的 vendor 白底样式覆盖；另同步请求了空文件 `static/js/myblog.js`。 |

> **核查澄清**：`myblog/static/css/myblog.css`（第 833 行）已将 `.bg-white`、`.bg-light` 映射至 `--color-surface` 与 `--color-surface-subtle`，只是语义类名带有旧包遗留痕迹。迁移时应优先消除真实硬编码与变量名分裂，并逐步将模板中的 `.bg-white` 等收敛为语义工具类（如 `.u-surface`）。

---

## 三、 架构设计与技术方案

### 3.1 底层框架选型：零构建原生 CSS 设计令牌协议

因为 `myblog` 采用纯 Django/Wagtail 模板服务端渲染 + 离线 `vendor` 资产管理（无 Node/Webpack 前端编译链路），且已具备大量自定义内容块（代码高亮、Mermaid、表格、KaTeX、Video.js），**不额外引入 Bootstrap 5.3、DaisyUI 或 Pico.css 等全站样式框架**，避免同名类（如 `.card`、`.btn`）权重打架及额外维护成本。

借鉴 **Open Props** 的变量分层、**Radix Colors** 的用途色阶与 **VitePress / Starlight** 的配置结构，直接采用原生 CSS Custom Properties（CSS 自定义属性）构建 **三层令牌架构**：

1. **第一层：基础色阶（Primitive Tokens）**
   - 仅定义实际使用的调色板色阶（如 `--palette-mist-50`、`--palette-blue-700`），严禁业务组件直接引用基础色阶。
2. **第二层：语义令牌（Semantic Tokens）**
   - 按“界面用途”命名，**强制成对配置前景色与背景色**，彻底避免类似“亮底白字”或“蓝底白字对比度不足”的失配：
     - **四层表面、遮罩与边框**：`--color-bg`（页面底）、`--color-surface`（卡片/正文阅读面）、`--color-surface-subtle`（次级工具面/侧栏/代码块头部）、`--color-surface-raised`（弹出菜单/`<dialog>` 浮层）、`--color-backdrop`（统一目录抽屉、Mermaid 全屏 `<dialog>` 与图片灯箱的 `::backdrop` 遮罩色）、`--color-border`、`--color-border-strong`；
     - **文字与交互**：`--color-text`（正文主字）、`--color-text-secondary`、`--color-muted`、`--color-link`、`--color-link-hover`、`--color-focus`；
     - **成对按钮与强调色**：`--color-primary` 必须与 `--color-on-primary` 成对声明（例如深色模式浅蓝按钮 `#38bdf8` 配深色文字 `#0f172a`），另有 `--color-primary-hover`、`--color-primary-soft`（浅徽章底）与 `--color-on-primary-soft`；
     - **状态色**：`--color-success` / `--color-warning` / `--color-danger` 各自的底色（`*-soft`）、文字色与边框色。
3. **第三层：组件令牌（Component Tokens）**
   - 仅在语义令牌不足以表达专有领域时定义，且统一声明在对应组件作用域并在令牌文件统一覆写浅/深映射：
     - 代码块语法高亮令牌（`--code-bg`、`--code-header-bg`、`--code-gutter`、`--code-fade-to`、`--code-kw`、`--code-str`、`--code-comment` 等，同时服务 Highlight.js `.hljs-*` 与无 JS 降级 Pygments `.hl-*`）；
     - 时间线日历热力图 4 级色阶（`--cal-heat-1` ～ `--cal-heat-4`，独立于成功/警告状态色，且保留数字/文字说明）；
     - 表格表头与斑马纹（`--table-head-bg`、`--table-stripe-bg`）。
4. **打印与系统高对比度兜底**：
   - 令牌表内统一定义 `@media print`，打印时强制切回浅色纸面与深色文字（避免暗黑模式下印出整页黑底浪费油墨）；
   - 保留系统强制颜色模式 `@media (forced-colors: active)` 的原生边框与 `:focus-visible` 焦点环可见性。

### 3.2 双维度 HTML 协议与首期两套昼夜色板

在 `<html>` 根节点上使用两个正交（互不干扰）的数据属性：
- `data-theme="light" | "dark"`：表示**当前实际生效的明暗状态**，同步控制 CSS `color-scheme: light | dark`（使原生滚动条、日期框、复选框自动跟随）。
- `data-palette="mist-blue" | "moss-lab"`：表示**当前站点的主题色板**。以后新增第 3 套“暖砂书房”或更多主题色，只需在令牌文件中新增 `[data-palette="..."]` 块并配齐浅/深语义映射，所有组件自动换肤，**业务 CSS 零改动**。

#### 首期预设色板规范（均自带昼/夜配套且通过 WCAG AA 对比度计算校验）

| 色板标识 | 适用定位 | 白天三层底色（画布 / 阅读面 / 次级面） | 黑夜三层底色（画布 / 阅读面 / 次级面） | 品牌强调色（昼 / 夜）与配对文字 |
| --- | --- | --- | --- | --- |
| **`mist-blue`**<br>（雾蓝工程手册 · **默认**） | 理性、清晰、工程技术感；一眼解决“白天全是白”的无层次感 | `#EEF2F7` / `#F8FAFC` / `#E8EEF6` | `#151B25` / `#1E2836` / `#293545` | 昼 `#1D4ED8`（配白字 `#FFFFFF`）<br>夜 `#93C5FD`（实心按钮配深字 `#0F172A`） |
| **`moss-lab`**<br>（苔青实验室 · **备选预设**） | 沉静低疲劳护眼绿灰调，适合长文深度阅读与个人风格展示 | `#F0F3EE` / `#F8FAF5` / `#E6EEE8` | `#151E1C` / `#202C28` / `#2A3933` | 昼 `#0F6658`（配白字 `#FFFFFF`）<br>夜 `#7BD9BE`（实心按钮配深字 `#0F1F1B`） |

> **对比度硬约束（WCAG 2.2 AA）**：普通正文与控件文字对比度 $\ge 4.5:1$（当前样色链接与核心阅读面最低已达 `5.63:1`），大号标题及关键控件边界/焦点环 $\ge 3:1$；错误信息与热力图禁止仅靠颜色传递（必须配合文字/图标）。

### 3.3 三态昼夜切换、单源防闪烁（FOUC）与跨标签同步

- **三态偏好存储**：`localStorage` 键名沿用 `myblog_theme_preference`，允许值为 `"system"`（默认）、`"light"`、`"dark"`。首次访问、值非法或存储不可用（隐私模式抛出异常）时均回落为 `"system"`；已有老用户的 `"light"`/`"dark"` 保持为显式手选。
- **单源解析与防 FOUC（无样式内容闪烁）**：
  - 将纯函数解析逻辑封装于 `base.html` `<head>` 首个样式表前的极简早载引导块并暴露 `window.__myblogTheme`（同时清理 `base.html:161` 对空文件 `static/js/myblog.js` 的无效同步请求，或将其收敛为主题/基础交互统一入口），运行期 `ui-components.js` 直接复用同一解析函数，**禁止首帧与运行期各自维护两套可能分叉的判断逻辑**。
  - 关闭 JavaScript 时，令牌表同时在 `@media (prefers-color-scheme: dark)` 下提供针对 `:root:not([data-theme="light"])` 的纯 CSS 暗黑兜底。
- **实时联动、导航兼容、bfcache 与 HTMX 同步**：
  - 仅当当前偏好为 `"system"` 时，监听 `window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", ...)`，操作系统昼夜变化时页面**无需刷新即时重绘**；
  - 监听跨标签页 `window.addEventListener("storage", ...)` 与浏览器前进后退缓存事件 `window.addEventListener("pageshow", ...)`（处理 `bfcache` 恢复后与其他标签页或系统偏好不同步的问题）；
  - 在 `htmx:afterSwap`（以及现有列表/时间线片段替换回调）后保持新插入 DOM 与当前主题一致（无需销毁已有实例）；
  - 顶部导航的“外观”控件升级为带文字与清晰选中态的三态切换器（`跟随系统` / `白天` / `黑夜`），**外层或主触发元素保留 `data-theme-toggle` 兼容属性**（确保不破坏既有回归测试 `tests/test_detail_shell_and_toc.py:310`）。
- **权限与缓存红线（防止 CDN 串色）**：首期仅站长配置全站默认色板，**访客三态偏好仅留在浏览器端，不通过 Cookie 参与服务端共享 HTML 缓存拼接**，确保匿名页或 CDN 缓存绝对安全。

### 3.4 后台站点主题配置与缓存解耦（`site_settings`）

- **独立 `SiteThemeSettings` 单例模型**（位于 `site_settings` App，使用 `@register_setting` 注册到 Wagtail 后台设置菜单，但**独立于 `BaseSiteSetting`**）：
  - 字段：`singleton_key`（固定为 `1`，唯一与 `CHECK(singleton_key=1)` 约束）、`default_palette`（`choices=[("mist-blue", "雾蓝工程手册"), ("moss-lab", "苔青实验室")]`，默认 `"mist-blue"`）、`default_appearance_mode`（`choices=[("system", "跟随系统"), ("light", "白天（浅色）"), ("dark", "黑夜（深色）")]`，默认 `"system"`）、`version`、`updated_at`。
  - **严禁继承会递增 `ContentDiscoveryEpoch` 的 `BaseSiteSetting.save()`**：改一次网站主题颜色不能使全站千万级搜索分页游标、时间线归档缓存和 RSS ETag 全部失效。外观配置采用独立缓存键 `site_theme_settings:v1`，设置有限 TTL（`300` 秒兜底防 Redis 抖动脏读），并在 `save()` 事务提交成功后通过 `transaction.on_commit()` 清理/刷新缓存。
  - **公开 GET 绝对只读（避开 Wagtail `{% get_settings %}` 隐式写库）**：经核查本机 Wagtail 8.1 源码，`BaseGenericSetting.load()` 与模板标签 `{% get_settings %}` 内部会调用 `cls._get_or_create()`，空表时会在公开 GET 触发数据库 `INSERT`。因此前台 `base.html` **严禁通过 `settings.site_settings.SiteThemeSettings` 读取**，必须提供专用只读模板标签 `{% get_theme_settings as theme_cfg %}`：先读独立缓存 `site_theme_settings:v1` → 未命中仅执行只读查询 `SiteThemeSettings.objects.filter(singleton_key=1).first()` → 若表为空直接返回内存默认实例，彻底保证公开 `GET` 零写库。
  - **安全输入边界**：首期仅开放经过对比度与安全审核的受控预设色板枚举（不允许自由粘贴原始 CSS 或未校验色值，杜绝 CSS 注入与不可读配色）。
- **前后台严格隔离**：前台访客切换外观绝不影响 Wagtail 后台自身的 `w-theme-*` 壳层；后台用户管理页 `myblog/templates/identity/admin/user_manage.html` 归口至 `admin-workbench.css`，不混入前台色板；后台预览面板亦不得读取 Wagtail 外壳的 `w-theme-dark` 覆盖站点预览规则。

### 3.5 Wagtail 实时预览面板与第三方组件联动方案

1. **Wagtail 编辑预览（`request.in_preview_panel`）**：
   - 预览页默认按站点正式 `SiteThemeSettings`（默认色板 + 默认策略）渲染，不读取、不改写公开站的 `localStorage` 访客偏好，也不将临时预览模式存入文章草稿；
   - 仅当 `request.in_preview_panel` 为真时，在预览顶角提供极简的**“预览外观：站点默认 / 白天 / 黑夜”临时切换条**（存于当前预览会话内存或 `sessionStorage` 独立键），连续实时刷新编辑内容时不重置编辑刚才选中的预览明暗。
   - **防新标签页弹出约束**：因 `base.html:27-29` 在预览态会输出 `<base target="_blank">`，预览外观切换条必须**全部使用 `<button type="button">`**，严禁使用 `<a href="#">` 导致点击后意外弹出空白新窗口。
2. **Highlight.js 代码块**：
   - 前台 `_block_assets_css.html` 移除硬编码的 `github-dark.min.css` `<link>`，改由 `code-highlighting.css` 将 `.hljs-*` 语法类、**无 JS/SSR 降级 Pygments `.hl-*` 语法类**、代码块工具栏/边框、行号（对比度从 `2.28:1` 提升至可读水平）以及超长代码折叠遮罩（`.is-code-collapsed .code-body::after`）统一映射到浅/深两套高对比度语义令牌，同时清理第 110–115 行写死的 `#0d1117 !important`。切换昼夜时**无需重新高亮 DOM，零性能损耗、不丢失代码折叠与滚动位置**。
   - **后台编辑器隔离与测试契约同步**：保留 `content_blocks/widgets.py:110` 后台分栏编辑器对 `github-dark.min.css` 的独立引用不受影响；同步更新 `tests/test_p1_content_enhancements.py:346` 对前台固定 `github-dark.min.css` 的旧断言，改为校验双主题语法令牌契约。
3. **Mermaid 流程图（关键兼容性契约）**：
   - Markdown 围栏 ` ```mermaid ``` `（`markdown.py:141`）由写死 `theme="default"` 改为 `theme="follow-site"`；独立 `CustomMermaidBlock`（`blocks/mermaid.py`）新增 `("follow-site", "跟随页面昼夜")` 选项并设为新建块默认值，**既有文章中已保存的 `default` / `forest` / `dark` / `neutral` 原样保留，绝不批量改写历史内容**。
   - **StreamField 元数据迁移说明**：修改 `CustomMermaidBlock.THEME_CHOICES` 与 `default="follow-site"` 会触发 `blog`、`guides`、`portfolio` 三个 App 生成非破坏性 `StreamField` 元数据迁移（仅更新 Django 迁移状态中的可选列表与默认值，不改写 PostgreSQL/MongoDB 既有历史记录，也不影响 `with_content_json()` 历史 Revision 回溯反序列化），实施阶段须显式生成并执行 `makemigrations --check --dry-run` 核验。
   - **五处白名单与前后台编辑器同步**：`"follow-site"` 是站内策略值，不可直接传给 Mermaid 引擎。必须同步扩展以下 5 处，防止被静默回落为 `"default"`：
     1. `content_blocks/blocks/mermaid.py`（`THEME_CHOICES` 与 `default="follow-site"`）；
     2. `content_blocks/rendering/mermaid.py`（`ALLOWED_THEMES` 允许 `"follow-site"`，输出容器保留 `data-theme="follow-site"`）；
     3. `content_blocks/static/content_blocks/js/mermaid-renderer.js`（解析策略值映射至真实 Mermaid 主题 `default`/`dark`）；
     4. `content_blocks/static/content_blocks/js/mermaid-block.js`（监听站点主题变更，仅重绘 `follow-site` 且未被读者手动覆盖的图；旧浅色图夜间暗化外框与工具栏并支持工具栏单图临时切明暗）；
     5. `content_blocks/static/content_blocks/js/admin-mermaid-split.js`（`THEME_LABELS` 补齐 `"follow-site"`；后台分栏预览中将其映射为 Wagtail 后台当前 `w-theme-dark` 或浅色态，且第 609 行 2× PNG 导出画布底色按解析后的真实主题判定）。
   - **防竞态与埋点保护**：`mermaid-block.js` 区分“最新期望主题”与“上次已渲染主题”，共用每图递增序号 `renderSeq` 丢弃快速连切时的过期异步结果；若全屏 `<dialog>` 弹窗正处于打开状态，切换主题时同步重绘弹窗 SVG 与导出缓冲；**重绘只更新 SVG 节点，绝不销毁实例或重复触发第 3 期的 `mermaid_view` 埋点**。
4. **Tom Select / Flatpickr / Splide / Video.js / KaTeX / 正文图片**：
   - Tom Select 与 Flatpickr 弹层常挂载在 `<body>` 根部，因此必须统一消费根节点 `[data-theme]` / `[data-palette]` 令牌，覆盖输入、下拉高亮、日期选中态与范围态；**切换主题时不销毁组件实例，完整保留已输入搜索词、日期和焦点**；
   - Video.js 延续既有 `color-scheme` 桥接，切主题不重建播放器、不中断播放；KaTeX 公式继承 `--color-text`；
   - 正文插图、透明 PNG 与第三方 `<iframe>` 嵌入卡片不统一施加 `invert()` 反色滤镜，只统一容器外框与工具栏样式。

---

## 四、 样式级联与分阶段实施计划

### 4.1 修正加载顺序并规避 `@layer` 迁移陷阱
- **调整 `base.html` 样式加载顺序**：将现有倒置的 `myblog.css → _ui_assets_css.html (vendor) → extra_css` 显式调整为：
  **`themes/tokens.css`（三层设计令牌）→ `_ui_assets_css.html`（Tom Select / Flatpickr / Splide vendor 样式）→ `myblog.css`（公共组件与第三方主题适配）→ `extra_css`（页面与内容块样式）**，确保同权重适配规则自然覆盖 vendor 默认白底，避免 `!important` 权重战。
- **规避 `@layer` 级联倒挂**：因评论样式等在局部模板加载且既有文件未分层，在旧样式全部收敛前不贸然对单个新文件套 `@layer`（防止未分层普通规则反压分层规则）；待各阶段清点并消除高冲突 `!important` 后，再评估全链统一分层。

### 4.2 实施分期
1. **阶段 1：底座与单源状态机**
   - 在 `myblog/static/css/themes/tokens.css` 落地三层设计令牌（含 `mist-blue` 与 `moss-lab` 浅/深完整映射、`--color-backdrop`、`@media print` 与 `@media (prefers-color-scheme: dark)` 无 JS 兜底）；调整 `base.html` 加载链并清理空文件 `myblog.js` 同步请求；
   - 升级 `base.html` + `ui-components.js` 为三态单源主题控制器（支持系统实时监听、`storage` 跨标签同步、`pageshow` `bfcache` 恢复、`htmx:afterSwap` 保持、三态外观菜单并保留 `data-theme-toggle` 兼容标记）；
   - 新增独立 `site_settings.SiteThemeSettings` 单例模型（含迁移、独立缓存 `site_theme_settings:v1` / `300s` TTL / `transaction.on_commit()` 刷新）、专用只读模板标签 `{% get_theme_settings %}`（严禁前台走 `{% get_settings %}`）及基于 `<button type="button">` 的 Wagtail 预览临时外观控制条。
2. **阶段 2：破除“白块”与全站 CSS/模板收敛**
   - 将 `templates/identity/` 下全部 4 个前台模板（`login.html`、`register.html`、`profile.html`、`force_password_change.html`）的内联 `<style>` 抽离至公共表单样式文件，彻底修复夜间白底白字；同时将后台 `templates/identity/admin/user_manage.html` 归口 `admin-workbench.css`；
   - 修复 `myblog.css` 深色主按钮对比度（成对 `--color-primary` / `--color-on-primary`）、`comments_widget.css`、`author_directory.css`（收敛 `--card-bg`/`--surface-muted`/`--text-color`）、`blog/article.css`、`analytics/article-rating.css`、`discovery` 列表与时间线热力日历；
   - 清除业务 CSS 与模板硬编码浅色孤岛，保留旧变量单向映射过渡。
3. **阶段 3：正文积木与第三方插件全联动**
   - 切换前台 `Highlight.js` 为双主题语法令牌（同步覆写无 JS Pygments `.hl-*`、行号对比度与折叠遮罩渐变），保持 `widgets.py:110` 后台独立引用不动，并同步更新 `tests/test_p1_content_enhancements.py:346` 断言；
   - 完成 Mermaid 5 处白名单同步（`blocks/mermaid.py`、`rendering/mermaid.py`、`mermaid-renderer.js`、`mermaid-block.js`、`admin-mermaid-split.js`）、生成 `blog`/`guides`/`portfolio` 三个 App 的非破坏性 StreamField 元数据迁移，落地全屏弹窗同步与防竞态修复；
   - 完善 `Tom Select`、`Flatpickr`（含 `body` 弹层）、`Splide`、`table.css`、`structured-blocks.css`、`embed-block.css`、`media-blocks.css` 的双主题/双色板适配。
4. **阶段 4：自动化校验与真机多视口验收**
   - 用 Python 编写基于确定性规则的令牌完整性与配对对比度静态检查测试（纳入 CI 门禁）；
   - 启动隔离测试服务完成 Playwright 真实浏览器验收。

---

## 五、 怎么验证与尚未解决的边界

### 5.1 核心验收矩阵
1. **主题切换、生命周期与降级**：首次打开跟随系统；不刷新切换 OS 昼夜，网页及打开的下拉/日历/弹窗实时响应；手动切为白天/黑夜后不被系统抢回；恢复“跟随系统”即时生效；多标签页同步、浏览器前进后退 `bfcache` 恢复同步、`HTMX` 局部换页同步；禁用 `localStorage` 时当前页仍可切换；禁用 JS 时首屏与 `@media (prefers-color-scheme: dark)` 兜底正常；空表公开 `GET` 期间 `SiteThemeSettings` 零写库；保存 `SiteThemeSettings` 不递增 `ContentDiscoveryEpoch`。
2. **对比度、表单、打印与高对比度**：在 `mist-blue` 与 `moss-lab` 的浅/深模式下，实测全部 4 个前台身份页（登录/注册/资料/强制改密）输入打字与错误提示、评论区发表/回复、作者专栏、首页 Hero 与轮播、列表筛选项、时间线日历、博客代码块（含无 JS Pygments 与行号）/Mermaid/复杂表格/KaTeX、指南步骤卡、项目仓库卡、Wagtail 预览面板（`<button>` 不弹新窗）及后台 Mermaid 分栏编辑器预览与 PNG 导出；核验 `@media print` 浅色打印回退与 `@media (forced-colors: active)` 焦点环。
3. **零状态丢失与迁移一致性**：切主题时评论草稿不丢失、视频不中断、搜索词与日期不清空、Mermaid 缩放与打开中的全屏弹窗同步重绘且不重复上报 `mermaid_view`；`makemigrations --check --dry-run` 无残留差异且历史 Revision `with_content_json()` 反序列化正常。
4. **改色效率证明**：通过编写临时第 3 套色板测试用例验证“只增令牌声明即可全站换肤，无需修改各 App 模板与 CSS”。

### 5.2 本期明确不做与后续边界
- **暂不开放访客自定义颜色选择器**：首期仅由站长在后台选择预设色板，访客控制昼夜三态；若后续需要开放访客切换多套预设色板，只需在顶部外观菜单挂载 `[data-palette]` 选择器即可，无需改动底层 CSS。
- **暂不强行染色外部媒体与 Wagtail 后台外壳**：文章插图、第三方嵌入视频播放器画面保持原貌；Wagtail 后台继续使用官方 `w-theme-*` 控制。

---

## 六、 审核与修订记录

- **2026-11-24 方案起草**：由 `pm`、`sol`、`terra` 经两轮只读源码核查与交叉讨论形成初稿，并经用户确认两套色板（`mist-blue` 默认 + `moss-lab` 备选）、Markdown Mermaid 默认 `follow-site` 及 Wagtail 预览临时昼夜切换范围。
- **2026-11-24 深度源码与 Wagtail 8.1 生命周期审核定稿**：对照本机 Wagtail 8.1 源码、StreamField 历史迁移、后台编辑器与既有测试完成二次审查，补充落地以下 3 项 P0、4 项 P1 与 2 项 P2 约束：
  - **P0-1**：禁止前台经 `{% get_settings %}` 读取 `SiteThemeSettings`，改用专用只读标签 `{% get_theme_settings %}` + 独立缓存（`300s` TTL + `transaction.on_commit()` 刷新），彻底阻断空表公开 `GET` 写库；
  - **P0-2**：明确 Mermaid `follow-site` 需同步修改前后台 5 处白名单与编辑器 PNG 导出逻辑，并列明 `blog`/`guides`/`portfolio` 3 个 App 的非破坏性 StreamField 元数据迁移；
  - **P0-3**：将 `templates/identity/` 全部 4 个前台身份模板的内联 `<style>` 统一抽离收敛至公共表单样式，同时明确后台 `user_manage.html` 归口 `admin-workbench.css`；
  - **P1-1 ~ P1-4**：补全无 JS Pygments `.hl-*` 与折叠遮罩令牌覆写（保留后台 `widgets.py:110` 引用，同步测试断言与 `data-theme-toggle` 兼容）；调整 `base.html` 样式加载顺序至 vendor 之后；预览切换条强制使用 `<button type="button">` 避开 `<base target="_blank">`；补充 `pageshow` (`bfcache`) / `htmx:afterSwap`、`--color-backdrop`、`@media print` 与 `@media (forced-colors: active)` 验收条款；
  - **P2**：纳入清理 `base.html:161` 空文件 `myblog.js` 同步请求与独立缓存 TTL 兜底设计。
