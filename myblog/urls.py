from django.conf import settings
from django.urls import include, path, re_path
from django.contrib import admin
from django.views.decorators.cache import never_cache

from wagtail.admin import urls as wagtailadmin_urls
from wagtail.admin.auth import require_admin_access
from wagtail import urls as wagtail_urls
from wagtail.documents import urls as wagtaildocs_urls

from identity.admin_account import CustomAccountView
from search import views as search_views
from discovery.timeline import timeline_view
from discovery.seo import sitemap_view, robots_txt_view
from discovery.redirects import (
    legacy_archive_redirect_view,
    legacy_author_pk_redirect_view,
    legacy_zh_hans_redirect_view,
)
from content_blocks.embeds.views import (
    controlled_embed_chooser,
    controlled_embed_chooser_upload,
)
from django.views.generic.base import RedirectView

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path(
        "admin/account/",
        never_cache(require_admin_access(CustomAccountView.as_view())),
        name="wagtailadmin_account",
    ),
    path("admin/embeds/chooser/", controlled_embed_chooser),
    path("admin/embeds/chooser/upload/", controlled_embed_chooser_upload),
    path("admin/", include(wagtailadmin_urls)),
    path("sitemap.xml", sitemap_view, name="sitemap"),
    path("robots.txt", robots_txt_view, name="robots_txt"),
    path("", include("identity.urls")),
    path("", include("media_library.urls")),
    path("documents/", include(wagtaildocs_urls)),
    path("authors/", include("people.urls")),
    path("search/", search_views.search, name="search"),
    path("timeline/", timeline_view, name="timeline"),
    path("archive/", legacy_archive_redirect_view, name="legacy_archive_redirect"),
    path("blog/authors/<int:author_id>/", legacy_author_pk_redirect_view, name="legacy_author_pk_redirect"),
    re_path(r"^zh-hans(?:/(?P<rest>.*))?$", legacy_zh_hans_redirect_view, name="legacy_zh_hans_redirect"),
    path("api/v1/", include("api.urls")),
    path("api/markdown-import/", include("markdown_import.urls")),
    path("feed/rss/", RedirectView.as_view(url="/feeds/", permanent=True, query_string=True)),
    path("feed/", RedirectView.as_view(url="/feeds/", permanent=True, query_string=True)),
    path("feeds/", include("discovery.urls")),
]


if settings.DEBUG:
    from django.conf.urls.static import static
    from django.contrib.staticfiles.urls import staticfiles_urlpatterns

    # Serve static and media files from development server
    urlpatterns += staticfiles_urlpatterns()
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

urlpatterns = urlpatterns + [
    # For anything not caught by a more specific rule above, hand over to
    # Wagtail's page serving mechanism. This should be the last pattern in
    # the list:
    path("", include(wagtail_urls)),
    # Alternatively, if you want Wagtail pages to be served from a subpath
    # of your site, rather than the site root:
    #    path("pages/", include(wagtail_urls)),
]
