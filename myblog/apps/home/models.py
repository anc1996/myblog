from django.db import models
from django.db.models import Count, Exists, OuterRef, Q
from wagtail.models import Page


class HomePage(Page):
    author_creatable = False
    author_allow_child_creation = False

    # 约束首页下可创建的子页面类型
    subpage_types = [
        "blog.BlogIndexPage",
        "blog.BlogPage",
        "guides.GuideIndexPage",
        "portfolio.PortfolioIndexPage",
        "taxonomy.TagIndexPage",
    ]

    def permissions_for_user(self, user):
        from content_lifecycle.permissions import OwnershipPagePermissionTester
        return OwnershipPagePermissionTester(user, self)

    def get_context(self, request, *args, **kwargs):
        context = super().get_context(request, *args, **kwargs)
        from content_lifecycle.models import RecycleEntry
        from content_lifecycle.public_access import get_public_blocked_page_ids
        from discovery.detail_navigation import _get_nested_site_root_paths
        from discovery.models import PublishedSearchTag, PublishedSearchDocument
        from discovery.navigation import (
            build_tag_detail_url,
            find_primary_entry_urls,
            get_site_directory_tree,
            resolve_tag_index_page,
        )
        from discovery.registry import get_model_for_type_key
        from wagtail.models import PageViewRestriction, Site

        # 解析当前站点与根子树边界（严格防止跨站串联）
        req_site = None
        if request is not None:
            try:
                req_site = Site.find_for_request(request)
            except Exception:
                req_site = None
        self_site = None
        try:
            self_site = self.get_site()
        except Exception:
            self_site = None

        if req_site is not None and req_site.root_page_id == self.id:
            site = req_site
        elif self_site is not None:
            site = self_site
        else:
            site = req_site or Site.objects.filter(is_default_site=True).first() or Site.objects.order_by("id").first()

        site_root = site.root_page if (site is not None and site.root_page_id) else self
        root_path = site_root.path
        root_depth = site_root.depth

        def _select_public_pages(qs, limit: int):
            """有界分批拉取并通过 get_public_blocked_page_ids 过滤祖先受限/回收页，杜绝全表扫描。"""
            selected_ids: list[int] = []
            batch_size = 30
            for step in range(3):
                offset = step * batch_size
                rows = list(qs.values_list("id", "path")[offset : offset + batch_size])
                if not rows:
                    break
                blocked = get_public_blocked_page_ids(rows)
                for pid, _ in rows:
                    if pid not in blocked:
                        selected_ids.append(pid)
                        if len(selected_ids) >= limit:
                            return selected_ids
                if len(rows) < batch_size:
                    break
            return selected_ids

        # 0. 解析站点标签大厅（单请求共享复用，不跨站）
        tag_index_obj = resolve_tag_index_page(request, page=self, site=site)
        tag_index_url = ""
        if tag_index_obj is not None:
            try:
                tag_index_url = (tag_index_obj.get_url(request) if request else tag_index_obj.url) or ""
            except Exception:
                tag_index_url = getattr(tag_index_obj, "url", "") or ""
        context["tag_index_url"] = tag_index_url

        # 1. 最新发布博客文章 (前 6 篇，严格限定当前站点根子树与公开资格)
        recycled_subq = RecycleEntry.objects.filter(
            page_id=OuterRef("id"),
            status__in=("recycled", "restoring"),
        )
        restricted_subq = PageViewRestriction.objects.filter(page_id=OuterRef("id"))
        article_model = get_model_for_type_key("blog_page")
        latest_articles = []
        if article_model is not None:
            latest_qs = (
                article_model.objects.filter(
                    live=True,
                    is_recycled=False,
                    alias_of__isnull=True,
                    path__startswith=root_path,
                    depth__gt=root_depth,
                )
                .filter(~Exists(recycled_subq) & ~Exists(restricted_subq))
                .order_by("-authored_on", "-id")
            )
            if site is not None:
                for np in _get_nested_site_root_paths(site):
                    latest_qs = latest_qs.exclude(path__startswith=np)
            latest_ids = _select_public_pages(latest_qs, 6)
            article_qs = article_model.objects.filter(id__in=latest_ids).select_related("featured_image").prefetch_related("tags")
            if hasattr(article_model, "authors"):
                article_qs = article_qs.prefetch_related("authors__person")
            articles_by_id = {a.id: a for a in article_qs}
            latest_articles = [articles_by_id[pid] for pid in latest_ids if pid in articles_by_id]
            if latest_articles:
                from django.conf import settings
                from django.urls import NoReverseMatch, reverse
                from analytics.services import get_batch_public_pv_readonly
                from people.attribution import extract_raw_author_refs_from_page, resolve_author_attributions

                pv_map = get_batch_public_pv_readonly(
                    site,
                    [(int(a.id), int(getattr(a, "live_revision_id", 0) or 0)) for a in latest_articles],
                )
                author_enabled = getattr(settings, "AUTHOR_DISCOVERY_ENABLED", True)
                for art in latest_articles:
                    art.public_view_count = pv_map.get(art.id) if (pv_map is not None and art.id in pv_map) else None
                    art.display_tags = [
                        {
                            "name": t.name,
                            "slug": t.slug,
                            "url": build_tag_detail_url(
                                tag_index_obj, t.slug, base_url=tag_index_url, request=request
                            ),
                        }
                        for t in art.tags.all()
                    ]
                    if author_enabled and hasattr(art, "authors"):
                        raw_refs = extract_raw_author_refs_from_page(art)
                        resolved = resolve_author_attributions(
                            raw_refs,
                            revision_id=getattr(art, "live_revision_id", None),
                            allow_anonymous=True,
                        )
                        display_authors = []
                        for au in resolved:
                            try:
                                au_url = reverse("people:author_detail", kwargs={"slug": au.slug})
                            except NoReverseMatch:
                                au_url = f"/authors/{au.slug}/"
                            display_authors.append({
                                "name": au.name,
                                "slug": au.slug,
                                "url": au_url,
                                "is_anonymous": au.is_anonymous,
                            })
                        art.display_authors = display_authors
                    else:
                        art.display_authors = []
        context["latest_articles"] = latest_articles

        # 2. 编辑精选推荐位（仅从已批准池投放，无已批准内容时返回空列表，绝不用最新文章冒充精选）
        from django.db import DatabaseError
        from analytics.curation import RecommendationService

        try:
            context["featured_carousel_items"] = RecommendationService.for_placement(
                site, "home_carousel", limit=8, request=request
            )
            context["featured_guides"] = RecommendationService.for_placement(
                site, "home_guides", limit=4, request=request
            )
            context["featured_projects"] = RecommendationService.for_placement(
                site, "home_projects", limit=3, request=request
            )
        except DatabaseError:
            context["featured_carousel_items"] = []
            context["featured_guides"] = []
            context["featured_projects"] = []

        # 3. 共享多类型栏目目录树（与全站左侧抽屉同源，按 type_key 派生各入口 URL，零页面硬编码）
        directory_tree = get_site_directory_tree(request, site=site, page=self)
        context["directory_tree"] = directory_tree
        primary_urls = find_primary_entry_urls(directory_tree)
        context["primary_blog_url"] = primary_urls.get("blog_index", "")
        context["guide_index_url"] = primary_urls.get("guide_index", "")
        context["portfolio_index_url"] = primary_urls.get("portfolio_index", "")

        # 4. 当前站点热门技术标签 (前 15 个活跃公开标签，过滤跨站与祖先受限/回收/未上线文档)
        doc_recycled_subq = RecycleEntry.objects.filter(
            page_id=OuterRef("page_id"),
            status__in=("recycled", "restoring"),
        )
        doc_restricted_subq = PageViewRestriction.objects.filter(page_id=OuterRef("page_id"))
        eligible_docs = (
            PublishedSearchDocument.objects.filter(
                is_live=True,
                is_recycled=False,
                index_generation=1,
                root_path__startswith=root_path,
                depth__gt=root_depth,
            )
            .filter(~Exists(doc_recycled_subq) & ~Exists(doc_restricted_subq))
        )
        if site is not None:
            for np in _get_nested_site_root_paths(site):
                eligible_docs = eligible_docs.exclude(root_path__startswith=np)

        # 在 SQL 层面剔除受限/回收/未上线分支祖先路径，防止祖先隐私穿透到标签统计
        bad_paths: set[str] = set()
        restricted_pids = list(PageViewRestriction.objects.values_list("page_id", flat=True)[:200])
        if restricted_pids:
            bad_paths.update(
                Page.objects.filter(id__in=restricted_pids, path__startswith=root_path).values_list("path", flat=True)
            )
        recycled_pids = list(
            RecycleEntry.objects.filter(status__in=("recycled", "restoring")).values_list("page_id", flat=True)[:200]
        )
        if recycled_pids:
            bad_paths.update(
                Page.objects.filter(id__in=recycled_pids, path__startswith=root_path).values_list("path", flat=True)
            )
        unlive_branch_paths = list(
            Page.objects.filter(
                path__startswith=root_path,
                depth__gt=root_depth,
                numchild__gt=0,
                live=False,
            ).values_list("path", flat=True)[:200]
        )
        bad_paths.update(unlive_branch_paths)
        for bp in bad_paths:
            if bp:
                eligible_docs = eligible_docs.exclude(root_path__startswith=bp)

        tag_counts = (
            PublishedSearchTag.objects.filter(document__in=eligible_docs)
            .values("tag_name", "tag_slug")
            .annotate(count=Count("document_id", distinct=True))
            .filter(count__gte=1)
            .order_by("-count", "tag_name")[:30]
        )
        verified_tags: list[dict] = []
        for tc in list(tag_counts):
            sample_docs = list(
                PublishedSearchTag.objects.filter(tag_slug=tc["tag_slug"], document__in=eligible_docs)
                .values_list("document__page_id", "document__root_path")[:20]
            )
            if sample_docs:
                blocked = get_public_blocked_page_ids(sample_docs)
                if any(pid not in blocked for pid, _ in sample_docs):
                    tc_item = dict(tc)
                    tc_item["url"] = build_tag_detail_url(
                        tag_index_obj,
                        tc_item.get("tag_slug"),
                        base_url=tag_index_url,
                        request=request,
                    )
                    verified_tags.append(tc_item)
                    if len(verified_tags) >= 15:
                        break
        context["popular_tags"] = verified_tags
        return context
