"""The ONLY gates through which an evergreen VoiceTrack's ProductionMedia
binding may change, or a VoiceTrack may be deliberately removed.

2.22B: ``VoiceTrack.media`` may be written only inside ``binding_scope()``,
which only library.services.voicetrack_media.bind_media enters -- after it has
taken the canonical ProductionMedia binding lock
(production.services.retention.lock_for_binding) in the same transaction. A
VoiceTrack may be deleted directly only inside ``removal_scope()``, which only
library.services.voicetrack_media.remove_voicetrack enters (optimistic
revision, audit, confined legacy-file cleanup). Deleting the parent Track
still cascades to its VoiceTracks (Django's deletion collector, not
``QuerySet.delete()``): that is the Track's lifecycle, not a VoiceTrack edit,
and it never touches ProductionMedia bytes.

How VoiceTrack enforces it (library.models):

* every QuerySet the ORM hands out for VoiceTrack -- ``objects``, the base
  manager Django itself uses for instance saves and related managers,
  ``.using()``, reverse/related managers -- is a VoiceTrackQuerySet, which
  refuses ``update()``, ``_update()``, ``bulk_update()``, ``_insert()`` /
  ``bulk_create()`` writing the binding, and ``delete()``;
* every instance save -- ``save()``, ``save_base()``, ``create()``,
  ``update_or_create()``, related ``add(bulk=False)``, ModelForm / admin --
  goes through ``_save_table``: an attempted change of ``media`` is refused,
  and outside the binding scope the column is never even written (so a stale
  instance cannot put an old binding back);
* ``Model.delete()`` is refused outside the removal scope.

Context variables (never thread-locals or process-global flags): a scope is
confined to the exact call stack -- thread or task -- that entered it, and it
is reset even when the body raises.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager

from django.db import transaction

_BINDING: contextvars.ContextVar[bool] = contextvars.ContextVar("voicetrack_media_binding_scope", default=False)
_REMOVAL: contextvars.ContextVar[bool] = contextvars.ContextVar("voicetrack_removal_scope", default=False)


class UnguardedVoiceTrackBinding(RuntimeError):
    """A VoiceTrack media binding was written outside the binding service."""


class UnguardedVoiceTrackRemoval(RuntimeError):
    """A VoiceTrack was deleted outside the removal service."""


def binding_allowed() -> bool:
    return _BINDING.get()


def removal_allowed() -> bool:
    return _REMOVAL.get()


@contextmanager
def _scope(var, error):
    if not transaction.get_connection().in_atomic_block:
        raise error("a VoiceTrack guard scope requires an open transaction")
    token = var.set(True)
    try:
        yield
    finally:
        var.reset(token)


def binding_scope():
    """Only for library.services.voicetrack_media.bind_media, inside its locked atomic block."""
    return _scope(_BINDING, UnguardedVoiceTrackBinding)


def removal_scope():
    """Only for library.services.voicetrack_media.remove_voicetrack, inside its atomic block."""
    return _scope(_REMOVAL, UnguardedVoiceTrackRemoval)


def refuse(what: str) -> None:
    raise UnguardedVoiceTrackBinding(
        f"{what}: VoiceTrack.media may only change through library.services.voicetrack_media "
        "(the canonical locked binding service)"
    )


def refuse_removal(what: str) -> None:
    raise UnguardedVoiceTrackRemoval(
        f"{what}: an evergreen VoiceTrack is removed through iPortal "
        "(library.services.voicetrack_media.remove_voicetrack), never deleted directly"
    )
