"""Station-managed reference audio (beds, intros): key -> pinned retained media.

The ONLY way a render recipe reaches reference audio. A key is a slug, never a
path; it resolves to the one current ReferenceAudio row, whose media must be
present and valid and whose bytes must still hash to the SHA-256 pinned at
registration. Replacement is explicit: ``register`` retires the current row
for the key and appends a new one in the same transaction, recording who did
it and why.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from ..errors import MediaInconsistent, MediaNotValidated, MediaPurged
from ..models import REFERENCE_KEY_PATTERN, ProductionMedia, ReferenceAudio
from . import media_io, retention

_KEY_RE = re.compile(REFERENCE_KEY_PATTERN)
_HASH_CHUNK = 1024 * 1024


class ReferenceAudioError(Exception):
    """A reference cannot be registered or resolved. ``code`` is stable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ResolvedReference:
    """An OPEN, hash-verified reference: ``file`` holds the exact bytes whose
    SHA-256 equals ``sha256`` (rewound to the start). The caller closes it."""

    key: str
    reference_id: str
    media: ProductionMedia
    sha256: str
    file: object

    def provenance(self) -> dict:
        return {"key": self.key, "reference": self.reference_id, "media": str(self.media.pk), "sha256": self.sha256}

    def close(self):
        self.file.close()


def _actor(user):
    if user is None or getattr(user, "pk", None) is None:
        return None, ""
    return user, user.get_username()[:150]


def check_key(key) -> str:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise ReferenceAudioError("invalid_reference_key", "a reference key is a lowercase slug, never a path")
    return key


def register(key: str, media, *, actor=None, label: str = "", note: str = "") -> ReferenceAudio:
    """Make ``media`` (a present, valid ``bed`` ProductionMedia) the current
    audio for ``key``, retiring whatever was current before."""
    check_key(key)
    user, username = _actor(actor)
    with transaction.atomic():
        try:
            row = retention.lock_for_binding(media, require_valid=True)        # THE binding rule
        except (MediaPurged, MediaNotValidated) as exc:
            raise ReferenceAudioError("reference_media_unusable", "reference audio must be present and valid") from exc
        if row.kind != ProductionMedia.KIND_BED:
            raise ReferenceAudioError("reference_media_kind", "reference audio must be a 'bed' production media")
        now = timezone.now()
        for current in ReferenceAudio.objects.select_for_update().filter(key=key, retired_at__isnull=True):
            current.retired_at, current.retired_by, current.retired_by_username = now, user, username
            current.retire_reason = f"replaced by a new registration of {key}"[:500]
            current.save(update_fields=["retired_at", "retired_by", "retired_by_username", "retire_reason"])
        return ReferenceAudio.objects.create(
            key=key, media=row, sha256=row.sha256, label=str(label)[:200], note=str(note)[:500],
            registered_by=user, registered_by_username=username,
        )


def retire(key: str, *, actor=None, reason: str = "") -> ReferenceAudio | None:
    check_key(key)
    user, username = _actor(actor)
    with transaction.atomic():
        current = ReferenceAudio.objects.select_for_update().filter(key=key, retired_at__isnull=True).first()
        if current is None:
            return None
        current.retired_at, current.retired_by, current.retired_by_username = timezone.now(), user, username
        current.retire_reason = str(reason)[:500]
        current.save(update_fields=["retired_at", "retired_by", "retired_by_username", "retire_reason"])
        return current


def resolve(key: str) -> ResolvedReference:
    """Open the current audio for ``key`` and prove its bytes are the pinned
    ones. Raises ReferenceAudioError: reference_missing, reference_purged,
    reference_not_valid, reference_hash_mismatch."""
    check_key(key)
    current = ReferenceAudio.objects.select_related("media").filter(key=key, retired_at__isnull=True).first()
    if current is None:
        raise ReferenceAudioError("reference_missing", f"no current reference audio for {key!r}")
    if current.media.sha256 != current.sha256:
        raise ReferenceAudioError("reference_hash_mismatch", f"reference {key!r} no longer names its pinned bytes")
    try:
        opened = media_io.open_media(current.media, require_valid=True)
    except MediaPurged as exc:
        raise ReferenceAudioError("reference_purged", f"the audio for {key!r} has been purged") from exc
    except MediaNotValidated as exc:
        raise ReferenceAudioError("reference_not_valid", f"the audio for {key!r} is not valid") from exc
    except MediaInconsistent as exc:
        raise ReferenceAudioError("reference_hash_mismatch", f"the stored audio for {key!r} is inconsistent") from exc
    handle = opened.file
    try:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
        if digest.hexdigest() != current.sha256:
            raise ReferenceAudioError("reference_hash_mismatch", f"the stored bytes for {key!r} do not match the pin")
        handle.seek(0, os.SEEK_SET)
    except BaseException:
        handle.close()
        raise
    return ResolvedReference(key=key, reference_id=str(current.pk), media=opened.media, sha256=current.sha256,
                             file=handle)
