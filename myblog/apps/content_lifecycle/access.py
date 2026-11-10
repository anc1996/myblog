"""
内容生命周期与访问控制契约。

【教学架构导读】：
在内容管理系统（CMS）与高并发门户中，“谁在看什么内容”是系统安全与数据一致性的头号生命线。
本模块是全站权限判定与访问边界的“宪法性基础契约”：
1. 【严格三态分流】：清晰定义公开正式版（PUBLIC）、草稿预览（PREVIEW_DRAFT）与历史版本对比（PREVIEW_REVISION）；
2. 【零 IO 纯净契约】：本模块仅定义纯数据类（dataclass）与枚举（Enum），导入时绝对不连接数据库、不发起网络 IO，
   因此可以在系统任何底层模块（API、搜索索引、生命周期信号、异步任务）中放心导入，永不发生“循环依赖”或“隐式数据库连接泄漏”；
3. 【不可变性保证】：所有上下文契约均采用 frozen=True 冻结，保证在多层管道函数传递过程中属性不被暗中篡改，天然线程安全。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AccessMode(str, Enum):
    """
    访问模式枚举（字符串枚举类型）。

    【变量与枚举成员说明】：
    - PUBLIC ("public"): 普通公众访问模式。
      * 含义：匿名的互联网读者或普通注册用户访问前台页面。
      * 安全契约：严格只读通过审核并正式发布的 live_revision 版本；草稿、下架、已入回收站的内容绝对不可见。
    - PREVIEW_DRAFT ("preview_draft"): 后台草稿预览模式。
      * 含义：已登录且拥有编辑权限的创作者在 Wagtail 后台点击“预览”按钮。
      * 行为特征：优先从 MongoDB 中水合提取当前尚未发布的最新草稿（get_latest_revision()），供编辑检查排版。
    - PREVIEW_REVISION ("preview_revision"): 后台历史版本比对与回溯模式。
      * 含义：管理员在 Wagtail 历史版本列表（Revisions）中点击查看某个具体的历史节点或进行版本差异对比。
      * 行为特征：严格根据传入的 target_revision_id 提取那一次特定的正文快照。
    """
    PUBLIC = "public"                      # 普通公众访问已发布正式版本
    PREVIEW_DRAFT = "preview_draft"        # 后台有权用户预览当前最新草稿
    PREVIEW_REVISION = "preview_revision"  # 后台有权用户查看指定历史版本


@dataclass(frozen=True)
class AccessContext:
    """
    访问者与权限上下文契约（不可变只读数据类）。

    【参数与字段详细说明】：
    - user (Any): 发起当前请求的用户对象（通常为 Django 的 User 模型实例或 AnonymousUser 匿名对象）。
    - mode (AccessMode): 当前访问模式，默认严格取值为 AccessMode.PUBLIC（即零配置下默认走最安全的公网读者模式）。
    - site (Any): 当前 HTTP 请求所命中的 Wagtail 站点对象（Site 实例），用于多站点多域名内容隔离。
    - session_key (str): 当前请求的 Django Session 唯一标识字符串，用于草稿临时鉴权防重放或频控跟踪。
    - target_revision_id (int | None): 目标历史版本 ID。仅在 mode == PREVIEW_REVISION 时生效，指定读取哪一条 Revision。
    - can_view_restricted_media (bool): 权限标记位。布尔值为 True 时代表该用户拥有解密或直链访问私有受保护媒体的特权。
    - permissions_fingerprint (str): 权限摘要指纹（字符串哈希）。用于 HTTP ETag 协商缓存与 Redis 缓存键前缀，
      确保特权用户浏览内容产生的缓存绝不会被错误提供给未登录读者。
    - extra_auth_info (dict[str, Any]): 扩展认证与审计信息字典（如客户端 IP、OAuth 作用域等），预留扩展通道。
    """
    user: Any = None                                                 # 发起请求的用户对象（User / AnonymousUser）
    mode: AccessMode = AccessMode.PUBLIC                             # 访问模式（默认严格为普通公网模式）
    site: Any = None                                                 # 命中的 Wagtail Site 站点实例
    session_key: str = ""                                            # 客户端 Session 标识
    target_revision_id: int | None = None                            # 显式指定的历史版本 ID（仅在 PREVIEW_REVISION 模式下有效）
    can_view_restricted_media: bool = False                          # 是否具备查看私有受保护媒体的特权
    permissions_fingerprint: str = ""                                # 权限指纹哈希（用于缓存键隔离）
    extra_auth_info: dict[str, Any] = field(default_factory=dict)    # 扩展认证元数据字典

    @property
    def is_public_reader(self) -> bool:
        """辅助方法：判断当前请求是否为普通公网公众访问"""
        return self.mode == AccessMode.PUBLIC


@dataclass(frozen=True)
class PublicationStatus:
    """
    页面正式发布状态快照（不可变只读数据类）。

    【参数与字段详细说明】：
    - page_id (int): 页面在 PostgreSQL 关系库中的主键 ID。
    - live (bool): Wagtail 原生发布标志位。True 表示该页面被标记为已发布状态。
    - live_revision_id (int | None): 当前正式在线生效的 Wagtail Revision 外键 ID。
      如果页面从未发布过，此字段为 None。
    - has_unpublished_changes (bool): 是否存在未发布的修改草稿。
      如果编辑者修改了页面内容但还没点击“发布”，此字段为 True。
    - is_recycled (bool): 原树回收站软删除标志位。默认 False。若为 True，说明该页面已被作者移入回收站。
    - publication_generation (int): 发布代次全局递增计数器（代次序号）。
      每次页面发生正式发布或下架跃迁时原子 +1，用于并发写操作防乱序与旧异步事件防覆盖。
    """
    page_id: int                     # 页面数据库主键 ID
    live: bool                       # 是否处于已发布状态
    live_revision_id: int | None     # 当前正式生效的 Revision 版本 ID（未发布时为 None）
    has_unpublished_changes: bool    # 是否存在尚未发布的本地修改草稿
    is_recycled: bool = False        # 是否处于受控回收站软删除状态
    publication_generation: int = 1  # 发布代次单调自增序号（防并发乱序与过期覆盖）

    @property
    def is_publicly_visible(self) -> bool:
        """
        全站公网可见性唯一判定法则（核心安全红线）。

        【业务规则】：
        一个页面想要被互联网公众正常访问，必须同时满足以下三个硬性条件：
        1. self.live 为 True（不是处于未发布或被下架状态）；
        2. self.live_revision_id 不为 None（存在确切核准通过的正式发布版本）；
        3. not self.is_recycled 为 True（没有被移入受控回收站）。
        """
        return self.live and (self.live_revision_id is not None) and (not self.is_recycled)
