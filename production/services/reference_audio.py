"""Station-managed reference audio (beds, intros): key -> pinned retained media.

The ONLY way a render recipe reaches reference audio, and the ONLY authority
that creates or retires a registration (production.models refuses every
generic ORM write). A key is a slug, never a path; it resolves to the one
current ReferenceAudio row, whose media must be present and valid and whose
bytes must still hash to the SHA-256 pinned at registration.

Serialization: every registration, retirement and completion-time pin read of
a key takes a transaction-scoped PostgreSQL advisory lock derived from the key
(``(namespace, first 32 bits of SHA-256("...reference_audio\\0" + key))`` --
stable across processes, never Python's randomized ``hash()``). Registration
and retirement take it exclusively, so same-key changes -- including the very
first registration, when no row exists to lock -- run one after another and
never fall through to the partial unique constraint (which remains as defense
in depth). ``current_pin`` takes it SHARED, so a render completion that reads
the current pin cannot interleave with a replacement. A hash collision only
serializes two unrelated keys; it never weakens safety. The lock is released
automatically at commit/rollback.

    register(key, media)   lock key -> current row -> lock_for_binding(media)
                           -> kind/validity/bytes-hash -> retire current ->
                           insert new (one transaction; a failed insert leaves
                           the former registration current)
    retire(key)            lock key -> retire the current row (idempotent:
                           returns None when nothing is current)

Registrations are never deleted: a retired row keeps its bed media retained
(PROTECT) as provenance. That low-volume cost is intentional.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from ..errors import MediaInconsistent, MediaNotValidated, MediaPurged
from ..models import REFERENCE_KEY_PATTERN, ProductionMedia, ReferenceAudio, reference_registration
from . import media_io, retention

_KEY_RE = re.compile(REFERENCE_KEY_PATTERN)
_HASH_CHUNK = 1024 * 1024
# Two-int advisory-lock key space: (namespace, per-key id). "REFA" as an int32.
LOCK_NAMESPACE = 0x52454641
_LOCK_SALT = b"isadoraair.production.reference_audio\0"


class ReferenceAudioError(Exception):
    """A reference cannot be registered or resolved. ``code`` is stable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def pin_of(row: ReferenceAudio) -> dict:
    """THE provenance identity of a registration, as render provenance records
    it: which registration, which media, which pinned bytes."""
    return {"key": row.key, "reference": str(row.pk), "media": str(row.media_id), "sha256": row.sha256}


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


def key_lock_id(key: str) -> int:
    """Deterministic signed 32-bit id for ``key`` (stable across processes)."""
    digest = hashlib.sha256(_LOCK_SALT + check_key(key).encode("ascii")).digest()
    return int.from_bytes(digest[:4], "big", signed=True)


def lock_key(key: str, *, shared: bool = False) -> None:
    """Take the key's transaction-scoped advisory lock (exclusive or shared)."""
    connection = transaction.get_connection()
    if not connection.in_atomic_block:
        raise RuntimeError("reference-audio key locks are taken inside transaction.atomic()")
    if connection.vendor != "postgresql":
        raise RuntimeError("reference-audio key serialization needs PostgreSQL advisory locks")
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {function}(%s, %s)", [LOCK_NAMESPACE, key_lock_id(key)])


def _current_locked(key):
    return ReferenceAudio.objects.select_for_update(of=("self",)).filter(key=key, retired_at__isnull=True).first()


def _retire_row(row, *, user, username, reason, now):
    """The ONE mutation of a registration: a state-qualified UPDATE of exactly
    the retirement columns (the guarded queryset refuses generic updates;
    like production.transitions, this vetted path calls the plain one)."""
    count = QuerySet.update(
        ReferenceAudio.objects.filter(pk=row.pk, retired_at__isnull=True),
        retired_at=now, retired_by=user, retired_by_username=username, retire_reason=str(reason)[:500],
    )
    if count != 1:                                      # impossible under the key lock; fail closed
        raise ReferenceAudioError("reference_changed", "the current registration changed concurrently")


def register(key: str, media, *, actor=None, label: str = "", note: str = "") -> ReferenceAudio:
    """Make ``media`` (a present, valid ``bed`` ProductionMedia whose stored
    bytes still hash to its SHA-256) the current audio for ``key``, retiring
    whatever was current before. Registering the media that is ALREADY current
    for the key is idempotent: the current row is returned, nothing is
    written."""
    check_key(key)
    user, username = _actor(actor)
    with transaction.atomic():
        lock_key(key)
        current = _current_locked(key)
        try:
            row = retention.lock_for_binding(media, require_valid=True)        # THE binding rule
        except (MediaPurged, MediaNotValidated, MediaInconsistent) as exc:
            raise ReferenceAudioError("reference_media_unusable", "reference audio must be present and valid") from exc
        if row.kind != ProductionMedia.KIND_BED:
            raise ReferenceAudioError("reference_media_kind", "reference audio must be a 'bed' production media")
        try:
            media_io.verify_integrity(row)
        except (MediaPurged, MediaInconsistent) as exc:
            raise ReferenceAudioError("reference_hash_mismatch",
                                      "the stored bed bytes do not match their SHA-256") from exc
        if current is not None and current.media_id == row.pk and current.sha256 == row.sha256:
            return current
        if current is not None:
            _retire_row(current, user=user, username=username, now=timezone.now(),
                        reason=f"replaced by a new registration of {key}")
        with reference_registration(key=key, media_id=row.pk, sha256=row.sha256):
            return ReferenceAudio.objects.create(
                key=key, media=row, sha256=row.sha256, label=str(label)[:200], note=str(note)[:500],
                registered_by=user, registered_by_username=username,
            )


def retire(key: str, *, actor=None, reason: str = "") -> ReferenceAudio | None:
    """Retire the current registration for ``key``. Idempotent: with nothing
    current it returns None and writes nothing."""
    check_key(key)
    user, username = _actor(actor)
    with transaction.atomic():
        lock_key(key)
        current = _current_locked(key)
        if current is None:
            return None
        _retire_row(current, user=user, username=username, reason=reason, now=timezone.now())
        return ReferenceAudio.objects.get(pk=current.pk)


def current_pin(key: str) -> dict:
    """The current registration's provenance identity (``pin_of``), read under
    the key's SHARED lock -- for render completion, which must compare a
    rendition's provenance with the reference that is current NOW and commit
    before any replacement can. Call inside transaction.atomic()."""
    check_key(key)
    lock_key(key, shared=True)
    current = ReferenceAudio.objects.select_related("media").filter(key=key, retired_at__isnull=True).first()
    if current is None:
        raise ReferenceAudioError("reference_missing", f"no current reference audio for {key!r}")
    if current.media.sha256 != current.sha256:
        raise ReferenceAudioError("reference_hash_mismatch", f"reference {key!r} no longer names its pinned bytes")
    return pin_of(current)


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
    pin = pin_of(current)
    return ResolvedReference(key=key, reference_id=pin["reference"], media=opened.media, sha256=pin["sha256"],
                             file=handle)
