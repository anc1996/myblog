# Generated manually to backfill Comment version pointers cleared by 0002 RemoveField/AddField

from django.db import migrations


def backfill_comment_version_pointers(apps, schema_editor):
    """
    幂等回填迁移：
    0002 迁移重构了 Comment.latest_version / approved_version / pending_version 外键列，
    导致既有评论的指针列变为 NULL（但对应的 CommentVersion 记录仍完整保留）。
    本步骤根据 CommentVersion 的版本号与审核状态重新恢复指针与主体标识，不改动已有非空值。
    """
    Comment = apps.get_model("comments", "Comment")
    CommentVersion = apps.get_model("comments", "CommentVersion")

    versions_by_comment = {}
    for ver in CommentVersion.objects.order_by("comment_id", "version_number", "id").iterator():
        versions_by_comment.setdefault(ver.comment_id, []).append(ver)

    for comment in Comment.objects.all().iterator():
        versions = versions_by_comment.get(comment.id, [])
        update_fields = []

        if versions:
            latest = versions[-1]
            approved = next((v for v in reversed(versions) if v.status == "approved"), None)
            pending = next((v for v in reversed(versions) if v.status == "pending"), None)

            if comment.latest_version_id is None:
                comment.latest_version_id = latest.id
                update_fields.append("latest_version")
            if comment.approved_version_id is None and approved is not None:
                comment.approved_version_id = approved.id
                update_fields.append("approved_version")
            if comment.pending_version_id is None and pending is not None:
                comment.pending_version_id = pending.id
                update_fields.append("pending_version")
                if comment.pending_submitted_at is None:
                    comment.pending_submitted_at = pending.created_at
                    update_fields.append("pending_submitted_at")

        if not comment.author_subject_key:
            if comment.author_id:
                comment.author_subject_key = f"user:{comment.author_id}"
                update_fields.append("author_subject_key")
            elif comment.ip_address:
                comment.author_subject_key = f"ip:{comment.ip_address}"
                update_fields.append("author_subject_key")

        if update_fields:
            comment.save(update_fields=update_fields)


class Migration(migrations.Migration):

    dependencies = [
        ("comments", "0002_commentgovernancebatch_commentgovernanceitem_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_comment_version_pointers, migrations.RunPython.noop),
    ]
