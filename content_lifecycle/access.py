"""
内容生命周期与访问控制契约。

统一定义全站页面与媒体的访问判定边界：
1. 严格区分公开正式版（PUBLIC）与有权限的草稿预览（PREVIEW_DRAFT）/历史版本对比（PREVIEW_REVISION）；
2. 校验页面与父级祖先的回收状态；
3. 校验受限媒体（restricted）的交付资格。

本模块为纯接口契约与数据结构，导入时绝对无外部数据库或网络 IO。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AccessMode(str, Enum):
    PUBLIC = "public"                    # 普通公众访问已发布正式版本
    PREVIEW_DRAFT = "preview_draft"      # 后台有权用户预览当前最新草稿
    PREVIEW_REVISION = "preview_revision"# 后台有权用户查看指定历史版本


@dataclass(frozen=True)
class AccessContext:
    """访问者与权限上下文"""
    user: Any = None
    mode: AccessMode = AccessMode.PUBLIC
    site: Any = None
    session_key: str = ""
    target_revision_id: int | None = None
    can_view_restricted_media: bool = False
    permissions_fingerprint: str = ""
    extra_auth_info: dict[str, Any] = field(default_factory=dict)

    @property
    def is_public_reader(self) -> bool:
        return self.mode == AccessMode.PUBLIC


@dataclass(frozen=True)
class PublicationStatus:
    """页面正式发布状态快照"""
    page_id: int
    live: bool
    live_revision_id: int | None
    has_unpublished_changes: bool
    is_recycled: bool = False
    publication_generation: int = 1

    @property
    def is_publicly_visible(self) -> bool:
        """只有处于上线状态且未被回收的页面才对公众可见"""
        return self.live and (self.live_revision_id is not None) and (not self.is_recycled)
