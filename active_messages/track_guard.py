"""Track claims on Active Messages' reserved files (D3E-C reviews 3 and the
quarantine ownership correction).

A library Track REFERS to a file through ``filepath`` (its audio) and through
``waveform_path`` (its waveform). Retention removes the reserved active-message
files -- ``LIBRARY_ROOT/UrgentPA/am-<32 hex>.flac``, a retired publication's
waveform ``WAVEFORMS_DIR/<track id snapshot>.json`` and anything held in a
cleanup quarantine ``<dir>/.am-quarantine/`` -- after deciding, under the
publication's destination lock (``playout._destination_lock``), that no Track
refers to them. Every supported writer that could create such a reference must
therefore be coordinated with that decision. This guard (registered with
``library.models.register_track_write_guard``, the mechanism spoken's
projection uses) does so on every ORM path -- save / create /
update_or_create, bulk_create, QuerySet.update and bulk_update -- for BOTH
fields, judging the path a write stores by where it RESOLVES (directory and
file symlinks, ``.`` and ``..``):

* into a cleanup quarantine: refused, always -- nothing legitimately refers
  to a quarantined object, so no lock is needed and none can be raced;
* to a reserved publication destination: only while THIS thread holds that
  destination's lock (publish holds it, and commits its Track before
  releasing it: ``atomic(durable=True)``);
* to the waveform name of a publication's Track id, by a Track that is not
  that id: only under that publication's destination lock. A Track writing
  its OWN ``<id>.json`` (every analysis) is untouched.

Only NEW claims are judged (a value a save does not change is not one); edits
of other columns are untouched; paths elsewhere are untouched. Writes through
Django's base QuerySet, raw SQL, and later filesystem changes (a symlink made
after the write) bypass the guard and are out of contract -- cleanup re-checks
every Track reference itself (``retention.track_references``) and never
treats such a reference as absent.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from django.conf import settings

from . import playout

FIELDS = ("filepath", "waveform_path")
QUARANTINE = ".am-quarantine"                     # active_messages.quarantine.ROOT (not imported: no cycle)
_WAVEFORM_NAME = re.compile(r"[0-9]{1,19}\.json")


def reserved_destination(filepath) -> Path | None:
    """The canonical publication destination ``filepath`` names, or None
    when it is not a reserved native pathname."""
    if not isinstance(filepath, str) or not filepath:
        return None
    name = os.path.basename(filepath)
    if not playout._NATIVE_NAME.fullmatch(name):
        return None
    namespace = playout._library_root() / playout.CATEGORY_CODE
    try:
        directory = os.path.realpath(os.path.dirname(os.path.abspath(filepath)))
    except (OSError, ValueError):
        return namespace / name                  # unknowable: treat as reserved (fail closed)
    if directory not in (str(namespace), os.path.realpath(namespace)):
        return None
    return namespace / name


def _waveforms_dir() -> Path:
    return Path(getattr(settings, "WAVEFORMS_DIR", "/srv/isadoraair/waveforms"))


def _quarantine_roots() -> list:
    roots = []
    for base in (playout._library_root() / playout.CATEGORY_CODE, _waveforms_dir()):
        root = base / QUARANTINE
        roots.extend({str(root), os.path.realpath(root)})
    return roots


def in_quarantine(resolved: str) -> bool:
    return any(resolved == root or resolved.startswith(root + os.sep) for root in _quarantine_roots())


def _publication_waveform(resolved: str, *, own_pk=None, queryset=None) -> Path | None:
    """The destination whose lock a claim of a publication's waveform name
    needs, or None (not such a name, or the claiming Track IS that id)."""
    name = os.path.basename(resolved)
    if not _WAVEFORM_NAME.fullmatch(name):
        return None
    if os.path.dirname(resolved) not in {str(_waveforms_dir()), os.path.realpath(_waveforms_dir())}:
        return None
    snapshot = int(name[:-5])
    if queryset is None and own_pk == snapshot:
        return None                                # a Track's own waveform
    from .models import ActiveBroadcastPublication
    relpath = (ActiveBroadcastPublication.objects.filter(track_id_snapshot=snapshot)
               .values_list("library_relpath", flat=True).first())
    if relpath is None:
        return None
    if queryset is not None and not queryset.exclude(pk=snapshot).exists():
        return None                                # updating only the Track that IS that id
    return playout._library_root() / relpath


def _refuse(message):
    from library.models import TrackWriteRefused
    raise TrackWriteRefused(message)


def _require_lock(destination: Path) -> None:
    if not playout.holds_destination_lock(destination):
        _refuse(f"{destination.name} is reserved for active-message publication: a Track may refer to it only "
                "under its destination lock (the publication's)")


def _decision(value, *, own_pk=None, queryset=None):
    """What a new reference ``value`` needs: None (nothing), ("refuse",
    message) or ("lock", destination). No query unless the path resolves to a
    publication waveform name another Track would claim."""
    if not isinstance(value, str) or not value:
        return None
    try:
        resolved = os.path.realpath(value)
    except (OSError, ValueError):
        return ("refuse", "the path cannot be resolved")
    if in_quarantine(resolved):
        return ("refuse", "a Track may never refer to an object held in the active-message cleanup quarantine")
    destination = reserved_destination(value) or reserved_destination(resolved)
    if destination is None:
        destination = _publication_waveform(resolved, own_pk=own_pk, queryset=queryset)
    return None if destination is None else ("lock", destination)


def _enforce(decision) -> None:
    if decision is None:
        return
    kind, detail = decision
    if kind == "refuse":
        _refuse(detail)
    _require_lock(detail)


def check_track_save(track, update_fields) -> None:
    adding = track._state.adding or track.pk is None
    current = None
    for field in FIELDS:
        if update_fields is not None and field not in update_fields:
            continue
        value = getattr(track, field, None)
        decision = _decision(value, own_pk=track.pk)
        if decision is None:
            continue
        if not adding:
            if current is None:
                current = type(track)._base_manager.filter(pk=track.pk).values(*FIELDS).first() or {}
            if current.get(field) == value:
                continue                          # unchanged: not a new claim
        _enforce(decision)


def check_track_update(queryset, columns: dict) -> None:
    for field in FIELDS:
        if field in columns:
            _enforce(_decision(columns[field], queryset=queryset))


def check_track_bulk_update(objs, fields) -> None:
    for field in FIELDS:
        if field in fields:
            for obj in objs:
                _enforce(_decision(getattr(obj, field, None), own_pk=obj.pk))
