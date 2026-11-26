"""
wagtailblog3 -> myblog 只读抽取与幂等 ETL 迁移工具包 (Phase 2)。

严格遵守以下安全边界：
1. 源生产环境（MySQL / MongoDB / MinIO）仅允许只读操作，绝不修改任何源数据；
2. 代码库内绝不保存任何生产凭据、SSH 密码或密钥；
3. 目标写库通过 OperationResult 与 MongoDB operation_id 实现全链路幂等与可重跑恢复。
"""
