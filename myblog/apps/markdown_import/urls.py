from django.urls import path

from . import views

app_name = "markdown_import"

urlpatterns = [
    path("catalog/", views.catalog_view, name="catalog"),
    path("pair/exchange/", views.pair_exchange_view, name="pair_exchange"),
    path("preflight/", views.preflight_view, name="preflight"),
    path("jobs/", views.jobs_create_view, name="jobs_create"),
    path("jobs/<uuid:job_id>/", views.job_detail_view, name="job_detail"),
    path("jobs/<uuid:job_id>/assets/", views.job_asset_upload_view, name="job_asset_upload"),
    path("jobs/<uuid:job_id>/commit/", views.job_commit_view, name="job_commit"),
    path("ai/templates/", views.ai_templates_view, name="ai_templates"),
    path("ai/suggest/", views.ai_suggest_view, name="ai_suggest"),
]
