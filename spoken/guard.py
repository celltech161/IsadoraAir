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
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass


class SpokenIntegrityError(Exception):
    """An ordinary write path tried to bypass the spoken-domain services."""


# Free outside the transition service.
ITEM_FREE_COLUMNS = ("title", "notes")
# Django nulls these itself when a user is deleted (on_delete=SET_NULL).
_USER_FKS = ("submitted_by", "submitted_by_id", "reviewed_by", "reviewed_by_id", "recalled_by", "recalled_by_id")


# -- grants (private) -----------------------------------------------------------------------

@dataclass(frozen=True)
class ItemCreate:
    item_id: object
    values: tuple                 # ((attname, value), ...) -- the planned columns


@dataclass(frozen=True)
class ItemWrite:
    item_id: object
    from_state: str
    to_state: str
    values: tuple                 # ((attname, value), ...) -- exact protected changes


@dataclass(frozen=True)
class EventCreate:
    values: tuple                 # ((attname, value), ...) -- every event column


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
        return
    if isinstance(record, EventCreate):
        values = dict(record.values)
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
    return [field for field in item._meta.concrete_fields
            if field.name not in ITEM_FREE_COLUMNS and not field.primary_key]


def refuse_item_bulk_update(fields) -> None:
    blocked = sorted(set(fields) - set(ITEM_FREE_COLUMNS))
    if blocked:
        raise SpokenIntegrityError(f"SpokenItem.bulk_update of {blocked} is refused; use spoken.transitions")


def _same(field, left, right) -> bool:
    return field.to_python(left) == field.to_python(right)


# -- the exact executor ---------------------------------------------------------------------

def _decode(model, values) -> dict:
    """The grant payload as ORM values. JSON columns are carried as canonical
    JSON text in the (deeply immutable) payload and decoded only here, into a
    fresh object that no other code holds."""
    from django.db.models import JSONField
    decoded = {}
    for attname, value in values:
        field = model._meta.get_field(attname)
        decoded[attname] = json.loads(value) if isinstance(field, JSONField) else value
    return decoded


def _verify_persisted(model, pk, values: dict) -> None:
    """Read the written row back (same transaction) and refuse -- so the
    enclosing transaction rolls back -- unless it is exactly the payload."""
    row = model._base_manager.filter(pk=pk).values(*values).first()
    if row is None:
        raise SpokenIntegrityError(f"the authorized {model.__name__} row is missing after its write")
    for attname, value in values.items():
        field = model._meta.get_field(attname)
        if not _same(field, row[attname], value):
            raise SpokenIntegrityError(f"{model.__name__}.{field.name} differs from the authorized write")


def _take(kind):
    grant = _active(kind)
    if grant is None:
        raise SpokenIntegrityError("no open grant authorizes this write")
    grant.used = True
    return grant.record


def _execute():
    """Perform THE write the open grant describes -- exactly its payload, once.
    Takes no data: there is nothing a caller can substitute."""
    from django.db import transaction
    from django.db.models import QuerySet

    from .models import SpokenItem, SpokenItemEvent
    grant = _grant.get()
    kind = type(grant.record) if grant is not None else None
    if kind not in (ItemWrite, ItemCreate, EventCreate):
        raise SpokenIntegrityError("no open grant authorizes this write")
    record = _take(kind)                    # owner-checked and consumed before anything else
    if not transaction.get_connection().in_atomic_block:
        raise SpokenIntegrityError("authorized spoken writes run inside the transition's transaction")
    if kind is ItemWrite:
        values = _decode(SpokenItem, record.values)
        count = QuerySet.update(SpokenItem._base_manager.filter(pk=record.item_id, state=record.from_state),
                                **values)
        if count != 1:
            raise SpokenIntegrityError("the item is not in the state this write was authorized from")
        _verify_persisted(SpokenItem, record.item_id, values)
        return None
    if kind is ItemCreate:
        values = _decode(SpokenItem, record.values)
        row = SpokenItem(id=record.item_id, **values)
        QuerySet.bulk_create(SpokenItem._base_manager.all(), [row])
        _verify_persisted(SpokenItem, record.item_id, values)
        return row.pk
    values = _decode(SpokenItemEvent, record.values)
    (event,) = QuerySet.bulk_create(SpokenItemEvent._base_manager.all(), [SpokenItemEvent(**values)])
    if event.pk is None:
        raise SpokenIntegrityError("the authorized event was not assigned an id")
    _verify_persisted(SpokenItemEvent, event.pk, values)
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
