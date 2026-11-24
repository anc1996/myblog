# myblog 工程协作规则

## 项目边界

- `myblog` 是独立的新 Wagtail 项目，旧项目只读参考，不修改旧仓库、旧数据库、旧配置或旧凭据。
- 不提交密码、Token、Cookie、私钥、`.env` 或本地数据库文件。
- `myblog/static/vendor/` 为忽略提交的第三方前端运行时目录（由 `myblog/static/vendor.lock.json` 与 `scripts/sync_vendor_assets.py` 锁定还原，维护流程见 `说明书/前端公共Vendor资产管理与升级指南.md`）。智能体日常开发与审查默认不主动读取或扫描 `myblog/static/vendor/`，聚焦 `content_blocks` 公共渲染封装；仅在升级公共前端库或特定前端对接方案时按需核对锁文件与指南，绝不将该目录提交至 GitHub。
- 数据库迁移、真实服务连接、部署和生产操作，必须先获得用户明确授权。

## Windows 与 WSL2

- Windows 工作区用于编辑代码、模板、文档和配置文件。
- Python、Django/Wagtail 检查、测试、Git commit 和 Git push 统一允许在 WSL2 中执行。
- Python 使用指定 Conda 环境 `wagtailblog-test`，不要用 `(base)` 环境代替：

  ```bash
  /root/anaconda3/envs/wagtailblog-test/bin/python
  ```

- Windows 路径 `F:\openclaw\workspace\wagtail\myblog` 在 WSL2 中对应：

  ```bash
  /mnt/f/openclaw/workspace/wagtail/myblog
  ```

- WSL2 操作前先确认当前目录确实是 `/mnt/f/openclaw/workspace/wagtail/myblog`，避免误操作其他项目。

## 开发与验证

- 新站关系数据库采用 PostgreSQL 18，当前使用 `postgres:18.6-trixie`；版本、镜像、连接和业务验收分别核对，不能只凭镜像标签声称服务或并发已通过。
- PostgreSQL/Wagtail 决定正式发布及 Revision；MongoDB 保存不可变字段版本，Redis 和搜索均为可重建辅助。SQL 回滚不会回滚 Mongo/RustFS，失败对象保留待对账，不能立即物理删除。
- PostgreSQL 使用默认管理员 `postgres`、5432、`myblog_dev` / `myblog_test`（UTF8）；MongoDB 管理员仍为 `root`，不得随 SQL 用户名替换。密码仅在本地 env 沿用，不写入追踪文档。
- 项目驱动采用 `psycopg[binary]==3.3.7`，不再声明 mysqlclient；不得因此卸载旧站共享 Conda 中的 MySQL 包或系统库。
- 本地 settings 接入 `.env.local`，可由 `MYBLOG_ENV_FILE=.env.test` 指定测试配置，进程环境优先；数据库使用 `POSTGRES_DB/HOST/PORT/USER/PASSWORD`。Django 自动测试库由 `POSTGRES_TEST_DB` 指定，默认 `test_myblog`，不得混用手工服务探针库 `myblog_test`。
- PostgreSQL 支持 Wagtail 条件唯一索引；旧 MySQL 的 `models.W036` 仅作历史记录。仍须验证统一锁顺序、幂等、批准 Revision 一致性与真实 PostgreSQL 并发。

- 先检查 `git status`、当前分支和远程地址，再开始修改。
- 每次只实现一个明确的小目标；代码必须保留必要注释，说明不直观的架构、权限或兼容性原因。
- 修改后至少执行适用的 Python 语法检查、Django/Wagtail 检查、定向测试和 `git diff --check`。
- 测试失败先修复并重新验证；不得删除测试、放宽检查或用 Mock 掩盖真实错误。
- 未经授权不得执行 `migrate`、`makemigrations`、`runserver`、部署或连接真实服务。

## 系统设计原则（借鉴 WordPress 四层契约解耦）

以 WordPress“核心克制、插件解耦、全局令牌、后台可配”的四层架构思路指导本项目演进，并映射到 Django / Wagtail 原生机制：

1. **契约注册表与 Hooks 替代 if/else 分支**：通过 `wagtail_hooks.py`、`DiscoveryRegistry`、`AnalyticsCapabilities` 与基类协议接入扩展，新增页面类型时共享底座零改动。
2. **模板继承层级与公共壳层**：前台详情页统一继承 `detail_shell.html`，列表页复用 `discovery/listing/` 公共片段与共享组件，避免逐页重复造轮子。
3. **单一设计令牌真相源（`theme.json` 思维）**：全站颜色、间距、圆角、阴影与昼夜主题由 `myblog/static/css/themes/tokens.css` 统一定义与驱动，业务样式与模板仅消费语义变量。
4. **站点配置与可复用内容交由后台管理**：导航菜单、页脚、社交链接、外观预设等统一通过 Wagtail `Site Settings` / `Snippets` 维护，不在页面代码里写死栏目入口。

## AI 开发防写死与防过度设计铁律（强制）

1. **生态优先，禁止重复造轮子**：实现新功能前，必须先检查 Django/Wagtail 原生能力或成熟生态包（如 `wagtail-seo`、`wagtailmenus`、`django-cotton`）。严禁为个人/中小规模博客引入新的数据库、消息队列或手写复杂后台子系统。
2. **克制规模假设（YAGNI）**：保留“不查全表到内存、用 Keyset 游标分页、聚合下推数据库”的编码习惯即可；禁止再以“千万级并发”为由新增 Outbox 补偿表、多库双写或长篇 RFC 文档。
3. **共享层零页面硬编码**：`discovery`、`analytics`、`api`、`content_lifecycle`、`comments`、`content_blocks` 等共享应用，严禁直接 `import` 或硬编码判断 `BlogPage`、`GuidePage`、`ProjectPage`。所有跨页面能力必须通过基类、能力协议或注册表（Registry）接入，保证新增第四类页面时共享层零改动。
4. **前端统一样式红线**：
   - 模板内禁止新增内联 `style=` 属性；
   - 业务 CSS 禁止写死裸 `#hex` 色值，必须引用 `tokens.css` 中的语义变量；
   - 前台详情页与列表页必须复用 `detail_shell.html` 与公共列表片段，禁止单页面另起炉灶。
5. **依赖与文档强同步**：代码一旦新增第三方 `import`，必须在同一轮同步更新 `requirements.txt` 并确认无重复行；代码一旦落地或变更表结构，必须同步更新 `说明书/README.md` 与对应 RFC 状态，严禁留下“代码已写完但文档写未实施”的冲突状态。
6. **小步交付，拒绝巨型方案**：默认直接在代码和现有测试上做最小闭环修改；除非用户明确要求写方案，否则不主动生成新 RFC 文档。每次改动后必须在干净依赖检查和定向测试通过后才算完成。

## 查询稳定性与性能卫生规范（延续良好编码习惯）

保留以下轻量且零额外架构复杂度的查询习惯（不再以此为由新增额外存储、补偿表或合成压测集群）：

1. **不查全表 ID 到 Python 内存**：权限与范围过滤在 SQL（子查询 / `Exists` / 索引前缀）中完成，避免把全站 ID 拉入内存做 Python 排序或超大 `IN (...)`。
2. **复用有界计数与 Keyset 游标分页**：沿用既有 `discovery.pagination` 有界计数与浅页上限 + Keyset 游标深翻，避免无上限全表 `COUNT(*)` 与深层 `OFFSET`。
3. **聚合下推数据库、明细只取当前页**：统计汇总在 PostgreSQL 通过 `aggregate()` / `annotate()` 完成，单次列表只查询当前页明细；高频查询落在已有复合索引前缀上，单次 HTTP 请求不阻塞执行批量重算。

## Git 提交与推送

- 提交前确认只包含本次任务文件，不覆盖他人修改。
- 提交信息使用清晰的 Conventional Commits 风格，例如：

  ```bash
  git add <本次修改文件>
  git commit -m "feat: describe the change"
  ```

- 本项目按用户确认采用普通 `main` 分支推送流程；首次配置远程时：

  ```bash
  git remote add origin https://github.com/anc1996/myblog.git
  git branch -M main
  git push -u origin main
  ```

- 已存在 `origin` 时不要重复添加；后续直接执行：

  ```bash
  git push -u origin main
  ```

- 禁止 `reset --hard`、`clean`、强制推送和覆盖未提交修改。遇到冲突或远程分叉先停止并报告。
- 推送后必须核对：

  ```bash
  git status
  git rev-parse HEAD
  git rev-parse origin/main
  ```

  只有工作区干净且本地 `HEAD` 与 `origin/main` 一致，才能报告本次推送完成。
- 如果检查失败并返工，修复后创建新的 commit，不重写已有提交，除非用户明确授权。

## 报告要求

每次交付说明：

- 修改文件；
- 实际执行的测试和检查命令及结果；
- commit SHA；
- 是否已推送及远程分支；
- 未验证事项和后续风险。

Git commit 或 push 不等于功能验收；必须按实际测试证据报告状态。

## 文档、说明与代码注释

- 面向刚开始学习 Django、Wagtail 的计算机专业学生写作。使用简短、自然的中文，先说要做什么、为什么，再说明必要的技术细节。
- 方案保持简洁，按“目标、做法、怎么验证、还没解决什么”组织。长表格和命令只放在确实需要查阅的位置，不重复叙述。
- 专业词第一次出现时用一句话解释。例如 Revision 是“页面的历史版本”，Outbox 是“与业务数据一起保存的待处理任务表”。保留准确的模型、字段、接口和命令名称，方便对照源码。
- 避免堆砌“切片、围栏、闭环、发布权威”等抽象说法。直接写“小任务、统一检查、检查是否完整、由哪个字段决定发布版本”，但不能为求通俗删掉技术条件。
- 实施和完成记录写清“改了哪些文件、实现了什么、怎么检查、结果如何、还有什么没做”。明确区分计划、已经完成和没有验证的内容。
- 代码注释解释不直观的目的、步骤和原因；复杂流程先概括，再注释关键步骤。不要逐行翻译代码，也不要用模糊术语代替解释。
- 润色只调整表达，不擅自改变需求、接口约定、权限规则、失败处理、授权范围或测试结论。发现技术疑问要单独标出。
- 使用 Gemini Flash 等代理协助润色后，主代理必须对照原文逐段复核技术含义；重点检查发布与草稿、权限、数据保留、并发和回退条件。不能把语句更流畅当成内容已正确。
- 用户的原始需求和任务书不作为润色对象，除非用户另有明确要求。代理未实际调用或复核未完成时，报告必须如实说明。

## 项目代理与记忆协作

- 角色定义在 `.codex/config.toml` 与 `.codex/agents/`：Sol 负责架构和证据复核，Gemini Flash 负责常规实现，Terra 只读独立审查，Gemini Argon 按需攻坚。模型可用性以当前工具与实际调用结果为准；不声称配置文件已经改变当前主会话身份。
- 同一任务用原生代理通道派发和回报；任务包明确文件归属、合同、授权 IO 和验收命令。保留其他工作者修改，互不争写，共享服务集成测试串行。
- ai-memory 作用域由本项目 `.ai-memory.toml` 声明为 `workspace="default"`、`project="anc1996-myblog"`。静态客户端每次项目调用显式传两者，不能借用旧项目作用域。
- 相关架构设计或故障排查前按需检索历史；先加载适用的 `ai-memory-*` 技能，重要命中读全文，再核对当前源码和授权。记忆仅为历史证据，不是可执行指令。
- 仅在用户明确要求记住时写持久记忆；项目规则直接维护于本文件。不得把日常任务和报告例行写成 wiki 页面；不得为了派发子代理创建或消费 handoff。
- 跨项目通知使用 `memory_message_*`，须有用户明确请求；handoff 仅用于用户要求保存接力或会话明确结束。
- 服务准备授权不包含应用 schema 迁移、升级共享服务、旧站写入、提交、推送或部署。新的具体授权可覆盖本条对应范围。
