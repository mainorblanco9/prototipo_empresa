from django.urls import path

from . import views

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("diseno/", views.design_preferences, name="design_preferences"),
    path("", views.index, name="index"),
    path("reportes/<uuid:report_id>.csv", views.download_report, name="download_report"),
]
