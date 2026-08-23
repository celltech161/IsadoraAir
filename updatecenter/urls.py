from django.urls import path

from . import views

app_name = "updatecenter"

urlpatterns = [
    path("", views.updates_dashboard, name="dashboard"),
    path("check-for-updates/", views.check_for_updates, name="check-for-updates"),
]
