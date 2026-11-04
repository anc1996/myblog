# WSL2 本地服务

本目录只启动基础服务，不运行应用 Dockerfile，也不执行 Django 迁移。
WSL 工作目录固定为 `/mnt/f/openclaw/workspace/wagtail/myblog`，Python 使用
`/root/anaconda3/envs/wagtailblog-test/bin/python`。

## 当前连接方式

| 服务 | Windows 连接地址 | 数据位置 |
|---|---|---|
| MySQL | `192.168.20.5:3306` | mysql-data 卷 |
| Redis | `192.168.20.5:6379` | redis-data 卷，启用 AOF |
| MongoDB | `192.168.20.5:27017`，authSource=admin | mongo-data、mongo-config 卷 |
| RustFS S3 API | `http://192.168.20.5:9000` | rustfs-data 卷 |
| RustFS 控制台 | `http://192.168.20.5:9001` | 与 S3 API 使用同一组管理员凭据 |

所有端口发布到 WSL 的 `0.0.0.0`。WSL 内部客户端仍使用 `127.0.0.1`。
管理员材料在 `/opt/myblog-services/secrets/`，不放入 Git；目录权限0700、env文件0600。
当前 RustFS 管理员按用户指定配置，与本项目 MySQL/MongoDB 管理身份一致；Redis 为 default 用户。
开发与测试使用 `myblog_dev` / `myblog_test` 数据库、`myblog-dev` / `myblog-test` 私有桶，
Redis 使用 DB9/11 和 `myblog:dev` / `myblog:test` 前缀。这是逻辑隔离，未做账号权限隔离。

项目 `.env.local`、`.env.test` 已被 Git 忽略。对象存储统一使用 `S3_ENDPOINT_URL`、
`S3_ACCESS_KEY_ID`、`S3_SECRET_ACCESS_KEY`、`S3_BUCKET`、`S3_REGION`，采用 boto3、
SigV4 签名和 path-style 桶路径。**Django 当前尚未加载这些文件，默认仍是本地文件存储。**

## 启停与验证

```bash
cd /mnt/f/openclaw/workspace/wagtail/myblog
test "$PWD" = /mnt/f/openclaw/workspace/wagtail/myblog
python_bin=/root/anaconda3/envs/wagtailblog-test/bin/python
# 首次在这台 WSL 准备镜像时执行；下载通过指定代理，逐层核对摘要。
"$python_bin" scripts/fetch_rustfs.py
docker compose -f infra/compose.yaml config --quiet
docker compose -f infra/compose.yaml up -d
docker compose -f infra/compose.yaml ps
# 以下是开发初期的空资源检查，不应在已有业务数据时重放。
"$python_bin" scripts/local_services.py verify
"$python_bin" scripts/check_local_s3.py
```

Docker 已启用 systemd，容器设置 `restart: unless-stopped`；要先启动 WSL，Docker 才会启动。
临时停止用 `docker compose -f infra/compose.yaml stop`。不要使用 `down -v` 或 volume prune。
检查配置用 `config --quiet`；不要将展开的凭据或完整 inspect 输出到终端。

`prepare --source-env <参考文件>` 只用于首次创建独立实例。它读取 MySQL/Redis 管理材料，
用同一管理身份生成 RustFS 配置；重写 env 不会修改已有 MySQL/Mongo 数据卷内的账号。
`provision` 仅建空库、Mongo/Redis 归属标记和私有桶，不建业务表。
`activate` 先检查再更新忽略的客户端 env，并移除旧 `MINIO_*` 变量。
`verify` 使用 MySQL 连接内临时表、Mongo/Redis 唯一测试记录和 S3 唯一测试对象，随后清理。
`check_local_s3.py` 补充检查签名、分片上传与批量删除，仅删除本次 UUID 下的对象。

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
MySQL/MongoDB/Redis 版本与原检查保持一致，未做升级或重建。
Conda 中旧 editable `wagtailblog2` 对 Wagtail 的版本要求仍冲突，完整 `pip check` 已知不通过。
严格 MySQL Django 检查仍会报告 Wagtail WorkflowState 的 `models.W036`；没有放宽该检查。
需求书和任务书已按用户要求同步 RustFS 产品名称，原业务与权限要求保留。完整证据见说明书的环境准备记录。
