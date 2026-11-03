# myblog 工程协作规则

## 项目边界

- `myblog` 是独立的新 Wagtail 项目，旧项目只读参考，不修改旧仓库、旧数据库、旧配置或旧凭据。
- 不提交密码、Token、Cookie、私钥、`.env` 或本地数据库文件。
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

- 先检查 `git status`、当前分支和远程地址，再开始修改。
- 每次只实现一个明确的小目标；代码必须保留必要注释，说明不直观的架构、权限或兼容性原因。
- 修改后至少执行适用的 Python 语法检查、Django/Wagtail 检查、定向测试和 `git diff --check`。
- 测试失败先修复并重新验证；不得删除测试、放宽检查或用 Mock 掩盖真实错误。
- 未经授权不得执行 `migrate`、`makemigrations`、`runserver`、部署或连接真实服务。

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
