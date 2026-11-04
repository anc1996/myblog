# WSL2 本地服务

本目录只启动基础服务，不运行应用 Dockerfile，也不执行 Django 迁移。
WSL 工作目录固定为 `/mnt/f/openclaw/workspace/wagtail/myblog`，Python 使用
`/root/anaconda3/envs/wagtailblog-test/bin/python`。

## PostgreSQL 切换合同与连接方式

2026-11-04 用户要求关系数据库全面改用 PostgreSQL。18.6 镜像已导入校验并启动，
实际 Debian 包版本 `18.6-1.pgdg13+2`；默认 `postgres` / superuser 认证成功，
两个 UTF8 库已建，用户表/序列/物化视图均为零。Windows TCP `192.168.20.5:5432` 通过，
psycopg 3.3.7 已安装。settings 3 项、服务脚本 8 项离线测试及四服务读写、
两库严格 Django/Wagtail 检查、中文临时 GIN 探针均通过。
修复测试隔离后，6 个真实凭据文件及 2 个客户端 env 在测试前后哈希不变。
应用迁移、业务并发、完整搜索集成、性能与恢复演练未做。
旧 MySQL 的两个库已实查零表；确认归属后已停止，重启策略为 `no`，容器、数据卷与回退备份保留。

| 服务 | Windows 连接地址 | 数据位置 |
|---|---|---|
| PostgreSQL 18.6 | `192.168.20.5:5432`；默认管理员 `postgres` | postgres-data 卷，挂载 `/var/lib/postgresql`，18 版使用版本子目录 |
| Redis | `192.168.20.5:6379` | redis-data 卷，启用 AOF |
| MongoDB | `192.168.20.5:27017`，authSource=admin | mongo-data、mongo-config 卷 |
| RustFS S3 API | `http://192.168.20.5:9000` | rustfs-data 卷 |
| RustFS 控制台 | `http://192.168.20.5:9001` | 与 S3 API 使用同一组管理员凭据 |

所有端口发布到 WSL 的 `0.0.0.0`。WSL 内部客户端仍使用 `127.0.0.1`。
管理员材料在 `/opt/myblog-services/secrets/`，不放入 Git；目录权限0700、env文件0600。
PostgreSQL 管理员为 `postgres`，MongoDB 管理员仍为 `root`，RustFS 管理员仍为 `root`；
Redis 为 `default` 用户。密码仅在本地 env 沿用，不写入追踪文档。
开发与手工服务探针使用 `myblog_dev` / `myblog_test`（PostgreSQL 为 UTF8）、`myblog-dev` / `myblog-test` 私有桶，
Redis 使用 DB9/11 和 `myblog:dev` / `myblog:test` 前缀。这是逻辑隔离，未做账号权限隔离。

项目 `.env.local`、`.env.test` 已被 Git 忽略。对象存储统一使用 `S3_ENDPOINT_URL`、
`S3_ACCESS_KEY_ID`、`S3_SECRET_ACCESS_KEY`、`S3_BUCKET`、`S3_REGION`，采用 boto3、
SigV4 签名和 path-style 桶路径。媒体业务接入仍未完成，不因数据库配置接入就声明媒体存储已切换。
数据库接入通过 settings 加载 `.env.local`，可用 `MYBLOG_ENV_FILE=.env.test` 切换，
进程环境优先，数据库键为 `POSTGRES_DB/HOST/PORT/USER/PASSWORD`。
Django 自动测试库使用 `POSTGRES_TEST_DB`，默认 `test_myblog`，不能混用手工服务库 `myblog_test`。
数据库 env 接入不等于媒体业务已经接入。
两个客户端 env 已移除旧 `MYSQL_*` / `MINIO_*` 键。

## 启停与验证

以下为操作参考；服务启停和读写检查仍须符合授权及资源归属，不应因更新文档重放建库。

```bash
cd /mnt/f/openclaw/workspace/wagtail/myblog
test "$PWD" = /mnt/f/openclaw/workspace/wagtail/myblog
python_bin=/root/anaconda3/envs/wagtailblog-test/bin/python
# 首次在这台 WSL 准备镜像时执行；下载通过指定代理，逐层核对摘要。
"$python_bin" scripts/fetch_postgres.py
"$python_bin" scripts/fetch_rustfs.py
docker compose -f infra/compose.yaml config --quiet
docker compose -f infra/compose.yaml up -d
docker compose -f infra/compose.yaml ps
# 以下是开发初期的空资源检查，不应在已有业务数据时重放。
"$python_bin" scripts/local_services.py verify
"$python_bin" scripts/check_local_postgres.py
"$python_bin" scripts/check_postgres_search.py
"$python_bin" scripts/check_local_s3.py
```

Docker 已启用 systemd，容器设置 `restart: unless-stopped`；要先启动 WSL，Docker 才会启动。
临时停止用 `docker compose -f infra/compose.yaml stop`。不要使用 `down -v` 或 volume prune。
检查配置用 `config --quiet`；不要将展开的凭据或完整 inspect 输出到终端。

`prepare --source-env <参考文件>` 仅用于 secrets 目录不存在或为空时的首次准备；
已有任意文件就拒绝，不覆盖已有凭据，也不用于现有服务的日常激活。
仅限单实例串行执行：空目录检查与多文件写入不是原子操作，不保证并发或中断安全；
中途失败需人工检查已写入的部分文件，禁止重复执行以覆盖这些文件。PostgreSQL 使用 `POSTGRES_*`，
沿用本地密码但使用默认账号 `postgres`，MongoDB 保持 `root`，不修改已有 Mongo/Redis/RustFS 身份。
写入 env 不会自动改变已有数据卷里的账号；既有数据和凭据不因切换方案而删除。
`provision` 仅建 UTF8 空库、Mongo/Redis 归属标记和私有桶，不建业务表。
`activate` 先检查再更新忽略的客户端 env，并移除旧 `MYSQL_*` / `MINIO_*` 变量。
`verify` 使用 PostgreSQL 连接内临时表、Mongo/Redis 唯一测试记录和 S3 唯一测试对象，随后只清理本次探针数据。
新 `check_local_postgres.py` 检查版本、编码、目标空库与 Django/Wagtail 兼容性，不生成或执行业务迁移；
两库严格检查已通过。`check_postgres_search.py` 只检查临时表的分词、GIN、AND 和单字备用查询，不验证业务搜索。
`check_local_s3.py` 补充检查签名、分片上传与批量删除，仅删除本次 UUID 下的对象。

本轮 `verify` / `activate` 已通过 PostgreSQL 临时 CRUD、回滚及条件唯一索引，
Mongo 认证读写、Redis DB9/11 读写及 RustFS 私有上传、Range、读取、删除。
严格检查最初出现 `postgres.E005`，注册 `django.contrib.postgres` 后重跑为 0 issues / 0 silenced；
dev 与 `MYBLOG_ENV_FILE=.env.test` 的 `manage.py check --database default --fail-level WARNING` 均为 0 issues。
服务脚本测试曾覆盖本地凭据，已从切换前备份恢复并逐一核对运行容器；
修复临时目录隔离和 `prepare` 防覆盖后，11 项离线测试及真实文件哈希不变检查通过，
事件详情见[切换记录](../说明书/myblog_PostgreSQL切换记录_2026-11-04.md)。

## RustFS 版本与备份

2026-11-03 核对官方 GitHub 最新稳定 Release 为 **1.0.4**，发布于2026-10-25。
官方仓库 `rustfs/rustfs` 使用 Apache-2.0 许可证。
镜像来源：Docker Hub `rustfs/rustfs:1.0.4`，linux/amd64。

- 官方多平台索引：`sha256:ed9210d6411d77ffceb70d94b964a3cb7d225983368bc25bebe01e995293dcc2`
- 官方 amd64 manifest：`sha256:e287e99762a4a3e7b706526e01f2ff755f67db165e9be9f6b907adc6fb6d6ee1`
- 官方 config：`sha256:2755bcf766f77f221f39e056864948fb4d1862381831ed0b347226e4bc1a43a9`
- 本机导入后 manifest：`sha256:411dc00e2cf07b1360995d6e4884a1e5190d0493088b81a3b2f6d00c082c988a`

`fetch_rustfs.py` 使用 `http://192.168.20.3:5071` 下载官方镜像，验证索引、manifest、
压缩层、解压层和 config；不修改 Docker 代理或重启其他服务。Docker 导入会转换 manifest，
所以本机摘要与官方摘要不同；脚本再次导出并校验 config 原始字节。Compose 固定本机摘要，
`pull_policy: never`。其他 Docker 引擎若导入结果不同，应先验证并更新本机摘要，不能直接拉取该本机摘要。
验证记录在 `/opt/myblog-services/rustfs-image/verified.json`。

切换前的备份目录由 `/opt/myblog-services/rustfs-switch-current` 指向。
其中包含停止后的旧 MinIO 卷归档、SHA256、Compose、客户端和管理凭据、测试文件校验记录。
旧 MinIO 数据卷和镜像保留用于回退；新 RustFS 使用独立卷，没有混用磁盘格式。
备份含凭据，只保留在 WSL 的受限目录，不能复制进仓库。

回退前先暂停应用写入和会修改对象的后台任务，保持 RustFS 可读。
若切换后存在新上传，应先通过 S3 导出、核对并准备反向迁移，完成后再停止 RustFS。
恢复备份中的管理、客户端和 MinIO env 后，
使用备份 Compose 的 `minio` 服务和原数据卷，单独 `up -d --no-deps minio`；
必须指定 `--project-directory /mnt/f/openclaw/workspace/wagtail/myblog`。
启动旧服务后导入已核对的新对象，再验证媒体引用。完整回退演练尚未执行。

## 检查边界

本次切换测试不等于 Django 媒体业务完成。四类媒体、鉴权交付、历史引用和回收流程仍待实现。
MongoDB 9.0.4、Redis、RustFS 保持；PostgreSQL 18.6 镜像与依赖准备进展见《PostgreSQL 切换记录》。
最新 `pip check` 仍失败，仅报告一条冲突：`wagtailblog2==0.1.0` 要求 `wagtail==8.0.1`，实际安装为 8.1。
旧 MySQL 严格检查曾因 WorkflowState 条件唯一约束报告 `models.W036`。
PostgreSQL 支持条件唯一索引，不能继承这个失败结论；获准迁移后仍需检查实际索引，
统一锁顺序、幂等、批准 Revision 一致性及真实 PostgreSQL 并发均未验收。
需求书和任务书的原业务与权限要求保留。本轮验证见[切换记录](../说明书/myblog_PostgreSQL切换记录_2026-11-04.md)，环境准备记录正文仅保留历史证据。

## PostgreSQL 备份与回退边界

本地回退目录为 `/opt/myblog-services/postgres-switch-current`，其中含凭据，只留本地，不提交。
镜像固定摘要及最新进展见[切换记录](../说明书/myblog_PostgreSQL切换记录_2026-11-04.md)。
业务备份计划采用 `pg_dump` 自定义格式与 `pg_restore` 恢复到另一个隔离库；
同时保存 Mongo 字段版本、RustFS 对象和 SQL 引用清单，协调备份时间，恢复后逐条对账。
SQL 恢复不会回滚 Mongo/RustFS，额外对象保留待核对，不能立即物理删除。
尚未执行备份恢复或跨存储回退演练；本地回退目录存在不等于演练通过。
