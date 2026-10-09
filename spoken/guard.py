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
    if "created_at" in generated:
        started, finished = window
        if row["created_at"] is None or not (started <= row["created_at"] <= finished):
            raise SpokenIntegrityError(f"{model.__name__}.created_at was not stamped by the write")


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
    from django.db.models import QuerySet
    from django.utils import timezone

    from .models import SpokenItem, SpokenItemEvent
    grant = _grant.get()
    kind = type(grant.record) if grant is not None else None
    if kind not in (ItemWrite, ItemCreate, EventCreate):
        raise SpokenIntegrityError("no open grant authorizes this write")
    record = _take(kind)                    # owner-checked and consumed before anything else
    if not transaction.get_connection().in_atomic_block:
        raise SpokenIntegrityError("authorized spoken writes run inside the transition's transaction")
    if kind is ItemWrite:
        count = QuerySet.update(SpokenItem._base_manager.filter(pk=record.item_id, state=record.from_state),
                                **_decode(SpokenItem, record.values))
        if count != 1:
            raise SpokenIntegrityError("the item is not in the state this write was authorized from")
        _verify_complete(SpokenItem, record.item_id, authoritative_item_fields(),
                         _decode(SpokenItem, record.expected))
        return None
    started = timezone.now()
    if kind is ItemCreate:
        row = SpokenItem(id=record.item_id, **_decode(SpokenItem, record.values))
        QuerySet.bulk_create(SpokenItem._base_manager.all(), [row])
        _verify_complete(SpokenItem, record.item_id, authoritative_item_fields(),
                         _decode(SpokenItem, record.expected), generated=ITEM_GENERATED,
                         window=(started, timezone.now()))
        return row.pk
    (event,) = QuerySet.bulk_create(SpokenItemEvent._base_manager.all(),
                                    [SpokenItemEvent(**_decode(SpokenItemEvent, record.values))])
    if event.pk is None:
        raise SpokenIntegrityError("the authorized event was not assigned an id")
    _verify_complete(SpokenItemEvent, event.pk, authoritative_event_fields(),
                     _decode(SpokenItemEvent, record.values), generated=EVENT_GENERATED,
                     window=(started, timezone.now()))
    return event.pk


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
