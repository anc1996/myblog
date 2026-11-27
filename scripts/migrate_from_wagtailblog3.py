"""
wagtailblog3 -> myblog 生产迁移 CLI 入口。

示例：
  # 1. 只读从 192.168.20.2 导出快照到未追踪目录 output/etl_wagtailblog3/
  python -B scripts/migrate_from_wagtailblog3.py --export --ssh-host 192.168.20.2 --ssh-user root

  # 2. 预检（零写库）
  MYBLOG_ENV_FILE=.env.staging python -B scripts/migrate_from_wagtailblog3.py --dry-run

  # 3. 幂等导入至独立暂存/目标库
  MYBLOG_ENV_FILE=.env.staging python -B scripts/migrate_from_wagtailblog3.py --apply

  # 4. 对账核验
  MYBLOG_ENV_FILE=.env.staging python -B scripts/migrate_from_wagtailblog3.py --reconcile
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def main() -> int:
    parser = argparse.ArgumentParser(description="wagtailblog3 -> myblog 只读抽取与幂等迁移工具")
    parser.add_argument("--snapshot-dir", default=str(ROOT_DIR / "output" / "etl_wagtailblog3"), help="本地快照目录（默认 output/etl_wagtailblog3）")
    parser.add_argument("--batch-id", default="wb3_v2", help="幂等批次前缀（默认 wb3_v2；历史批次如 wb3_v1 仅支持只读 --reconcile）")
    parser.add_argument("--export", action="store_true", help="从远端生产库执行只读快照导出")
    parser.add_argument("--dry-run", action="store_true", help="仅预检转换与校验，不写入目标库")
    parser.add_argument("--apply", action="store_true", help="执行幂等落库（强制三端目标隔离，严禁写入 myblog_dev/旧站库/本地文件存储）")
    parser.add_argument("--reconcile", action="store_true", help="执行目标库、MongoDB 与 RustFS 实体对账")
    parser.add_argument("--include-test-pages", action="store_true", help="连同源库 测试页(id=526) 分支一并迁入")
    parser.add_argument("--ssh-host", default=os.environ.get("WB3_SSH_HOST", "192.168.20.2"))
    parser.add_argument("--ssh-port", type=int, default=int(os.environ.get("WB3_SSH_PORT", "22")))
    parser.add_argument("--ssh-user", default=os.environ.get("WB3_SSH_USER", "root"))
    parser.add_argument("--ssh-password", default=os.environ.get("WB3_SSH_PASSWORD", ""))
    parser.add_argument(
        "--ssh-known-hosts",
        default=os.environ.get("WB3_SSH_KNOWN_HOSTS", ""),
        help="SSH known_hosts 文件路径（默认 ~/.ssh/known_hosts，强制 StrictHostKeyChecking=yes）",
    )
    parser.add_argument("--remote-dir", default=os.environ.get("WB3_REMOTE_DIR", "/home/source/Django/wagtail/wagtailblog3"))
    parser.add_argument("--remote-python", default=os.environ.get("WB3_REMOTE_PYTHON", "/root/anaconda3/envs/wagtailblog/bin/python"))
    args = parser.parse_args()

    if not any((args.export, args.dry_run, args.apply, args.reconcile)):
        parser.error("请至少指定 --export / --dry-run / --apply / --reconcile 之一。")

    snap_dir = Path(args.snapshot_dir)
    reports: dict[str, object] = {}

    if args.export:
        from scripts.etl_wagtailblog3.remote_export import export_production_snapshot
        ssh_pwd = args.ssh_password or getpass.getpass(f"SSH password for {args.ssh_user}@{args.ssh_host}: ")
        reports["export"] = export_production_snapshot(
            out_dir=snap_dir,
            ssh_host=args.ssh_host,
            ssh_port=args.ssh_port,
            ssh_user=args.ssh_user,
            ssh_password=ssh_pwd,
            remote_dir=args.remote_dir,
            remote_python=args.remote_python,
            ssh_known_hosts=args.ssh_known_hosts or None,
        )

    if any((args.dry_run, args.apply, args.reconcile)):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myblog.settings.dev")
        import django
        django.setup()
        from scripts.etl_wagtailblog3.loader import apply_snapshot, dry_run_snapshot, reconcile_target

        if args.dry_run:
            rep = dry_run_snapshot(snap_dir, include_test_pages=args.include_test_pages)
            reports["dry_run"] = rep
            (snap_dir / "dry_run_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.apply:
            rep = apply_snapshot(
                snap_dir,
                batch_id=args.batch_id,
                include_test_pages=args.include_test_pages,
            )
            reports["apply"] = rep
            (snap_dir / "apply_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.reconcile:
            rep = reconcile_target(snap_dir, batch_id=args.batch_id, include_test_pages=args.include_test_pages)
            reports["reconcile"] = rep
            (snap_dir / "reconciliation_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
