# 给 Codex 的第一阶段方案设计任务书（只读／禁止实施）

请在 **Codex 桌面版**中用此任务书，并同时提供《myblog 第一阶段需求规格说明书 v1.0》。

**2026-11-04 对接更新**：按用户要求，本文对象存储统一改为 RustFS，保留原设计任务和权限要求。以下“当前仅授权只读”描述最初方案设计阶段；后续已授权并完成独立服务准备，本轮获准同步文档。实际版本、地址、资源与未完成事项见《第一阶段技术方案》A 节及《RustFS 对接方案》。本文不能单独授权业务迁移、升级共享环境或部署。

## 指令

你是 `anc1996/myblog` 的首席方案设计/架构审查代理。用户已逐项确认第一阶段需求，详见附带的需求规格说明书。**当前仅授权只读调查和方案设计，不授权修改项目代码或配置、创建/重写迁移、访问/操作数据库、调用真实 MySQL/MongoDB/Redis/RustFS 服务、运行 Docker、部署、Git commit/push、调用任何可能自动执行 migrate 的启动命令。** 不要询问或查找密码、Token、`.env`、私钥。旧站 `anc1996/wagtailblog2` 为只读能力参考，绝对不得写入。

### 现场信息

- 工程本地 WSL2：`/mnt/f/openclaw/workspace/wagtail/myblog`
- Windows：`F:\openclaw\workspace\wagtail\myblog`
- GitHub：`https://github.com/anc1996/myblog`
- 指定 Conda Python：`/root/anaconda3/envs/wagtailblog-test/bin/python`
- 仓库声明：`Django>=6.1,<6.2`，`wagtail>=8.1,<8.2`；需只读核对本地版本。
- **用户确认本机已安装 MySQL、MongoDB、Redis、RustFS**；安装不代表已配置或授权连接。请在方案中充分利用这些基础设施，但不得现在探测或登录服务。
- 最新需求包括：最小 AbstractUser、公共 Person/Tag、必填成文日期、BlogPage 全生命周期、Mongo 外置 `intro/body`、公共四种媒体/私有 RustFS、MEDIA-REF-01、受控回收站、全站搜索/归档、Wagtail 8.1 API v3 安全边界。完整要求以附件为准。

### 第一项任务：P0-A 身份与迁移现状只读核查（最优先）

1. 首先只读查看 `AGENTS.md`、`pwd`、`git status --short`、`git branch --show-current`、`git remote -v`；核实确实位于 `myblog` 工程。
2. 只读读取 `requirements.txt`、`myblog/settings/*`、`home/models.py`、`home/migrations/*`、`search/views.py`、`myblog/urls.py`、`Dockerfile`、`manage.py`、用户/迁移相关配置。
3. 只检查是否存在本地 SQLite 文件及迁移文件（文件存在性/路径/内容）；**禁止打开数据库、读取 `django_migrations`、运行 `showmigrations`、`migrate`、`makemigrations`、`runserver`、Django ORM 或测试数据库命令**。
4. 通过指定 Conda Python `-V` 和 `-m pip show Django wagtail` 查实际版本；不要安装、升级、联网修改依赖。
5. 给出最小 `AbstractUser` 的身份 App/App label、`AUTH_USER_MODEL`、`0001_initial` 依赖图及现有 home/Wagtail 迁移衔接方案。候选为 `identity.User`，但尚待技术方案和用户最后确认。
6. 关键：仓库里有 `home/0001_initial.py` 和 `0002_create_homepage.py`，**不能据此推断数据库已经迁移或尚未迁移**。本地迁移执行状态无只读证据就报告“未知”，不要猜。
7. 点明 `Dockerfile` 启动路径中自动 `migrate` 的风险，禁止执行。

### 第二项任务：P0-B 公共媒体模型方案对比（P0-A 之后）

- 比较：Wagtail 原生 Image/Document + 独立权限/引用/交付服务，和首次迁移即采用自定义 Image/Document 模型的两条路线。
- Audio/Video 必须为全站公共资源，第一阶段交付基本上传/选择/播放、RustFS 存储与安全控制。
- 私有 RustFS：上传默认受限；媒体权限独立于 Page；公开 Page 引用受限媒体必须阻止发布；统一代理/鉴权覆盖原图、Rendition、文档、音视频 Range、API、预览、缓存。
- MEDIA-REF-01：当前发布、草稿、历史 Revision、回收站与 Mongo 字段中的每个引用都要追踪；存在引用或索引不可信时禁止物理删除。
- 标注现有 `/documents/`、Wagtail 8.1 API v3 和管理员删除操作可能绕过自建服务的风险。
- 输出明确的推荐方案及缺点、前置条件、用户需要再次确认的点；**不要写模型代码或迁移**。

### 第三项任务：架构与实施分片

按照附带规格设计：

1. 全站公共基本 Block vs Page 专属组合的接口图（绝不复用跨 Page 可变正文）。
2. MySQL Wagtail Revision 权威与 MongoDB `intro`/`body` 不可变字段版本绑定合同、幂等 Outbox、发布失败补偿、历史恢复。
3. `authored_on` 必填字段适用业务 Page/IndexPage，Wagtail 原生发布时间独立。
4. Person 多人/贡献角色、共享 Tag（无 Category）、图集双形态的 Revision 关系模型。
5. F10 单页/子树受控回收站的技术比较（保留原 Page ID / Revision / 树结构、恢复后未发布，防止 API v3 DELETE 硬删除绕过）。
6. Search/Tag/BlogIndexPage/Archive 公共查询合同，Mongo 正式版本全文提取；基础前台第一阶段，Elasticsearch 第二阶段。
7. 页面、媒体、API、缓存、回收站及故障注入的安全验收矩阵；明确 E1/E2/E3 和哪些步骤必须额外授权。
8. 分片实施顺序、依赖图、可回滚点与风险清单，**不开始任何开发**。

### 输出格式（请在 Codex 对话中直接返回方案，不要创建/修改仓库文件）

A. `现状核查证据表`：每个结论标明文件/行号或检查输出，严格区分事实、推断、未知。
B. `P0-A 推荐方案`：User、迁移依赖图、冲突和下一步；不得声称本地数据库状态已知。
C. `P0-B 方案比较及推荐`：原生 vs 自定义媒体模型、安全与删除引用的覆盖面。
D. `总体架构/关键时序`：Wagtail/MySQL、Mongo、RustFS、Redis 的权威和投影边界。
E. `第一阶段开发切片 + 验收矩阵`：每一项附需验证的风险和可交付证据。
F. `待用户再次审批的问题`：只列真实未定技术事项，避免重复询问需求规格中已确定的 F01–F12 等结论。
G. `操作声明`：列出实际执行过的只读命令、读取的文件，明确**无修改、无迁移、无连接真实服务、无 commit/push**。

**停止条件**：任何需要连接本地/远程数据库或媒体服务、访问凭据、运行可能触发迁移的命令、修改文件，均立即停止并说明原因，等待用户明确批准。不得为了完成核查而绕过限制。
