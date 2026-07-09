from django.urls import path

from . import views

app_name = "monitoring"

urlpatterns = [
    path("", views.monitoring_dashboard, name="dashboard"),
    path("api/status/", views.api_monitoring_status, name="api-status"),
    path("api/restart/<int:check_id>/", views.api_restart_check, name="api-restart-check"),
    path("api/recent-logins/", views.api_recent_logins, name="api-recent-logins"),
]
