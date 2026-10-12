"""Keep ``spoken.transitions`` the ONLY authority over item lifecycle.

There is no trusted context. A protected write happens only as the exact
execution of an active, single-use authorization ("grant"):

* ``ItemCreate``  -- one new item: its id and every planned column value
  (all other columns take the model default);
* ``ItemWrite``   -- one existing item: its id, the state it must be in, the
  state it moves to, and the exact value of every protected column it
  changes;
* ``EventCreate`` -- one event: every column (item, event, from/to state,
  actor kind/id/snapshot, media, asset, edition, detail).

The transition service opens a grant (``_authorized``, private) around one
call to ``_execute()``, which takes NO data: it writes exactly the grant's
own immutable payload with a single ORM statement that emits no model
signals and exposes no model instance to application code (base
``QuerySet.update`` filtered by id AND source state; base
``QuerySet.bulk_create`` of an instance built from the payload), then reads
the row back and refuses -- rolling the transaction back -- unless it equals
the payload. So nothing between authorization and SQL (``pre_save``
receivers, descriptors, model hooks) can change what is written, and
``Model.save()`` never honors a grant at all.

A grant is itself checked against the fixed transition table (spoken.lifecycle):
it can never describe a C2 (library) transition, a column its rule does not
write, or an event the rule does not record -- so even a hand-built grant
cannot deliver, forge failure fields onto a submission or claim a delivery. A
grant is bound to the thread and asyncio task that opened it, is consumed by
its execution, cannot be nested, and is always removed on exit (``finally``).

Everywhere else -- views, admin, shells, other apps, and code running while
a grant is open -- every ordinary ORM path refuses. The guarded querysets are
the default AND the base managers (``Meta.base_manager_name``), and every
reverse related manager (``content_type.items``, ``item.events``) is built
from them:

* ``bulk_create()`` (every form), ``save()``/``create()`` of a new row and the
  creating branch of ``get_or_create()``/``update_or_create()`` refuse always;
* an ordinary ``save()`` of an existing item may change only the editorial
  columns, and WRITES only them (``update_fields`` is narrowed to title/notes),
  so a ``pre_save`` receiver cannot smuggle a protected value into its SQL;
  ``QuerySet.update()`` and ``bulk_update()`` may change only the editorial
  columns (and Django's SET_NULL of a deleted user) -- grant or not;
* a ``state`` that is not an ItemState is refused on every write path (the
  state set is application authority, deliberately not a database check);
* items and events are never deleted or changed through the ORM (events are
  append-only; no FK into either cascades).

The grant primitives are private implementation details of spoken.transitions,
not an application API; deliberately importing them, or calling Django's base
QuerySet methods directly, is equivalent to raw SQL and outside this
boundary, as for ProductionMedia.
"""
from __future__ import annotations

import asyncio
import contextvars
import datetime
import decimal
import json
import math
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass


class SpokenIntegrityError(Exception):
    """An ordinary write path tried to bypass the spoken-domain services."""


# Free outside the transition service.
ITEM_FREE_COLUMNS = ("title", "notes")
# Set by Django itself as the row is inserted (verified semantically, not by value).
ITEM_GENERATED = ("created_at",)
EVENT_GENERATED = ("id", "created_at")
# Django nulls these itself when a user is deleted (on_delete=SET_NULL).
_USER_FKS = ("submitted_by", "submitted_by_id", "reviewed_by", "reviewed_by_id", "recalled_by", "recalled_by_id")


# -- grants (private) -----------------------------------------------------------------------

@dataclass(frozen=True)
class ItemCreate:
    item_id: object
    values: tuple                 # ((attname, value), ...) -- the planned columns
    expected: tuple               # EVERY authoritative column but the generated ones, as written


@dataclass(frozen=True)
class ItemWrite:
    item_id: object
    from_state: str
    to_state: str
    values: tuple                 # ((attname, value), ...) -- exact protected changes
    expected: tuple               # EVERY authoritative column after the write: locked row + values


@dataclass(frozen=True)
class EventCreate:
    values: tuple                 # ((attname, value), ...) -- every event column but the generated ones


# -- C2B projection grants (private to spoken.delivery) ---------------------------------------

@dataclass(frozen=True)
class ProjectedItemWrite:
    """A C2 item transition (delivered / superseded / delivered-or-superseded
    -> recalled / expired). ``write`` is the exact item write; ``asset_id`` /
    ``edition_id`` name the projection state that WITNESSES it, re-read and
    checked by the executor against the locked database before the write
    (_witness): an item is never marked delivered without its active edition,
    nor recalled/expired while its content is still projected live."""
    write: ItemWrite
    asset_id: object
    edition_id: object


@dataclass(frozen=True)
class ProjectedEventCreate:
    """The event of a C2 transition: written only after its witnessed item
    write (the executor requires the item to already be in ``to_state``)."""
    values: tuple


@dataclass(frozen=True)
class AssetCreate:
    asset_id: object
    values: tuple                 # EVERY authoritative asset column but the generated ones


@dataclass(frozen=True)
class AssetWrite:
    asset_id: object
    match: tuple                  # ((attname, value), ...) the row must still hold (compare-and-write)
    values: tuple                 # ((attname, value), ...) -- exact changes (updated_at included)
    expected: tuple               # EVERY authoritative asset column afterwards


@dataclass(frozen=True)
class EditionCreate:
    edition_id: object
    values: tuple                 # EVERY authoritative edition column but the generated ones


@dataclass(frozen=True)
class EditionWrite:
    edition_id: object
    from_state: str
    values: tuple
    expected: tuple


@dataclass(frozen=True)
class TrackCreate:
    values: tuple                 # EVERY projection-owned Track column


@dataclass(frozen=True)
class TrackWrite:
    track_id: object
    values: tuple                 # projection-owned columns changed (+ updated_at)
    expected: tuple               # EVERY projection-owned Track column afterwards


# -- the canonical column sets ------------------------------------------------------------

def authoritative_item_fields() -> list:
    """THE lifecycle-governed SpokenItem columns: every concrete column except
    the primary key and the editorial title/notes. One definition for the
    ordinary-save guard, plan snapshots, grants and post-write verification."""
    from .models import SpokenItem
    return [field for field in SpokenItem._meta.concrete_fields
            if not field.primary_key and field.name not in ITEM_FREE_COLUMNS]


def authoritative_event_fields() -> list:
    """Every SpokenItemEvent column (an event is wholly authoritative)."""
    from .models import SpokenItemEvent
    return list(SpokenItemEvent._meta.concrete_fields)


def _attnames(fields, exclude=()) -> set:
    return {field.attname for field in fields if field.attname not in exclude}


# The library Track columns a projection OWNS on a Track linked to a
# SpokenAsset: its file identity, scheduling identity, displayed metadata,
# readiness and the media facts/marks the projection derives from the
# validated rendition. Ordinary Track workflows may not change them on a
# projected Track (check_track_*); every other Track column (play counts,
# art cache, rotation weight, waveform...) stays the library's.
PROJECTED_TRACK_FIELDS = ("filepath", "filename", "format", "title", "artist_id", "category_id", "ready2air",
                          "duration_seconds", "sample_rate", "channels", "bit_depth", "cue_in_seconds",
                          "cue_out_seconds", "next_start_seconds", "remote_url")
ASSET_GENERATED = ("created_at", "updated_at")
EDITION_GENERATED = ("created_at",)

# Permitted asset state moves (from, to) -- one table for the executor.
ASSET_MOVES = frozenset({
    ("pending", "pending"), ("pending", "retiring"), ("pending", "live"),
    ("live", "live"), ("live", "retiring"), ("live", "pending"), ("live", "retired"),
    ("retiring", "retiring"), ("retiring", "live"), ("retiring", "pending"),
    ("retiring", "detached"), ("retiring", "retired"),
    ("pending", "retired"), ("pending", "detached"), ("live", "detached"), ("detached", "pending"),
    ("detached", "detached"), ("retired", "retired"),
})
EDITION_MOVES = frozenset({("pending", "active"), ("pending", "aborted"), ("active", "superseded")})


def authoritative_asset_fields() -> list:
    from .models import SpokenAsset
    return [field for field in SpokenAsset._meta.concrete_fields if not field.primary_key]


def authoritative_edition_fields() -> list:
    from .models import SpokenAssetEdition
    return [field for field in SpokenAssetEdition._meta.concrete_fields if not field.primary_key]


def projected_track_fields() -> list:
    from library.models import Track
    return [Track._meta.get_field(name.removesuffix("_id")) for name in PROJECTED_TRACK_FIELDS]


# -- strict canonical JSON --------------------------------------------------------------------

class NotCanonicalJSON(ValueError):
    """A value has no strict JSON meaning (nothing is ever coerced)."""


_JSON_MAX_DEPTH = 32


def _strict_json(value, depth=0):
    if depth > _JSON_MAX_DEPTH:
        raise NotCanonicalJSON("JSON nesting is too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return str.__str__(value)                       # the characters, never a subclass's __str__
    if isinstance(value, int):
        return int.__int__(value)
    if isinstance(value, float):
        number = float.__float__(value)
        if not math.isfinite(number):
            raise NotCanonicalJSON("NaN and infinities are not JSON")
        return number
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise NotCanonicalJSON(f"JSON object keys are strings, not {type(key).__name__}")
            out[str.__str__(key)] = _strict_json(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_strict_json(item, depth + 1) for item in value]
    raise NotCanonicalJSON(f"{type(value).__name__} is not a JSON value")


def canonical_json(value) -> str:
    """Strict, deterministic JSON text: objects with string keys, arrays
    (tuples become arrays), strings, finite numbers, booleans and null only;
    sorted keys, compact separators, ASCII. Anything else -- Decimal, UUID,
    datetime, sets, bytes, arbitrary objects, non-string keys, NaN/Infinity --
    raises NotCanonicalJSON; nothing is stringified."""
    return json.dumps(_strict_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


_IMMUTABLE_SCALARS = (str, int, float, bool, type(None), datetime.datetime, datetime.date, decimal.Decimal,
                      uuid.UUID)


def freeze(field, value):
    """A column value as an immutable scalar: related objects by id, JSON as
    canonical text (None stays SQL NULL)."""
    from django.db.models import JSONField
    if field.is_relation:
        value = getattr(value, "pk", value)
    if isinstance(field, JSONField) and value is not None:
        value = canonical_json(value)
    if not isinstance(value, _IMMUTABLE_SCALARS):
        raise TypeError(f"{field.name} value {type(value).__name__} is not an immutable scalar")
    return value


def snapshot(row) -> tuple:
    """The authoritative columns of a (locked) item row, frozen."""
    return tuple(sorted((field.attname, freeze(field, row.__dict__[field.attname]))
                        for field in authoritative_item_fields()))


def creation_expected(values: tuple) -> tuple:
    """Every authoritative column of a new item: the planned values, and the
    model default for everything else (generated columns excepted)."""
    planned = dict(values)
    return tuple(sorted(
        (field.attname, planned[field.attname] if field.attname in planned else freeze(field, field.get_default()))
        for field in authoritative_item_fields() if field.attname not in ITEM_GENERATED))


class _Grant:
    __slots__ = ("record", "thread", "task", "used")

    def __init__(self, record):
        self.record, self.used = record, False
        self.thread, self.task = threading.get_ident(), _current_task()


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


_grant = contextvars.ContextVar("spoken_write_grant", default=None)


def _check_record(record) -> None:
    """A grant must itself be a write the fixed transition table allows."""
    from . import lifecycle
    if isinstance(record, ItemCreate):
        values = dict(record.values)
        if (set(values) - lifecycle.CREATE_COLUMNS or values.get("state") != "draft"
                or values.get("origin") not in lifecycle.CREATE_ORIGINS):
            raise SpokenIntegrityError("an item is only ever created as a submitted/assigned draft")
        if record.expected != creation_expected(record.values):
            raise SpokenIntegrityError("a creation grant must expect its planned values and the defaults")
        return
    if isinstance(record, ItemWrite):
        rule = lifecycle.TRANSITIONS.get((record.from_state, record.to_state))
        if rule is None or rule.c2:
            raise SpokenIntegrityError(f"{record.from_state} -> {record.to_state} cannot be authorized here")
        values = dict(record.values)
        if values.get("state") != record.to_state:
            raise SpokenIntegrityError("an item write grant must set exactly its target state")
        extra = set(values) - lifecycle.writable_columns(record.from_state, record.to_state)
        if extra:
            raise SpokenIntegrityError(f"{record.from_state} -> {record.to_state} never writes {sorted(extra)}")
        expected = dict(record.expected)
        if set(expected) != _attnames(authoritative_item_fields()) or any(
                expected[name] != value for name, value in values.items()):
            raise SpokenIntegrityError("an item write grant must expect every authoritative column")
        return
    if isinstance(record, EventCreate):
        values = dict(record.values)
        if set(values) != _attnames(authoritative_event_fields(), EVENT_GENERATED):
            raise SpokenIntegrityError("an event grant must give every event column")
        rule = lifecycle.TRANSITIONS.get((values.get("from_state") or None, values.get("to_state")))
        if rule is None or rule.c2 or values.get("event") != rule.event:
            raise SpokenIntegrityError("an event grant must record a permitted, non-projection transition")
        return
    _check_projection_record(record)


def _check_projection_record(record) -> None:
    """The C2B grants: each must itself be a move the projection allows."""
    from . import lifecycle
    if isinstance(record, ProjectedItemWrite):
        write = record.write
        key = (write.from_state, write.to_state)
        rule = lifecycle.TRANSITIONS.get(key)
        if not isinstance(write, ItemWrite) or rule is None or not rule.c2 or key not in lifecycle.C2B_PERFORMABLE:
            raise SpokenIntegrityError(f"{write.from_state} -> {write.to_state} is not a performable projection move")
        values = dict(write.values)
        if values.get("state") != write.to_state:
            raise SpokenIntegrityError("an item write grant must set exactly its target state")
        extra = set(values) - lifecycle.writable_columns(write.from_state, write.to_state)
        if extra:
            raise SpokenIntegrityError(f"{write.from_state} -> {write.to_state} never writes {sorted(extra)}")
        expected = dict(write.expected)
        if set(expected) != _attnames(authoritative_item_fields()) or any(
                expected[name] != value for name, value in values.items()):
            raise SpokenIntegrityError("an item write grant must expect every authoritative column")
        if write.to_state == "delivered" and (record.asset_id is None) != (record.edition_id is None):
            raise SpokenIntegrityError("a delivery is witnessed by an asset AND its active edition, or by neither")
        return
    if isinstance(record, ProjectedEventCreate):
        values = dict(record.values)
        if set(values) != _attnames(authoritative_event_fields(), EVENT_GENERATED):
            raise SpokenIntegrityError("an event grant must give every event column")
        key = (values.get("from_state") or None, values.get("to_state"))
        rule = lifecycle.TRANSITIONS.get(key)
        if rule is None or not rule.c2 or key not in lifecycle.C2B_PERFORMABLE or values.get("event") != rule.event:
            raise SpokenIntegrityError("a projection event must record a performable projection transition")
        return
    if isinstance(record, AssetCreate):
        values = dict(record.values)
        if set(values) != _attnames(authoritative_asset_fields(), ASSET_GENERATED):
            raise SpokenIntegrityError("an asset creation grant must give every asset column")
        if (values["state"] != "pending" or values["generation"] != 0 or values["track_id"] is not None
                or values["current_item_id"] is not None or values["current_rendition_id"] is not None
                or values["retired_at"] is not None):
            raise SpokenIntegrityError("an asset is only ever created pending and empty")
        return
    if isinstance(record, AssetWrite):
        match, values = dict(record.match), dict(record.values)
        if "state" not in match or "generation" not in match or "id" in values or "created_at" in values:
            raise SpokenIntegrityError("an asset write compares its state and generation and never its identity")
        if (match["state"], values.get("state", match["state"])) not in ASSET_MOVES:
            raise SpokenIntegrityError(f"an asset never moves {match['state']} -> {values.get('state')}")
        if "generation" in values and values["generation"] < match["generation"]:
            raise SpokenIntegrityError("an asset generation never decreases")
        expected = dict(record.expected)
        if set(expected) != _attnames(authoritative_asset_fields()) or any(
                expected[name] != value for name, value in values.items()):
            raise SpokenIntegrityError("an asset write grant must expect every authoritative column")
        return
    if isinstance(record, EditionCreate):
        values = dict(record.values)
        if set(values) != _attnames(authoritative_edition_fields(), EDITION_GENERATED):
            raise SpokenIntegrityError("an edition creation grant must give every edition column")
        if (values["state"] != "pending" or values["promoted_at"] is not None or values["superseded_at"] is not None
                or values["superseded_by_id"] is not None):
            raise SpokenIntegrityError("an edition is only ever created pending")
        return
    if isinstance(record, EditionWrite):
        values = dict(record.values)
        if (record.from_state, values.get("state")) not in EDITION_MOVES:
            raise SpokenIntegrityError(f"an edition never moves {record.from_state} -> {values.get('state')}")
        if set(values) - {"state", "promoted_at", "superseded_at", "superseded_by_id"}:
            raise SpokenIntegrityError("an edition write changes only its state and its dates")
        expected = dict(record.expected)
        if set(expected) != _attnames(authoritative_edition_fields()) or any(
                expected[name] != value for name, value in values.items()):
            raise SpokenIntegrityError("an edition write grant must expect every authoritative column")
        return
    if isinstance(record, TrackCreate):
        if set(dict(record.values)) != set(PROJECTED_TRACK_FIELDS):
            raise SpokenIntegrityError("a projected Track is created with every projection-owned column")
        return
    if isinstance(record, TrackWrite):
        values = dict(record.values)
        if not set(values) - {"updated_at"} or set(values) - set(PROJECTED_TRACK_FIELDS) - {"updated_at"}:
            raise SpokenIntegrityError("a projected Track write changes projection-owned columns only")
        expected = dict(record.expected)
        if set(expected) != set(PROJECTED_TRACK_FIELDS) or any(
                expected[name] != value for name, value in values.items() if name != "updated_at"):
            raise SpokenIntegrityError("a projected Track write must expect every projection-owned column")
        return
    raise SpokenIntegrityError("unknown write grant")


@contextmanager
def _authorized(record):
    """Authorize exactly ONE write: the one ``record`` describes. Private to
    spoken.transitions. Not nestable; always removed on exit."""
    if _grant.get() is not None:
        raise SpokenIntegrityError("spoken write grants do not nest")
    _check_record(record)
    token = _grant.set(_Grant(record))
    try:
        yield
    finally:
        _grant.reset(token)


def _active(kind):
    """The current grant if it is an unused ``kind`` owned by this thread/task."""
    grant = _grant.get()
    if grant is None or not isinstance(grant.record, kind):
        return None
    if grant.used:
        raise SpokenIntegrityError("this spoken write grant has already been used")
    if grant.thread != threading.get_ident() or grant.task is not _current_task():
        raise SpokenIntegrityError("a spoken write grant is only valid where it was opened")
    return grant


# -- checks --------------------------------------------------------------------------------

def refuse(message) -> None:
    raise SpokenIntegrityError(message)


def check_state_value(value) -> None:
    from .models import ItemState
    if not isinstance(value, str) or value not in ItemState.values:
        raise SpokenIntegrityError(f"{value!r} is not a spoken item state")


def refuse_queryset_update(model_name, columns: dict, *, allowed) -> None:
    blocked = sorted(name for name, value in columns.items()
                     if name not in allowed and not (name in _USER_FKS and value is None))
    if blocked:
        raise SpokenIntegrityError(
            f"{model_name} {blocked} may only change through the spoken services (spoken.transitions)")


def _protected_item_fields(item) -> list:
    return authoritative_item_fields()


def refuse_item_bulk_update(fields) -> None:
    blocked = sorted(set(fields) - set(ITEM_FREE_COLUMNS))
    if blocked:
        raise SpokenIntegrityError(f"SpokenItem.bulk_update of {blocked} is refused; use spoken.transitions")


def _same(field, left, right) -> bool:
    return field.to_python(left) == field.to_python(right)


# -- the exact executor ---------------------------------------------------------------------

def _decode(model, values) -> dict:
    """The frozen payload as ORM values. JSON columns travel as canonical
    text and are decoded only here, into fresh objects no other code holds."""
    from django.db.models import JSONField
    decoded = {}
    for attname, value in values:
        field = model._meta.get_field(attname)
        decoded[attname] = json.loads(value) if isinstance(field, JSONField) and value is not None else value
    return decoded


def _verify_complete(model, pk, fields, expected: dict, *, generated=(), window=None) -> None:
    """Read back EVERY authoritative column of the written row (the canonical
    set -- never just the grant's keys) in the same transaction, and refuse
    -- rolling the transaction back -- unless each equals ``expected``.
    Generated columns are checked for their approved semantics instead: a
    primary key exists; created_at was stamped during the write."""
    names = [field.attname for field in fields]
    if set(expected) != set(names) - set(generated):
        raise SpokenIntegrityError(f"the {model.__name__} expectation does not cover every authoritative column")
    row = model._base_manager.filter(pk=pk).values("pk", *names).first()
    if row is None:
        raise SpokenIntegrityError(f"the authorized {model.__name__} row is missing after its write")
    for field in fields:
        if field.attname in generated:
            continue
        if not _same(field, row[field.attname], expected[field.attname]):
            raise SpokenIntegrityError(f"{model.__name__}.{field.name} differs from the authorized write")
    for stamp in ("created_at", "updated_at"):
        if stamp in generated:
            started, finished = window
            if row[stamp] is None or not (started <= row[stamp] <= finished):
                raise SpokenIntegrityError(f"{model.__name__}.{stamp} was not stamped by the write")


def _take(kind):
    grant = _active(kind)
    if grant is None:
        raise SpokenIntegrityError("no open grant authorizes this write")
    grant.used = True
    return grant.record


def _execute():
    """Perform THE write the open grant describes -- exactly its payload, once
    -- and verify the COMPLETE authoritative row it produced. Takes no data:
    there is nothing a caller can substitute."""
    from django.db import transaction

    grant = _grant.get()
    kind = type(grant.record) if grant is not None else None
    executor = _EXECUTORS.get(kind)
    if executor is None:
        raise SpokenIntegrityError("no open grant authorizes this write")
    record = _take(kind)                    # owner-checked and consumed before anything else
    if not transaction.get_connection().in_atomic_block:
        raise SpokenIntegrityError("authorized spoken writes run inside the transition's transaction")
    return executor(record)


def _exec_item_write(record):
    from django.db.models import QuerySet

    from .models import SpokenItem
    count = QuerySet.update(SpokenItem._base_manager.filter(pk=record.item_id, state=record.from_state),
                            **_decode(SpokenItem, record.values))
    if count != 1:
        raise SpokenIntegrityError("the item is not in the state this write was authorized from")
    _verify_complete(SpokenItem, record.item_id, authoritative_item_fields(), _decode(SpokenItem, record.expected))
    return None


def _exec_item_create(record):
    from django.db.models import QuerySet
    from django.utils import timezone

    from .models import SpokenItem
    started = timezone.now()
    row = SpokenItem(id=record.item_id, **_decode(SpokenItem, record.values))
    QuerySet.bulk_create(SpokenItem._base_manager.all(), [row])
    _verify_complete(SpokenItem, record.item_id, authoritative_item_fields(), _decode(SpokenItem, record.expected),
                     generated=ITEM_GENERATED, window=(started, timezone.now()))
    return row.pk


def _exec_event(record):
    from django.db.models import QuerySet
    from django.utils import timezone

    from .models import SpokenItemEvent
    started = timezone.now()
    (event,) = QuerySet.bulk_create(SpokenItemEvent._base_manager.all(),
                                    [SpokenItemEvent(**_decode(SpokenItemEvent, record.values))])
    if event.pk is None:
        raise SpokenIntegrityError("the authorized event was not assigned an id")
    _verify_complete(SpokenItemEvent, event.pk, authoritative_event_fields(),
                     _decode(SpokenItemEvent, record.values), generated=EVENT_GENERATED,
                     window=(started, timezone.now()))
    return event.pk


def _witness(record) -> None:
    """The projection state a C2 item transition claims, re-read from the
    database inside the transaction (the projection service holds the type,
    item and asset locks): refuse unless it is exactly so."""
    from .models import SpokenAsset, SpokenAssetEdition, SpokenItem
    write = record.write
    item_id = write.item_id
    item = SpokenItem._base_manager.filter(pk=item_id).values(
        "rendition_media_id", "content_type__delivery_mode").first()
    if item is None:
        raise SpokenIntegrityError("the witnessed item does not exist")
    live_elsewhere = SpokenAsset._base_manager.filter(current_item_id=item_id, state="live")
    open_editions = SpokenAssetEdition._base_manager.filter(item_id=item_id, state__in=("pending", "active"))
    if write.to_state == "delivered":
        if record.edition_id is None:
            if item["content_type__delivery_mode"] != "retain_only" or open_editions.exists():
                raise SpokenIntegrityError("only retain-only content is delivered without a projected edition")
            return
        edition = SpokenAssetEdition._base_manager.filter(pk=record.edition_id).values(
            "asset_id", "item_id", "rendition_id", "generation", "state").first()
        asset = SpokenAsset._base_manager.filter(pk=record.asset_id).values(
            "state", "current_item_id", "current_rendition_id", "generation").first()
        if (edition is None or asset is None or edition["state"] != "active"
                or str(edition["item_id"]) != str(item_id) or str(edition["asset_id"]) != str(record.asset_id)
                or edition["rendition_id"] != item["rendition_media_id"] or asset["state"] != "live"
                or str(asset["current_item_id"]) != str(item_id)
                or asset["current_rendition_id"] != edition["rendition_id"]
                or asset["generation"] != edition["generation"]):
            raise SpokenIntegrityError("a delivery is witnessed only by the item's ACTIVE edition on its live asset")
        return
    if write.to_state == "superseded":
        edition = SpokenAssetEdition._base_manager.filter(pk=record.edition_id).values(
            "item_id", "state", "superseded_by_id").first()
        if (edition is None or str(edition["item_id"]) != str(item_id) or edition["state"] != "superseded"
                or edition["superseded_by_id"] is None or live_elsewhere.exists() or open_editions.exists()):
            raise SpokenIntegrityError("an item is superseded only once a newer edition replaced its own")
        return
    # -> recalled / expired: nothing of the item may still be projected
    if live_elsewhere.exists() or open_editions.exists():
        raise SpokenIntegrityError("an item whose content is still projected cannot leave delivery")


def _exec_projected_item(record):
    _witness(record)
    return _exec_item_write(record.write)


def _exec_projected_event(record):
    from .models import SpokenAssetEdition, SpokenItem
    values = dict(record.values)
    state = SpokenItem._base_manager.filter(pk=values["item_id"]).values_list("state", flat=True).first()
    if state != values["to_state"]:
        raise SpokenIntegrityError("a projection event follows its witnessed item write")
    if values["to_state"] == "delivered" and values["edition_id"] is not None and not (
            SpokenAssetEdition._base_manager.filter(pk=values["edition_id"], item_id=values["item_id"],
                                                    state="active").exists()):
        raise SpokenIntegrityError("a delivery event names the item's active edition")
    return _exec_event(record)


def _exec_asset_create(record):
    from django.db.models import QuerySet
    from django.utils import timezone

    from .models import SpokenAsset
    started = timezone.now()
    QuerySet.bulk_create(SpokenAsset._base_manager.all(),
                         [SpokenAsset(id=record.asset_id, **_decode(SpokenAsset, record.values))])
    _verify_complete(SpokenAsset, record.asset_id, authoritative_asset_fields(),
                     _decode(SpokenAsset, record.values), generated=ASSET_GENERATED, window=(started, timezone.now()))
    return record.asset_id


def _exec_asset_write(record):
    from django.db.models import QuerySet

    from .models import SpokenAsset
    match = _decode(SpokenAsset, record.match)
    count = QuerySet.update(SpokenAsset._base_manager.filter(pk=record.asset_id, **match),
                            **_decode(SpokenAsset, record.values))
    if count != 1:
        raise SpokenIntegrityError("the asset changed since this write was authorized (compare-and-write)")
    _verify_complete(SpokenAsset, record.asset_id, authoritative_asset_fields(), _decode(SpokenAsset, record.expected))
    return None


def _exec_edition_create(record):
    from django.db.models import QuerySet
    from django.utils import timezone

    from .models import SpokenAssetEdition
    started = timezone.now()
    QuerySet.bulk_create(SpokenAssetEdition._base_manager.all(),
                         [SpokenAssetEdition(id=record.edition_id, **_decode(SpokenAssetEdition, record.values))])
    _verify_complete(SpokenAssetEdition, record.edition_id, authoritative_edition_fields(),
                     _decode(SpokenAssetEdition, record.values), generated=EDITION_GENERATED,
                     window=(started, timezone.now()))
    return record.edition_id


def _exec_edition_write(record):
    from django.db.models import QuerySet

    from .models import SpokenAssetEdition
    count = QuerySet.update(SpokenAssetEdition._base_manager.filter(pk=record.edition_id, state=record.from_state),
                            **_decode(SpokenAssetEdition, record.values))
    if count != 1:
        raise SpokenIntegrityError("the edition is not in the state this write was authorized from")
    _verify_complete(SpokenAssetEdition, record.edition_id, authoritative_edition_fields(),
                     _decode(SpokenAssetEdition, record.expected))
    return None


def _exec_track_create(record):
    from django.db.models import QuerySet

    from library.models import Track
    (track,) = QuerySet.bulk_create(Track._base_manager.all(), [Track(**_decode(Track, record.values))])
    if track.pk is None:
        raise SpokenIntegrityError("the projected Track was not assigned an id")
    _verify_complete(Track, track.pk, projected_track_fields(), _decode(Track, record.values))
    return track.pk


def _exec_track_write(record):
    from django.db.models import QuerySet

    from library.models import Track
    count = QuerySet.update(Track._base_manager.filter(pk=record.track_id), **_decode(Track, record.values))
    if count != 1:
        raise SpokenIntegrityError("the projected Track does not exist")
    _verify_complete(Track, record.track_id, projected_track_fields(), _decode(Track, record.expected))
    return None


_EXECUTORS = {
    ItemWrite: _exec_item_write, ItemCreate: _exec_item_create, EventCreate: _exec_event,
    ProjectedItemWrite: _exec_projected_item, ProjectedEventCreate: _exec_projected_event,
    AssetCreate: _exec_asset_create, AssetWrite: _exec_asset_write,
    EditionCreate: _exec_edition_create, EditionWrite: _exec_edition_write,
    TrackCreate: _exec_track_create, TrackWrite: _exec_track_write,
}


# -- ordinary ORM paths ----------------------------------------------------------------------

def check_item_save(item, update_fields) -> None:
    """An ordinary save: never a creation, never a protected change."""
    if "state" in item.__dict__ and (update_fields is None or "state" in update_fields):
        check_state_value(item.state)
    if item._state.adding:
        raise SpokenIntegrityError("spoken items are created only by spoken.transitions.create_item")
    fields = _protected_item_fields(item)
    if update_fields is not None:
        fields = [field for field in fields if field.name in update_fields or field.attname in update_fields]
    loaded = [field for field in fields if field.attname in item.__dict__]
    if not loaded:
        return
    current = type(item)._base_manager.filter(pk=item.pk).values(*[f.attname for f in loaded]).first()
    if current is None:
        raise SpokenIntegrityError("refusing to save a spoken item whose row does not exist")
    changed = [field.name for field in loaded if not _same(field, item.__dict__[field.attname],
                                                           current[field.attname])]
    if changed:
        raise SpokenIntegrityError(
            f"SpokenItem {sorted(changed)} may only change through the spoken services (spoken.transitions)")


def editorial_update_fields(update_fields) -> list:
    """What an ordinary item save may actually WRITE: the editorial columns."""
    requested = ITEM_FREE_COLUMNS if update_fields is None else update_fields
    return [name for name in requested if name in ITEM_FREE_COLUMNS]


def check_event_write(event) -> None:
    raise SpokenIntegrityError("spoken item events are written only by spoken.transitions (append-only)")


# -- ordinary ORM paths: projection identity (C2B) ---------------------------------------------

def refuse_projection(model_name) -> None:
    raise SpokenIntegrityError(
        f"{model_name} changes only through the spoken projection service (spoken.delivery)")


def check_asset_queryset_update(queryset, columns: dict) -> None:
    """The one Django-owned asset write: the SET_NULL of a deleted Track --
    and only for rows the Track deletion already DETACHED (or that no longer
    link a Track). Unlinking a live projected Track any other way is refused:
    a projected Track is detached by deleting it (spoken.delivery)."""
    if set(columns) <= {"track", "track_id"} and all(value is None for value in columns.values()):
        if queryset.filter(track__isnull=False).exclude(state__in=("detached", "retired")).exists():
            raise SpokenIntegrityError("a live projected Track is detached by deleting it, never by unlinking it")
        return
    refuse_projection("SpokenAsset")


_OWNED_NAMES = None


def _owned_names() -> frozenset:
    global _OWNED_NAMES
    if _OWNED_NAMES is None:
        _OWNED_NAMES = frozenset(name for field in projected_track_fields() for name in (field.name, field.attname))
    return _OWNED_NAMES


def _projected(track_ids) -> bool:
    from .models import SpokenAsset
    return SpokenAsset._base_manager.filter(track_id__in=[pk for pk in track_ids if pk is not None]).exists()


def _reserved(filepath) -> bool:
    if not isinstance(filepath, str):
        return False
    from . import library_files
    from .models import SpokenAsset
    relpath = library_files.relative(filepath)
    return relpath is not None and SpokenAsset._base_manager.filter(library_relpath=relpath).exists()


def _queryset_uses_reserved_path(queryset) -> bool:
    """One bounded EXISTS; never materialize the growing asset namespace."""
    from django.db.models import CharField, Subquery, Value
    from django.db.models.functions import Concat

    from . import library_files
    from .models import SpokenAsset
    prefix = f"{library_files.library_root()}/"
    paths = (SpokenAsset._base_manager.annotate(
        absolute_path=Concat(Value(prefix), "library_relpath", output_field=CharField()),
    ).values("absolute_path"))
    return queryset.filter(filepath__in=Subquery(paths)).exists()


def _any_reserved(filepaths) -> bool:
    from . import library_files
    from .models import SpokenAsset
    relpaths = {relpath for filepath in filepaths if (relpath := library_files.relative(filepath)) is not None}
    return bool(relpaths) and SpokenAsset._base_manager.filter(library_relpath__in=relpaths).exists()


def _track_refusal(message):
    from library.models import TrackWriteRefused

    class ProjectedTrackWriteRefused(SpokenIntegrityError, TrackWriteRefused):
        pass
    return ProjectedTrackWriteRefused(message)


def check_track_save(track, update_fields) -> None:
    """An ordinary Track save may not change a projection-owned column of a
    Track linked to a SpokenAsset (its file, category, title, artist,
    readiness or media facts come from the projection, never from an edit).
    Ordinary Tracks, and the library's own columns, are untouched."""
    if track._state.adding or track.pk is None:
        if _reserved(getattr(track, "filepath", None)):
            raise _track_refusal("the projected spoken pathname is reserved for its SpokenAsset")
        return
    owned = [field for field in projected_track_fields()
             if update_fields is None or field.name in update_fields or field.attname in update_fields]
    loaded = [field for field in owned if field.attname in track.__dict__]
    current_path = type(track)._base_manager.filter(pk=track.pk).values_list("filepath", flat=True).first()
    sensitive = (_projected([track.pk]) or _reserved(current_path)
                 or _reserved(getattr(track, "filepath", None)))
    if not loaded or not sensitive:
        return
    current = type(track)._base_manager.filter(pk=track.pk).values(*[f.attname for f in loaded]).first()
    if current is None:
        return
    changed = sorted(field.name for field in loaded
                     if not _same(field, track.__dict__[field.attname], current[field.attname]))
    if changed:
        raise _track_refusal(
            f"Track {changed} of a projected spoken Track change only through the spoken projection")


def check_track_update(queryset, columns: dict) -> None:
    owned = set(columns) & _owned_names()
    proposed_reserved = "filepath" in columns and _reserved(columns["filepath"])
    sensitive = (queryset.filter(spoken_asset__isnull=False).exists()
                 or _queryset_uses_reserved_path(queryset) or proposed_reserved)
    if owned and sensitive:
        raise _track_refusal(
            f"Track {sorted(owned)} of a projected spoken Track change only through "
            "the spoken projection")


def check_track_bulk_update(objs, fields) -> None:
    owned = set(fields) & _owned_names()
    current_reserved = _any_reserved(_track_filepaths([obj.pk for obj in objs]).values())
    proposed_reserved = "filepath" in fields and any(_reserved(getattr(obj, "filepath", None)) for obj in objs)
    if owned and (_projected([obj.pk for obj in objs]) or current_reserved or proposed_reserved):
        raise _track_refusal("a projected spoken Track's owned columns change only through the projection")


def _track_filepaths(track_ids) -> dict:
    from library.models import Track
    ids = [pk for pk in track_ids if pk is not None]
    return dict(Track._base_manager.filter(pk__in=ids).values_list("pk", "filepath"))
