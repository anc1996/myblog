from django.urls import path
from captcha import views

urlpatterns = [
    path("challenges/", views.issue_challenge_api, name="captcha_challenge_issue"),
    path("image/<str:captcha_id>/", views.challenge_image_api, name="captcha_challenge_image"),
]
