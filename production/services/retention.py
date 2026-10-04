"""Retention state and the reference-safe purge of media BYTES.

Phase A deliberately ships NO retention policy: nothing here ages anything out,
there is no scheduler and no default lifetime. What exists is the safety
machinery a later policy will call:

* ``find_references`` -- every durable row that points at a ProductionMedia,
  discovered from Django's own relation metadata (not a hand-maintained
  registry), so a future domain that adds a foreign key participates the moment
  its migration lands. ``related_name="+"`` (hidden) relations are included on
  purpose: Django's public ``related_objects`` omits them, which would let a
  careless domain silently opt out of protection. Every reverse relation counts
  whatever its ``on_delete``: purging removes BYTES, not rows, so SET_NULL and
  CASCADE referrers still depend on those bytes.
* ``purge_media`` -- refuses while anything references the media; otherwise
  marks the row ``purged`` (with ``purged_at``) and then removes the bytes.

THE BINDING RULE (mandatory for every reference to ProductionMedia)
------------------------------------------------------------------
Any code that creates or changes a durable foreign key / reference to a
ProductionMedia MUST, inside the SAME database transaction that writes the
reference, first call ``lock_for_binding(media)`` and use the row it returns:

    with transaction.atomic():
        media = retention.lock_for_binding(media_id)   # locks + re-reads + proves present/valid
        voice_track.media = media                      # (Phase B: VoiceTrack.media)
        voice_track.save()

Why a plain FK is NOT enough: Django creates foreign keys DEFERRABLE INITIALLY
DEFERRED, so a referrer INSERT that has not committed yet holds no lock on the
media row and is invisible to a purge (proven by
test_retention.PurgeRaceTests). ``lock_for_binding`` takes the same row lock as
``purge_media``: either the purge waits and then sees the committed reference
and refuses, or the bind waits, sees the purge, and refuses. Inside production
the only reference creator, ``intake.ingest_derivative`` (``derived_from``),
follows this rule. A future consumer must route every binding through one
service that does the same -- never a naked FK assignment in a view or model.
References through GenericForeignKey, or from another database, are NOT
supported and must not be used: they are invisible to ``find_references``.
``reconcile.find_purged_media_still_referenced`` reports any violation.

Crash ordering: the state change commits first; the bytes are unlinked after
the commit (``transaction.on_commit``). A crash in between leaves a purged row
with bytes still on disk -- harmless, and swept by the reconciler. The reverse
order would leave a ``present`` row with no bytes, which is the one state this
substrate must never produce. Metadata, hashes and provenance outlive the
bytes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from .. import transitions
from ..errors import MediaInconsistent, MediaNotValidated, MediaPurged, PurgeRefused
from ..models import ProductionMedia
from . import layout


@dataclass(frozen=True)
class Reference:
    model_label: str
    field_name: str
    count: int


def _reverse_relations():
    for relation in ProductionMedia._meta.get_fields(include_hidden=True):
        if relation.auto_created and not relation.concrete and (
            relation.one_to_many or relation.one_to_one or relation.many_to_many
        ):
            yield relation


def find_references(media) -> list[Reference]:
    """Every model that currently references ``media`` (counts only)."""
    pk = getattr(media, "pk", media)
    found = []
    for relation in _reverse_relations():
        related = relation.related_model
        field_name = relation.field.name
        # _base_manager: include rows a domain's default manager might hide
        # (soft-deleted etc.) -- they are still references.
        count = related._base_manager.filter(**{field_name: pk}).count()
        if count:
            found.append(Reference(related._meta.label, field_name, count))
    return found


def _unlink_bytes(storage_key: str) -> None:
    try:
        os.unlink(layout.resolve_storage_path(storage_key))
    except FileNotFoundError:
        pass
    except OSError:
        pass        # the purged row stays correct; the reconciler removes leftovers


def purge_media(media, *, now=None) -> ProductionMedia:
    """Remove a media's bytes, keeping its row as evidence. Idempotent.

    Raises PurgeRefused (with reference counts) while anything references it."""
    pk = getattr(media, "pk", media)
    with transaction.atomic():
        # of=("self",): the FK to owner/derived_from is nullable, so Postgres
        # refuses FOR UPDATE across the outer join otherwise.
        row = ProductionMedia.objects.select_for_update(of=("self",)).get(pk=pk)
        if row.retention_state == ProductionMedia.RETENTION_PURGED:
            storage_key = row.storage_key
            transaction.on_commit(lambda: _unlink_bytes(storage_key))      # repair leftovers
            return row
        # Under the row lock: any domain that followed the lock_for_binding
        # contract has either committed its reference (visible here) or is
        # waiting for this transaction (and will then see the purge).
        references = find_references(row)
        if references:
            raise PurgeRefused(
                "media is still referenced and its bytes cannot be purged",
                references=[(ref.model_label, ref.field_name, ref.count) for ref in references],
            )
        if transitions.mark_purged(pk, now or timezone.now()) != 1:
            # Impossible under the row lock; never pretend it happened.
            raise MediaInconsistent("purge transition did not apply", code="purge_not_applied")
        storage_key = row.storage_key
        transaction.on_commit(lambda: _unlink_bytes(storage_key))
    return ProductionMedia.objects.get(pk=pk)


def lock_for_binding(media, *, require_valid=True) -> ProductionMedia:
    """THE canonical binding lock (see "THE BINDING RULE" above).

    Call inside the ``transaction.atomic()`` that writes the reference, and
    reference the returned row. Takes the same row lock purge_media takes,
    RE-READS the authoritative row (a stale in-memory object passed in is used
    only for its identity), and refuses a purged -- or, by default, not-valid --
    media with MediaPurged / MediaNotValidated."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("lock_for_binding must be called inside transaction.atomic()")
    try:
        row = ProductionMedia.objects.select_for_update(of=("self",)).get(pk=getattr(media, "pk", media))
    except ProductionMedia.DoesNotExist as exc:
        raise MediaInconsistent("no such production media", code="unknown_media") from exc
    if row.retention_state != ProductionMedia.RETENTION_PRESENT:
        raise MediaPurged("media bytes have been purged")
    if require_valid and not row.is_valid:
        raise MediaNotValidated("media has not been validated as valid")
    return row
