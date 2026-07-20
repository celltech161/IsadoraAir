from django.urls import path

from . import views

app_name = "aircheck"

urlpatterns = [
    path("api/aircheck/status/", views.api_aircheck_status, name="api-status"),
    path("api/aircheck/start/", views.api_aircheck_start, name="api-start"),
    path("api/aircheck/stop/", views.api_aircheck_stop, name="api-stop"),
]
