"""
RustFS (S3 API) 保护性对象存储后端。

继承自 storages.backends.s3.S3Storage，贯彻以下核心保护机制：
1. file_overwrite = False：严禁重名覆盖，每次上传新文件生成新 key，保护历史 Revision 引用；
2. 重写 delete()：拦截业务代码与后台表单的同步物理删除调用，保留底层文件以待对账；
3. 全桶私有（default_acl = None）：禁止生成绕过鉴权的公网直链；
4. 仅供对账任务调用的 _controlled_physical_delete() 受控物理删除接口。
"""

import logging
from django.core.files.storage import FileSystemStorage
from storages.backends.s3 import S3Storage

logger = logging.getLogger(__name__)


class ProtectedRustFSStorage(S3Storage):
    default_acl = None
    file_overwrite = False
    querystring_auth = False
    signature_version = "s3v4"
    addressing_style = "path"

    def delete(self, name):
        """
        拦截同步直接物理删除调用。
        防止 Wagtail 原生 BaseImageForm / BaseDocumentForm 替换文件时直接删除正在被历史版本引用的底层文件。
        """
        logger.warning(
            "ProtectedRustFSStorage.delete called for %s - intercepted to protect references. "
            "Physical deletion must go through controlled reconciliation tasks.",
            name,
        )
        return

    def _controlled_physical_delete(self, name):
        """受控物理删除内部接口：仅供离线对账任务在确认全站零引用后调用"""
        return super().delete(name)


class ControlledFileSystemStorage(FileSystemStorage):
    """
    本地测试/离线开发使用的受控文件系统存储后端。
    与 ProtectedRustFSStorage 保持相同的 delete() 拦截与 _controlled_physical_delete() 语义。
    """

    def delete(self, name):
        logger.warning(
            "ControlledFileSystemStorage.delete called for %s - intercepted to protect references.",
            name,
        )
        return

    def _controlled_physical_delete(self, name):
        return super().delete(name)
