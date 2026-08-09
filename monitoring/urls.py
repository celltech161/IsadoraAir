from django.urls import path

from . import views

app_name = "monitoring"

urlpatterns = [
    path("", views.monitoring_dashboard, name="dashboard"),
    path("api/status/", views.api_monitoring_status, name="api-status"),
    path("api/restart/<int:check_id>/", views.api_restart_check, name="api-restart-check"),
    path("api/recent-logins/", views.api_recent_logins, name="api-recent-logins"),
    path("api/recent-events/", views.api_recent_events, name="api-recent-events"),
    path("api/listeners/", views.api_listener_status, name="api-listener-status"),
    path("api/listeners/reset-peak/", views.api_listener_peak_reset, name="api-listener-peak-reset"),
    path("api/listeners/reset-tlh/", views.api_listener_tlh_reset, name="api-listener-tlh-reset"),
]
