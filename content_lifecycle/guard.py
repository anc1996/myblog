"""
PostgreSQL 单行协调锁 (ContentMutationGuard) 上下文管理器。
"""

from contextlib import contextmanager
from django.db import connection, transaction
from content_lifecycle.models import ContentMutationGuard


@contextmanager
def content_mutation_guard(timeout_seconds: int = 3):
    """
    全局写操作协调行锁：
    1. 设置局部事务超时 SET LOCAL lock_timeout = '3s'，防止死锁无限阻塞连接；
    2. 获取 ContentMutationGuard 单行排他锁 (SELECT FOR UPDATE)；
    3. 退出上下文时自动提交并释放行锁。
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL lock_timeout = '{int(timeout_seconds)}s';")
        guard, _ = ContentMutationGuard.objects.select_for_update().get_or_create(name="global")
        yield guard
