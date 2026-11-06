"""
content_lifecycle.management.commands.purge_expired_recycled_pages
受控回收站 30 天超期物理清理命令 (RECYCLE-RETENTION-03)。

执行机制：
1. 查找所有 status="recycled" 且 recycled_at 超过指定天数（默认 30 天）的批次；
2. 逐批次调用 RecycleService.purge_page_or_subtree 执行受控物理清库；
3. 触发 OutboxProcessor 处理 page.purged 任务，彻底抹除 MongoDB 中的长文版本；
4. 无论 Linux 系统的定时 Crontab 调度，还是本地开发环境手动执行，均可安全调用。
"""

from datetime import timedelta
import logging
from django.core.management.base import BaseCommand
from django.utils import timezone

from content_lifecycle.models import RecycleBatch
from content_lifecycle.recycle import RecycleService
from external_content.outbox import OutboxProcessor

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "清理受控回收站中超期（默认 30 天）未恢复的废弃页面批次"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="回收站保留天数阈值（默认 30 天）",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="仅预览将被销毁的超期批次，不实际执行物理清空",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(days=days)

        expired_batches = RecycleBatch.objects.filter(
            status="recycled",
            recycled_at__lte=cutoff,
        ).order_by("recycled_at")

        total_count = expired_batches.count()
        if total_count == 0:
            self.stdout.write(self.style.SUCCESS(f"未发现超过 {days} 天的回收批次，回收站无需清理。"))
            return

        self.stdout.write(f"共发现 {total_count} 个回收批次滞留已超过 {days} 天（放入时间 <= {cutoff.strftime('%Y-%m-%d %H:%M:%S')}）。")

        if dry_run:
            for b in expired_batches:
                self.stdout.write(f"  [预览待销毁] 批次 {b.batch_id}: 《{b.root_page_title}》 (共 {b.total_pages} 页, 回收于 {b.recycled_at})")
            self.stdout.write(self.style.WARNING("【Dry Run 模式】未执行实际物理删除。"))
            return

        purged_count = 0
        error_count = 0

        for b in expired_batches:
            self.stdout.write(f"正在物理销毁超期批次: 《{b.root_page_title}》 (ID: {b.root_page_id}, 批次: {b.batch_id})...")
            try:
                res = RecycleService.purge_page_or_subtree(
                    page_id=b.root_page_id,
                    actor=None,
                    batch_id=b.batch_id,
                )
                purged_count += 1
                self.stdout.write(self.style.SUCCESS(f"  成功销毁 SQL 记录（共 {res.get('purged_count', 1)} 页）"))
            except Exception as exc:
                error_count += 1
                self.stdout.write(self.style.ERROR(f"  销毁失败: {exc}"))

        # 触发异步 Outbox 擦除 MongoDB 版本
        try:
            self.stdout.write("正在消费 Outbox 异步擦除 MongoDB 正文版本...")
            outbox_count = OutboxProcessor.process_pending_events(batch_size=100)
            self.stdout.write(self.style.SUCCESS(f"Outbox 处理完毕，共处理 {outbox_count} 项事件。"))
        except Exception as outbox_exc:
            self.stdout.write(self.style.WARNING(f"Outbox 处理遇到异常: {outbox_exc}"))

        self.stdout.write(self.style.SUCCESS(f"超期清理完成！成功销毁 {purged_count} 个批次，失败 {error_count} 个批次。"))
