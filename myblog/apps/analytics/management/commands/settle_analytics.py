"""
python manage.py settle_analytics
随时可补跑的数据分析结算命令：
1. 冻结并幂等结算 Redis 活动 PV 与阅读深度批次；
2. 在 48 小时 Redis 窗口内补跑封账已结束自然日的日 UV（含 SUNION 与缺测识别）；
3. 可选清理超过留存期的批次收据与 13 个月日聚合明细；
4. 可选执行第 2 期滞回告警评估、每日精选候选生成与榜单快照计算（--evaluate）。
"""

from datetime import date

from django.core.management.base import BaseCommand
from wagtail.models import Site

from analytics.settlement import (
    acquire_settle_cooldown,
    cleanup_expired_analytics,
    seal_daily_uv,
    settle_pv_and_reading_batches,
)
from analytics.scoring import compute_ranking_snapshots, evaluate_daily_alerts
from analytics.curation import generate_curated_candidates


class Command(BaseCommand):
    help = "结算 Redis 分析活动桶到 PostgreSQL，并补跑 48 小时窗口内的日 UV 封账。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days-back",
            type=int,
            default=2,
            help="向前回溯检查并补跑日 UV 封账的天数（默认 2 天，适配 48 小时 Redis 窗口）。",
        )
        parser.add_argument(
            "--cleanup",
            action="store_true",
            help="同时清理过期批次收据（>7 天）与超过 13 个月的日聚合明细。",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="跳过短冷却锁，并强制重算回溯窗口内已封账日期的日 UV。",
        )
        parser.add_argument(
            "--evaluate",
            action="store_true",
            help="结算完成后执行全站口碑滞回告警评估、每日精选候选生成与榜单快照计算。",
        )
        parser.add_argument(
            "--stat-date",
            type=str,
            default=None,
            help="配合 --evaluate 指定评估统计日期（YYYY-MM-DD），默认为昨日（最近已完成自然日）。",
        )

    def handle(self, *args, **options):
        days_back = max(1, int(options["days_back"]))
        force = bool(options["force"])
        do_cleanup = bool(options["cleanup"])
        do_evaluate = bool(options["evaluate"])
        raw_stat_date = options.get("stat_date")
        eval_date = date.fromisoformat(raw_stat_date.strip()) if raw_stat_date else None

        if not force and not acquire_settle_cooldown(timeout_seconds=60):
            self.stdout.write(self.style.WARNING("settle_analytics 冷却锁生效中，跳过本次并发执行（可用 --force 强制执行）。"))
            return

        pv_res = settle_pv_and_reading_batches()
        uv_res = seal_daily_uv(days_back=days_back, force=force)
        cleanup_res = cleanup_expired_analytics() if do_cleanup else None

        self.stdout.write(
            self.style.SUCCESS(
                f"PV/阅读结算: degraded={pv_res.get('degraded')}, "
                f"settled={pv_res.get('settled_batches')}, skipped={pv_res.get('skipped_batches')} | "
                f"日UV封账: degraded={uv_res.get('degraded')}, "
                f"sealed={uv_res.get('sealed_dates')}, missing={uv_res.get('missing_dates')}, "
                f"skipped={uv_res.get('skipped_dates')}"
            )
        )
        if cleanup_res is not None:
            self.stdout.write(
                self.style.SUCCESS(
                    f"过期清理: receipts={cleanup_res['receipts_deleted']}, "
                    f"page_daily={cleanup_res['page_daily_deleted']}, "
                    f"scope_daily={cleanup_res['scope_daily_deleted']}"
                )
            )

        if do_evaluate:
            for site in Site.objects.all().order_by("id"):
                alert_res = evaluate_daily_alerts(site, stat_date=eval_date)
                curation_res = generate_curated_candidates(site, stat_date=eval_date)
                ranking_res = compute_ranking_snapshots(site, stat_date=eval_date)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[站点 {site.id} 评估完成] "
                        f"告警评估: pages={alert_res.get('evaluated_pages', 0)}, "
                        f"active+={alert_res.get('promoted_active', 0)}, resolved={alert_res.get('resolved', 0)} | "
                        f"精选候选: created={curation_res.get('candidates_created', 0)}, updated={curation_res.get('candidates_updated', 0)} | "
                        f"榜单快照: snapshots={ranking_res.get('snapshots_written', 0)}"
                    )
                )
