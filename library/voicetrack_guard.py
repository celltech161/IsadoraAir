"""The ONE gate through which a VoiceTrack's ProductionMedia binding may change.

2.22B: ``VoiceTrack.media`` may be written only inside ``binding_scope()``,
which only library.services.voicetrack_media enters -- after it has taken the
canonical ProductionMedia binding lock (production.services.retention.
lock_for_binding) in the same transaction. VoiceTrack's model and QuerySet
refuse every other path (instance save, ``update()``, ``bulk_update()``,
``bulk_create()``), so a view, the admin or a shell can never perform the
naked ``voice_track.media = ...; voice_track.save()`` the Phase-A binding rule
forbids.

A context variable (not a thread-local or a global flag), so the scope is
confined to the exact call stack that entered it.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager

from django.db import transaction

_SCOPE: contextvars.ContextVar[bool] = contextvars.ContextVar("voicetrack_media_binding_scope", default=False)


class UnguardedVoiceTrackBinding(RuntimeError):
    """A VoiceTrack media binding was written outside the binding service."""


def binding_allowed() -> bool:
    return _SCOPE.get()


@contextmanager
def binding_scope():
    """Only for library.services.voicetrack_media, inside its locked atomic block."""
    if not transaction.get_connection().in_atomic_block:
        raise UnguardedVoiceTrackBinding("a VoiceTrack binding scope requires an open transaction")
    token = _SCOPE.set(True)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def refuse(what: str) -> None:
    raise UnguardedVoiceTrackBinding(
        f"{what}: VoiceTrack.media may only change through library.services.voicetrack_media "
        "(the canonical locked binding service)"
    )
