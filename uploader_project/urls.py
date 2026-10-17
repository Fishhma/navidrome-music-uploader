from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.i18n import set_language


urlpatterns = [
    path("i18n/setlang/", set_language, name="set_language"),
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("uploads.urls")),
]
