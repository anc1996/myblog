"""
content_lifecycle.management.commands.reconcile_cross_storage
跨存储一致性对账与受控垃圾清理管理命令 (CrossStorageReconciler / E.1 契约)。

命令能力：
1. --audit (默认)：对已上线页面执行 PostgreSQL、MongoDB、Discovery 与 Media 四方对账审计；
2. --clean-mongo-gc：清理未被引用的 MongoDB 孤儿正文版本（遵循 7 天安全宽限期）；
3. --clean-orphan-media：对账全站零引用媒体文件，默认仅输出待销毁清单（Dry-Run），
   只有明确传入 --force-delete 时，才调取 S3 擦除底层文件并清理记录。
"""

from django.core.management.base import BaseCommand
from content_lifecycle.reconciliation import CrossStorageReconciler


class Command(BaseCommand):
    help = "跨存储（PostgreSQL + MongoDB + RustFS + Discovery）数据一致性对账与受控清理引擎"

    def add_arguments(self, parser):
        parser.add_argument(
            "--audit",
            action="store_true",
            help="执行已发布页面的四方一致性深度审计（默认行为）",
        )
        parser.add_argument(
            "--clean-mongo-gc",
            action="store_true",
            help="执行 MongoDB 历史未引用正文垃圾清理 (GC)",
        )
        parser.add_argument(
            "--grace-days",
            type=int,
            default=7,
            help="MongoDB 孤儿正文清理的安全宽限期天数（默认 7 天）",
        )
        parser.add_argument(
            "--clean-orphan-media",
            action="store_true",
            help="扫描全站零引用的孤儿媒体资源",
        )
        parser.add_argument(
            "--media-grace-days",
            type=int,
            default=30,
            help="媒体资源零引用的安全保留天数（默认 30 天）",
        )
        parser.add_argument(
            "--force-delete",
            action="store_true",
            help="明确授权执行真正的 S3 物理文件抹除与数据库销毁（默认仅为 Dry-Run 预检）",
        )

    def handle(self, *args, **options):
        audit_only = not (options["clean_mongo_gc"] or options["clean_orphan_media"])

        # 1. 默认或显式执行四方一致性审计
        if options["audit"] or audit_only:
            self.stdout.write(self.style.MIGRATE_HEADING("=== 1. 执行全站已发布页面四方一致性对账 ==="))
            audit_res = CrossStorageReconciler.audit_published_pages_consistency()
            self.stdout.write(f"审计完成：已核验 {audit_res['audited_pages']} 篇已上线文章，异常数: {audit_res['discrepancies_count']}")
            if audit_res["healthy"]:
                self.stdout.write(self.style.SUCCESS("  [PASS] PostgreSQL、MongoDB、Discovery 与 Media 数据 100% 一致！"))
            else:
                self.stdout.write(self.style.ERROR(f"  [FAIL] 发现 {audit_res['discrepancies_count']} 项数据不一致："))
                for d in audit_res["discrepancies"]:
                    self.stdout.write(self.style.WARNING(f"    - 页面 ID={d.get('page_id')}: {d.get('message')} ({d.get('error')})"))

        # 2. 执行 MongoDB 孤儿正文清理
        if options["clean_mongo_gc"]:
            grace_days = options["grace_days"]
            grace_seconds = grace_days * 86400
            self.stdout.write(self.style.MIGRATE_HEADING(f"=== 2. 执行 MongoDB 孤儿正文垃圾清理 (宽限期 {grace_days} 天) ==="))
            gc_res = CrossStorageReconciler.run_garbage_collection(
                grace_period_seconds=grace_seconds,
                dry_run=not options["force_delete"],
            )
            mode_str = "【执行物理清理】" if options["force_delete"] else "【Dry-Run 预览模式】"
            self.stdout.write(f"{mode_str} 扫描到超出 {grace_days} 天宽限期的孤儿正文版本: {gc_res['purged_count']} 个")
            for o in gc_res.get("purged_orphans", [])[:5]:
                self.stdout.write(f"    - doc_uuid={o['document_uuid']} ver={o['version_id']} field={o['field_name']} created={o['created_at']}")
            if len(gc_res.get("purged_orphans", [])) > 5:
                self.stdout.write(f"    ... 以及其余 {len(gc_res['purged_orphans']) - 5} 项")

        # 3. 执行媒体文件对账与清理
        if options["clean_orphan_media"]:
            media_grace = options["media_grace_days"]
            is_dry_run = not options["force_delete"]
            mode_str = "【执行底层 S3 擦除】" if options["force_delete"] else "【Dry-Run 审计预览】"
            self.stdout.write(self.style.MIGRATE_HEADING(f"=== 3. 扫描全站零引用孤儿媒体资源 ({mode_str}, 宽限期 {media_grace} 天) ==="))
            media_res = CrossStorageReconciler.reconcile_and_purge_orphan_media(
                dry_run=is_dry_run,
                grace_period_days=media_grace,
            )
            count = media_res["orphan_candidates_count"]
            self.stdout.write(f"扫描完毕：共发现 {count} 个超期零引用媒体文件。")
            total_bytes = sum(c.get("file_size", 0) for c in media_res.get("candidates", []))
            self.stdout.write(f"预计可释放存储空间: {total_bytes / (1024 * 1024):.2f} MB")
            for c in media_res.get("candidates", [])[:5]:
                self.stdout.write(f"    - [{c['asset_type']}] 《{c['title']}》 file={c['file_name']} (大小: {c['file_size']} 字节)")

            if is_dry_run and count > 0:
                self.stdout.write(self.style.NOTICE("提示：当前为安全预览模式，未真正删除 S3 物理文件。若确认彻底擦除，请附加 --force-delete 参数。"))
            elif not is_dry_run:
                self.stdout.write(self.style.SUCCESS(f"已受控物理销毁 {media_res['purged_count']} 个孤儿媒体文件及其数据库记录。"))
