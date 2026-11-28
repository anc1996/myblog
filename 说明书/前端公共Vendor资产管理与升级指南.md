# 前端公共 Vendor 资产管理与升级指南

## 一、 目标：为什么要这样管理

1. **不往 GitHub 传大体积第三方库**：`Mermaid`（约 3.5 MB）、`KaTeX`（含 60 多个字体文件）、`Highlight.js` 多语言包、`Vditor`、`Video.js 10` 及全站 UI 组件库均属于外部开源库打包产物，放进 Git 会让仓库膨胀、每次 `git diff` 很难看。现在由 `.gitignore` 忽略 `myblog/static/vendor/`，仓库里只保留一份很小的锁定清单 `myblog/static/vendor.lock.json`。
2. **让 AI 智能体省上下文、专心看业务代码**：在 `AGENTS.md` 中已经约定，智能体日常开发、查 Bug 或做代码审查时，**不主动读取或扫描 `myblog/static/vendor/`**，平时主要关注 `content_blocks` 里的公共渲染和交互逻辑；只有在做第三方库升级或特定前端对接方案时，才按需查看锁文件与本指南。
3. **版本可锁定、可校验、可一键还原**：靠 `vendor.lock.json` 记录精确版本号与 `SHA-512` 校验值，配合纯 Python 标准库脚本 `scripts/sync_vendor_assets.py`（无需安装 Node.js/npm），任何新环境、Docker 镜像或换电脑都能一键恢复出字节级一致的静态资源。

---

## 二、 文件分工清单

| 路径 | 是否提交 Git | 职责说明 |
|---|---|---|
| `myblog/static/vendor.lock.json` | **提交** | 第三方库版本锁文件，记录包名、`npm_package` 映射、版本、官方 npm 下载地址、SHA-512/SHA-1 校验值与精确提取路径 |
| `scripts/sync_vendor_assets.py` | **提交** | 同步、校验与升级脚本（纯 Python 实现，自带路径穿越防护和压缩包校验） |
| `myblog/static/vendor/` | **不提交（已忽略）** | 实际运行时的第三方静态产物目录（包含下方锁定的全部 11 个包目录，以及自动生成的 `UPSTREAM.md`、`.vendor-manifest.json`） |
| `myblog/templates/includes/_ui_assets_css.html`<br>`myblog/templates/includes/_ui_assets_js.html` | **提交** | 全站基础 UI 资产引入片段（`htmx`、`tomselect`、`flatpickr`、`splide`、`alpinejs` 与 `ui-components.js`），由 `base.html` 统一加载 |
| `myblog/static/js/ui-components.js` | **提交** | 全站公共 UI 统一桥接器（`MyblogUI`），负责三态昼夜主题联动、Tom Select、Flatpickr、Splide 挂载及 HTMX CSRF/局部刷新重挂 |
| `myblog/apps/content_blocks/static/content_blocks/` | **提交** | 我们自己编写的公共积木交互层（`stage-viewer.js`、`mermaid-renderer.js`、`mermaid-block.js`、`math-render.js`、`code-highlighting.js`、`code-copy.js`、`media-player.js`、`admin-vditor-markdown.js`、`admin-media-split.js`、`admin-mermaid-split.js`、`admin-table-split.js`、`content-images.js`、`embed-render.js`、`table-block.js` 及对应 CSS），负责对接调用 `vendor/` |
| `myblog/apps/content_blocks/templates/content_blocks/includes/` | **提交** | 正文积木资源引入片段（`_block_assets_css.html`、`_block_assets_js.html`），供 `BlogPage`、`GuidePage`、`ProjectPage` 统一复用 |

### 当前已锁定的 11 个公共 Vendor 包（以 `myblog/static/vendor.lock.json` 为准）

| 本地键名 (`vendor/<键名>/`) | 真实 npm 包名 (`npm_package`) | 锁定版本 | 主要用途与对接入口 |
|---|---|---|---|
| `mermaid` | `mermaid` | `11.17.2` | 流程图安全渲染（`mermaid-renderer.js`、`mermaid-block.js`、`admin-mermaid-split.js`） |
| `katex` | `katex` | `0.19.1` | LaTeX 数学公式与字体（`math-render.js`、`_block_assets_css.html`） |
| `highlightjs` | `@highlightjs/cdn-assets` | `11.12.0` | 前台代码按需语言高亮与后台代码分栏预览（`code-highlighting.js`、`widgets.py`） |
| `vditor` | `vditor` | `3.11.2` | Wagtail 后台 Markdown 分栏编辑器（`admin-vditor-markdown.js`、`widgets.py`） |
| `videojs` | `@videojs/cdn` | `10.5.0` | 前台与后台分栏站内音视频播放器增强（`media-player.js`、`_block_assets_js.html`） |
| `alpinejs` | `alpinejs` | `3.17.5` | 轻量声明式交互组件（`_ui_assets_js.html`） |
| `htmx` | `htmx.org` | `2.0.11` | 局部片段异步替换（`_ui_assets_js.html`、`ui-components.js`） |
| `tomselect` | `tom-select` | `2.7.0` | 可搜索下拉框与作者/标签多选胶囊（`_ui_assets_*`、`ui-components.js`） |
| `flatpickr` | `flatpickr` | `4.6.13` | 统一日期/范围选择器及中文语言包（`_ui_assets_*`、`ui-components.js`） |
| `splide` | `@splidejs/splide` | `4.1.4` | 首页与专题页响应式轮播组件（`_ui_assets_*`、`ui-components.js`） |
| `panzoom` | `@panzoom/panzoom` | `4.6.2` | 公共沉浸式画布缩放与拖拽内核，统一供 `stage-viewer.js`（Mermaid 复杂流程图全屏与图片灯箱）按需懒加载调用 |

---

## 三、 新环境拉取代码后怎么恢复（日常使用）

在项目根目录（WSL2 `/mnt/f/openclaw/workspace/wagtail/myblog`）执行：

```bash
# 1. 按 vendor.lock.json 下载并校验提取所需文件（已存在且版本一致会自动跳过）
/root/anaconda3/envs/wagtailblog-test/bin/python scripts/sync_vendor_assets.py sync

# 2. 核对本地文件与锁文件是否完整一致（通过返回退出码 0）
/root/anaconda3/envs/wagtailblog-test/bin/python scripts/sync_vendor_assets.py check
```

- **如果处于无外网的离线/内网环境**：先在有网机器把 `.tgz` 包放进某个目录（例如 `/tmp/vendor-cache/`，文件名为 `mermaid-11.17.2.tgz`、`katex-0.19.1.tgz`），然后加上 `--cache-dir` 参数：
  ```bash
  /root/anaconda3/envs/wagtailblog-test/bin/python scripts/sync_vendor_assets.py sync --cache-dir /tmp/vendor-cache --force
  ```
- **部署收集静态资源前**：直接运行一次 `python scripts/sync_vendor_assets.py sync && python scripts/sync_vendor_assets.py check`，再执行 `python manage.py collectstatic --noinput`，无需手工拷贝静态包。

---

## 四、 以后怎么升级公共前端库版本

当 Mermaid 或 KaTeX 发布新版本或安全补丁时，按下面 5 步操作：

### 第 1 步：预览升级信息（不改文件）

```bash
/root/anaconda3/envs/wagtailblog-test/bin/python scripts/sync_vendor_assets.py upgrade mermaid 11.18.0 --dry-run
```

### 第 2 步：更新锁文件并同步到本地

```bash
/root/anaconda3/envs/wagtailblog-test/bin/python scripts/sync_vendor_assets.py upgrade mermaid 11.18.0 --sync
```

脚本会自动从 `registry.npmjs.org` 拉取新版本的下载链接与 `SHA-512` 校验和，写回 `myblog/static/vendor.lock.json`，然后立即校验并重新提取 `myblog/static/vendor/mermaid/`。

### 第 3 步：核对我们的公共胶水代码是否兼容

第三方库大版本升级时，重点核对以下 3 处（其余业务页面不用改）：
1. **API 调用与安全开关**：例如 `myblog/apps/content_blocks/static/content_blocks/js/mermaid-renderer.js` 中的 `mermaid.initialize({ securityLevel: "strict", htmlLabels: false, ... })` 与 `mermaid.render(...)` 签名是否变化（`mermaid-block.js` 负责在此之上挂载工具栏、缩放与全屏弹窗）；
2. **版本号徽标与安全选项**：`myblog/apps/content_blocks/rendering/mermaid.py` 界面显示的徽标文本（如 `Mermaid 11.17`）与 `math-render.js` 中 `katex.render(..., { trust: false })`；
3. **压缩包内路径变化**：若新版改变了 `dist/` 目录文件名，在 `myblog/static/vendor.lock.json` 的 `extract` 列表里对应调整即可。

### 第 4 步：跑完整验证（缺一不可）

```bash
# 1. 核对 vendor 完整性
/root/anaconda3/envs/wagtailblog-test/bin/python scripts/sync_vendor_assets.py check

# 2. 跑生产静态资源收集演练（确认 CSS 字体和资源路径没报错）
MYBLOG_ENV_FILE=.env.test DJANGO_SETTINGS_MODULE=myblog.settings.production \
  DJANGO_SECRET_KEY="prod-dry-run-secret-key-at-least-50-unique-chars-123456" \
  ALLOWED_HOSTS="localhost,127.0.0.1" \
  DJANGO_STATIC_ROOT=/tmp/myblog_collectstatic_check \
  /root/anaconda3/envs/wagtailblog-test/bin/python -B manage.py collectstatic --dry-run --noinput

# 3. 跑定向与全站测试（重点看 test_vendor_sync、test_embed_playback、test_admin_split_editors、test_s3_blocks、test_s4_table、test_p1_content_enhancements）
MYBLOG_ENV_FILE=.env.test /root/anaconda3/envs/wagtailblog-test/bin/python -B manage.py test \
  tests.test_vendor_sync tests.test_embed_playback tests.test_admin_split_editors --keepdb
MYBLOG_ENV_FILE=.env.test /root/anaconda3/envs/wagtailblog-test/bin/python -B manage.py test tests --keepdb
```

然后启动开发服务器，用真实浏览器打开一篇包含 Mermaid 与 LaTeX 公式的文章，确认无控制台报错且放大、导出 SVG、全屏弹窗正常。

### 第 5 步：只提交锁文件与代码改动

确认无误后执行 `git status`，确认 `myblog/static/vendor/` 没有出现在待提交列表里；只提交 `myblog/static/vendor.lock.json` 和对应修改的胶水代码即可。

**音视频相关注意事项：** Video.js 的音频、视频入口会通过相对路径加载同版本模块，不能只保留两个入口。Vditor 的字体路径和无效调试映射由同步脚本在提取后处理；安装规则变更后，即使依赖版本未变，`sync` 也会重建受影响的包。升级这两项时，除 `check` 外，还要在临时输出目录实际执行 `collectstatic`，再打开页面验证模块、字体和播放器。

---

## 五、 以后怎么新增一个公共前端库

如果后续需要新增其他全站公共库（例如代码高亮库或图表库）：
1. 在 `myblog/static/vendor.lock.json` 的 `packages` 下新增一个条目：当本地目录键名与 npm 包名不同或包含作用域（例如本地键 `highlightjs` 对应 `@highlightjs/cdn-assets`、本地键 `videojs` 对应 `@videojs/cdn`、本地键 `htmx` 对应 `htmx.org`）时，必须配置 `"npm_package"` 字段供 `scripts/sync_vendor_assets.py` 查询 npm registry 与匹配离线缓存文件名；只声明运行时真正需要的 `min.js` / `min.css` / 字体目录（**不要把 `.map` 调试映射文件或测试目录提取进来**，需要剥离失效 sourcemap 引用时配置 `"strip_missing_sourcemaps": true`）；
2. 执行 `python scripts/sync_vendor_assets.py sync` 校验并提取；
3. 按定位接入：正文积木类库在 `myblog/apps/content_blocks/static/content_blocks/` 编写统一的按需加载封装脚本并接入 `_block_assets_css.html` / `_block_assets_js.html`；全站公共 UI 控件则在 `myblog/static/js/ui-components.js` 与 `myblog/templates/includes/_ui_assets_*.html` 接入；
4. 同样遵循“无内容不请求”原则，不要在首页或普通页面全量加载重型运行时。

---

## 六、 还没解决 / 注意事项

- **历史包清理**：如果从 `vendor.lock.json` 中彻底删掉了某个不再使用的旧库，`sync` 只会更新清单里还在的包，需要手动删除 `myblog/static/vendor/<已废弃包名>`。
- **生产环境缓存哈希注意**：`myblog/settings/production.py` 开启了 `ManifestStaticFilesStorage`。像 `math-render.js`、`mermaid-renderer.js` 与 `code-highlighting.js` 里通过字符串 `/static/vendor/...` 动态拼接加载脚本时，生产发布运行 `collectstatic` 会同时保留原文件名；对于有相对分块或需严格走带哈希清单路径的库（如 `media-player.js` 通过 `_block_assets_js.html` 的 `data-videojs-*-url` 传入 `{% static %}` 地址），应在模板层由 `{% static %}` 注入实际 URL。
