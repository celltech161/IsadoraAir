"""Mount the shared recorder/editor inside a consuming domain's URL space.

    urlpatterns += recorder_urlpatterns("evergreen-voicetrack",
                                        page="voicetracks/studio/", api="api/voicetrack/iportal/")

The recorder implementation is shared; the URLs belong to the consumer, so the
consumer's existing access rules (e.g. GroupAccess path grants) apply to its
recording workspace with no new grants. Route names are fixed per adapter:
``iportal-<adapter>-<endpoint>``.
"""
from __future__ import annotations

from django.urls import path

from . import views


def recorder_urlpatterns(adapter_key: str, *, page: str, api: str):
    kwargs = {"adapter": adapter_key}

    def name(endpoint):
        return views.url_name(adapter_key, endpoint)

    return [
        path(page, views.workstation, kwargs, name=name("page")),
        path(f"{api}context/", views.api_context, kwargs, name=name("context")),
        path(f"{api}take/", views.api_take, kwargs, name=name("take")),
        path(f"{api}revalidate/", views.api_revalidate, kwargs, name=name("revalidate")),
        path(f"{api}commit/", views.api_commit, kwargs, name=name("commit")),
        path(f"{api}remove/", views.api_remove, kwargs, name=name("remove")),
        path(f"{api}source/", views.api_source, kwargs, name=name("source")),
        path(f"{api}media/<uuid:media_id>/", views.api_media, kwargs, name=name("media")),
    ]
