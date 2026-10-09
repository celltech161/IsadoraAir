"""Keep ``spoken.transitions`` the ONLY authority over item lifecycle.

Inside ``transition_scope()`` (entered only by spoken.transitions) the service
may create items and events and change lifecycle columns. Everywhere else --
views, admin, shells, other apps -- every ordinary ORM path refuses. The
guarded querysets are the default AND the base managers (``Meta.base_manager_name``),
and Django builds every reverse related manager (``content_type.items``,
``item.events``) from the default manager's class, so the same checks apply
to all of them:

* creating a SpokenItem or a SpokenItemEvent: instance ``save()`` while
  adding, ``create()``, and the creating branch of ``get_or_create()`` /
  ``update_or_create()`` (they call ``create()``) refuse outside the scope;
  ``bulk_create()`` -- including its conflict/upsert forms -- refuses ALWAYS
  (it skips ``save()``; the service never uses it);
* changing any lifecycle column of a SpokenItem through ``save()`` (the
  instance is compared with the authoritative row, so a stale or deferred
  instance cannot smuggle a change), ``QuerySet.update()`` or
  ``bulk_update()`` (refused for every column but the editorial ones, even in
  the scope: the service never bulk-updates);
* a ``state`` that is not an ItemState is refused on every write path, even
  inside the scope -- the state set is application authority, deliberately not
  a database check (a new state must not need a manual migration);
* deleting an item or an event: instance ``delete()`` and ``QuerySet.delete()``
  refuse always (no FK into either cascades, so the deletion collector never
  reaches them);
* changing an event: refused always (append-only).

Only the editorial text (title, notes) and Django's own ``SET_NULL`` of a
deleted user's foreign keys (``update(<user fk>=None)``: that column alone,
to NULL alone) are free. Raw SQL is outside this boundary, as for
ProductionMedia.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager


class SpokenIntegrityError(Exception):
    """An ordinary write path tried to bypass the spoken-domain services."""


_scope = contextvars.ContextVar("spoken_transition_scope", default=False)

# Free outside the transition service.
ITEM_FREE_COLUMNS = ("title", "notes")
# Django nulls these itself when a user is deleted (on_delete=SET_NULL).
_USER_FKS = ("submitted_by", "submitted_by_id", "reviewed_by", "reviewed_by_id", "recalled_by", "recalled_by_id")


@contextmanager
def transition_scope():
    token = _scope.set(True)
    try:
        yield
    finally:
        _scope.reset(token)


def in_scope() -> bool:
    return _scope.get()


def refuse(message) -> None:
    raise SpokenIntegrityError(message)


def check_state_value(value) -> None:
    from .models import ItemState
    if not isinstance(value, str) or value not in ItemState.values:
        raise SpokenIntegrityError(f"{value!r} is not a spoken item state")


def refuse_queryset_update(model_name, columns: dict, *, allowed) -> None:
    if in_scope():
        if "state" in columns:
            check_state_value(columns["state"])
        return
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


def check_item_save(item, update_fields) -> None:
    if "state" in item.__dict__ and (update_fields is None or "state" in update_fields):
        check_state_value(item.state)
    if in_scope():
        return
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
    changed = [field.name for field in loaded
               if field.to_python(item.__dict__[field.attname]) != field.to_python(current[field.attname])]
    if changed:
        raise SpokenIntegrityError(
            f"SpokenItem {sorted(changed)} may only change through the spoken services (spoken.transitions)")


def check_event_write() -> None:
    if not in_scope():
        raise SpokenIntegrityError("spoken item events are written only by spoken.transitions")
