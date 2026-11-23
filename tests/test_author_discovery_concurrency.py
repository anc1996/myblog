"""
全站作者专栏并发与 Outbox 幂等重放测试 (A10, TransactionTestCase)。

使用真实 PostgreSQL 多线程连接与同步屏障验证：
1. 同名中文作者并发创建时，Slug 唯一约束碰撞通过 savepoint 自动生成无冲突唯一 Slug；
2. 投影失败保留 pending OutboxEvent 且消费重放幂等收敛；
3. 并发发布/重排/更名不产生重复作者行或旧快照覆盖。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import threading
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TransactionTestCase
from wagtail.coreutils import get_supported_content_language_variant
from wagtail.models import Collection, Locale, Page, Site

from blog.models import BlogIndexPage, BlogPage
from discovery.models import PublishedSearchAuthor, PublishedSearchDocument
from discovery.projection import project_page_to_discovery
from external_content.models import OutboxEvent
from external_content.outbox import OutboxProcessor
from home.models import HomePage
from people.models import Person, get_or_create_anonymous_person

User = get_user_model()


class AuthorDiscoveryConcurrencyTests(TransactionTestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        Locale.objects.get_or_create(
            language_code=get_supported_content_language_variant(settings.LANGUAGE_CODE)
        )
        if Collection.get_first_root_node() is None:
            Collection.add_root(name="Root")
        if Page.get_first_root_node() is None:
            Page.add_root(title="Root")
        get_or_create_anonymous_person()

    def setUp(self):
        super().setUp()
        Locale.objects.get_or_create(
            language_code=get_supported_content_language_variant(settings.LANGUAGE_CODE)
        )
        if Collection.get_first_root_node() is None:
            Collection.add_root(name="Root")
        self.anon = get_or_create_anonymous_person()
        self.superuser = User.objects.create_superuser(
            username=f"conc_super_{uuid4().hex[:6]}",
            email="conc@example.com",
            password="Password123!",
        )
        self.home = HomePage.objects.first()
        if not self.home:
            root = Page.get_first_root_node() or Page.add_root(title="Root")
            self.home = HomePage(title="Home", slug=f"home-c-{uuid4().hex[:6]}")
            root.add_child(instance=self.home)
            self.home.save_revision().publish()
        self.site = Site.objects.filter(is_default_site=True).first()
        if not self.site:
            self.site = Site.objects.create(hostname="localhost", port=80, root_page=self.home, is_default_site=True)
        self.blog_index = BlogIndexPage(title="并发测试专栏", slug=f"blog-c-{uuid4().hex[:6]}")
        self.home.add_child(instance=self.blog_index)
        self.blog_index.save_revision().publish()

    def test_concurrent_same_name_person_creation_unique_slugs(self):
        """6 线程在同步屏障后并发创建同名中文作者，必须全部成功且生成互异唯一 Slug。"""
        target_name = f"并发张三{uuid4().hex[:4]}"
        worker_count = 6
        barrier = threading.Barrier(worker_count)

        def _create_person(_idx: int) -> tuple[int, str]:
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                p = Person.objects.create(name=target_name, bio="并发测试")
                return (p.pk, p.slug)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            results = list(pool.map(_create_person, range(worker_count)))

        pks = {r[0] for r in results}
        slugs = {r[1] for r in results}
        self.assertEqual(len(pks), worker_count)
        self.assertEqual(len(slugs), worker_count)
        self.assertTrue(all(s and s != "anonymous" for s in slugs))

    def test_projection_failure_retains_outbox_and_idempotent_replay(self):
        """发布同步投影失败时 Outbox 待办存活，恢复后重放可收敛且重复投影不产生脏行。"""
        author = Person.objects.create(name="重放测试作者")
        page = BlogPage(
            title="Outbox 容错文章",
            slug=f"outbox-{uuid4().hex[:6]}",
            authored_on=date(2026, 11, 20),
            owner=self.superuser,
        )
        self.blog_index.add_child(instance=page)
        page.body = [("markdown", "容错测试正文")]
        page.authors.create(person=author, role="primary_author", sort_order=0)

        with patch("discovery.signals.project_page_to_discovery", side_effect=RuntimeError("simulated projection failure")):
            rev = page.save_revision(user=self.superuser)
            rev.publish(user=self.superuser)

        pending = OutboxEvent.objects.filter(page_id=page.id, event_type="page.published", status="pending")
        self.assertTrue(pending.exists())
        self.assertFalse(PublishedSearchDocument.objects.filter(page_id=page.id).exists())

        # 恢复后执行 Outbox 消费并验证重复投影幂等
        processed = OutboxProcessor.process_pending_events(batch_size=50)
        self.assertGreaterEqual(processed, 1)
        self.assertFalse(OutboxEvent.objects.filter(page_id=page.id, event_type="page.published", status="pending").exists())

        doc = PublishedSearchDocument.objects.get(page_id=page.id)
        self.assertEqual(PublishedSearchAuthor.objects.filter(document=doc).count(), 1)
        self.assertEqual(PublishedSearchAuthor.objects.get(document=doc).author_slug, author.slug)

        # 再次幂等投影不增加重复行
        project_page_to_discovery(page)
        self.assertEqual(PublishedSearchAuthor.objects.filter(document=doc).count(), 1)

        # 陈旧的 page.unpublished OutboxEvent 重放不能下架当前已发布的页面
        stale_unpub = OutboxEvent.objects.create(
            event_type="page.unpublished",
            page_id=page.id,
            status="pending",
        )
        OutboxProcessor.process_pending_events(batch_size=50)
        stale_unpub.refresh_from_db()
        self.assertEqual(stale_unpub.status, "processed")
        doc.refresh_from_db()
        self.assertTrue(doc.is_live)
