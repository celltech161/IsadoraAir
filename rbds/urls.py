from django.urls import path

from . import views

app_name = "rbds"

urlpatterns = [
    path("", views.rbds_dashboard, name="dashboard"),
    path("api/status/", views.api_rbds_status, name="api-status"),
]
