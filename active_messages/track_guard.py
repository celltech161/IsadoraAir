"""Track claims on Active Messages' protected files (D3E-C reviews 3, the
quarantine ownership correction and the final guard authorization
correction).

A library Track REFERS to a file through ``filepath`` (its audio) and through
``waveform_path`` (its waveform). Retention removes the protected
active-message files -- ``LIBRARY_ROOT/UrgentPA/am-<32 hex>.flac``, a
publication's waveform ``WAVEFORMS_DIR/<track id snapshot>.json`` and anything
held in a cleanup quarantine ``<dir>/.am-quarantine/`` -- after deciding,
under the publication's destination lock (``playout._destination_lock``),
that no Track refers to them, and keeps that lock until the verified
deletion. Every supported writer that could create such a reference is
therefore coordinated with that decision. This guard (registered with
``library.models.register_track_write_guard``, the mechanism spoken's
projection uses) judges every write of EITHER field on every supported ORM
path -- save / create / update_or_create, bulk_create (including its
conflict-update upsert), QuerySet.update and bulk_update:

1. The written value must be a LITERAL path: a string (or path object), or a
   ``Value`` wrapping a plain string, which is judged exactly as that string.
   Any other ORM expression (``F``, ``Case``/``When``, ``Concat``, ``Cast``,
   ``Coalesce``, ``Subquery``, ``RawSQL``, combinations, nested values) is
   computed by the database when the statement runs and cannot be judged
   beforehand: it is REFUSED for these two fields (other columns may use
   expressions freely). No supported workflow writes either field through an
   expression.
2. The literal is judged by where it RESOLVES (directory and file symlinks,
   ``.`` and ``..``):

   * into a cleanup quarantine: refused, always;
   * a reserved publication destination, or a publication's waveform name:
     permitted only while THIS thread holds that publication's destination
     lock -- and then only if the file is there NOW (re-validated under the
     lock: a writer that waited for a cleanup never refers to what it removed).
     The holder commits before releasing the lock (publish registers its Track
     in ``atomic(durable=True)`` inside it);
   * the single lock-free exception: an UPDATE of the live publication's own
     Track -- the row the publication's foreign key still points to (a
     retired or pruned publication's key is NULL) -- writing that
     publication's own file name. Such a write can never create a row (a save
     is forced to be an update; a QuerySet update is accepted only when it
     filters on exactly that primary key), so it cannot recreate a deleted
     Track. A Track id merely EQUAL to ``track_id_snapshot`` -- historical
     evidence, not authority -- never permits anything.

Paths elsewhere, empty values and other columns are untouched; the library
schema is unchanged. Writes through Django's base QuerySet, raw SQL, and
later filesystem changes bypass every application hook and are outside the
supported contract (privileged writers of the station account): cleanup
re-checks every Track reference itself (``retention.track_references``) and
never treats such a reference as absent.
"""
from __future__ import annotations

import os
import re
import stat
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


def _refuse(message):
    from library.models import TrackWriteRefused
    raise TrackWriteRefused(message)


# -- 1. literal values only ----------------------------------------------------------------------------

def _literal(value, field: str) -> str | None:
    """The literal path a write stores in ``field`` ("" and None: nothing).
    Anything the database would compute is refused, never guessed."""
    from django.db.models import Value
    if value is None:
        return None
    if type(value) is Value:                        # exactly Value (a subclass may render other SQL)
        value = value.value
        if value is None:
            return None
    if hasattr(value, "resolve_expression"):
        _refuse(f"{field} may only be written as a literal path, not as an ORM expression "
                f"({type(value).__name__}): its target cannot be authorized before the database computes it")
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str):
        _refuse(f"{field} must be a path string, not {type(value).__name__}")
    return value or None


# -- 2. what the literal resolves to ---------------------------------------------------------------------

def _protection(path: str):
    """None, or (kind, destination, snapshot): kind "quarantine", "reserved"
    (an ``am-<hex>.flac``) or "waveform" (a publication's ``<snapshot>.json``);
    destination is the publication destination whose lock guards it."""
    try:
        resolved = os.path.realpath(path)
    except (OSError, ValueError):
        _refuse("the path cannot be resolved")
    if in_quarantine(resolved):
        return ("quarantine", None, None)
    destination = reserved_destination(path) or reserved_destination(resolved)
    if destination is not None:
        return ("reserved", destination, None)
    name = os.path.basename(resolved)
    if (_WAVEFORM_NAME.fullmatch(name)
            and os.path.dirname(resolved) in {str(_waveforms_dir()), os.path.realpath(_waveforms_dir())}):
        from .models import ActiveBroadcastPublication
        snapshot = int(name[:-5])
        relpath = (ActiveBroadcastPublication.objects.filter(track_id_snapshot=snapshot)
                   .values_list("library_relpath", flat=True).first())
        if relpath is not None:
            return ("waveform", playout._library_root() / relpath, snapshot)
    return None


def _own_live_track(protection, pk) -> bool:
    """Is ``pk`` the row the publication's foreign key still points to, for
    this very file? (The snapshot alone never answers yes.)"""
    from .models import ActiveBroadcastPublication
    kind, destination, snapshot = protection
    if pk is None:
        return False
    if kind == "reserved":
        return ActiveBroadcastPublication.objects.filter(
            library_relpath=f"{playout.CATEGORY_CODE}/{destination.name}", track_id=pk).exists()
    return pk == snapshot and ActiveBroadcastPublication.objects.filter(
        track_id_snapshot=snapshot, track_id=pk).exists()


def _authorize(field, path, protection, *, own_pk=None) -> bool:
    """Raise TrackWriteRefused unless this new reference is synchronized with
    cleanup. True when it is admitted only as an update of ``own_pk``'s
    existing row (the caller makes sure no row can be created)."""
    kind, destination, _snapshot = protection
    if kind == "quarantine":
        _refuse(f"{field}: a Track may never refer to an object held in the active-message cleanup quarantine")
    if playout.holds_destination_lock(destination):
        # Re-validated under the lock: what a waiting writer saw before may be gone.
        try:
            present = stat.S_ISREG(os.stat(path).st_mode)
        except OSError:
            present = False
        if not present:
            _refuse(f"{field}: {os.path.basename(path)} does not exist (removed by cleanup?); "
                    "a Track may not refer to it")
        return False
    if own_pk is not None and _own_live_track(protection, own_pk):
        return True
    _refuse(f"{field}: {os.path.basename(path)} is an active-message publication file: a Track may refer to it "
            "only under that publication's destination lock")


# -- entry points (library.models.register_track_write_guard) ----------------------------------------------

def check_track_save(track, update_fields):
    """Every save / create / update_or_create / bulk_create of a Track.
    Returns ``library.models.UPDATE_ONLY`` when the write is admitted only as
    an update of the existing row (Track.save then forces an UPDATE)."""
    from library.models import UPDATE_ONLY
    adding = track._state.adding or track.pk is None
    update_only = False
    for field in FIELDS:
        if update_fields is not None and field not in update_fields:
            continue
        path = _literal(getattr(track, field, None), field)
        if path is None:
            continue
        protection = _protection(path)
        if protection is None:
            continue
        update_only |= _authorize(field, path, protection, own_pk=None if adding else track.pk)
    return UPDATE_ONLY if update_only else None


def _single_pk(queryset):
    """The primary key a QuerySet.update targets when it filters on exactly
    one ``pk = <int>`` and nothing else (no join, no slice, no union) --
    otherwise None (the rows it reaches cannot be known beforehand)."""
    from django.db.models.lookups import Exact
    query = queryset.query
    where = query.where
    if (where.negated or len(where.children) != 1 or query.combinator or query.is_sliced
            or len(query.alias_map) > 1):
        return None
    lookup = where.children[0]
    target = getattr(getattr(lookup, "lhs", None), "target", None)
    if (not isinstance(lookup, Exact) or target is None or not target.primary_key
            or target.model is not queryset.model):
        return None
    value = lookup.rhs
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _rows_by_pk(value):
    """[(pk, then)] for exactly ``Case(When(pk=<int>, then=...), ...)`` with a
    NULL/empty default -- the per-row statement Django's own bulk_update
    issues (each value can only land on the row with that primary key; on
    PostgreSQL it wraps the Case in a Cast to the field's type) -- else None."""
    from django.db.models import Case, Q, Value, When
    from django.db.models.functions import Cast
    if type(value) is Cast:
        value = value.get_source_expressions()[0]
    if type(value) is not Case:
        return None
    default = value.default
    if not (type(default) is Value and default.value in (None, "")):
        return None
    rows = []
    for when in value.cases:
        condition = getattr(when, "condition", None)
        if (type(when) is not When or not isinstance(condition, Q) or condition.negated
                or len(condition.children) != 1 or not isinstance(condition.children[0], tuple)):
            return None
        key, pk = condition.children[0]
        if key not in ("pk", "id", "pk__exact", "id__exact") or not isinstance(pk, int) or isinstance(pk, bool):
            return None
        rows.append((pk, when.result))
    return rows


def check_track_update(queryset, columns: dict) -> None:
    pk = None
    for field in FIELDS:
        if field not in columns:
            continue
        rows = _rows_by_pk(columns[field])
        if rows is not None:                                   # bulk_update's own per-row CASE
            for row_pk, then in rows:
                path = _literal(then, field)
                protection = None if path is None else _protection(path)
                if protection is not None:
                    _authorize(field, path, protection, own_pk=row_pk)
            continue
        path = _literal(columns[field], field)
        if path is None:
            continue
        protection = _protection(path)
        if protection is None:
            continue
        if pk is None:
            pk = _single_pk(queryset)
        _authorize(field, path, protection, own_pk=pk)         # an UPDATE never creates a row


def check_track_bulk_update(objs, fields) -> None:
    for field in FIELDS:
        if field not in fields:
            continue
        for obj in objs:
            path = _literal(getattr(obj, field, None), field)
            if path is None:
                continue
            protection = _protection(path)
            if protection is not None:
                _authorize(field, path, protection, own_pk=obj.pk)   # bulk_update only UPDATEs
