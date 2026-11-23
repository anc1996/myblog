# 课件附件下载增强与通用多页面 Markdown 导入系统（含 10 万字超长文 AI 元数据引擎）架构与实现方案 RFC

> **版本状态**：第一～三批功能已全部实现（2026-11-22），并通过 Sol 设计、Argon 生命周期审查、Terra 独立安全反例审查修复，以及自动隔离测试库 108 项单元与集成回归测试验证。

---

## 一、 目标

1. **下载附件与导入页面拆分为两个独立入口**：
   - **附件下载**：上传 `.md`、`.markdown`、`.zip` 等文件到文档库，在页面正文中以单个文档或「课件/附件下载列表」形式提供给读者按原文件名下载，**不会自动创建文章**。
   - **Markdown 导入页面**：使用本地客户端或服务端导入接口，将本地 `.md` 与配套图片/附件转换成可编辑的 Wagtail **未发布草稿**，**默认不把原 `.md` 公开挂成下载附件、不自动发布、不自动送审**。
2. **通用多页面导入框架（避免重复造轮子）**：
   - 新建独立子应用 `myblog/apps/markdown_import`，采用与 `discovery/registry.py` 同源的**适配器注册机制（`MarkdownImportAdapterContract`）**。
   - 首期支持 `BlogPage`、`GuidePage`、`ProjectPage`；未来新增 `XxPage` 只需注册对应的 `ImportAdapter`，导入引擎与本地客户端自动支持，无需重写上传、解析与权限代码。
3. **支持双写作模式 + 10 万字以上超长文 AI 防崩元数据引擎**：
   - **双模式兼容**：既支持开头带 `---`（YAML Front Matter）批量自动映射，也支持完全不写 `---` 的随手笔记直接导入。
   - **智能补全**：`title` 优先级为 `YAML title` → 正文首块顶层 `# H1`（提取后剥离首块重复 H1）→ **无 H1 时 AI 自动生成** → 清洗后文件名兜底；`date` 缺省自动填今天 `authored_on`；`tags` 支持中英文逗号/顿号/数组并归一化对齐站内 Taggit 词库。
   - **10 万字防崩与可升级 AI 封装**：通过 `StratifiedDigestBuilder` 对超长文去噪后按四区（20% 全文骨架+高频词、30% 开篇、30% 中段跨 `##` 均匀抽样、20% 结尾）压缩为 `16,000` 字符预算摘要；采用薄协议 `LLMClientProtocol` 解耦 `openai` / `httpx`，升级 SDK 仅改单文件，并保证 AI 故障时 100% 自动降级到本地提取，绝不阻断草稿创建。

---

## 二、 做法与关键加固契约

### 2.1 附件上传、下载网关与课件列表积木

- **上传白名单与服务端强校验**（[base.py](../myblog/settings/base.py)、[forms.py](../myblog/apps/media_library/forms.py)）：
  - `WAGTAILDOCS_EXTENSIONS` 新增 `'md'`、`'markdown'`；`WAGTAILDOCS_MAX_UPLOAD_SIZE` 提升至 `50 MiB`；`DATA_UPLOAD_MAX_MEMORY_SIZE` 显式设为 `20 MiB`。
  - 提取公共 `validate_document_upload()`，在 `ProtectedDocumentForm.clean_file()` 与导入上传服务中共用，防止 API 绕过后台前端校验上传非法扩展名、0 字节或超大文件，并保留被 `MediaReferenceRecord` 引用后禁止替换原文件的保护。
- **网关统一安全响应头**（[delivery.py](../myblog/apps/media_library/delivery.py)）：
  - 对 `document` 的普通 `GET`、`HEAD`、`206 Range` 与 `USE_X_ACCEL_REDIRECT` 内部转发四条路径，统一补齐 RFC 5987 下载头 `Content-Disposition: attachment; filename="..."; filename*=UTF-8''...`、`X-Content-Type-Options: nosniff`、`Content-Security-Policy: default-src 'none'` 与 `Cache-Control: private, no-store`。
  - 修正 `_handle_range_request` 在退出前显式关闭 `file_obj` 防止句柄泄露，并对 `X-Accel-Redirect` 路径做 `quote(..., safe="/")` 编码以支持中文/空格文件名。
- **HTML 净化与公共积木**（[sanitizer.py](../myblog/apps/content_blocks/sanitizer.py)、[media.py](../myblog/apps/content_blocks/blocks/media.py)）：
  - 净化器对 `<a>` 放行 `"download"` 与 `"class"`，并通过结构化 `HTMLParser` 二次过滤，**仅保留站内 `/media-gateway/document/<UUID>/` 链接的 `download` 属性与 `doc-download-link` 样式**。
  - `CustomDocumentBlock` 展示格式徽标（如 `MD`、`ZIP`、`PDF`）与本地读取的文件大小（不请求对象存储）；新增 `CustomAttachmentListBlock`（`heading` + `items: ListBlock`），逐项按 `{path}.items.{idx}.document` 登记 `MediaReferenceRecord`。
- 已将其加入 `get_common_stream_blocks()` 与 `PortfolioStreamBlock`（补齐 `document`/`audio`/`embed`/`attachment_list`），并已生成 `blog.0003`、`guides.0003`、`portfolio.0003` 迁移。

### 2.2 通用多页面导入框架与 Wagtail 8.1 生命周期加固

1. **共享适配器契约**（`myblog/apps/markdown_import/contracts.py`、`registry.py`）：
   - `MarkdownImportAdapterContract` 定义 `describe()`、`normalize()`、`build()`、`publish_warnings()`；由各页面应用自愿注册，公共导入层不反向耦合具体页面模型。
   - 首期内置映射：
     - `BlogPage`：映射到 `intro`（外置 `StreamField`）与 `body`（外置 `StreamField`），两者绑定同一 `Revision`；
     - `GuidePage`：映射到 `summary`（SQL `TextField`）、`topic`（按 `GuideTopic.slug` 精确匹配）、`difficulty`、`estimated_minutes` 与 `body`（识别 `:::step ... :::` 块）；缺失 `topic` 或 `step` 允许存草稿并明确提示发布阻断项，不改写 `last_reviewed_at` 为人工复核日期；
     - `ProjectPage`：映射到 `project_status`、`repo_url`、`demo_url`、`summary`（SQL `TextField`）与 `body`（外置 `StreamField`）。
2. **已落地的生命周期与安全加固（结合 Argon 与 Terra 双轮审查）**：
   - **统一写前脱水与 `MediaReferenceRecord` 登记**：`BlogPage`、`GuidePage` 与 `ProjectPage`（`ExternalStreamPageMixin`）统一先在完整内存正文上执行 `full_clean` / `minimal_clean`，再于 `save()` 与 `serializable_data()` 写入 PostgreSQL 前即清空外置列为 `[]`，同时在事务内从内存正文完整登记 `MediaReferenceRecord`；导入提交阶段使用确定性积木 `id`（`uuid5`），消除 SQL 失败重试时的 Mongo 幂等哈希冲突。
   - **会话代次绑定与编程式建页权限**：`MarkdownImportToken` 与 `MarkdownImportPairCode` 绑定 `MemberProfile.session_epoch`，改密或管理员踢下线后立即失效；建页阶段复用 `bind_linked_person_for_page_owner()` 与 `OwnershipPagePermissionTester`、`can_create_at()`、`author_allow_child_creation`，并排除回收/别名父页；受限页面发布时严禁自动公开作者附件。
   - **清单强校验、明文 HTTP 防护与对账 Fail-Closed**：资源清单强制要求正整数大小与 64 位 SHA-256 及实收字节累加 `<= 300 MiB`；客户端默认强制 HTTPS（非本机 HTTP 需显式授权）；`CrossStorageReconciler` 暂存资产查询异常时 Fail-Closed 绝不静默跳过物理清理防误删。

### 2.3 10 万字超长文 AI 引擎与可维护第三方库架构

```text
10 万字+ Markdown 原文
  ├─ 1. 去噪与脱敏（去 Base64、长表格截断、长代码保留签名、疑似密钥替换为 [REDACTED]）
  └─ 2. StratifiedDigestBuilder 四区采样（默认 16,000 字符）
        ├─ 20% (3,200) 全文 H1~H4 目录骨架 + jieba 高频技术词
        ├─ 30% (4,800) 开篇背景段落
        ├─ 30% (4,800) 中段跨 ## 章节均匀轮转抽样（绝不丢失中段核心内容）
        └─ 20% (3,200) 结尾总结段落
              ↓
      AI 服务层降级阶梯（最多 3 次出网，总时限 <= 20s）
        ├─ 正常：16,000 -> 上下文超限折半：6,000 / 8,000 -> 4,000
        ├─ 调用薄封装 LLMClientProtocol（OpenAISDKProvider / HttpxProvider，惰性导入，强制 store=False，max_retries=0）
        └─ 失败或关闭外发：自动回退本地确定性提取（文件名/首句 + 首段 + jieba 匹配站内标签），100% 不阻断草稿
```

---

## 三、 已完成验证

1. **离线与集成测试（隔离测试库 `test_myblog`）**：`tests.test_markdown_import_integration`、`tests.test_markdown_import_parser`、`tests.test_markdown_import_ai`、`tests.test_document_download_and_sanitizer`、`tests.test_s3_blocks` 及全站既有核心回归（`test_guides`、`test_portfolio`、`test_author_isolation`、`test_lifecycle_and_argon_fixes`、`test_s5_blog_models`、`test_s6_mongo_revision`、`test_s8_recycle_restore`、`test_p1_content_enhancements`、`test_admin_split_editors`、`test_embeds_and_av_uploads`、`test_vendor_sync`、`test_embed_playback`）共 **108 项全部通过**。
2. **系统检查**：`manage.py check` 无问题；`manage.py makemigrations --check --dry-run` 无未生成迁移；`git diff --check` 通过。

---

## 四、 还没解决与待实测项

- 若后续在本地开发库（`.env.local`）启动服务体验新界面，需按授权在该库执行 `migrate` 并视需求启动测试服务。
- 桌面 Tkinter GUI 窗口实操、后台界面上传 `.md`/50 MiB `.zip` 浏览器实操、生产环境 Nginx `X-Accel-Redirect` 最终响应头及真实外部大模型网关调用尚未在真实生产/桌面图形界面联合实测。
