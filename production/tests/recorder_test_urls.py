"""Test-only URL configuration: the shared recorder mounted for a scratch,
non-VoiceTrack consumer (production.tests.test_recorder_core)."""
from django.urls import include, path

from production.recorder.urls import recorder_urlpatterns

urlpatterns = [
    *recorder_urlpatterns("scratch-pad", page="scratch/", api="api/scratch/"),
    path("", include("isadoraair.urls")),        # the real site (base.html links into it)
]
