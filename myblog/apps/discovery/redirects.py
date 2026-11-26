"""
discovery.redirects
旧站迁移 URL 单跳 301 永久重定向视图 (D-01 / D-02)：
1. /zh-hans/<rest> -> /<rest>（保留安全查询串，防 // 开放重定向）；
2. /archive/ -> /timeline/；
3. /blog/authors/<int:author_id>/ -> /authors/<slug>/（仅当存在公开作品的作者时 301，否则 404）。
"""

from __future__ import annotations

from django.conf import settings
from django.http import Http404, HttpResponsePermanentRedirect
from django.urls import reverse
from django.utils import timezone
from wagtail.models import Site

from discovery.eligibility import apply_public_eligibility_filter
from discovery.models import PublishedSearchAuthor, PublishedSearchDocument
from discovery.registry import get_author_enabled_type_keys


def _append_query_string(target_path: str, request) -> str:
    qs = (request.META.get("QUERY_STRING") or "").strip()
    if qs:
        return f"{target_path}?{qs}"
    return target_path


def legacy_zh_hans_redirect_view(request, rest: str = ""):
    """D-01: 剥离旧站 /zh-hans/ 语言前缀，单跳 301 重定向至新站干净根路径。"""
    cleaned_rest = str(rest or "").lstrip("/")
    normalized_path = "/" + cleaned_rest if cleaned_rest else "/"
    # 若去前缀后命中已知旧辅助入口，直接单跳合并到最终目标
    if normalized_path in ("/archive", "/archive/"):
        normalized_path = "/timeline/"
    elif normalized_path in ("/feed/rss", "/feed/rss/", "/feed", "/feed/"):
        normalized_path = "/feeds/"
    return HttpResponsePermanentRedirect(_append_query_string(normalized_path, request))


def legacy_archive_redirect_view(request):
    """D-02: 旧站 /archive/ -> /timeline/ 301 永久重定向。"""
    return HttpResponsePermanentRedirect(_append_query_string("/timeline/", request))


def legacy_author_pk_redirect_view(request, author_id: int):
    """D-02: 旧站 /blog/authors/<pk>/ -> /authors/<slug>/ 301 永久重定向（公开作者白名单）。"""
    if not getattr(settings, "AUTHOR_DISCOVERY_ENABLED", True):
        raise Http404("Author discovery is disabled.")
    from people.models import Person

    person = Person.objects.filter(pk=author_id, is_system_anonymous=False).first()
    if person is None:
        raise Http404("Author not found.")

    author_types = get_author_enabled_type_keys()
    if not author_types:
        raise Http404("Author has no public content.")

    site = Site.find_for_request(request)
    eligible_docs = PublishedSearchDocument.objects.filter(
        is_live=True,
        is_recycled=False,
        type_key__in=author_types,
        authored_on__lte=timezone.localdate(),
    )
    if site is not None:
        eligible_docs = eligible_docs.filter(site_id=site.id)
    eligible_docs = apply_public_eligibility_filter(eligible_docs)

    has_public = PublishedSearchAuthor.objects.filter(
        person_id=person.pk,
        document__in=eligible_docs,
    ).exists()
    if not has_public:
        raise Http404("Author has no public content.")

    return HttpResponsePermanentRedirect(reverse("people:author_detail", kwargs={"slug": person.slug}))
