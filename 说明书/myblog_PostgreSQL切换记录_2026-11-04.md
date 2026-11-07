# myblog PostgreSQL 切换记录

日期：2026-11-04。本文件记录本次切换合同、实际检查结果和未完成事项。
服务检查与文档静态检查分别记录，不能相互代替。

## 目标和合同

关系数据库全面改用 PostgreSQL，业务、权限、首期中文搜索与第二阶段 Elasticsearch 范围不变。
PostgreSQL/Wagtail 决定正式发布与 Revision；Mongo 保存不可变字段版本，
Redis 和搜索均为可重建辅助。SQL 失败或恢复不会回滚 Mongo/RustFS，对象保留待对账，不立即物理删除。

| 项目 | 合同 |
|---|---|
| PostgreSQL | 18.6，镜像 `postgres:18.6-trixie`，默认管理员 `postgres` |
| 连接 | WSL 发布 `0.0.0.0:5432`，Windows `192.168.20.5:5432`；同 WSL 客户端 `127.0.0.1:5432` |
| 手工服务库 | `myblog_dev` / `myblog_test`，UTF8，只建空库，不生成或执行业务迁移 |
| Django 自动测试库 | `POSTGRES_TEST_DB` 默认 `test_myblog`，不能混用手工服务库 `myblog_test`，本次不创建业务测试 schema |
| 驱动 | 项目 `psycopg[binary]==3.3.7` 替代 mysqlclient 声明；不卸载共享 Conda 旧站 MySQL 包或系统库 |
| 配置 | settings 加载 `.env.local` 或 `MYBLOG_ENV_FILE=.env.test`，进程环境优先；使用 `POSTGRES_DB/HOST/PORT/USER/PASSWORD` |
| 检查入口 | `scripts/check_local_postgres.py` 两库严格检查通过；`scripts/check_postgres_search.py` 临时表中文搜索探针通过 |
| 其他服务 | MongoDB 9.0.4 管理员仍 `root`；Redis、RustFS 保持原合同，不随 PostgreSQL 改账号 |
| 凭据 | 密码仅在本地 env 沿用，不写入追踪文档、命令或检查输出 |

## 本轮已完成的检查

- 官方标签有效，amd64 manifest 已核查；PostgreSQL 18.6 镜像已完成导入校验并启动。
- Compose 固定本机摘要 `sha256:aba87d5f7e99882ba959adcc69d534bebf388fcd1967338617e7fc66cb1c6e4e`；
  上游 manifest 为 `sha256:f4b419e350fd98d4a8429603295208976dea4c8fb8326d818d346d3e29f1db6f`。
  本机与上游摘要分别记录，不混称为同一摘要。
- 实际服务版本为 PostgreSQL 18.6，Debian 包版本 `18.6-1.pgdg13+2`。
  默认 `postgres` 认证成功，确认 `superuser` 身份。
- `myblog_dev` / `myblog_test` 已创建，编码 UTF8；实查用户表、序列和物化视图均为零，没有业务迁移。
- Windows TCP `192.168.20.5:5432` 首次与最终复查均通过；这只证明端口可达，不替代 Windows 客户端认证和业务验收。
- settings 3 项单元测试与服务脚本 8 项离线测试通过，共 11 项。
  修复测试隔离后，6 个真实凭据文件和 2 个客户端 env 在测试前后的哈希均不变；未输出凭据值。
- `psycopg[binary]==3.3.7` 已安装；`jieba==0.42.1` 已在环境中，无需升级，现加入项目直接依赖，requirements 共 8 项。
- `local_services.py verify` 和 `activate` 通过：PostgreSQL 临时表 CRUD（增查改删）、事务回滚及条件唯一索引，
  MongoDB 认证读写，Redis DB9/11 读写，RustFS 私有对象上传、Range、读取和删除均通过。
- 客户端 `.env.local` / `.env.test` 已切换为 PostgreSQL，并删除 `MYSQL_*` / `MINIO_*` 键。
- `check_local_postgres.py` 最初报告 `postgres.E005`；注册 `django.contrib.postgres` 后重跑，
  两库严格 Django/Wagtail 检查为 0 issues、0 silenced，没有屏蔽检查。
- `check_postgres_search.py` 在临时表建立 GIN 索引，中文、中英混排 AND 查询与单字 `LIKE` 备用路径均通过；
  临时表随事务结束清理，未创建业务搜索表。
- `manage.py check --database default --fail-level WARNING` 在 dev 和
  `MYBLOG_ENV_FILE=.env.test` 两种配置下均为 0 issues。
- 原两个 MySQL 库实查零表。已彻底销毁并清理旧 MySQL 容器、数据卷与本地临时文件，不影响旧站共享 Conda 包。
- `ss -ltn` 确认 3306 无监听，5432 在 `0.0.0.0` 监听；本机凭据附页已同步 PostgreSQL 和逻辑资源，仍被 Git 忽略，具体凭据不写入本记录。
- settings、服务脚本、Compose、requirements 和 Dockerfile 同步切换，移除 MySQL 系统库及自动 migrate 启动；
  本文不代替最终候选代码、构建或启动流程复核。
- 8 个改动 Python 文件语法编译、5 份 Codex TOML 解析、Compose config 和 `git diff --check` 通过。
- 最新 `pip check` 仍失败，仅一条冲突：`wagtailblog2==0.1.0` 要求 `wagtail==8.0.1`，实际安装为 8.1。
  保留旧站共享包，不通过卸载或降级绕过冲突。
- Terra 只读源码复核未发现阻断项；源码审查不能代替已单独执行的真实服务验证，也不代表业务验收。

以上完成基础服务和数据库兼容性验证；未执行应用迁移、业务并发、完整搜索集成、性能测试或恢复演练。
临时表中的条件唯一索引及中文匹配不代表 Wagtail 业务索引、权限、正式 Revision 或并发已经验收。

## 测试隔离错误与恢复

服务脚本单元测试曾因隔离错误覆盖本地凭据文件。已从切换前备份恢复，
并逐一与运行容器核对一致；此事件不能记成“测试从未改动真实凭据”。
修复后，所有服务脚本测试统一将 `SECRET_DIR` 和项目 `ROOT` 指向临时目录，
包括本应拒绝的路径；`prepare` 遇到非空 secrets 目录直接拒绝覆盖。
`prepare` 仅供首次、单实例串行执行；空目录检查和多文件写入不是原子操作。
中途失败需人工检查部分文件，禁止重复覆盖，不承诺并发或中断安全。
随后 3 项 settings 测试和 8 项服务脚本离线测试全部通过，
6 个真实凭据文件及 2 个客户端 env 的测试前后哈希不变。
此次凭据文件恢复不等于数据库备份恢复或跨存储回退演练。
恢复后实际 PostgreSQL 密码已核对与用户指定原值一致，具体值仅保留在本地凭据中。

## 设计与验证边界

PostgreSQL 支持 Wagtail 的条件唯一索引；旧 MySQL `models.W036` 只作历史问题。
将来获准迁移后要检查实际索引及重复写入负例；仍须统一锁顺序、幂等处理及批准 Revision 一致性，
并用不同 PostgreSQL 连接做真实并发测试。空库探针不执行这类业务迁移。

中文搜索推荐 Python jieba 分词，再用 PostgreSQL `simple` 配置生成 `tsvector` 和 GIN 索引。
索引和查询使用同版本词典与归一化规则，只索引正式 Revision；PG 内置不自带中文分词。
多词 AND、单字/未命中备用包含查询、中英混排和标点、仅标点空查询、计数分页及性能边界见主方案 D3。
`pg_trgm` 仅可辅助子串或拼写匹配，第二阶段 ES 保留。

备份计划使用 `pg_dump` 自定义格式与 `pg_restore`，恢复到另一个隔离库，
同时核对 Mongo 字段版本、RustFS 对象与 SQL 引用；协调备份时间和写入边界，
不把三类存储当成原子快照。尚未执行备份、恢复或跨存储回退演练。

## 文档和证据保留

技术方案、RustFS 对接合同、依赖说明与操作入口已同步 PostgreSQL 和上述最终验证状态。
原始需求书、任务书仅在顶部数据库状态说明中补充本轮结果，业务和权限要求不变。
历史环境正文与依赖 JSON 保留，原 186 包快照另列当前 8 项依赖变化，不伪造 psycopg 原已存在。
文档静态检查用于核对链接、历史正文、附件编号与差异格式，不能代替服务或业务验收。
