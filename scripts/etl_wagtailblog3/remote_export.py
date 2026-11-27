"""
生产库只读快照导出器 (Stage 2A)。

连接目标由运行时环境变量 / CLI 传入，绝不硬编码任何密码或私钥：
- WB3_SSH_HOST, WB3_SSH_PORT, WB3_SSH_USER, WB3_SSH_PASSWORD
- WB3_SSH_KNOWN_HOSTS（默认 ~/.ssh/known_hosts，强制启用 StrictHostKeyChecking=yes）
- WB3_REMOTE_DIR, WB3_REMOTE_PYTHON

远程脚本全程严格只读（仅 ORM 查询、MongoDB find() 与 MinIO get_object()），
在本地 output/etl_wagtailblog3/ 下生成 snapshot.json 与按 SHA-256 核验的 blobs/ 目录。
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tarfile
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA_VERSION = 2

_REMOTE_READONLY_DUMP_SCRIPT = r"""
import hashlib, json, os, sys
from pathlib import Path

sys.path.insert(0, os.getcwd())
os.environ.setdefault("WAGTAILBLOG_ENV", "production")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wagtailblog3.settings.dev")
import django
django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from pymongo import MongoClient
from taggit.models import Tag, TaggedItem
from wagtail.contrib.redirects.models import Redirect
from wagtail.documents import get_document_model
from wagtail.images import get_image_model
from wagtail.models import PageViewRestriction, Site

from blog.models import (
    Author,
    BlogCategory,
    BlogImage,
    BlogIndexPage,
    BlogPage,
    BlogPublicationState,
    PageViewCount,
)
from comments.models import BlogPageComment, CommentReaction
from home.models import HomePage
from base.models import NavigationSettings
from wagtailmedia.models import Media as WagtailMedia

BlogDocument = get_document_model()
ImageModel = get_image_model()

OUT_DIR = Path(sys.argv[1])
BLOB_DIR = OUT_DIR / "blobs"
BLOB_DIR.mkdir(parents=True, exist_ok=True)

def _dt(val):
    return val.isoformat() if val is not None else None

def _dump_blob(kind: str, obj_id: int, field_file) -> dict | None:
    if not field_file or not getattr(field_file, "name", ""):
        return None
    key_name = field_file.name
    with field_file.open("rb") as f:
        data = f.read()
    sha = hashlib.sha256(data).hexdigest()
    size = len(data)
    base = os.path.basename(key_name) or f"{kind}_{obj_id}"
    rel_path = f"blobs/{kind}/{obj_id}_{sha[:12]}_{base}"
    target = OUT_DIR / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {
        "storage_key": key_name,
        "local_rel_path": rel_path,
        "byte_size": size,
        "sha256": sha,
        "filename": base,
    }

mongo_cfg = getattr(settings, "MONGO_DB", None) or getattr(settings, "MONGO_SETTINGS", {})
mclient = MongoClient(
    mongo_cfg.get("HOST", "127.0.0.1"),
    int(mongo_cfg.get("PORT", 27017)),
    serverSelectionTimeoutMS=5000,
)
mdb = mclient[mongo_cfg.get("NAME") or mongo_cfg.get("DATABASE", "wagtailblog")]
pub_states = {ps.page_id: ps for ps in BlogPublicationState.objects.all()}

User = get_user_model()
users_data = [
    {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "is_superuser": bool(u.is_superuser),
        "is_staff": bool(u.is_staff),
        "is_active": bool(u.is_active),
    }
    for u in User.objects.filter(is_active=True).order_by("id")
]

site = Site.objects.filter(is_default_site=True).first() or Site.objects.first()
home = HomePage.objects.first()

# 全站页面访问限制元信息（绝不导出明文密码或口令哈希）
restrictions_data = []
referenced_group_ids = set()
for pvr in PageViewRestriction.objects.select_related("page").prefetch_related("groups").order_by("id"):
    gnames = []
    for g in pvr.groups.all():
        referenced_group_ids.add(g.id)
        if g.name:
            gnames.append(g.name)
    gnames = sorted(set(gnames))
    page_obj = getattr(pvr, "page", None)
    restrictions_data.append({
        "id": pvr.id,
        "page_id": pvr.page_id,
        "page_path": getattr(page_obj, "path", "") or "",
        "page_url_path": getattr(page_obj, "url_path", "") or "",
        "restriction_type": str(pvr.restriction_type),
        "group_names": gnames,
        "has_password": bool(getattr(pvr, "password", None)),
    })

# 导出非空（有成员）或被 PageViewRestriction 引用的用户组名列表
group_q = Q(user__isnull=False)
if referenced_group_ids:
    group_q |= Q(id__in=referenced_group_ids)
groups_data = sorted(
    {
        name
        for name in Group.objects.filter(group_q).distinct().values_list("name", flat=True)
        if name
    }
)

nav = NavigationSettings.objects.first()
social_links = []
if nav and getattr(nav, "social_links", None):
    for blk in nav.social_links:
        val = getattr(blk, "value", None) or {}
        social_links.append({
            "platform": str(val.get("platform") or "custom"),
            "label": str(val.get("label") or ""),
            "url": str(val.get("url") or ""),
        })

img_ct = ContentType.objects.get_for_model(BlogImage)
img_tags_map = {}
for ti in TaggedItem.objects.filter(content_type=img_ct).select_related("tag"):
    img_tags_map.setdefault(int(ti.object_id), []).append(ti.tag.name)

images_data = []
for img in BlogImage.objects.order_by("id"):
    blob = _dump_blob("image", img.id, img.file)
    images_data.append({
        "id": img.id,
        "title": img.title,
        "description": getattr(img, "description", "") or "",
        "caption": getattr(img, "caption", "") or "",
        "width": img.width,
        "height": img.height,
        "file_size": img.file_size,
        "file_hash": img.file_hash,
        "uploaded_by_user_id": img.uploaded_by_user_id,
        "tags": img_tags_map.get(img.id, []),
        "blob": blob,
    })

doc_ct = ContentType.objects.get_for_model(BlogDocument)
doc_tags_map = {}
for ti in TaggedItem.objects.filter(content_type=doc_ct).select_related("tag"):
    doc_tags_map.setdefault(int(ti.object_id), []).append(ti.tag.name)

docs_data = []
for doc in BlogDocument.objects.order_by("id"):
    blob = _dump_blob("document", doc.id, doc.file)
    docs_data.append({
        "id": doc.id,
        "title": doc.title,
        "description": getattr(doc, "description", "") or "",
        "file_size": doc.file_size,
        "file_hash": doc.file_hash,
        "uploaded_by_user_id": doc.uploaded_by_user_id,
        "tags": doc_tags_map.get(doc.id, []),
        "blob": blob,
    })

media_data = []
for m in WagtailMedia.objects.order_by("id"):
    blob = _dump_blob(str(m.type), m.id, m.file)
    thumb_blob = _dump_blob("media_thumb", m.id, m.thumbnail) if getattr(m, "thumbnail", None) else None
    media_data.append({
        "id": m.id,
        "title": m.title,
        "type": str(m.type),
        "duration": m.duration,
        "width": getattr(m, "width", None),
        "height": getattr(m, "height", None),
        "uploaded_by_user_id": getattr(m, "uploaded_by_user_id", None),
        "blob": blob,
        "thumbnail_blob": thumb_blob,
    })

authors_data = []
for au in Author.objects.order_by("id"):
    raw_bio = ""
    if hasattr(au, "bio") and au.bio is not None:
        raw_bio = au.bio.get_prep_value() if hasattr(au.bio, "get_prep_value") else str(au.bio)
    authors_data.append({
        "id": au.id,
        "name": au.name,
        "slug": au.slug,
        "bio_raw": raw_bio,
        "author_image_id": getattr(au, "author_image_id", None),
    })

tags_data = [{"id": t.id, "name": t.name, "slug": t.slug} for t in Tag.objects.order_by("id")]
categories_data = [{"id": c.id, "name": c.name, "slug": c.slug} for c in BlogCategory.objects.order_by("id")]

index_pages_data = []
for idx in BlogIndexPage.objects.order_by("path"):
    index_pages_data.append({
        "id": idx.id,
        "title": idx.title,
        "slug": idx.slug,
        "path": idx.path,
        "depth": idx.depth,
        "url_path": idx.url_path,
        "show_in_menus": bool(idx.show_in_menus),
        "seo_title": idx.seo_title or "",
        "search_description": idx.search_description or "",
        "live": bool(idx.live),
        "has_unpublished_changes": bool(getattr(idx, "has_unpublished_changes", False)),
        "live_revision_id": getattr(idx, "live_revision_id", None),
        "date": idx.date.isoformat() if getattr(idx, "date", None) else None,
        "first_published_at": _dt(idx.first_published_at),
        "last_published_at": _dt(idx.last_published_at),
        "owner_id": idx.owner_id,
        "featured_image_id": getattr(idx, "featured_image_id", None),
        "intro": idx.intro or "",
    })

def _parse_revision_content(rev):
    if rev is None:
        return {}
    content = getattr(rev, "content", None)
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}

def _fetch_mongo_body_version(vid):
    if not vid:
        return None
    return (
        mdb["content_body_versions"].find_one({"body_version_id": vid})
        or mdb["content_body_versions"].find_one({"_id": vid})
    )

def _fetch_legacy_blog_content(mongo_content_id):
    if not mongo_content_id:
        return None
    from bson import ObjectId
    try:
        oid = ObjectId(str(mongo_content_id))
        return mdb["blog_content"].find_one({"_id": oid})
    except Exception:
        return None

blog_pages_data = []
for bp in BlogPage.objects.order_by("path"):
    ps = pub_states.get(bp.id)
    body_blocks = None
    body_source = None
    published_body_unresolved = False
    is_live = bool(bp.live)
    has_unpublished = bool(getattr(bp, "has_unpublished_changes", False))
    live_rev_id = getattr(bp, "live_revision_id", None)
    pub_vid = getattr(ps, "published_body_version_id", None) or getattr(ps, "published_version_id", None) if ps else None

    if is_live:
        # 已发布页面严格只取正式发布版本，绝不回退读取 MongoDB 最新草稿
        if pub_vid:
            doc = _fetch_mongo_body_version(pub_vid)
            if doc and doc.get("body") is not None:
                body_blocks = doc.get("body")
                body_source = "published_content_body_versions"

        if body_blocks is None and live_rev_id:
            live_rev = getattr(bp, "live_revision", None)
            rev_content = _parse_revision_content(live_rev)
            rev_vid = rev_content.get("published_body_version_id") or rev_content.get("body_version_id")
            if rev_vid:
                doc = _fetch_mongo_body_version(rev_vid)
                if doc and doc.get("body") is not None:
                    body_blocks = doc.get("body")
                    body_source = "live_revision_content_body_versions"
            if body_blocks is None and rev_content.get("mongo_content_id"):
                old_doc = _fetch_legacy_blog_content(rev_content.get("mongo_content_id"))
                if old_doc and old_doc.get("body") is not None:
                    body_blocks = old_doc.get("body")
                    body_source = "live_revision_blog_content"
            if body_blocks is None and live_rev is not None:
                try:
                    live_obj = live_rev.as_object()
                    if hasattr(live_obj, "body") and live_obj.body is not None:
                        cand = live_obj.body.get_prep_value() if hasattr(live_obj.body, "get_prep_value") else []
                        if cand:
                            body_blocks = cand
                            body_source = "live_revision_inline"
                except Exception:
                    pass

        if body_blocks is None and not has_unpublished and getattr(bp, "mongo_content_id", None):
            old_doc = _fetch_legacy_blog_content(getattr(bp, "mongo_content_id", None))
            if old_doc and old_doc.get("body") is not None:
                body_blocks = old_doc.get("body")
                body_source = "blog_content_clean_live"

        if body_blocks is None and not has_unpublished:
            cand = bp.body.get_prep_value() if hasattr(bp.body, "get_prep_value") else []
            if cand:
                body_blocks = cand
                body_source = "mysql_inline_clean_live"

        if body_blocks is None:
            body_blocks = []
            body_source = "missing_published_body"
            published_body_unresolved = True
    else:
        # 草稿页面（live=False）允许读取最新草稿版本，但快照中明确标注 live=False
        doc = mdb["content_body_versions"].find_one(
            {"aggregate_type": "blog.BlogPage", "aggregate_id": str(bp.id)},
            sort=[("_id", -1)],
        ) or mdb["content_body_versions"].find_one({"page_id": bp.id}, sort=[("_id", -1)])
        if doc and doc.get("body") is not None:
            body_blocks = doc.get("body")
            body_source = "draft_content_body_versions"
        if body_blocks is None and getattr(bp, "mongo_content_id", None):
            old_doc = _fetch_legacy_blog_content(getattr(bp, "mongo_content_id", None))
            if old_doc and old_doc.get("body") is not None:
                body_blocks = old_doc.get("body")
                body_source = "draft_blog_content"
        if body_blocks is None:
            latest_rev = bp.get_latest_revision() if hasattr(bp, "get_latest_revision") else None
            if latest_rev is not None:
                try:
                    latest_obj = latest_rev.as_object()
                    if hasattr(latest_obj, "body") and latest_obj.body is not None:
                        cand = latest_obj.body.get_prep_value() if hasattr(latest_obj.body, "get_prep_value") else []
                        if cand:
                            body_blocks = cand
                            body_source = "draft_latest_revision"
                except Exception:
                    pass
        if body_blocks is None:
            body_blocks = bp.body.get_prep_value() if hasattr(bp.body, "get_prep_value") else []
            body_source = "draft_mysql_inline"

    author_ids = []
    if hasattr(bp, "authors"):
        for item in bp.authors.all():
            if hasattr(item, "author_id") and item.author_id:
                author_ids.append(int(item.author_id))
            elif hasattr(item, "person_id") and item.person_id:
                author_ids.append(int(item.person_id))
            elif hasattr(item, "id"):
                author_ids.append(int(item.id))
    elif getattr(bp, "author_id", None):
        author_ids = [int(bp.author_id)]

    cat_names = []
    if hasattr(bp, "categories"):
        cat_names = list(bp.categories.values_list("name", flat=True))

    blog_pages_data.append({
        "id": bp.id,
        "title": bp.title,
        "slug": bp.slug,
        "path": bp.path,
        "depth": bp.depth,
        "url_path": bp.url_path,
        "show_in_menus": bool(bp.show_in_menus),
        "seo_title": bp.seo_title or "",
        "search_description": bp.search_description or "",
        "live": is_live,
        "has_unpublished_changes": has_unpublished,
        "live_revision_id": live_rev_id,
        "published_body_version_id": str(pub_vid) if pub_vid else None,
        "published_body_unresolved": published_body_unresolved,
        "date": bp.date.isoformat() if getattr(bp, "date", None) else None,
        "first_published_at": _dt(bp.first_published_at),
        "last_published_at": _dt(bp.last_published_at),
        "owner_id": bp.owner_id,
        "featured_image_id": getattr(bp, "featured_image_id", None),
        "intro": bp.intro or "",
        "body_blocks": body_blocks or [],
        "body_source": body_source,
        "author_ids": author_ids,
        "category_names": cat_names,
        "tag_names": list(bp.tags.names()),
    })

comments_data = []
for cm in BlogPageComment.objects.order_by("id"):
    comments_data.append({
        "id": cm.id,
        "page_id": cm.page_id,
        "author_user_id": getattr(cm, "author_user_id", None) or getattr(cm, "user_id", None),
        "guest_name": getattr(cm, "guest_name", "") or getattr(cm, "author_name", "") or "",
        "parent_id": cm.parent_id,
        "status": str(cm.status),
        "content": getattr(cm, "content", None) or getattr(cm, "body", "") or "",
        "created_at": _dt(cm.created_at),
    })

comment_reactions_data = []
for cr in CommentReaction.objects.order_by("id"):
    comment_reactions_data.append({
        "id": cr.id,
        "comment_id": cr.comment_id,
        "user_id": cr.user_id,
        "reaction": int(getattr(cr, "reaction", None) if getattr(cr, "reaction", None) is not None else getattr(cr, "reaction_type", 1)),
        "created_at": _dt(cr.created_at),
    })

pv_data = []
for pv in PageViewCount.objects.order_by("page_id", "date"):
    pv_data.append({
        "page_id": pv.page_id,
        "date": pv.date.isoformat(),
        "count": int(getattr(pv, "count", 0) or 0),
        "unique_count": int(getattr(pv, "unique_count", 0) or 0),
        "view_count_v2": int(getattr(pv, "view_count_v2", 0) or 0),
        "unique_visitor_count_v2": int(getattr(pv, "unique_visitor_count_v2", 0) or 0),
        "engaged": int(getattr(pv, "engaged", 0) or 0),
        "scroll_90": int(getattr(pv, "scroll_90", 0) or 0),
        "active_reading_seconds": int(getattr(pv, "active_reading_seconds", 0) or 0),
    })

redirects_data = []
for rd in Redirect.objects.order_by("id"):
    redirects_data.append({
        "id": rd.id,
        "old_path": rd.old_path,
        "redirect_page_id": rd.redirect_page_id,
        "redirect_link": rd.redirect_link or "",
        "is_permanent": bool(rd.is_permanent),
    })

snapshot = {
    "schema_version": 2,
    "source": "wagtailblog3@192.168.20.2",
    "site": {
        "site_name": site.site_name if site else "我的博客",
        "home_id": home.id if home else None,
        "home_path": home.path if home else "",
        "home_url_path": home.url_path if home else "/welcome/",
    },
    "users": users_data,
    "groups": groups_data,
    "page_view_restrictions": restrictions_data,
    "social_links": social_links,
    "authors": authors_data,
    "categories": categories_data,
    "tags": tags_data,
    "images": images_data,
    "documents": docs_data,
    "media": media_data,
    "index_pages": index_pages_data,
    "blog_pages": blog_pages_data,
    "comments": comments_data,
    "comment_reactions": comment_reactions_data,
    "page_views": pv_data,
    "redirects": redirects_data,
}
(OUT_DIR / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
summary_payload = {
    "schema_version": 2,
    "groups": len(groups_data),
    "page_view_restrictions": len(restrictions_data),
    "index_pages": len(index_pages_data),
    "blog_pages": len(blog_pages_data),
    "images": len(images_data),
    "documents": len(docs_data),
    "media": len(media_data),
    "comments": len(comments_data),
    "page_views": len(pv_data),
    "redirects": len(redirects_data),
}
(OUT_DIR / "export_summary.json").write_text(json.dumps(summary_payload, ensure_ascii=False), encoding="utf-8")
"""


def resolve_ssh_known_hosts(ssh_known_hosts: str | Path | None = None) -> str:
    """解析 SSH known_hosts 路径，默认使用 ~/.ssh/known_hosts，严禁退化到 /dev/null。"""
    raw = str(ssh_known_hosts).strip() if ssh_known_hosts is not None else ""
    if not raw:
        raw = os.environ.get("WB3_SSH_KNOWN_HOSTS", "").strip()
    if not raw:
        raw = str(Path.home() / ".ssh" / "known_hosts")
    resolved = str(Path(raw).expanduser())
    if resolved.lower() in {"/dev/null", "nul", "none"}:
        raise ValueError("ssh_known_hosts 不允许指向 /dev/null，必须提供真实主机指纹文件。")
    return resolved


def build_ssh_command(
    *,
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
    ssh_known_hosts: str | Path | None = None,
) -> list[str]:
    """构造强制主机指纹核验（StrictHostKeyChecking=yes）的 SSH 命令。"""
    known_hosts_path = resolve_ssh_known_hosts(ssh_known_hosts)
    return [
        "sshpass", "-e", "ssh",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts_path}",
        "-p", str(int(ssh_port)),
        f"{ssh_user}@{ssh_host}",
    ]


def build_remote_export_command(*, remote_dir: str, remote_python: str) -> str:
    """
    构造单次 SSH 远端只读导出并流式打包回传命令：
    - 通过 mktemp -d 创建独立随机临时目录，避免固定 /tmp 路径竞态或残留；
    - 通过 trap 在正常结束或异常中断时无条件清理临时目录；
    - 将远端 Python stdout 重定向至 stderr，保证 stdout 仅承载纯净 tar.gz 流。
    """
    return (
        "set -euo pipefail; "
        'TMPDIR=$(mktemp -d /tmp/wb3_readonly_export.XXXXXX); '
        'trap \'rm -rf "$TMPDIR"\' EXIT INT TERM; '
        f"cd {shlex.quote(remote_dir)}; "
        f"{shlex.quote(remote_python)} - \"$TMPDIR\" >&2; "
        'tar -C "$TMPDIR" -czf - .'
    )


def export_production_snapshot(
    *,
    out_dir: Path,
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
    ssh_password: str,
    remote_dir: str,
    remote_python: str,
    ssh_known_hosts: str | Path | None = None,
) -> dict[str, Any]:
    """
    在远端执行只读导出并将 snapshot.json 与 blobs 打包回传至本地 out_dir，
    通过 mktemp -d + trap 保障远端临时目录自动清理，并逐文件校验 SHA-256。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tar_local = out_dir / "wb3_snapshot.tar.gz"

    known_hosts_path = resolve_ssh_known_hosts(ssh_known_hosts)
    if not Path(known_hosts_path).is_file():
        raise FileNotFoundError(
            f"SSH known_hosts 文件不存在：{known_hosts_path}。"
            "请先核验远端主机公钥并写入 known_hosts，或通过 --ssh-known-hosts 指定路径。"
        )

    env = os.environ.copy()
    env["SSHPASS"] = ssh_password
    ssh_base = build_ssh_command(
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_user=ssh_user,
        ssh_known_hosts=known_hosts_path,
    )
    remote_cmd = build_remote_export_command(
        remote_dir=remote_dir,
        remote_python=remote_python,
    )

    # 单次安全 SSH 会话完成只读导出、流式打包回传与 trap 自动清理远端临时目录
    with tar_local.open("wb") as fp:
        pull_proc = subprocess.run(
            ssh_base + [remote_cmd],
            input=_REMOTE_READONLY_DUMP_SCRIPT.encode("utf-8"),
            stdout=fp,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
    if pull_proc.returncode != 0:
        tar_local.unlink(missing_ok=True)
        raise RuntimeError(
            f"Remote read-only export/transfer failed: {pull_proc.stderr.decode('utf-8', 'replace').strip()}"
        )

    with tarfile.open(tar_local, "r:gz") as tf:
        tf.extractall(out_dir, filter="data")
    tar_local.unlink(missing_ok=True)

    snap_path = out_dir / "snapshot.json"
    snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    if int(snapshot.get("schema_version") or 0) < SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(
            f"Exported snapshot schema_version={snapshot.get('schema_version')} "
            f"is lower than required {SNAPSHOT_SCHEMA_VERSION}."
        )

    # 本地校验所有已下载对象的字节数与 SHA-256
    verified_blobs = 0
    for group_key in ("images", "documents", "media"):
        for row in snapshot.get(group_key, []):
            for blob_field in ("blob", "thumbnail_blob"):
                b = row.get(blob_field)
                if not b:
                    continue
                p = out_dir / b["local_rel_path"]
                if not p.is_file():
                    raise FileNotFoundError(f"Missing exported blob: {p}")
                raw = p.read_bytes()
                if len(raw) != int(b["byte_size"]) or hashlib.sha256(raw).hexdigest() != b["sha256"]:
                    raise ValueError(f"Checksum mismatch for blob {b['local_rel_path']}")
                verified_blobs += 1

    summary_file = out_dir / "export_summary.json"
    if summary_file.is_file():
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
    else:
        summary = {
            "schema_version": snapshot.get("schema_version"),
            "groups": len(snapshot.get("groups", [])),
            "page_view_restrictions": len(snapshot.get("page_view_restrictions", [])),
            "index_pages": len(snapshot.get("index_pages", [])),
            "blog_pages": len(snapshot.get("blog_pages", [])),
            "images": len(snapshot.get("images", [])),
            "documents": len(snapshot.get("documents", [])),
            "media": len(snapshot.get("media", [])),
            "comments": len(snapshot.get("comments", [])),
            "page_views": len(snapshot.get("page_views", [])),
            "redirects": len(snapshot.get("redirects", [])),
        }

    return {
        "snapshot_path": str(snap_path),
        "verified_blobs": verified_blobs,
        "summary": summary,
    }
