"""The producer's My Content workspace (iPortal D3C), inside /iportal/."""
from django.urls import path

from production.recorder.urls import recorder_urlpatterns

from . import views
from .iportal import ADAPTER_KEY

app_name = "produced_content"

urlpatterns = [
    path("iportal/content/", views.my_content, name="my-content"),
    path("iportal/content/start/", views.start, name="start"),
    path("iportal/content/<uuid:item_id>/withdraw/", views.withdraw, name="withdraw"),
    # The shared recorder/editor, mounted for this domain (subject: ?item=<uuid> | ?test_type=<key>).
    *recorder_urlpatterns(ADAPTER_KEY, page="iportal/content/record/", api="iportal/content/api/recorder/"),
]
