"""
管理命令：按日归档预聚合表对账、Outbox 消费与双代次重建。

默认执行只读核对（--dry-run），不会写入数据库；
传入 --apply 时按 generation 双代次重建 ArchiveDailyCount 并切换读指针。
"""

import json
from django.core.management.base import BaseCommand
from discovery.archive_counts import process_archive_outbox, reconcile_archive_counts


class Command(BaseCommand):
    help = "核对或重建全站按日归档计数表 (ArchiveDailyCount)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="执行双代次原子重建并切换读指针（默认只读报告差异）",
        )
        parser.add_argument(
            "--process-outbox",
            action="store_true",
            help="先消费待处理的 ArchiveSyncOutbox 任务",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="批量扫描与写入大小（默认 1000）",
        )

    def handle(self, *args, **options):
        if options["process_outbox"]:
            outbox_res = process_archive_outbox()
            self.stdout.write(f"Outbox processed: {json.dumps(outbox_res, ensure_ascii=False)}")

        result = reconcile_archive_counts(
            apply=bool(options["apply"]),
            batch_size=max(100, int(options["batch_size"] or 1000)),
        )
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
