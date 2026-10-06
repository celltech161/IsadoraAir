"""iPortal workspace hub (2.22B). Each recorder consumer mounts its own
workstation and API under its own URL space (production.recorder.urls)."""
from django.urls import path

from .recorder import views

urlpatterns = [
    path("iportal/", views.workspace_index, name="iportal-index"),
]
