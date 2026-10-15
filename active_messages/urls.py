"""The producer's Active Messages workspace (iPortal D3B), inside /iportal/."""
from django.urls import path

from production.recorder.urls import recorder_urlpatterns

from . import views
from .iportal import ADAPTER_KEY

app_name = "active_messages"

urlpatterns = [
    path("iportal/messages/", views.my_messages, name="my-messages"),
    path("iportal/messages/start/", views.start_draft, name="start"),
    path("iportal/messages/<uuid:message_id>/publish/", views.publish, name="publish"),
    path("iportal/messages/<uuid:message_id>/recall/", views.recall, name="recall"),
    path("iportal/messages/<uuid:message_id>/discard/", views.discard, name="discard"),
    # The shared recorder/editor, mounted for this domain (subject: ?message=<uuid>).
    *recorder_urlpatterns(ADAPTER_KEY, page="iportal/messages/record/", api="iportal/messages/api/recorder/"),
]
