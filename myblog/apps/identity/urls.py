"""
前台会员中心与账号凭证路由定义 (identity.urls)。
"""

from django.urls import path

from identity import views

app_name = "identity"

urlpatterns = [
    path("account/register/", views.register_view, name="register"),
    path("account/login/", views.login_view, name="login"),
    path("account/logout/", views.logout_view, name="logout"),
    path("account/profile/", views.profile_view, name="profile"),
    path("account/password-change/", views.force_password_change_view, name="force_password_change"),
]
