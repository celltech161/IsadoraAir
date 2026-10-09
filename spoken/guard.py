"""Keep ``spoken.transitions`` the ONLY authority over item lifecycle.

There is no trusted context. A protected write is allowed only when it is
EXACTLY the write an active, single-use authorization ("grant") describes:

* ``ItemCreate``  -- one new item: its id and every planned column value
  (all other protected columns must hold the model default);
* ``ItemWrite``   -- one existing item: its id, the state it must be in, the
  state it moves to, and the exact value of every protected column the write
  may change (anything else that differs from the authoritative row refuses);
* ``EventCreate`` -- one event: every column (item, event, from/to state,
  actor kind/id/snapshot, media, asset, edition, detail).

Grants are opened by the transition service only around the single
``save()``/``create()`` they authorize (``_authorized``, private). A grant is
itself checked against the fixed transition table (spoken.lifecycle): it can
never describe a C2 (library) transition, a column its rule does not write,
or an event the rule does not record -- so even a hand-built grant cannot
deliver, forge failure fields onto a submission or claim a delivery. A grant
is bound to the thread and asyncio task that opened it, is consumed by the
first write that matches it (before Django's own save, so signal receivers
cannot reuse it), cannot be nested, and is always removed on exit (``finally``).

Everywhere else -- views, admin, shells, other apps, and code running while
a grant is open -- every ordinary ORM path refuses. The guarded querysets are
the default AND the base managers (``Meta.base_manager_name``), and every
reverse related manager (``content_type.items``, ``item.events``) is built
from them:

* ``bulk_create()`` (every form) refuses always; ``save()``/``create()`` and
  the creating branch of ``get_or_create()``/``update_or_create()`` need a
  matching ItemCreate / EventCreate grant;
* lifecycle columns of an item change only through ``save()`` under a
  matching ItemWrite grant; ``QuerySet.update()`` and ``bulk_update()`` may
  change only the editorial columns (and Django's SET_NULL of a deleted
  user) -- never protected columns, grant or not;
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
import threading
from contextlib import contextmanager
from dataclasses import dataclass


class SpokenIntegrityError(Exception):
    """An ordinary write path tried to bypass the spoken-domain services."""


# Free outside the transition service.
ITEM_FREE_COLUMNS = ("title", "notes")
# Django nulls these itself when a user is deleted (on_delete=SET_NULL).
_USER_FKS = ("submitted_by", "submitted_by_id", "reviewed_by", "reviewed_by_id", "recalled_by", "recalled_by_id")
# Set by Django itself as the row is inserted.
_AUTO_COLUMNS = ("created_at",)


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


def _check_item_create(item) -> None:
    grant = _active(ItemCreate)
    if grant is None or str(grant.record.item_id) != str(item.pk):
        raise SpokenIntegrityError("spoken items are created only by spoken.transitions.create_item")
    planned = dict(grant.record.values)
    for field in _protected_item_fields(item):
        if field.attname in _AUTO_COLUMNS:
            continue
        expected = planned[field.attname] if field.attname in planned else field.get_default()
        if not _same(field, item.__dict__.get(field.attname), expected):
            raise SpokenIntegrityError(f"SpokenItem.{field.name} differs from the authorized creation")
    grant.used = True


def _check_item_write(item, grant, update_fields) -> None:
    record = grant.record
    planned = dict(record.values)
    fields = _protected_item_fields(item)
    by_attname = {field.attname: field for field in fields}
    if update_fields is not None and not all(
            name in update_fields or by_attname[name].name in update_fields for name in planned):
        raise SpokenIntegrityError("an authorized item write must write every planned column")
    missing = [name for name in planned if name not in item.__dict__]
    if missing:
        raise SpokenIntegrityError(f"an authorized item write must carry {sorted(missing)}")
    current = type(item)._base_manager.filter(pk=item.pk).values(*by_attname).first()
    if current is None or current["state"] != record.from_state:
        raise SpokenIntegrityError("the item is not in the state this write was authorized from")
    for attname, field in by_attname.items():
        if attname not in item.__dict__ or attname in _AUTO_COLUMNS:
            continue
        expected = planned[attname] if attname in planned else current[attname]
        if not _same(field, item.__dict__[attname], expected):
            raise SpokenIntegrityError(f"SpokenItem.{field.name} differs from the authorized write")
    grant.used = True


def check_item_save(item, update_fields) -> None:
    if "state" in item.__dict__ and (update_fields is None or "state" in update_fields):
        check_state_value(item.state)
    if item._state.adding:
        _check_item_create(item)
        return
    grant = _active(ItemWrite)
    if grant is not None and str(grant.record.item_id) == str(item.pk):
        _check_item_write(item, grant, update_fields)
        return
    # No grant for THIS row: only the editorial columns may differ.
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


def check_event_write(event) -> None:
    if not event._state.adding:
        raise SpokenIntegrityError("spoken item events are append-only")
    grant = _active(EventCreate)
    if grant is None:
        raise SpokenIntegrityError("spoken item events are written only by spoken.transitions")
    planned = dict(grant.record.values)
    for field in event._meta.concrete_fields:
        if field.primary_key or field.attname in _AUTO_COLUMNS:
            continue
        if field.attname not in planned or not _same(field, event.__dict__.get(field.attname),
                                                     planned[field.attname]):
            raise SpokenIntegrityError(f"SpokenItemEvent.{field.name} differs from the authorized event")
    grant.used = True
