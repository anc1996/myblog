from django.urls import path
from analytics import views

app_name = "analytics"

urlpatterns = [
    path("context/", views.analytics_context_view, name="context"),
    path("events/", views.analytics_events_view, name="events"),
    path("pages/<int:page_id>/rating/", views.page_rating_view, name="page_rating"),
    path("pages/<int:page_id>/reading-proof/", views.reading_proof_view, name="reading_proof"),
]
