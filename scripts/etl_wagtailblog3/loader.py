"""
幂等 ETL 加载、预检 (--dry-run) 与对账 (--reconcile) 引擎 (Stage 2B)。

核心保证：
1. 三端目标隔离：同时锁定 PostgreSQL、MongoDB 与 RustFS/S3 私有存储，
   禁止任意一端为空、指向开发库/旧站库或混用本地文件存储；
2. 双层幂等：SQL 侧通过 external_content.OperationResult 记录 input_hash + result_json，
   MongoDB 侧通过确定性 operation_id 幂等写入 content_field_versions；
3. 严格时序（Pass 1 -> Pass 1.5 -> Pass 2）：
   - Pass 1 拓扑建树（全员 live=False 骨架）；
   - Pass 1.5 在任何发布前恢复 PageViewRestriction 与 Group（无法确认组映射/口令/祖先限制则保留草稿）；
   - Pass 2 水合正文、写入 MongoDB 快照，仅对源站 live=True 且限制已确认的页面执行发布，
     且仅将无限制公开页实际引用的本批资产提升为 PUBLIC，受限页与草稿资产严守 RESTRICTED；
4. 完整对账：区分已发布/草稿/受限预期，核验公开媒体全转正且无受限/草稿媒体越权公开。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .transformer import (
    compute_excluded_test_page_ids,
    merge_page_view_counts,
    normalize_legacy_redirect_old_path,
    plain_text_to_richtext_html,
    rewrite_richtext_db_html,
    sha256_json,
    strip_html_to_plain_text,
    transform_comments_with_folding,
    transform_stream_blocks,
)

logger = logging.getLogger(__name__)


class TargetIsolationError(RuntimeError):
    """目标库隔离校验失败时抛出，防止误污染开发库。"""


SNAPSHOT_MIN_SCHEMA_VERSION = 2
_FORBIDDEN_DB_NAMES = {"myblog_dev", "wagtailblog", "wagtailblog3"}
_FORBIDDEN_BUCKET_NAMES = {"myblog_dev", "myblog-dev", "dev", "wagtailblog", "wagtailblog3"}


def _is_forbidden_db_or_bucket_name(raw_name: str, *, forbidden_exact: set[str]) -> bool:
    val = raw_name.strip().lower()
    if not val:
        return True
    if val in forbidden_exact:
        return True
    if val.endswith(("_dev", "-dev")) or val.startswith(("dev_", "dev-")):
        return True
    if "wagtailblog" in val:
        return True
    return False


def assert_target_isolation(*, _allow_test_isolation: bool = False) -> dict[str, Any]:
    """
    三端成套目标隔离守卫（PostgreSQL + MongoDB + RustFS/S3）。
    CLI 不可传入测试豁免标志；正式执行时任意一端为空、指向开发/旧站库或使用本地文件存储均立即阻断。
    """
    pg_cfg = (getattr(settings, "DATABASES", None) or {}).get("default") or {}
    pg_engine = str(pg_cfg.get("ENGINE") or "").strip()
    pg_host = str(pg_cfg.get("HOST") or "").strip()
    pg_port = str(pg_cfg.get("PORT") or "").strip()
    pg_name = str(pg_cfg.get("NAME") or "").strip()

    mongo_cfg = getattr(settings, "MONGO_SETTINGS", None) or {}
    mongo_host = str(mongo_cfg.get("HOST") or "").strip()
    mongo_port_raw = mongo_cfg.get("PORT")
    mongo_port = str(mongo_port_raw).strip() if mongo_port_raw not in (None, "") else ""
    mongo_db = str(mongo_cfg.get("DATABASE") or "").strip()

    storages_cfg = getattr(settings, "STORAGES", None) or {}
    default_storage_cfg = storages_cfg.get("default") if isinstance(storages_cfg, dict) else {}
    if not isinstance(default_storage_cfg, dict):
        default_storage_cfg = {}
    storage_backend = str(default_storage_cfg.get("BACKEND") or "").strip()
    storage_opts = default_storage_cfg.get("OPTIONS")
    if not isinstance(storage_opts, dict):
        storage_opts = {}
    s3_endpoint = str(storage_opts.get("endpoint_url") or "").strip()
    s3_bucket = str(storage_opts.get("bucket_name") or "").strip()

    if _is_forbidden_db_or_bucket_name(pg_name, forbidden_exact=_FORBIDDEN_DB_NAMES):
        raise TargetIsolationError(
            f"拒绝向受限或空 PostgreSQL 库 '{pg_name}' 执行 ETL 导入！请配置独立的 staging/production 目标库。"
        )

    if _allow_test_isolation:
        # 测试专用豁免仅允许作用于 test_* 测试库下的 ControlledFileSystemStorage，绝不能用于 staging/prod
        if not pg_name.lower().startswith("test"):
            raise TargetIsolationError(
                f"测试隔离豁免仅限 test_* 测试库使用，当前 PostgreSQL 库为 '{pg_name}'。"
            )
        if _is_forbidden_db_or_bucket_name(mongo_db, forbidden_exact=_FORBIDDEN_DB_NAMES) or "test" not in mongo_db.lower():
            raise TargetIsolationError(
                f"测试隔离豁免模式下 MongoDB 同样必须指向测试库（名称含 test），当前为 {mongo_db!r}。"
            )
        backend_lower = storage_backend.lower()
        if backend_lower not in (
            "django.core.files.storage.filesystemstorage",
            "media_library.storage.controlledfilesystemstorage",
        ):
            raise TargetIsolationError(
                f"测试隔离豁免模式禁止连接真实对象存储后端（当前 BACKEND={storage_backend!r}）。"
            )
        media_root = str(getattr(settings, "MEDIA_ROOT", "") or "").strip()
        if not media_root:
            raise TargetIsolationError("测试隔离模式下使用本地文件存储时必须显式设置 MEDIA_ROOT。")
    else:
        if pg_name.lower().startswith("test"):
            raise TargetIsolationError(
                f"正式 ETL 导入禁止直接指向测试库 '{pg_name}'。"
            )
        if "postgresql" not in pg_engine.lower() or not pg_host or not pg_port:
            raise TargetIsolationError(
                f"PostgreSQL 目标配置不完整或非 PostgreSQL 后端：ENGINE={pg_engine!r}, HOST={pg_host!r}, PORT={pg_port!r}。"
            )
        if not mongo_host or not mongo_port or _is_forbidden_db_or_bucket_name(mongo_db, forbidden_exact=_FORBIDDEN_DB_NAMES):
            raise TargetIsolationError(
                f"MongoDB 目标配置被隔离守卫拦截：HOST={mongo_host!r}, PORT={mongo_port!r}, DATABASE={mongo_db!r}。"
            )
        try:
            if int(mongo_port) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise TargetIsolationError(f"MongoDB 端口无效：{mongo_port!r}") from exc

        if storage_backend != "media_library.storage.ProtectedRustFSStorage":
            raise TargetIsolationError(
                f"ETL 目标存储必须使用受控私有后端 media_library.storage.ProtectedRustFSStorage（当前 BACKEND={storage_backend!r}）。"
            )
        if storage_opts.get("default_acl") is not None or bool(storage_opts.get("querystring_auth")):
            raise TargetIsolationError(
                "ETL 目标对象存储必须保持私有配置（default_acl=None, querystring_auth=False）。"
            )
        if not s3_endpoint or _is_forbidden_db_or_bucket_name(s3_bucket, forbidden_exact=_FORBIDDEN_BUCKET_NAMES):
            raise TargetIsolationError(
                f"S3/RustFS 目标桶配置被隔离守卫拦截：endpoint_url={s3_endpoint!r}, bucket_name={s3_bucket!r}。"
            )
        norm_pg = pg_name.lower().replace("-", "_")
        norm_mongo = mongo_db.lower().replace("-", "_")
        norm_bucket = s3_bucket.lower().replace("-", "_")
        if norm_mongo != norm_pg or norm_bucket != norm_pg:
            raise TargetIsolationError(
                f"三端目标未成套绑定，拒绝混用跨环境存储：postgres={pg_name!r}, mongo={mongo_db!r}, bucket={s3_bucket!r}。"
            )

    return {
        "postgres": {
            "engine": pg_engine,
            "host": pg_host,
            "port": pg_port,
            "database": pg_name,
        },
        "mongo": {
            "host": mongo_host,
            "port": mongo_port,
            "database": mongo_db,
        },
        "storage": {
            "backend": storage_backend,
            "endpoint_url": s3_endpoint,
            "bucket_name": s3_bucket,
        },
        "postgres_db": pg_name,
        "mongo_db": mongo_db,
        "s3_bucket": s3_bucket,
    }


def _parse_dt(raw: str | None):
    if not raw:
        return None
    dt = parse_datetime(raw)
    if dt is not None and timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _run_idempotent(
    operation_id: str,
    action: str,
    input_payload: Any,
    fn,
    *,
    page_id: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """
    基于 external_content.OperationResult 的 SQL 幂等守卫：
    - 同 operation_id + 同 input_hash：跳过执行并直接返回历史 result_json；
    - 同 operation_id + 异 input_hash：抛出 IdempotencyConflictError 立即停机，绝不静默覆盖。
    """
    from external_content.contracts import IdempotencyConflictError
    from external_content.models import OperationResult

    in_hash = sha256_json(input_payload)
    with transaction.atomic():
        existing = OperationResult.objects.select_for_update().filter(operation_id=operation_id).first()
        if existing is not None:
            if existing.input_hash != in_hash:
                raise IdempotencyConflictError(
                    f"Operation '{operation_id}' hash mismatch: expected {existing.input_hash}, got {in_hash}"
                )
            return dict(existing.result_json or {}), True

        result_json = fn()
        OperationResult.objects.create(
            operation_id=operation_id,
            action=action,
            page_id=page_id,
            input_hash=in_hash,
            result_json=result_json,
        )
        return result_json, False


def _load_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    snap_file = Path(snapshot_dir) / "snapshot.json"
    if not snap_file.is_file():
        raise FileNotFoundError(f"Snapshot file not found: {snap_file}")
    return json.loads(snap_file.read_text(encoding="utf-8"))


def _validate_snapshot_schema(snap: dict[str, Any]) -> int:
    raw_ver = snap.get("schema_version")
    try:
        schema_version = int(raw_ver) if raw_ver is not None else 1
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效的快照 schema_version={raw_ver!r}") from exc
    if schema_version < SNAPSHOT_MIN_SCHEMA_VERSION or "page_view_restrictions" not in snap or not isinstance(
        snap.get("page_view_restrictions"), list
    ):
        raise ValueError(
            f"拒绝导入旧版快照（schema_version={schema_version}）："
            f"必须为 schema_version >= {SNAPSHOT_MIN_SCHEMA_VERSION} 且包含 page_view_restrictions 字段，"
            "请使用新版 remote_export 重新导出。"
        )
    return schema_version


def _collect_source_restricted_page_ids(
    snap: dict[str, Any],
    pages: list[dict[str, Any]],
) -> set[int]:
    """根据快照中的 page_view_restrictions 与路径前缀计算所有自身或祖先受限的源页面 ID。"""
    page_path_by_id = {int(p["id"]): str(p.get("path") or "") for p in pages}
    site_home_id = snap.get("site", {}).get("home_id")
    site_home_path = str(snap.get("site", {}).get("home_path") or "")
    if site_home_id is not None and site_home_path:
        page_path_by_id.setdefault(int(site_home_id), site_home_path)

    restricted_prefixes: list[str] = []
    restricted_direct_ids: set[int] = set()
    has_global_unknown_path = False

    for pvr in snap.get("page_view_restrictions", []) or []:
        rtype = str(pvr.get("restriction_type") or "").strip().lower()
        if rtype in ("", "none"):
            continue
        pid = int(pvr["page_id"])
        restricted_direct_ids.add(pid)
        ppath = str(pvr.get("page_path") or page_path_by_id.get(pid) or "")
        if ppath:
            restricted_prefixes.append(ppath)
        elif pid not in page_path_by_id:
            has_global_unknown_path = True

    out: set[int] = set()
    for p in pages:
        pid = int(p["id"])
        ppath = str(p.get("path") or "")
        if has_global_unknown_path or pid in restricted_direct_ids or (
            ppath and any(ppath.startswith(prefix) for prefix in restricted_prefixes)
        ):
            out.add(pid)
    return out


def dry_run_snapshot(snapshot_dir: Path, *, include_test_pages: bool = False) -> dict[str, Any]:
    """
    仅内存预检与转换校验，绝不写入 PostgreSQL、MongoDB 或 RustFS。
    输出预计迁入数量、排除数量与异常明细。
    """
    snapshot_dir = Path(snapshot_dir)
    snap = _load_snapshot(snapshot_dir)
    schema_version = int(snap.get("schema_version") or 1)
    index_pages = snap.get("index_pages", [])
    blog_pages = snap.get("blog_pages", [])
    excluded_ids = compute_excluded_test_page_ids(index_pages, blog_pages, include_test_pages=include_test_pages)

    included_indexes = [p for p in index_pages if int(p["id"]) not in excluded_ids]
    included_blogs = [p for p in blog_pages if int(p["id"]) not in excluded_ids]

    # 构造虚拟 ID 映射以全量校验所有块与正文转换逻辑
    image_id_map = {int(r["id"]): int(r["id"]) for r in snap.get("images", [])}
    image_uuid_map = {int(r["id"]): f"00000000-0000-4000-8000-{int(r['id']):012d}" for r in snap.get("images", [])}
    image_caption_map = {int(r["id"]): str(r.get("caption") or "") for r in snap.get("images", [])}
    doc_id_map = {int(r["id"]): int(r["id"]) for r in snap.get("documents", [])}
    doc_desc_map = {int(r["id"]): str(r.get("description") or "") for r in snap.get("documents", [])}
    audio_id_map = {int(m["id"]): int(m["id"]) for m in snap.get("media", []) if m.get("type") == "audio"}
    video_id_map = {int(m["id"]): int(m["id"]) for m in snap.get("media", []) if m.get("type") == "video"}
    page_id_map = {int(p["id"]): int(p["id"]) for p in included_indexes + included_blogs}

    anomalies: list[dict[str, Any]] = []
    if schema_version < SNAPSHOT_MIN_SCHEMA_VERSION or "page_view_restrictions" not in snap:
        anomalies.append({
            "kind": "legacy_snapshot_schema",
            "schema_version": schema_version,
        })
    missing_blobs: list[str] = []
    for group in ("images", "documents", "media"):
        for row in snap.get(group, []):
            for bkey in ("blob", "thumbnail_blob"):
                b = row.get(bkey)
                if b:
                    p = snapshot_dir / b["local_rel_path"]
                    if not p.is_file():
                        missing_blobs.append(b["local_rel_path"])

    block_counts: dict[str, int] = {}
    for idx in included_indexes:
        rewrite_richtext_db_html(
            plain_text_to_richtext_html(idx.get("intro") or ""),
            image_id_map=image_id_map,
            page_id_map=page_id_map,
            doc_id_map=doc_id_map,
            excluded_page_ids=excluded_ids,
            anomalies=anomalies,
            source_page_id=int(idx["id"]),
        )

    for bp in included_blogs:
        transformed = transform_stream_blocks(
            bp.get("body_blocks") or [],
            image_id_map=image_id_map,
            image_uuid_map=image_uuid_map,
            image_caption_map=image_caption_map,
            doc_id_map=doc_id_map,
            doc_desc_map=doc_desc_map,
            audio_id_map=audio_id_map,
            video_id_map=video_id_map,
            page_id_map=page_id_map,
            excluded_page_ids=excluded_ids,
            anomalies=anomalies,
            source_page_id=int(bp["id"]),
        )
        for blk in transformed:
            bt = blk["type"]
            block_counts[bt] = block_counts.get(bt, 0) + 1

    folded_comments, comment_anomalies = transform_comments_with_folding(
        snap.get("comments", []),
        excluded_page_ids=excluded_ids,
    )
    anomalies.extend(comment_anomalies)
    pv_by_page = merge_page_view_counts(snap.get("page_views", []), excluded_page_ids=excluded_ids)

    included_redirects = [
        rd for rd in snap.get("redirects", [])
        if rd.get("redirect_page_id") is None or int(rd["redirect_page_id"]) not in excluded_ids
    ]

    source_restricted_ids = _collect_source_restricted_page_ids(snap, included_indexes + included_blogs)

    return {
        "mode": "dry_run",
        "schema_version": schema_version,
        "excluded_test_page_ids": sorted(excluded_ids),
        "counts": {
            "users": len(snap.get("users", [])),
            "groups": len(snap.get("groups", [])),
            "page_view_restrictions": len(snap.get("page_view_restrictions", [])),
            "authors": len(snap.get("authors", [])),
            "images": len(snap.get("images", [])),
            "documents": len(snap.get("documents", [])),
            "audio": len(audio_id_map),
            "video": len(video_id_map),
            "index_pages_total": len(index_pages),
            "index_pages_included": len(included_indexes),
            "blog_pages_total": len(blog_pages),
            "blog_pages_included": len(included_blogs),
            "blog_pages_live": sum(1 for p in included_blogs if bool(p.get("live"))),
            "blog_pages_draft": sum(1 for p in included_blogs if not bool(p.get("live"))),
            "blog_pages_restricted": sum(1 for p in included_blogs if int(p["id"]) in source_restricted_ids),
            "approved_comments_included": len(folded_comments),
            "comment_reactions_included": len([
                cr for cr in snap.get("comment_reactions", [])
                if any(int(c["id"]) == int(cr["comment_id"]) for c in folded_comments)
            ]),
            "pv_pages": len(pv_by_page),
            "pv_daily_rows": sum(len(v) for v in pv_by_page.values()),
            "pv_total_sum": sum(r["pv"] for rows in pv_by_page.values() for r in rows),
            "redirects_included": len(included_redirects),
            "social_links": len(snap.get("social_links", [])),
        },
        "block_counts": block_counts,
        "missing_blobs": missing_blobs,
        "anomalies": anomalies,
    }


def apply_snapshot(
    snapshot_dir: Path,
    *,
    batch_id: str = "wb3_v2",
    skip_search_rebuild: bool = False,
    include_test_pages: bool = False,
    restriction_passwords: dict[int, str] | None = None,
    group_name_map: dict[str, str | None] | None = None,
    _allow_test_isolation: bool = False,
) -> dict[str, Any]:
    """
    执行全量幂等迁移落库（三端隔离与快照版本守卫 -> 媒体 -> 作者/标签 -> Pass 1 骨架 ->
    Pass 1.5 访问限制恢复 -> Pass 2 正文/受控发布 -> 互动/统计/重定向 -> 索引重建）。
    支持中断后同 batch_id 重复执行恢复，绝不重复建页或翻倍统计。
    """
    isolation = assert_target_isolation(_allow_test_isolation=_allow_test_isolation)
    snapshot_dir = Path(snapshot_dir)
    snap = _load_snapshot(snapshot_dir)
    schema_version = _validate_snapshot_schema(snap)
    snap_sha256 = hashlib.sha256((snapshot_dir / "snapshot.json").read_bytes()).hexdigest()

    # 历史批次只读保护（Q4）：若 batch_id 已存在旧操作记录但无匹配的 v2 batch_meta，在首次写入前立即停机
    from external_content.models import OperationResult
    batch_meta_op_id = f"{batch_id}:batch_meta"
    if (
        OperationResult.objects.filter(operation_id__startswith=f"{batch_id}:").exists()
        and not OperationResult.objects.filter(operation_id=batch_meta_op_id).exists()
    ):
        raise TargetIsolationError(
            f"批次 '{batch_id}' 属于历史已落库批次，按规定仅允许只读 --reconcile 对账，禁止写入或覆盖！请改用新 --batch-id。"
        )
    _run_idempotent(
        batch_meta_op_id,
        "etl_batch_meta",
        {
            "schema_version": schema_version,
            "snapshot_sha256": snap_sha256,
            "include_test_pages": bool(include_test_pages),
        },
        lambda: {
            "batch_id": batch_id,
            "schema_version": schema_version,
            "snapshot_sha256": snap_sha256,
        },
    )

    from django.contrib.contenttypes.models import ContentType
    from django.contrib.auth.models import Group
    from django.db.models import Sum
    from taggit.models import Tag
    from wagtail.contrib.redirects.models import Redirect
    from wagtail.models import Collection, Page, PageViewRestriction, Site

    from analytics.models import PageAnalyticsDaily, PageAnalyticsTotal
    from blog.blocks import BlogBodyBlocks, BlogIntroBlocks
    from blog.models import BlogIndexPage, BlogPage, BlogPagePerson
    from comments.models import Comment, CommentReaction, CommentVersion
    from comments.sanitizer import MAX_MEMBER_COMMENT_CHARS, sanitize_comment_html
    from discovery.archive_counts import reconcile_archive_counts
    from discovery.projection import project_page_to_discovery
    from external_content.store import MongoContentStore
    from home.models import HomePage
    from identity.models import MemberProfile
    from media_library.models import (
        Audio,
        CustomDocument,
        CustomImage,
        MediaReferenceRecord,
        Video,
        VisibilityChoices,
    )
    from people.models import Person
    from site_settings.blocks import ALLOWED_SOCIAL_PLATFORM_KEYS, sanitize_social_url
    from site_settings.models import SocialMediaSettings
    from taxonomy.models import TagDirectoryMetadata, TagIndexPage

    User = get_user_model()
    home = HomePage.objects.first()
    if home is None:
        root = Page.get_first_root_node() or Page.add_root(title="Root")
        home = HomePage(title="Home", slug="home")
        root.add_child(instance=home)
        home.save_revision().publish()

    site = Site.objects.filter(is_default_site=True).first()
    if site is None:
        site = Site.objects.create(hostname="localhost", port=80, root_page=home, is_default_site=True, site_name="我的博客")

    root_collection = Collection.get_first_root_node() or Collection.add_root(name="Root")
    anomalies: list[dict[str, Any]] = []
    stats = {"created": {}, "replayed": {}}

    def _bump(kind: str, replayed: bool):
        bucket_key = "replayed" if replayed else "created"
        stats[bucket_key][kind] = stats[bucket_key].get(kind, 0) + 1

    # 1. 管理员用户映射 (E-03=A)
    user_id_map: dict[int, int] = {}
    primary_admin = None
    for u_row in snap.get("users", []):
        old_uid = int(u_row["id"])
        def _create_user(r=u_row):
            existing = User.objects.filter(username=r["username"]).first()
            if existing is None:
                existing = User.objects.create_user(
                    username=r["username"],
                    email=r.get("email") or "",
                    password=None,
                )
                existing.set_unusable_password()
                existing.first_name = r.get("first_name") or ""
                existing.last_name = r.get("last_name") or ""
                existing.is_superuser = bool(r.get("is_superuser"))
                existing.is_staff = bool(r.get("is_staff"))
                existing.is_active = bool(r.get("is_active", True))
                existing.save()
            MemberProfile.get_or_create_for_user(existing)
            return {"new_user_id": existing.id, "username": existing.username}

        res, replayed = _run_idempotent(f"{batch_id}:user:{old_uid}", "etl_user", u_row, _create_user)
        _bump("user", replayed)
        user_id_map[old_uid] = int(res["new_user_id"])
        if primary_admin is None and u_row.get("is_superuser"):
            primary_admin = User.objects.get(pk=res["new_user_id"])

    if primary_admin is None:
        primary_admin = User.objects.filter(is_superuser=True, is_active=True).order_by("id").first()
        if primary_admin is None:
            primary_admin = User.objects.create_superuser(username="etl_admin", email="admin@example.com", password=None)
            primary_admin.set_unusable_password()
            primary_admin.save()

    if not TagIndexPage.objects.filter(is_recycled=False).exists():
        def _ensure_tag_index():
            ti = TagIndexPage(title="全站标签", slug="tags")
            home.add_child(instance=ti)
            ti.save_revision(user=primary_admin).publish(user=primary_admin)
            return {"tag_index_id": ti.id}

        _run_idempotent(f"{batch_id}:tag_index", "etl_tag_index", {"slug": "tags"}, _ensure_tag_index)

    # 2. 站点社交链接迁移 (E-04=A)
    if snap.get("social_links"):
        def _apply_social():
            items = []
            for sl in snap["social_links"]:
                plat = str(sl.get("platform") or "custom").strip().lower()
                if plat not in ALLOWED_SOCIAL_PLATFORM_KEYS:
                    plat = "custom"
                safe_url, _ = sanitize_social_url(sl.get("url") or "")
                if not safe_url:
                    continue
                label = str(sl.get("label") or "").strip() or plat.title()
                items.append(("social_link", {"platform": plat, "label": label[:64], "url": safe_url[:255]}))
            cfg, _ = SocialMediaSettings.objects.get_or_create(singleton_key=1)
            if items:
                cfg.social_links = items
                cfg.save()
            return {"count": len(items)}

        _, replayed = _run_idempotent(f"{batch_id}:social_links", "etl_social_links", snap["social_links"], _apply_social)
        _bump("social_links", replayed)

    # 3. 标签与分类合并 (C-02=A, C-03, E-06=A)
    tag_names_needed: dict[str, str] = {}
    for t_row in snap.get("tags", []):
        name = str(t_row["name"]).strip()
        if name:
            tag_names_needed.setdefault(name, str(t_row.get("slug") or "").strip())
    for bp in snap.get("blog_pages", []):
        for tname in bp.get("tag_names", []):
            if str(tname).strip():
                tag_names_needed.setdefault(str(tname).strip(), "")
        for cname in bp.get("category_names", []):
            if str(cname).strip():
                tag_names_needed.setdefault(str(cname).strip(), "")
    for img in snap.get("images", []):
        for tname in img.get("tags", []):
            if str(tname).strip():
                tag_names_needed.setdefault(str(tname).strip(), "")

    for tname, tslug in sorted(tag_names_needed.items()):
        def _ensure_tag(n=tname, s=tslug):
            tag = Tag.objects.filter(name=n).first()
            if tag is None:
                tag = Tag(name=n)
                if s:
                    tag.slug = s
                tag.save()
            TagDirectoryMetadata.get_or_create_for_tag(tag)
            return {"tag_id": tag.id, "slug": tag.slug}

        _, replayed = _run_idempotent(f"{batch_id}:tag:{sha256_json(tname)[:24]}", "etl_tag", {"name": tname}, _ensure_tag)
        _bump("tag", replayed)

    # 4. 媒体资产搬迁至 RustFS (C-04=B, C-05, C-11, E-05)
    def _upload_blob_to_storage(blob_meta: dict[str, Any], target_subdir: str) -> str:
        local_file = snapshot_dir / blob_meta["local_rel_path"]
        raw = local_file.read_bytes()
        if len(raw) != int(blob_meta["byte_size"]) or hashlib.sha256(raw).hexdigest() != blob_meta["sha256"]:
            raise ValueError(f"Source blob integrity failed: {blob_meta['local_rel_path']}")
        fname = blob_meta["filename"]
        saved_key = default_storage.save(f"{target_subdir}/{fname}", ContentFile(raw))
        with default_storage.open(saved_key, "rb") as rf:
            stored_bytes = rf.read()
        if len(stored_bytes) != len(raw) or hashlib.sha256(stored_bytes).hexdigest() != blob_meta["sha256"]:
            raise ValueError(f"Uploaded RustFS object verification failed: {saved_key}")
        return saved_key

    image_id_map: dict[int, int] = {}
    image_uuid_map: dict[int, str] = {}
    image_caption_map: dict[int, str] = {}
    imported_image_db_ids: set[int] = set()

    for img_row in snap.get("images", []):
        old_id = int(img_row["id"])
        image_caption_map[old_id] = str(img_row.get("caption") or "").strip()

        def _import_image(r=img_row):
            blob = r["blob"]
            saved_key = _upload_blob_to_storage(blob, "original_images")
            uploader_id = user_id_map.get(int(r["uploaded_by_user_id"])) if r.get("uploaded_by_user_id") else primary_admin.id
            raw_bytes = (snapshot_dir / blob["local_rel_path"]).read_bytes()
            img_obj = CustomImage(
                title=str(r["title"])[:255],
                collection=root_collection,
                uploaded_by_user_id=uploader_id,
                visibility=VisibilityChoices.RESTRICTED,
                storage_key=saved_key,
                description=str(r.get("description") or ""),
                caption=str(r.get("caption") or "")[:255],
                width=int(r.get("width") or 1),
                height=int(r.get("height") or 1),
                file_size=len(raw_bytes),
                file_hash=hashlib.sha1(raw_bytes).hexdigest(),
            )
            img_obj.file.name = saved_key
            img_obj.save()
            if r.get("tags"):
                img_obj.tags.add(*r["tags"])
            return {"new_image_id": img_obj.id, "public_id": str(img_obj.public_id), "storage_key": saved_key}

        res, replayed = _run_idempotent(f"{batch_id}:image:{old_id}", "etl_image", img_row, _import_image)
        _bump("image", replayed)
        image_id_map[old_id] = int(res["new_image_id"])
        image_uuid_map[old_id] = str(res["public_id"])
        imported_image_db_ids.add(int(res["new_image_id"]))

    doc_id_map: dict[int, int] = {}
    doc_desc_map: dict[int, str] = {}
    imported_doc_db_ids: set[int] = set()
    for doc_row in snap.get("documents", []):
        old_id = int(doc_row["id"])
        doc_desc_map[old_id] = str(doc_row.get("description") or "").strip()

        def _import_doc(r=doc_row):
            blob = r["blob"]
            saved_key = _upload_blob_to_storage(blob, "documents")
            uploader_id = user_id_map.get(int(r["uploaded_by_user_id"])) if r.get("uploaded_by_user_id") else primary_admin.id
            raw_bytes = (snapshot_dir / blob["local_rel_path"]).read_bytes()
            doc_obj = CustomDocument(
                title=str(r["title"])[:255],
                collection=root_collection,
                uploaded_by_user_id=uploader_id,
                visibility=VisibilityChoices.RESTRICTED,
                storage_key=saved_key,
                description=str(r.get("description") or ""),
                file_size=len(raw_bytes),
                file_hash=hashlib.sha1(raw_bytes).hexdigest(),
            )
            doc_obj.file.name = saved_key
            doc_obj.save()
            if r.get("tags"):
                doc_obj.tags.add(*r["tags"])
            return {"new_doc_id": doc_obj.id, "public_id": str(doc_obj.public_id), "storage_key": saved_key}

        res, replayed = _run_idempotent(f"{batch_id}:document:{old_id}", "etl_document", doc_row, _import_doc)
        _bump("document", replayed)
        doc_id_map[old_id] = int(res["new_doc_id"])
        imported_doc_db_ids.add(int(res["new_doc_id"]))

    audio_id_map: dict[int, int] = {}
    video_id_map: dict[int, int] = {}
    imported_audio_db_ids: set[int] = set()
    imported_video_db_ids: set[int] = set()
    for m_row in snap.get("media", []):
        old_id = int(m_row["id"])
        m_type = str(m_row["type"])
        thumb_img_id = None
        if m_row.get("thumbnail_blob"):
            def _import_thumb(r=m_row):
                tb = r["thumbnail_blob"]
                saved_key = _upload_blob_to_storage(tb, "original_images")
                from PIL import Image as PilImage
                with PilImage.open(snapshot_dir / tb["local_rel_path"]) as pim:
                    tw, th = pim.size
                raw_bytes = (snapshot_dir / tb["local_rel_path"]).read_bytes()
                img_obj = CustomImage(
                    title=f"{r['title']} 封面"[:255],
                    collection=root_collection,
                    uploaded_by_user_id=primary_admin.id,
                    visibility=VisibilityChoices.RESTRICTED,
                    storage_key=saved_key,
                    width=tw,
                    height=th,
                    file_size=len(raw_bytes),
                    file_hash=hashlib.sha1(raw_bytes).hexdigest(),
                )
                img_obj.file.name = saved_key
                img_obj.save()
                return {"new_image_id": img_obj.id, "public_id": str(img_obj.public_id)}

            tres, treplayed = _run_idempotent(
                f"{batch_id}:media_thumb:{m_type}:{old_id}",
                "etl_media_thumb",
                m_row["thumbnail_blob"],
                _import_thumb,
            )
            _bump("media_thumb", treplayed)
            thumb_img_id = int(tres["new_image_id"])
            imported_image_db_ids.add(thumb_img_id)

        if m_type == "audio":
            def _import_audio(r=m_row):
                saved_key = _upload_blob_to_storage(r["blob"], "audio")
                uploader_id = user_id_map.get(int(r["uploaded_by_user_id"])) if r.get("uploaded_by_user_id") else primary_admin.id
                dur = int(round(float(r.get("duration") or 0))) or None
                obj = Audio(
                    title=str(r["title"])[:255],
                    uploaded_by_user_id=uploader_id,
                    visibility=VisibilityChoices.RESTRICTED,
                    storage_key=saved_key,
                    duration=dur,
                )
                obj.file.name = saved_key
                obj.save()
                return {"new_audio_id": obj.id, "public_id": str(obj.public_id), "storage_key": saved_key}

            res, replayed = _run_idempotent(f"{batch_id}:audio:{old_id}", "etl_audio", m_row, _import_audio)
            _bump("audio", replayed)
            audio_id_map[old_id] = int(res["new_audio_id"])
            imported_audio_db_ids.add(int(res["new_audio_id"]))
        elif m_type == "video":
            def _import_video(r=m_row, poster_id=thumb_img_id):
                saved_key = _upload_blob_to_storage(r["blob"], "video")
                uploader_id = user_id_map.get(int(r["uploaded_by_user_id"])) if r.get("uploaded_by_user_id") else primary_admin.id
                dur = int(round(float(r.get("duration") or 0))) or None
                obj = Video(
                    title=str(r["title"])[:255],
                    poster_id=poster_id,
                    uploaded_by_user_id=uploader_id,
                    visibility=VisibilityChoices.RESTRICTED,
                    storage_key=saved_key,
                    duration=dur,
                )
                obj.file.name = saved_key
                obj.save()
                return {"new_video_id": obj.id, "public_id": str(obj.public_id), "storage_key": saved_key}

            res, replayed = _run_idempotent(f"{batch_id}:video:{old_id}", "etl_video", m_row, _import_video)
            _bump("video", replayed)
            video_id_map[old_id] = int(res["new_video_id"])
            imported_video_db_ids.add(int(res["new_video_id"]))

    # 5. 作者 Person 迁移 (C-01=A)
    author_id_map: dict[int, int] = {}
    author_slug_map: dict[int, str] = {}
    for au_row in snap.get("authors", []):
        old_au_id = int(au_row["id"])

        def _import_author(r=au_row):
            clean_bio = strip_html_to_plain_text(r.get("bio_raw"))
            avatar_id = image_id_map.get(int(r["author_image_id"])) if r.get("author_image_id") else None
            person = Person.objects.create(
                name=str(r["name"])[:120],
                bio=clean_bio,
                avatar_id=avatar_id,
            )
            if avatar_id:
                CustomImage.objects.filter(pk=avatar_id, visibility=VisibilityChoices.RESTRICTED).update(
                    visibility=VisibilityChoices.PUBLIC
                )
            return {"new_person_id": person.id, "slug": person.slug}

        res, replayed = _run_idempotent(f"{batch_id}:author:{old_au_id}", "etl_author", au_row, _import_author)
        _bump("author", replayed)
        author_id_map[old_au_id] = int(res["new_person_id"])
        author_slug_map[old_au_id] = str(res["slug"])

    # 6. Pass 1: 按树路径拓扑顺序建立 BlogIndexPage 与 BlogPage 骨架 (A-03, E-02=B)
    index_pages = snap.get("index_pages", [])
    blog_pages = snap.get("blog_pages", [])
    excluded_ids = compute_excluded_test_page_ids(index_pages, blog_pages, include_test_pages=include_test_pages)
    home_old_path = str(snap.get("site", {}).get("home_path") or "")
    if not home_old_path and index_pages:
        min_depth_path = min(str(p["path"]) for p in index_pages)
        home_old_path = min_depth_path[:-4]

    combined_nodes = [
        ("blog_index", row) for row in index_pages if int(row["id"]) not in excluded_ids
    ] + [
        ("blog_page", row) for row in blog_pages if int(row["id"]) not in excluded_ids
    ]
    combined_nodes.sort(key=lambda pair: (len(str(pair[1]["path"])), str(pair[1]["path"])))

    page_id_map: dict[int, int] = {}
    old_path_to_new_id: dict[str, int] = {home_old_path: home.id}

    for node_kind, row in combined_nodes:
        old_pid = int(row["id"])
        old_path = str(row["path"])
        parent_old_path = old_path[:-4]
        parent_new_id = old_path_to_new_id.get(parent_old_path, home.id)

        def _create_skeleton(kind=node_kind, r=row, p_id=parent_new_id):
            parent_page = Page.objects.get(pk=p_id).specific
            owner_id = user_id_map.get(int(r["owner_id"])) if r.get("owner_id") else primary_admin.id
            if kind == "blog_index":
                idx_authored_on = parse_date(str(r.get("date") or "")) or (
                    _parse_dt(r.get("first_published_at")).date() if r.get("first_published_at") else timezone.localdate()
                )
                obj = BlogIndexPage(
                    title=str(r["title"])[:255],
                    slug=str(r["slug"])[:255],
                    authored_on=idx_authored_on,
                    show_in_menus=bool(r.get("show_in_menus", True)),
                    seo_title=str(r.get("seo_title") or "")[:255],
                    search_description=str(r.get("search_description") or ""),
                    owner_id=owner_id,
                    live=False,
                )
            else:
                authored_on = parse_date(str(r.get("date") or "")) or (
                    _parse_dt(r.get("first_published_at")).date() if r.get("first_published_at") else timezone.localdate()
                )
                obj = BlogPage(
                    title=str(r["title"])[:255],
                    slug=str(r["slug"])[:255],
                    show_in_menus=bool(r.get("show_in_menus", False)),
                    seo_title=str(r.get("seo_title") or "")[:255],
                    search_description=str(r.get("search_description") or ""),
                    authored_on=authored_on,
                    owner_id=owner_id,
                    live=False,
                )
            parent_page.add_child(instance=obj)
            return {
                "new_page_id": obj.id,
                "document_uuid": str(obj.document_uuid),
                "url_path": obj.url_path,
            }

        res, replayed = _run_idempotent(
            f"{batch_id}:skel:{old_pid}",
            f"etl_skel_{node_kind}",
            {"id": old_pid, "slug": row["slug"], "path": old_path},
            _create_skeleton,
        )
        _bump(f"skel_{node_kind}", replayed)
        new_pid = int(res["new_page_id"])
        page_id_map[old_pid] = new_pid
        old_path_to_new_id[old_path] = new_pid

    # 6.5 Pass 1.5: 在任何发布动作前仅按显式核准映射恢复 PageViewRestriction (P0-2 / Q3)
    confirmed_group_names: set[str] = set()
    all_included_rows = [
        row for row in (index_pages + blog_pages) if int(row["id"]) not in excluded_ids
    ]
    source_path_by_id: dict[int, str] = {
        int(row["id"]): str(row.get("path") or "") for row in all_included_rows
    }
    source_url_path_by_id: dict[int, str] = {
        int(row["id"]): str(row.get("url_path") or "") for row in all_included_rows
    }
    home_old_id = snap.get("site", {}).get("home_id")
    if home_old_id is not None and home_old_path:
        source_path_by_id.setdefault(int(home_old_id), home_old_path)

    unconfirmed_restriction_page_ids: set[int] = set()

    def _mark_unconfirmed_subtree(
        root_old_pid: int,
        pvr_path: str,
        pvr_url_path: str,
        reason: str,
        *,
        restriction_id: int | None = None,
    ) -> None:
        unconfirmed_restriction_page_ids.add(root_old_pid)
        matched_any = False
        if pvr_path:
            for row in all_included_rows:
                rpath = str(row.get("path") or "")
                if rpath.startswith(pvr_path):
                    unconfirmed_restriction_page_ids.add(int(row["id"]))
                    matched_any = True
        elif pvr_url_path:
            for row in all_included_rows:
                rupath = str(row.get("url_path") or "")
                if rupath.startswith(pvr_url_path):
                    unconfirmed_restriction_page_ids.add(int(row["id"]))
                    matched_any = True
        if not matched_any and root_old_pid not in page_id_map:
            # 无法定位边界的未映射祖先限制：Fail-Closed 将全批保留为草稿
            for row in all_included_rows:
                unconfirmed_restriction_page_ids.add(int(row["id"]))
        anomalies.append({
            "kind": reason,
            "old_page_id": root_old_pid,
            "restriction_id": restriction_id,
            "page_path": pvr_path,
        })

    passwords_by_key: dict[int, str] = {}
    for k, v in (restriction_passwords or {}).items():
        if v is not None and str(v) != "":
            passwords_by_key[int(k)] = str(v)

    for pvr_row in snap.get("page_view_restrictions", []) or []:
        old_rid = int(pvr_row["id"])
        old_pid = int(pvr_row["page_id"])
        if old_pid in excluded_ids:
            continue
        rtype = str(pvr_row.get("restriction_type") or "").strip().lower()
        if rtype in ("", "none"):
            continue

        pvr_path = str(pvr_row.get("page_path") or source_path_by_id.get(old_pid) or "")
        pvr_url_path = str(pvr_row.get("page_url_path") or source_url_path_by_id.get(old_pid) or "")

        if old_pid not in page_id_map:
            _mark_unconfirmed_subtree(
                old_pid,
                pvr_path,
                pvr_url_path,
                "restriction_unmapped_ancestor_page",
                restriction_id=old_rid,
            )
            continue

        new_pid = page_id_map[old_pid]
        supplied_pwd = passwords_by_key.get(old_pid) or passwords_by_key.get(old_rid) or ""

        mapped_group_names: list[str] = []
        missing_group_names: list[str] = []
        if rtype == PageViewRestriction.GROUPS:
            raw_gnames = [str(g).strip() for g in (pvr_row.get("group_names") or []) if str(g).strip()]
            if not raw_gnames:
                missing_group_names.append("<empty>")
            else:
                for gname in raw_gnames:
                    # 安全硬规则：禁止同名自动放行，必须通过 group_name_map 显式核准且目标组已存在于目标库
                    if group_name_map is None or gname not in group_name_map:
                        missing_group_names.append(gname)
                        continue
                    mapped = group_name_map[gname]
                    target_gname = str(mapped).strip() if mapped is not None else ""
                    if not target_gname:
                        missing_group_names.append(gname)
                        continue
                    if Group.objects.filter(name=target_gname).exists():
                        mapped_group_names.append(target_gname)
                    else:
                        missing_group_names.append(target_gname)

        pvr_input = {
            "pvr": pvr_row,
            "has_supplied_password": bool(supplied_pwd),
            "supplied_password_sha256": hashlib.sha256(supplied_pwd.encode("utf-8")).hexdigest() if supplied_pwd else None,
            "mapped_group_names": sorted(set(mapped_group_names)) if rtype == PageViewRestriction.GROUPS else None,
            "missing_group_names": sorted(set(missing_group_names)) if rtype == PageViewRestriction.GROUPS else None,
        }

        def _apply_pvr(
            npid=new_pid,
            rt=rtype,
            gnames=sorted(set(mapped_group_names)),
            missing_gs=sorted(set(missing_group_names)),
            pwd=supplied_pwd,
        ):
            if rt == PageViewRestriction.LOGIN:
                pvr_obj, _ = PageViewRestriction.objects.get_or_create(
                    page_id=npid,
                    restriction_type=PageViewRestriction.LOGIN,
                )
                return {"confirmed": True, "restriction_id": pvr_obj.id, "restriction_type": rt, "new_page_id": npid}

            if rt == PageViewRestriction.GROUPS:
                if missing_gs or not gnames:
                    return {
                        "confirmed": False,
                        "reason": "restriction_missing_groups",
                        "restriction_type": rt,
                        "new_page_id": npid,
                    }
                grp_objs = list(Group.objects.filter(name__in=gnames))
                if len(grp_objs) != len(set(gnames)):
                    return {
                        "confirmed": False,
                        "reason": "restriction_missing_groups",
                        "restriction_type": rt,
                        "new_page_id": npid,
                    }
                confirmed_group_names.update(gnames)
                pvr_obj = PageViewRestriction.objects.create(
                    page_id=npid,
                    restriction_type=PageViewRestriction.GROUPS,
                )
                pvr_obj.groups.set(grp_objs)
                return {"confirmed": True, "restriction_id": pvr_obj.id, "restriction_type": rt, "new_page_id": npid}

            if rt == PageViewRestriction.PASSWORD:
                if not pwd:
                    return {
                        "confirmed": False,
                        "reason": "restriction_missing_password",
                        "restriction_type": rt,
                        "new_page_id": npid,
                    }
                pvr_obj = PageViewRestriction.objects.create(
                    page_id=npid,
                    restriction_type=PageViewRestriction.PASSWORD,
                    password=pwd,
                )
                return {"confirmed": True, "restriction_id": pvr_obj.id, "restriction_type": rt, "new_page_id": npid}

            return {
                "confirmed": False,
                "reason": "restriction_unknown_type",
                "restriction_type": rt,
                "new_page_id": npid,
            }

        pvr_res, pvr_replayed = _run_idempotent(
            f"{batch_id}:pvr:{old_rid}",
            "etl_view_restriction",
            pvr_input,
            _apply_pvr,
            page_id=new_pid,
        )
        _bump("view_restriction", pvr_replayed)
        if not pvr_res.get("confirmed"):
            _mark_unconfirmed_subtree(
                old_pid,
                pvr_path,
                pvr_url_path,
                str(pvr_res.get("reason") or "restriction_unconfirmed"),
                restriction_id=old_rid,
            )

    source_restricted_page_ids = _collect_source_restricted_page_ids(snap, all_included_rows)
    for old_pid in sorted(source_restricted_page_ids):
        if old_pid in unconfirmed_restriction_page_ids:
            continue
        new_pid = page_id_map.get(old_pid)
        target_page = Page.objects.filter(pk=new_pid).first() if new_pid else None
        if target_page is None or not target_page.get_view_restrictions().exists():
            _mark_unconfirmed_subtree(
                old_pid,
                source_path_by_id.get(old_pid, ""),
                source_url_path_by_id.get(old_pid, ""),
                "restriction_not_confirmed_in_target",
            )

    def _promote_public_page_assets(page_obj, rev_id: int) -> None:
        """仅对无访问限制且即将正式发布的公开页面，在同事务中将本批引用到的资产转为 PUBLIC。"""
        refs = list(
            MediaReferenceRecord.objects.filter(page_id=page_obj.id, revision_id=rev_id)
            .values_list("asset_type", "asset_id")
        )
        ref_uuids_by_type: dict[str, list[UUID]] = {}
        for atype, aid in refs:
            if aid:
                ref_uuids_by_type.setdefault(atype, []).append(aid)

        img_uuids = ref_uuids_by_type.get("image", [])
        if img_uuids and imported_image_db_ids:
            CustomImage.objects.filter(
                public_id__in=img_uuids,
                pk__in=imported_image_db_ids,
                visibility=VisibilityChoices.RESTRICTED,
            ).update(visibility=VisibilityChoices.PUBLIC)

        doc_uuids = ref_uuids_by_type.get("document", [])
        if doc_uuids and imported_doc_db_ids:
            CustomDocument.objects.filter(
                public_id__in=doc_uuids,
                pk__in=imported_doc_db_ids,
                visibility=VisibilityChoices.RESTRICTED,
            ).update(visibility=VisibilityChoices.PUBLIC)

        audio_uuids = ref_uuids_by_type.get("audio", [])
        if audio_uuids and imported_audio_db_ids:
            Audio.objects.filter(
                public_id__in=audio_uuids,
                pk__in=imported_audio_db_ids,
                visibility=VisibilityChoices.RESTRICTED,
            ).update(visibility=VisibilityChoices.PUBLIC)

        video_uuids = ref_uuids_by_type.get("video", [])
        if video_uuids and imported_video_db_ids:
            pub_videos = Video.objects.filter(
                public_id__in=video_uuids,
                pk__in=imported_video_db_ids,
            )
            pub_videos.filter(visibility=VisibilityChoices.RESTRICTED).update(visibility=VisibilityChoices.PUBLIC)
            poster_ids = [
                pid
                for pid in pub_videos.filter(poster_id__isnull=False).values_list("poster_id", flat=True)
                if pid in imported_image_db_ids
            ]
            if poster_ids:
                CustomImage.objects.filter(
                    pk__in=poster_ids,
                    visibility=VisibilityChoices.RESTRICTED,
                ).update(visibility=VisibilityChoices.PUBLIC)

    # 7. Pass 2: 水合内容、MongoDB 快照、保存 Revision、按源状态与权限确认结果受控发布
    # 传播源站草稿/未发布祖先边界（含被保留为草稿的节点）：其所有子孙同样保持草稿且不转正媒体
    draft_ancestor_paths: list[str] = []
    for row in all_included_rows:
        r_pid = int(row["id"])
        r_path = str(row.get("path") or "")
        if not r_path:
            continue
        if (not bool(row.get("live"))) or (r_pid in unconfirmed_restriction_page_ids) or bool(row.get("published_body_unresolved")):
            draft_ancestor_paths.append(r_path)
    unlive_ancestor_page_ids: set[int] = set()
    for row in all_included_rows:
        r_pid = int(row["id"])
        r_path = str(row.get("path") or "")
        if r_path and any(r_path.startswith(dp) and r_path != dp for dp in draft_ancestor_paths):
            unlive_ancestor_page_ids.add(r_pid)
            if bool(row.get("live")):
                anomalies.append({
                    "kind": "draft_ancestor_held_as_draft",
                    "old_page_id": r_pid,
                    "new_page_id": page_id_map.get(r_pid),
                    "page_path": r_path,
                })

    store = MongoContentStore()
    try:
        for idx_row in [r for r in index_pages if int(r["id"]) not in excluded_ids]:
            old_pid = int(idx_row["id"])
            new_pid = page_id_map[old_pid]
            is_source_live = bool(idx_row.get("live"))
            is_unconfirmed = (old_pid in unconfirmed_restriction_page_ids) or (old_pid in unlive_ancestor_page_ids)
            is_restricted_source = old_pid in source_restricted_page_ids

            def _publish_index(
                r=idx_row,
                npid=new_pid,
                opid=old_pid,
                src_live=is_source_live,
                unconfirmed=is_unconfirmed,
                restricted_src=is_restricted_source,
            ):
                idx_obj = BlogIndexPage.objects.get(pk=npid)
                owner_id = user_id_map.get(int(r["owner_id"])) if r.get("owner_id") else primary_admin.id
                owner_user = User.objects.get(pk=owner_id)
                publish_actor = owner_user if idx_obj.permissions_for_user(owner_user).can_publish() else primary_admin
                idx_obj.owner = owner_user
                idx_obj.featured_image_id = (
                    image_id_map.get(int(r["featured_image_id"]))
                    if r.get("featured_image_id")
                    else None
                )
                idx_obj.intro = rewrite_richtext_db_html(
                    plain_text_to_richtext_html(r.get("intro") or ""),
                    image_id_map=image_id_map,
                    page_id_map=page_id_map,
                    doc_id_map=doc_id_map,
                    excluded_page_ids=excluded_ids,
                    anomalies=anomalies,
                    source_page_id=opid,
                )
                idx_actor = owner_user if owner_user.is_superuser else primary_admin
                rev = idx_obj.save_revision(user=idx_actor)

                should_publish = src_live and (not unconfirmed)
                if should_publish:
                    has_target_restriction = idx_obj.get_view_restrictions().exists()
                    if restricted_src and not has_target_restriction:
                        anomalies.append({
                            "kind": "restriction_missing_before_publish",
                            "old_page_id": opid,
                            "new_page_id": idx_obj.id,
                        })
                        return {
                            "new_page_id": idx_obj.id,
                            "latest_revision_id": rev.id,
                            "live": False,
                            "is_restricted": True,
                            "held_as_draft": True,
                        }
                    if not has_target_restriction:
                        _promote_public_page_assets(idx_obj, rev.id)
                    rev.publish(user=idx_actor)
                    fp = _parse_dt(r.get("first_published_at"))
                    lp = _parse_dt(r.get("last_published_at"))
                    if fp or lp:
                        upd = {}
                        if fp:
                            upd["first_published_at"] = fp
                        if lp:
                            upd["last_published_at"] = lp
                        BlogIndexPage.objects.filter(pk=idx_obj.pk).update(**upd)
                    return {
                        "new_page_id": idx_obj.id,
                        "live_revision_id": rev.id,
                        "live": True,
                        "is_restricted": has_target_restriction,
                        "held_as_draft": False,
                    }
                return {
                    "new_page_id": idx_obj.id,
                    "latest_revision_id": rev.id,
                    "live": False,
                    "is_restricted": restricted_src,
                    "held_as_draft": bool(src_live),
                }

            _, replayed = _run_idempotent(
                f"{batch_id}:pub_index:{old_pid}",
                "etl_publish_blog_index",
                {"row": idx_row, "unconfirmed_restriction": is_unconfirmed, "restricted": is_restricted_source},
                _publish_index,
                page_id=new_pid,
            )
            _bump("pub_index", replayed)

        for bp_row in [r for r in blog_pages if int(r["id"]) not in excluded_ids]:
            old_pid = int(bp_row["id"])
            new_pid = page_id_map[old_pid]
            is_source_live = bool(bp_row.get("live"))
            published_body_unresolved = bool(bp_row.get("published_body_unresolved"))
            is_unconfirmed = (old_pid in unconfirmed_restriction_page_ids) or (old_pid in unlive_ancestor_page_ids)
            is_restricted_source = old_pid in source_restricted_page_ids
            if is_source_live and published_body_unresolved:
                anomalies.append({
                    "kind": "missing_published_body_held_as_draft",
                    "old_page_id": old_pid,
                    "new_page_id": new_pid,
                })

            def _publish_blog(
                r=bp_row,
                npid=new_pid,
                opid=old_pid,
                src_live=is_source_live,
                body_unresolved=published_body_unresolved,
                unconfirmed=is_unconfirmed,
                restricted_src=is_restricted_source,
            ):
                bp_obj = BlogPage.objects.get(pk=npid)
                owner_id = user_id_map.get(int(r["owner_id"])) if r.get("owner_id") else primary_admin.id
                owner_user = User.objects.get(pk=owner_id)
                publish_actor = owner_user if owner_user.is_superuser else primary_admin
                bp_obj.owner = owner_user
                bp_obj.featured_image_id = (
                    image_id_map.get(int(r["featured_image_id"]))
                    if r.get("featured_image_id")
                    else None
                )

                intro_html = rewrite_richtext_db_html(
                    plain_text_to_richtext_html(r.get("intro") or ""),
                    image_id_map=image_id_map,
                    page_id_map=page_id_map,
                    doc_id_map=doc_id_map,
                    excluded_page_ids=excluded_ids,
                    anomalies=anomalies,
                    source_page_id=opid,
                )
                intro_payload = [{"type": "rich_text", "value": intro_html}] if intro_html else []
                body_payload = transform_stream_blocks(
                    r.get("body_blocks") or [],
                    image_id_map=image_id_map,
                    image_uuid_map=image_uuid_map,
                    image_caption_map=image_caption_map,
                    doc_id_map=doc_id_map,
                    doc_desc_map=doc_desc_map,
                    audio_id_map=audio_id_map,
                    video_id_map=video_id_map,
                    page_id_map=page_id_map,
                    excluded_page_ids=excluded_ids,
                    anomalies=anomalies,
                    source_page_id=opid,
                )

                # 先以确定性 operation_id 写入 MongoDB（跨重跑幂等，零重复版本）
                ptrs = []
                mongo_op = f"{batch_id}:mongo:{opid}:v1"
                if intro_payload:
                    ptrs.append(
                        store.save_field_version(
                            document_uuid=bp_obj.document_uuid,
                            field_name="intro",
                            payload=intro_payload,
                            model_label="blog.BlogPage",
                            operation_id=f"{mongo_op}_intro",
                        )
                    )
                if body_payload:
                    ptrs.append(
                        store.save_field_version(
                            document_uuid=bp_obj.document_uuid,
                            field_name="body",
                            payload=body_payload,
                            model_label="blog.BlogPage",
                            operation_id=f"{mongo_op}_body",
                        )
                    )

                bp_obj.intro = BlogIntroBlocks().to_python(intro_payload)
                bp_obj.body = BlogBodyBlocks().to_python(body_payload)
                merged_tags = list(dict.fromkeys(list(r.get("tag_names", [])) + list(r.get("category_names", []))))
                if merged_tags:
                    bp_obj.tags.set(merged_tags)
                else:
                    bp_obj.tags.clear()

                bp_obj.authors.all().delete()
                for sort_idx, old_au_id in enumerate(r.get("author_ids") or []):
                    new_person_id = author_id_map.get(int(old_au_id))
                    if new_person_id:
                        BlogPagePerson.objects.create(
                            page=bp_obj,
                            person_id=new_person_id,
                            role="primary_author" if sort_idx == 0 else "co_author",
                            sort_order=sort_idx,
                        )

                bp_obj._prepared_external_pointers = ptrs
                try:
                    rev = bp_obj.save_revision(user=publish_actor)
                finally:
                    bp_obj._prepared_external_pointers = None

                should_publish = src_live and (not unconfirmed) and (not body_unresolved)
                if should_publish:
                    has_target_restriction = bp_obj.get_view_restrictions().exists()
                    if restricted_src and not has_target_restriction:
                        anomalies.append({
                            "kind": "restriction_missing_before_publish",
                            "old_page_id": opid,
                            "new_page_id": bp_obj.id,
                        })
                        return {
                            "new_page_id": bp_obj.id,
                            "latest_revision_id": rev.id,
                            "live": False,
                            "is_restricted": True,
                            "held_as_draft": True,
                        }
                    if not has_target_restriction:
                        # 仅无限制公开页可将本发布版本实际引用的本批资产转为 PUBLIC
                        _promote_public_page_assets(bp_obj, rev.id)
                    rev.publish(user=publish_actor)
                    fp = _parse_dt(r.get("first_published_at"))
                    lp = _parse_dt(r.get("last_published_at"))
                    if fp or lp:
                        upd = {}
                        if fp:
                            upd["first_published_at"] = fp
                        if lp:
                            upd["last_published_at"] = lp
                        BlogPage.objects.filter(pk=bp_obj.pk).update(**upd)
                    return {
                        "new_page_id": bp_obj.id,
                        "live_revision_id": rev.id,
                        "live": True,
                        "is_restricted": has_target_restriction,
                        "held_as_draft": False,
                    }

                # 草稿页或访问限制未确认页：绝不调用 rev.publish()，保持 live=False，媒体保持 RESTRICTED
                return {
                    "new_page_id": bp_obj.id,
                    "latest_revision_id": rev.id,
                    "live": False,
                    "is_restricted": restricted_src,
                    "held_as_draft": bool(src_live),
                }

            _, replayed = _run_idempotent(
                f"{batch_id}:pub_blog:{old_pid}",
                "etl_publish_blog_page",
                {
                    "row": bp_row,
                    "unconfirmed_restriction": is_unconfirmed,
                    "restricted": is_restricted_source,
                    "published_body_unresolved": published_body_unresolved,
                },
                _publish_blog,
                page_id=new_pid,
            )
            _bump("pub_blog", replayed)
    finally:
        store.close()

    # 8. 评论与赞踩迁移 (C-07=A)
    folded_comments, c_anoms = transform_comments_with_folding(
        snap.get("comments", []),
        excluded_page_ids=excluded_ids,
    )
    anomalies.extend(c_anoms)
    comment_id_map: dict[int, int] = {}
    for cm_row in folded_comments:
        old_cid = int(cm_row["id"])

        def _import_comment(r=cm_row):
            new_pid = page_id_map[int(r["page_id"])]
            target_page = BlogPage.objects.get(pk=new_pid)
            new_uid = user_id_map.get(int(r["author_user_id"])) if r.get("author_user_id") else primary_admin.id
            parent_new_id = comment_id_map.get(int(r["folded_parent_old_id"])) if r.get("folded_parent_old_id") else None
            reply_to_new_id = comment_id_map.get(int(r["folded_reply_to_old_id"])) if r.get("folded_reply_to_old_id") else None
            raw_text = str(r.get("content") or "").strip() or "（空评论）"
            try:
                safe_html = sanitize_comment_html(raw_text, max_chars=MAX_MEMBER_COMMENT_CHARS, allow_rich_embeds=True)
            except Exception:
                import html as _html
                safe_html = f"<p>{_html.escape(raw_text)}</p>"
            c_obj = Comment.objects.create(
                page=target_page,
                page_document_uuid=target_page.document_uuid,
                page_title_snapshot=target_page.title[:255],
                author_id=new_uid,
                author_subject_key=f"user:{new_uid}" if new_uid else "",
                guest_name=str(r.get("guest_name") or "")[:50],
                parent_id=parent_new_id,
                reply_to_comment_id=reply_to_new_id,
                status="active",
                is_publicly_visible=True,
            )
            ver = CommentVersion.objects.create(
                comment=c_obj,
                version_number=1,
                raw_content=raw_text,
                safe_html=safe_html,
                content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                status="approved",
                reviewed_by=primary_admin,
                reviewed_at=_parse_dt(r.get("created_at")) or timezone.now(),
            )
            c_obj.latest_version = ver
            c_obj.approved_version = ver
            c_obj.save(update_fields=["latest_version", "approved_version"])
            if r.get("created_at"):
                dt_val = _parse_dt(r["created_at"])
                if dt_val is not None:
                    Comment.objects.filter(pk=c_obj.pk).update(created_at=dt_val)
            return {"new_comment_id": c_obj.id, "page_id": new_pid, "parent_id": parent_new_id}

        res, replayed = _run_idempotent(
            f"{batch_id}:comment:{old_cid}",
            "etl_comment",
            cm_row,
            _import_comment,
            page_id=page_id_map.get(int(cm_row["page_id"])),
        )
        _bump("comment", replayed)
        comment_id_map[old_cid] = int(res["new_comment_id"])

    for cr_row in snap.get("comment_reactions", []):
        old_cr_id = int(cr_row["id"])
        if int(cr_row["comment_id"]) not in comment_id_map:
            continue

        def _import_reaction(r=cr_row):
            new_cid = comment_id_map[int(r["comment_id"])]
            new_uid = user_id_map.get(int(r["user_id"])) if r.get("user_id") else primary_admin.id
            rtype = 1 if int(r.get("reaction") or 1) >= 0 else -1
            CommentReaction.objects.update_or_create(
                comment_id=new_cid,
                user_id=new_uid,
                defaults={"reaction_type": rtype},
            )
            return {"comment_id": new_cid, "reaction_type": rtype}

        _, replayed = _run_idempotent(f"{batch_id}:creact:{old_cr_id}", "etl_comment_reaction", cr_row, _import_reaction)
        _bump("comment_reaction", replayed)

    # 同步刷新所有导入评论的 reply_count / like_count / dislike_count（幂等覆盖准确值）
    for new_cid in comment_id_map.values():
        replies_n = Comment.objects.filter(parent_id=new_cid, is_publicly_visible=True).count()
        likes_n = CommentReaction.objects.filter(comment_id=new_cid, reaction_type=1).count()
        dislikes_n = CommentReaction.objects.filter(comment_id=new_cid, reaction_type=-1).count()
        Comment.objects.filter(pk=new_cid).update(
            reply_count=replies_n,
            like_count=likes_n,
            dislike_count=dislikes_n,
        )

    # 9. 历史浏览量迁移 (C-09=A)
    pv_by_page = merge_page_view_counts(snap.get("page_views", []), excluded_page_ids=excluded_ids)
    for old_pid, daily_rows in pv_by_page.items():
        if old_pid not in page_id_map:
            continue
        new_pid = page_id_map[old_pid]

        def _import_pv(rows=daily_rows, npid=new_pid):
            bp_obj = BlogPage.objects.get(pk=npid)
            rev_id = int(bp_obj.live_revision_id or 0)
            tot_pv = 0
            tot_uv = 0
            tot_valid = 0
            tot_completed = 0
            last_dt = None
            for dr in rows:
                dt_obj = date.fromisoformat(dr["stat_date"])
                PageAnalyticsDaily.objects.update_or_create(
                    site=site,
                    page=bp_obj,
                    stat_date=dt_obj,
                    live_revision_id=rev_id,
                    defaults={
                        "type_key": "blog_page",
                        "pv": dr["pv"],
                        "daily_uv": dr["uv"],
                        "valid_views": dr["valid_views"],
                        "completed_views": dr["completed_views"],
                        "total_active_seconds": dr["total_active_seconds"],
                        "health_status": "normal",
                    },
                )
                tot_pv += dr["pv"]
                tot_uv += dr["uv"]
                tot_valid += dr["valid_views"]
                tot_completed += dr["completed_views"]
                if last_dt is None or dt_obj > last_dt:
                    last_dt = dt_obj
            PageAnalyticsTotal.objects.update_or_create(
                site=site,
                page=bp_obj,
                defaults={
                    "total_pv": tot_pv,
                    "total_visitor_days": tot_uv,
                    "total_valid_views": tot_valid,
                    "total_completed_views": tot_completed,
                    "last_settled_date": last_dt,
                },
            )
            return {"page_id": npid, "days": len(rows), "total_pv": tot_pv}

        _, replayed = _run_idempotent(
            f"{batch_id}:pv:{old_pid}",
            "etl_page_pv",
            daily_rows,
            _import_pv,
            page_id=new_pid,
        )
        _bump("pv_page", replayed)

    # 10. 重定向规则迁移 (D-01=A, D-02)
    for rd_row in snap.get("redirects", []):
        old_rd_id = int(rd_row["id"])
        target_old_pid = rd_row.get("redirect_page_id")
        if target_old_pid is not None and int(target_old_pid) in excluded_ids:
            continue
        if target_old_pid is not None and int(target_old_pid) not in page_id_map:
            continue
        norm_old_path = normalize_legacy_redirect_old_path(rd_row["old_path"])
        if not norm_old_path or norm_old_path == "/":
            continue

        def _import_redirect(r=rd_row, np_path=norm_old_path):
            target_new_pid = page_id_map.get(int(r["redirect_page_id"])) if r.get("redirect_page_id") else None
            if Redirect.objects.filter(old_path=np_path, site=site).exists():
                return {"skipped_collision": True, "old_path": np_path}
            Redirect.objects.create(
                old_path=np_path[:255],
                site=site,
                redirect_page_id=target_new_pid,
                redirect_link=str(r.get("redirect_link") or "") if not target_new_pid else "",
                is_permanent=bool(r.get("is_permanent", True)),
            )
            return {"old_path": np_path, "redirect_page_id": target_new_pid}

        _, replayed = _run_idempotent(f"{batch_id}:redir:{old_rd_id}", "etl_redirect", rd_row, _import_redirect)
        _bump("redirect", replayed)

    for old_au_id, au_slug in author_slug_map.items():
        def _import_author_redirect(aid=old_au_id, slug=au_slug):
            opath = f"/blog/authors/{aid}"
            if not Redirect.objects.filter(old_path=opath, site=site).exists():
                Redirect.objects.create(
                    old_path=opath,
                    site=site,
                    redirect_link=f"/authors/{slug}/",
                    is_permanent=True,
                )
            return {"old_path": opath, "redirect_link": f"/authors/{slug}/"}

        _, replayed = _run_idempotent(
            f"{batch_id}:author_redir:{old_au_id}",
            "etl_author_redirect",
            {"old_id": old_au_id, "slug": au_slug},
            _import_author_redirect,
        )
        _bump("author_redirect", replayed)

    # 11. 索引与归档预聚合重建
    if not skip_search_rebuild:
        for new_pid in page_id_map.values():
            p = Page.objects.filter(pk=new_pid).first()
            if p is not None and p.live:
                project_page_to_discovery(p.specific)
        reconcile_archive_counts(apply=True)
        call_command("wagtail_update_index", verbosity=0)

    return {
        "mode": "apply",
        "schema_version": schema_version,
        "batch_id": batch_id,
        "isolation": isolation,
        "stats": stats,
        "excluded_test_page_ids": sorted(excluded_ids),
        "unconfirmed_restriction_page_ids": sorted(unconfirmed_restriction_page_ids),
        "held_draft_ancestor_page_ids": sorted(unlive_ancestor_page_ids),
        "mapped_counts": {
            "users": len(user_id_map),
            "groups": len(confirmed_group_names),
            "authors": len(author_id_map),
            "images": len(image_id_map),
            "documents": len(doc_id_map),
            "audio": len(audio_id_map),
            "video": len(video_id_map),
            "pages": len(page_id_map),
            "comments": len(comment_id_map),
        },
        "anomalies": anomalies,
    }


def reconcile_target(snapshot_dir: Path, *, batch_id: str = "wb3_v2", include_test_pages: bool = False) -> dict[str, Any]:
    """
    全量核验目标数据库、MongoDB 与 RustFS 实体完整性，输出差异对账报告。
    """
    from django.db.models import Sum
    from taggit.models import Tag
    from wagtail.contrib.redirects.models import Redirect

    from analytics.models import PageAnalyticsDaily, PageAnalyticsTotal
    from blog.models import BlogIndexPage, BlogPage
    from comments.models import Comment, CommentReaction
    from discovery.models import PublishedSearchDocument
    from external_content.models import OperationResult
    from media_library.models import (
        Audio,
        CustomDocument,
        CustomImage,
        MediaReferenceRecord,
        Video,
        VisibilityChoices,
    )
    from people.models import Person

    snap = _load_snapshot(Path(snapshot_dir))
    excluded_ids = compute_excluded_test_page_ids(
        snap.get("index_pages", []),
        snap.get("blog_pages", []),
        include_test_pages=include_test_pages,
    )
    included_indexes = [p for p in snap.get("index_pages", []) if int(p["id"]) not in excluded_ids]
    included_blogs = [p for p in snap.get("blog_pages", []) if int(p["id"]) not in excluded_ids]
    source_restricted_ids = _collect_source_restricted_page_ids(snap, included_indexes + included_blogs)

    expected_indexes = len(included_indexes)
    expected_source_live_blogs = sum(1 for p in included_blogs if bool(p.get("live")))
    expected_source_draft_blogs = sum(1 for p in included_blogs if not bool(p.get("live")))
    expected_restricted_blogs = sum(1 for p in included_blogs if int(p["id"]) in source_restricted_ids)

    folded_comments, _ = transform_comments_with_folding(snap.get("comments", []), excluded_page_ids=excluded_ids)
    pv_by_page = merge_page_view_counts(snap.get("page_views", []), excluded_page_ids=excluded_ids)
    expected_pv_sum = sum(r["pv"] for rows in pv_by_page.values() for r in rows)

    # 核验 RustFS/storage 物理文件存在性
    missing_storage_keys: list[str] = []
    for cls, label in ((CustomImage, "image"), (CustomDocument, "document"), (Audio, "audio"), (Video, "video")):
        for obj in cls.objects.all():
            key = getattr(obj, "storage_key", "") or getattr(obj.file, "name", "")
            if not key or not default_storage.exists(key):
                missing_storage_keys.append(f"{label}:{obj.id}:{key}")

    # 核验本批次迁移页面在目标库的实际发布/草稿/受限状态
    migrated_index_ids = [
        int(r["page_id"])
        for r in OperationResult.objects.filter(
            operation_id__startswith=f"{batch_id}:pub_index:",
            page_id__isnull=False,
        ).values("page_id")
    ]
    migrated_blog_rows = list(
        OperationResult.objects.filter(
            operation_id__startswith=f"{batch_id}:pub_blog:",
            page_id__isnull=False,
        ).values("page_id", "result_json")
    )
    migrated_blog_ids = [int(r["page_id"]) for r in migrated_blog_rows]
    held_draft_blog_ids = {
        int(r["page_id"])
        for r in migrated_blog_rows
        if isinstance(r.get("result_json"), dict) and r["result_json"].get("held_as_draft")
    }
    expected_live_blogs = max(0, expected_source_live_blogs - len(held_draft_blog_ids))
    expected_draft_blogs = expected_source_draft_blogs + len(held_draft_blog_ids)

    migrated_index_objs = list(BlogIndexPage.objects.filter(pk__in=migrated_index_ids))
    migrated_blog_objs = list(BlogPage.objects.filter(pk__in=migrated_blog_ids))

    actual_live_blogs = sum(1 for bp in migrated_blog_objs if bp.live)
    actual_draft_blogs = sum(1 for bp in migrated_blog_objs if not bp.live)
    actual_restricted_blogs = sum(1 for bp in migrated_blog_objs if bp.get_view_restrictions().exists())

    live_migrated_blog_ids = [bp.id for bp in migrated_blog_objs if bp.live]
    projected_count = PublishedSearchDocument.objects.filter(
        page_id__in=live_migrated_blog_ids,
        is_live=True,
        is_recycled=False,
    ).count()

    # 按公开（live=True、无草稿祖先且无自身/祖先 PageViewRestriction）vs 草稿/受限划分本批迁移页面
    public_live_pages = []
    restricted_or_draft_pages = []
    for p_obj in [*migrated_index_objs, *migrated_blog_objs]:
        has_unlive_ancestor = p_obj.get_ancestors().filter(depth__gte=2, live=False).exists()
        if p_obj.live and (not has_unlive_ancestor) and (not p_obj.get_view_restrictions().exists()):
            public_live_pages.append(p_obj)
        else:
            restricted_or_draft_pages.append(p_obj)

    def _collect_page_asset_pairs(page_objs) -> set[tuple[str, UUID]]:
        pairs: set[tuple[str, UUID]] = set()
        for po in page_objs:
            rev_id = getattr(po, "live_revision_id", None) if po.live else getattr(po, "latest_revision_id", None)
            qs = MediaReferenceRecord.objects.filter(page_id=po.id)
            if rev_id:
                qs = qs.filter(revision_id=rev_id)
            for atype, aid in qs.values_list("asset_type", "asset_id"):
                if aid:
                    pairs.add((atype, aid))
            fimg = getattr(po, "featured_image", None)
            if fimg is not None and getattr(fimg, "public_id", None):
                pairs.add(("image", fimg.public_id))
        return pairs

    public_asset_pairs = _collect_page_asset_pairs(public_live_pages)
    restricted_or_draft_asset_pairs = _collect_page_asset_pairs(restricted_or_draft_pages)

    # 公开视频的海报允许公开；受限/草稿视频的海报计入受限侧核验
    public_video_uuids = {aid for atype, aid in public_asset_pairs if atype == "video"}
    if public_video_uuids:
        for poster_uid in Video.objects.filter(
            public_id__in=public_video_uuids,
            poster__isnull=False,
        ).values_list("poster__public_id", flat=True):
            if poster_uid:
                public_asset_pairs.add(("image", poster_uid))

    restricted_video_uuids = {aid for atype, aid in restricted_or_draft_asset_pairs if atype == "video"}
    if restricted_video_uuids:
        for poster_uid in Video.objects.filter(
            public_id__in=restricted_video_uuids,
            poster__isnull=False,
        ).values_list("poster__public_id", flat=True):
            if poster_uid:
                restricted_or_draft_asset_pairs.add(("image", poster_uid))

    # 公开作者头像允许保持 PUBLIC（不计入越权公开泄漏）
    allowed_public_pairs = set(public_asset_pairs)
    for avatar_uid in Person.objects.filter(
        is_system_anonymous=False,
        avatar__isnull=False,
    ).values_list("avatar__public_id", flat=True):
        if avatar_uid:
            allowed_public_pairs.add(("image", avatar_uid))

    model_by_type = {
        "image": CustomImage,
        "document": CustomDocument,
        "audio": Audio,
        "video": Video,
    }

    def _count_assets_with_visibility(pairs: set[tuple[str, UUID]], visibility_val: str) -> dict[str, int]:
        counts_by_type: dict[str, int] = {}
        for atype, model_cls in model_by_type.items():
            uids = [aid for t, aid in pairs if t == atype]
            if uids:
                counts_by_type[atype] = model_cls.objects.filter(
                    public_id__in=uids,
                    visibility=visibility_val,
                ).count()
            else:
                counts_by_type[atype] = 0
        return counts_by_type

    unpromoted_by_type = _count_assets_with_visibility(public_asset_pairs, VisibilityChoices.RESTRICTED)
    unpromoted_public_assets = sum(unpromoted_by_type.values())

    restricted_only_pairs = restricted_or_draft_asset_pairs - allowed_public_pairs
    leaked_by_type = _count_assets_with_visibility(restricted_only_pairs, VisibilityChoices.PUBLIC)
    leaked_restricted_only_assets = sum(leaked_by_type.values())

    actual_pv_sum = int(PageAnalyticsTotal.objects.aggregate(s=Sum("total_pv")).get("s") or 0)

    return {
        "mode": "reconcile",
        "batch_id": batch_id,
        "expected_vs_actual": {
            "blog_index_pages": {"expected": expected_indexes, "actual": BlogIndexPage.objects.count()},
            "blog_pages": {"expected": len(included_blogs), "actual": len(migrated_blog_objs)},
            "expected_live_blogs": {"expected": expected_live_blogs, "actual": actual_live_blogs},
            "expected_draft_blogs": {"expected": expected_draft_blogs, "actual": actual_draft_blogs},
            "expected_restricted_blogs": {"expected": expected_restricted_blogs, "actual": actual_restricted_blogs},
            "held_as_draft_blogs": len(held_draft_blog_ids),
            "search_projected_blogs": {"expected": actual_live_blogs, "actual": projected_count},
            "images_incl_thumbs": {
                "expected": len(snap.get("images", [])) + sum(1 for m in snap.get("media", []) if m.get("thumbnail_blob")),
                "actual": CustomImage.objects.count(),
            },
            "documents": {"expected": len(snap.get("documents", [])), "actual": CustomDocument.objects.count()},
            "audio": {"expected": len([m for m in snap.get("media", []) if m.get("type") == "audio"]), "actual": Audio.objects.count()},
            "video": {"expected": len([m for m in snap.get("media", []) if m.get("type") == "video"]), "actual": Video.objects.count()},
            "authors_non_anon": {"expected": len(snap.get("authors", [])), "actual": Person.objects.filter(is_system_anonymous=False).count()},
            "comments": {"expected": len(folded_comments), "actual": Comment.objects.count()},
            "comment_reactions": {"actual": CommentReaction.objects.count()},
            "pv_total_sum": {"expected": expected_pv_sum, "actual": actual_pv_sum},
            "pv_daily_rows": {"actual": PageAnalyticsDaily.objects.count()},
            "tags": {"actual": Tag.objects.count()},
            "redirects": {"actual": Redirect.objects.count()},
        },
        "integrity": {
            "missing_storage_keys": missing_storage_keys,
            "unpromoted_referenced_images": unpromoted_by_type["image"],
            "unpromoted_public_assets": unpromoted_public_assets,
            "unpromoted_public_assets_by_type": unpromoted_by_type,
            "leaked_restricted_only_assets": leaked_restricted_only_assets,
            "leaked_restricted_only_assets_by_type": leaked_by_type,
        },
    }
