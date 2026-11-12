"""
头像存储命名空间、枚举与受控删除适配层。

统一兼容 RustFS (ProtectedRustFSStorage) 与测试环境本地存储，
避免业务表记录密钥或连接串，仅记录非敏感的存储命名空间摘要。
"""

from datetime import datetime, timezone as dt_timezone
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from django.core.files.storage import FileSystemStorage, storages

logger = logging.getLogger(__name__)


def get_storage_backend(storage_alias: str = "default") -> Any:
    """按 Django STORAGES 别名获取对应存储实例。"""
    return storages[storage_alias]


def compute_storage_namespace(storage: Any) -> str:
    """
    根据存储后端的非敏感位置参数计算稳定的命名空间标识（32 位十六进制摘要）。
    当存储别名切换到不同桶或不同根目录时，命名空间会随之变化，防止误删新桶对象。
    """
    cls_name = f"{type(storage).__module__}.{type(storage).__qualname__}"
    if hasattr(storage, "bucket_name"):
        endpoint = getattr(storage, "endpoint_url", "") or ""
        bucket = getattr(storage, "bucket_name", "") or ""
        raw = f"s3|{cls_name}|{endpoint}|{bucket}"
    elif hasattr(storage, "location"):
        loc = os.path.realpath(str(getattr(storage, "location", "") or ""))
        raw = f"fs|{cls_name}|{loc}"
    else:
        raw = f"other|{cls_name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def get_storage_and_namespace(storage_alias: str = "default") -> tuple[Any, str]:
    """返回存储后端实例及对应的非敏感命名空间摘要。"""
    storage = get_storage_backend(storage_alias)
    return storage, compute_storage_namespace(storage)


def controlled_delete_storage_object(storage: Any, storage_key: str) -> None:
    """
    执行受控物理删除。
    ProtectedRustFSStorage / ControlledFileSystemStorage 优先走 _controlled_physical_delete；
    标准本地 FileSystemStorage 仅在路径严格位于根目录内时执行删除。
    """
    if not storage_key:
        return
    if hasattr(storage, "_controlled_physical_delete"):
        storage._controlled_physical_delete(storage_key)
        return
    if isinstance(storage, FileSystemStorage):
        root = Path(storage.location).resolve()
        target = (root / storage_key).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"Refusing to delete path outside storage root: {storage_key}")
        storage.delete(storage_key)
        return
    raise RuntimeError(f"Storage backend {type(storage).__name__} does not support controlled physical delete")


def list_prefix_objects(storage: Any, prefix: str) -> tuple[list[dict[str, Any]], list[str]]:
    """
    列出指定存储前缀下的对象键与可信修改时间。
    返回 (objects, warnings)：
    - objects: [{"key": str, "last_modified": datetime | None, "size": int}]
    - warnings: 枚举或时间获取异常告警（只告警不猜测时间）
    """
    clean_prefix = prefix.lstrip("/")
    objects: list[dict[str, Any]] = []
    warnings: list[str] = []

    if hasattr(storage, "bucket_name") and hasattr(storage, "connection"):
        try:
            client = storage.connection.meta.client
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=storage.bucket_name, Prefix=clean_prefix):
                for item in page.get("Contents", []):
                    key = item.get("Key", "")
                    if not key or key.endswith("/"):
                        continue
                    lm = item.get("LastModified")
                    if lm is not None and lm.tzinfo is None:
                        lm = lm.replace(tzinfo=dt_timezone.utc)
                    objects.append({
                        "key": key,
                        "last_modified": lm,
                        "size": int(item.get("Size", 0) or 0),
                    })
        except Exception as exc:
            warnings.append(f"S3 枚举前缀 {clean_prefix} 失败: {exc}")
        return objects, warnings

    if hasattr(storage, "location"):
        root = Path(storage.location).resolve()
        target_dir = (root / clean_prefix).resolve()
        if target_dir != root and root not in target_dir.parents:
            warnings.append(f"前缀路径越界: {clean_prefix}")
            return objects, warnings
        if not target_dir.exists():
            return objects, warnings
        for dirpath, _, filenames in os.walk(target_dir):
            for fname in filenames:
                fpath = Path(dirpath) / fname
                try:
                    resolved = fpath.resolve()
                    if root not in resolved.parents:
                        continue
                    rel_key = resolved.relative_to(root).as_posix()
                    st = resolved.stat()
                    lm = datetime.fromtimestamp(st.st_mtime, tz=dt_timezone.utc) if st.st_mtime else None
                    objects.append({
                        "key": rel_key,
                        "last_modified": lm,
                        "size": int(st.st_size),
                    })
                except Exception as exc:
                    warnings.append(f"读取本地对象 {fpath} 属性失败: {exc}")
        return objects, warnings

    warnings.append(f"存储后端 {type(storage).__name__} 暂不支持前缀枚举")
    return objects, warnings
