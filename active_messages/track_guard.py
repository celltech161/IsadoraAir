"""Track ownership of the reserved native publication pathnames (D3E-C review 3).

A library Track OWNS the file at its ``filepath``. For the reserved
active-message pathnames ``LIBRARY_ROOT/UrgentPA/am-<32 hex>.flac`` that
ownership must be coordinated with the publication destination lock
(``playout._destination_lock``), because retirement cleanup removes those
files: cleanup decides "no Track owns this path" and unlinks while holding
that lock, so every writer that could make a Track own the path must hold the
same lock -- and commit before releasing it (publish's Track registration is
``atomic(durable=True)`` inside the lock).

This guard (registered with library.models.register_track_write_guard, the
mechanism spoken's projection uses for its reserved pathnames) therefore
refuses -- on every ORM path: save / create / update_or_create, bulk_create,
QuerySet.update and bulk_update -- creating a Track at a reserved path, or
re-pointing a Track's filepath to one, unless THIS thread holds that path's
destination lock. Aliases are the same path: the directory is compared by
its real path. Every other Track write -- including edits of an existing
active-message Track's other columns (analysis, waveform, title) -- is
untouched. Writes through Django's base QuerySet bypass every guard by
convention (as for spoken and ProductionMedia) and are out of contract;
cleanup still re-checks ownership itself, so even such a bypass cannot make
it remove a Track's file.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import playout


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


def _require_lock(destination: Path) -> None:
    from library.models import TrackWriteRefused
    if not playout.holds_destination_lock(destination):
        raise TrackWriteRefused(
            f"{destination.name} is reserved for active-message publication: a Track may be registered "
            "there only by the publication (under its destination lock)")


def check_track_save(track, update_fields) -> None:
    destination = reserved_destination(getattr(track, "filepath", None))
    if destination is None:
        return
    if track._state.adding or track.pk is None:
        _require_lock(destination)
        return
    if update_fields is not None and "filepath" not in update_fields:
        return
    current = type(track)._base_manager.filter(pk=track.pk).values_list("filepath", flat=True).first()
    if current is None or reserved_destination(current) != destination:
        _require_lock(destination)                # newly pointing this Track at a reserved path


def check_track_update(queryset, columns: dict) -> None:
    if "filepath" in columns:
        destination = reserved_destination(columns["filepath"])
        if destination is not None:
            _require_lock(destination)


def check_track_bulk_update(objs, fields) -> None:
    if "filepath" in fields:
        for obj in objs:
            destination = reserved_destination(getattr(obj, "filepath", None))
            if destination is not None:
                _require_lock(destination)
