"""
content_lifecycle.management.commands.setup_author_roles
幂等初始化 Authors 受控作者权限组与双区媒体集合（作者个人工作区 / 全站公共素材库）。
"""

from django.core.management.base import BaseCommand
from content_lifecycle.permissions import ensure_author_role_infrastructure


class Command(BaseCommand):
    help = "幂等创建或校准 Authors 受控创作权限组与作者个人工作区/公共素材库集合"

    def handle(self, *args, **options):
        res = ensure_author_role_infrastructure()
        self.stdout.write(
            self.style.SUCCESS(
                f"已就绪 Authors 组 (id={res['authors_group'].id})、"
                f"挂载页面 '{res['target_root_page'].title}'、"
                f"工作区集合 '{res['workspace_collection'].name}'、"
                f"公共素材库 '{res['public_collection'].name}'。"
            )
        )
