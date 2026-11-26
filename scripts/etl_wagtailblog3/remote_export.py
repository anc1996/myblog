"""
生产库只读快照导出器 (Stage 2A)。

连接目标由运行时环境变量 / CLI 传入，绝不硬编码任何密码或私钥：
- WB3_SSH_HOST, WB3_SSH_PORT, WB3_SSH_USER, WB3_SSH_PASSWORD
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
from django.contrib.contenttypes.models import ContentType
from pymongo import MongoClient
from taggit.models import Tag, TaggedItem
from wagtail.contrib.redirects.models import Redirect
from wagtail.documents import get_document_model
from wagtail.images import get_image_model
from wagtail.models import Site

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
        "date": idx.date.isoformat() if getattr(idx, "date", None) else None,
        "first_published_at": _dt(idx.first_published_at),
        "last_published_at": _dt(idx.last_published_at),
        "owner_id": idx.owner_id,
        "featured_image_id": getattr(idx, "featured_image_id", None),
        "intro": idx.intro or "",
    })

blog_pages_data = []
for bp in BlogPage.objects.order_by("path"):
    ps = pub_states.get(bp.id)
    body_blocks = None
    body_source = None
    pub_vid = getattr(ps, "published_body_version_id", None) or getattr(ps, "published_version_id", None) if ps else None
    if pub_vid:
        doc = mdb["content_body_versions"].find_one({"body_version_id": pub_vid}) or mdb["content_body_versions"].find_one({"_id": pub_vid})
        if doc:
            body_blocks = doc.get("body")
            body_source = "content_body_versions"
    if body_blocks is None:
        doc = mdb["content_body_versions"].find_one(
            {"aggregate_type": "blog.BlogPage", "aggregate_id": str(bp.id)},
            sort=[("_id", -1)],
        ) or mdb["content_body_versions"].find_one({"page_id": bp.id}, sort=[("_id", -1)])
        if doc:
            body_blocks = doc.get("body")
            body_source = "content_body_versions_fallback"
    if body_blocks is None and getattr(bp, "mongo_content_id", None):
        from bson import ObjectId
        try:
            oid = ObjectId(str(bp.mongo_content_id))
            old_doc = mdb["blog_content"].find_one({"_id": oid})
            if old_doc:
                body_blocks = old_doc.get("body")
                body_source = "blog_content"
        except Exception:
            pass
    if body_blocks is None:
        body_blocks = bp.body.get_prep_value() if hasattr(bp.body, "get_prep_value") else []
        body_source = "mysql_inline"

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
        "live": bool(bp.live),
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
    "source": "wagtailblog3@192.168.20.2",
    "site": {
        "site_name": site.site_name if site else "我的博客",
        "home_id": home.id if home else None,
        "home_path": home.path if home else "",
        "home_url_path": home.url_path if home else "/welcome/",
    },
    "users": users_data,
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
print(json.dumps({
    "index_pages": len(index_pages_data),
    "blog_pages": len(blog_pages_data),
    "images": len(images_data),
    "documents": len(docs_data),
    "media": len(media_data),
    "comments": len(comments_data),
    "page_views": len(pv_data),
    "redirects": len(redirects_data),
}, ensure_ascii=False))
"""


def export_production_snapshot(
    *,
    out_dir: Path,
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
    ssh_password: str,
    remote_dir: str,
    remote_python: str,
) -> dict[str, Any]:
    """
    在远端执行只读导出并将 snapshot.json 与 blobs 打包回传至本地 out_dir，
    随后清理远端 /tmp 临时目录并逐文件校验 SHA-256。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    remote_tmp = "/tmp/wb3_readonly_export"
    tar_local = out_dir / "wb3_snapshot.tar.gz"

    env = os.environ.copy()
    env["SSHPASS"] = ssh_password
    ssh_base = [
        "sshpass", "-e", "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-p", str(int(ssh_port)),
        f"{ssh_user}@{ssh_host}",
    ]

    # 1. 远端只读导出到 /tmp/wb3_readonly_export
    remote_cmd = (
        f"rm -rf {shlex.quote(remote_tmp)} && mkdir -p {shlex.quote(remote_tmp)} && "
        f"cd {shlex.quote(remote_dir)} && "
        f"{shlex.quote(remote_python)} - {shlex.quote(remote_tmp)}"
    )
    proc = subprocess.run(
        ssh_base + [remote_cmd],
        input=_REMOTE_READONLY_DUMP_SCRIPT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Remote read-only export failed: {proc.stderr.strip()}")

    # 2. 流式打包回传并立即清理远端临时目录
    stream_cmd = f"tar -C {shlex.quote(remote_tmp)} -czf - . && rm -rf {shlex.quote(remote_tmp)}"
    with tar_local.open("wb") as fp:
        pull_proc = subprocess.run(
            ssh_base + [stream_cmd],
            stdout=fp,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
    if pull_proc.returncode != 0:
        raise RuntimeError(f"Snapshot transfer failed: {pull_proc.stderr.decode('utf-8', 'replace')}")

    with tarfile.open(tar_local, "r:gz") as tf:
        tf.extractall(out_dir, filter="data")
    tar_local.unlink(missing_ok=True)

    snap_path = out_dir / "snapshot.json"
    snapshot = json.loads(snap_path.read_text(encoding="utf-8"))

    # 3. 本地校验所有已下载对象的字节数与 SHA-256
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

    return {
        "snapshot_path": str(snap_path),
        "verified_blobs": verified_blobs,
        "summary": json.loads(proc.stdout.strip().splitlines()[-1]),
    }
