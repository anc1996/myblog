# RustFS 对接方案

日期：2026-11-04

状态：**核心应用已落地实施**。`media_library` 已实现 `ProtectedRustFSStorage` 保护拦截、全桶私有化、零公网签名、Django 逐次鉴权、206 Range 续传 / `X-Accel-Redirect` 交付与引用保护；全站当前使用 WSGI（无 ASGI），`tests/` 下已累积 54 份 `test_*.py` 自动化测试模块（早期记录的“96 项”为第一阶段历史基线）。

## 1. 当前选型

本项目对象存储统一使用 **RustFS 1.0.4**，通过标准 S3 API 对接。
MinIO 不再作为当前运行产品、客户端依赖或应用架构前提。
原方案中关于私有对象、媒体权限、Range 读取、引用保护和受控删除的要求继续有效，
只替换底层对象存储产品。

RustFS 官方 Docker 镜像使用 `rustfs/rustfs:1.0.4`，本机运行
linux/amd64，镜像版本摘要、提交号和下载校验记录见环境准备记录。

## 2. 服务连接合同

| 服务 | 版本 | 地址/端口 | 用户 | 数据范围 |
|---|---:|---|---|---|
| PostgreSQL | 18.6（`postgres:18.6-trixie`） | 由 `.env.local` / `.env.test` 注入（默认 `127.0.0.1:5432`） | `postgres` | 开发库 `myblog_dev` / 探针库 `myblog_test` / 自动测试库 `POSTGRES_TEST_DB`（默认 `test_myblog`） |
| Redis | 8.10.3 | 由 `.env.local` / `.env.test` 注入（默认 `REDIS_HOST=127.0.0.1`、`REDIS_PORT=6379`、`REDIS_DB=1`） | `default` | 由 `REDIS_DB` 与 `REDIS_KEY_PREFIX`（默认 `myblog`）隔离开发/测试缓存与限流命名空间 |
| MongoDB | 9.0.4 | 由 `.env.local` / `.env.test` 注入（默认 `127.0.0.1:27017`） | `root`，`authSource=admin` | `myblog_dev`、`myblog_test` |
| RustFS S3 API | 1.0.4 | 由 `.env.local` / `.env.test` 注入（默认 `http://127.0.0.1:9000`） | `root` | `myblog-dev`、`myblog-test` |
| RustFS 控制台 | 1.0.4 | 同宿主控制台端口（默认 `9001`） | `root` | 与 S3 API 共用管理员 |

各服务连接参数统一由本地 `.env.local` 或通过 `MYBLOG_ENV_FILE=.env.test` 注入（进程环境变量优先），代码中不硬编码局域网 IP 或固定测试库编号。
RustFS 的两个桶均为私有桶。当环境配置中存在 `S3_BUCKET` 时，`myblog/settings/base.py` 自动启用 `media_library.storage.ProtectedRustFSStorage`：

```text
S3_ENDPOINT_URL=http://127.0.0.1:9000   # 按本地 .env.local / .env.test 实际地址注入
S3_BUCKET=myblog-dev 或 myblog-test
S3_REGION=us-east-1
```

密码仅在本地 env 中按用户要求沿用，不写入 Git 可提交文档。
PostgreSQL 管理员为 `postgres`，MongoDB 和 RustFS 仍为 `root`；不把不同服务用户名一起替换。

## 3. 应用对接方式

1. Django 媒体层使用 boto3，不直接依赖 RustFS 私有实现。
2. 使用 SigV4 签名和 path-style 桶地址，兼容本地 RustFS。
3. RustFS 只保存对象；PostgreSQL 保存媒体元数据、权限、引用和删除状态。
4. 所有原图、Rendition、文档、音频、视频和 Range 请求先经过 Django 权限入口。
5. 首期由权限入口检查后代理文件响应，HEAD、Range 和续传请求每次重新检查权限。
   S3 预签名地址只用于已授权的存储探针；在有效期内通常不能即时撤销，
   因此不作为首期浏览器交付地址，也不能替代权限入口。不能暴露管理员密钥。
6. `myblog-dev` 与 `myblog-test` 只做开发/测试逻辑隔离，不提供账号级安全隔离。

媒体上传计划使用不可覆盖的对象 key；更换文件生成新 key，保留历史版本引用的旧对象。
复制 Page 共享公共媒体身份，仅复制页面自己的排列和图注。
真正删除文件前，必须在 PostgreSQL 中检查当前发布、草稿、历史 Revision 和回收站引用，
锁定资源并保存删除任务；重复任务和旧任务不能删除新对象。引用不完整时拒绝删除。
对象先上传而 SQL 保存失败时保留待核对对象，不静默切回本地文件存储。

按用户要求，本地密码沿用，各服务管理员用户名按上表区分。直接在控制台或通过管理员 S3 客户端删除文件，
会绕过 Django 的引用检查；此前批量删除验证只证明存储操作可用。
有业务数据后，正常删除应从应用入口发起，不能把管理控制台的删除当作业务验收。

## 4. 服务切换时已完成的验证

以下为 2026-11-03 切换记录，本轮文档更新没有重跑上传、删除和重启测试。

- RustFS 1.0.4 容器 healthy，API/控制台端口均发布到 `0.0.0.0`。
- RustFS S3 账号认证成功，控制台登录成功。
- 两个私有桶创建成功，匿名 GET/List 被拒绝。
- 上传、用户元数据、完整读取和 206 Range 读取成功。
- 短期预签名 GET 成功，篡改签名返回 403。
- 分片上传、S3 批量删除和控制台批量删除成功。
- RustFS 单独重启后对象内容和元数据保持一致。
- 其他 Redis、MongoDB 容器保持健康运行。

## 5. 已落地的 `media_library` 实现架构

原规划步骤已全部在 `myblog/apps/media_library/` 落地实现：

1. **受控媒体模型与引用索引**（`media_library/models.py`）：落地 `CustomImage`、`CustomDocument`、`Audio`、`Video`（均带 `public_id` UUID 与可见性控制）以及 `MediaReferenceRecord` 引用关系表。
2. **保护性对象存储后端**（`media_library/storage.py`）：落地 `ProtectedRustFSStorage(S3Storage)`（强制 `file_overwrite=False`、`default_acl=None`、`querystring_auth=False`、SigV4 与 path-style；重写 `delete()` 拦截同步物理删，保留仅供对账任务使用的 `_controlled_physical_delete()`），并配套离线测试用 `ControlledFileSystemStorage`。
3. **统一鉴权与 Range 交付网关**（`media_library/delivery.py`、`media_library/urls.py`）：`serve_media_asset` 与 `serve_rendition_asset` 实现逐次权限核验、HTTP 206 Range 续传、文档下载安全响应头（`X-Content-Type-Options: nosniff`、RFC 5987 `Content-Disposition` 强制附件下载）及生产 `X-Accel-Redirect` 支持；`settings/base.py` 通过 `WAGTAILDOCS_SERVE_METHOD = "serve_view"` 与自定义后台表单接管直链。
4. **Revision 引用追踪与删除防线**（`media_library/references.py`、`forms.py`、`signals.py`）：`MediaReferenceTracker.register_revision_references()` 在 Revision 生成时登记图片/文档/音视频引用，整数 PK 精确解析为 `public_id`；`check_asset_references()` 在物理删除前核验发布、草稿、历史 Revision 与回收站引用，存在引用即一票否决（Fail-Closed）。
5. **专项与全站测试覆盖**：`tests/test_s7_media_delivery.py`、`tests/test_document_download_and_sanitizer.py`、`tests/test_embeds_and_av_uploads.py` 等纳入全站 `tests/` 54 份 `test_*.py` 测试套件。

本方案不自动执行 `migrate`、`makemigrations`、`runserver` 或部署。

## 6. 环境降级边界与回退说明

- **配置降级条件**：`myblog/settings/base.py` 仅在环境变量未提供 `S3_BUCKET` 时才回退为本地 `FileSystemStorage`（离线单元/纯本地无桶环境）；一旦配置 `S3_BUCKET`，媒体默认存储即切换为 `ProtectedRustFSStorage`。
- **容错验证边界**：单节点开发与容器验证不能证明 RustFS 的分布式容错、横向扩容或厂商性能指标。

旧 MinIO 数据卷、镜像和切换前备份仅作为历史回退材料保留，不是当前对接方案。
若需要回退，先暂停应用写入和会修改对象的后台任务，保持 RustFS 可读，
通过 S3 导出新对象并核对清单、内容摘要、元数据和媒体引用；确认导出完整后停止 RustFS，
再恢复旧数据卷并反向迁移新对象。不能把 RustFS 数据目录直接挂载到旧产品。
完整回退演练和非空业务桶迁移仍未验证。
