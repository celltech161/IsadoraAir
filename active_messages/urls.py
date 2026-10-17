"""The producer's Active Messages workspace (iPortal D3B) and the operator board (D3D), inside /iportal/."""
from django.urls import path

from production.recorder.urls import recorder_urlpatterns

from . import board, views
from .iportal import ADAPTER_KEY

app_name = "active_messages"

urlpatterns = [
    path("iportal/messages/", views.my_messages, name="my-messages"),
    path("iportal/messages/start/", views.start_draft, name="start"),
    path("iportal/messages/<uuid:message_id>/publish/", views.publish, name="publish"),
    path("iportal/messages/<uuid:message_id>/recall/", views.recall, name="recall"),
    path("iportal/messages/<uuid:message_id>/discard/", views.discard, name="discard"),
    # The operator board (D3D, control only): one exact message UUID per action.
    path("iportal/messages/board/", board.board, name="board"),
    path("iportal/messages/board/status/", board.board_status, name="board-status"),
    path("iportal/messages/board/replay/", board.replay, name="board-replay"),
    path("iportal/messages/board/airings/", board.airings, name="board-airings"),
    path("iportal/messages/board/airings/<uuid:execution_id>/", board.airing_detail, name="board-airing"),
    path("iportal/messages/board/<uuid:message_id>/", board.message_detail, name="board-message"),
    path("iportal/messages/board/<uuid:message_id>/publish/", board.publish, name="board-publish"),
    path("iportal/messages/board/<uuid:message_id>/clear/", board.clear, name="board-clear"),
    path("iportal/messages/board/<uuid:message_id>/decline/", board.decline, name="board-decline"),
    path("iportal/messages/board/<uuid:message_id>/expiry/", board.set_expiry, name="board-expiry"),
    # The shared recorder/editor, mounted for this domain (subject: ?message=<uuid>).
    *recorder_urlpatterns(ADAPTER_KEY, page="iportal/messages/record/", api="iportal/messages/api/recorder/"),
]
