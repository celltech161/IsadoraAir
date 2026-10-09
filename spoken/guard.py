"""Keep ``spoken.transitions`` the ONLY authority over item lifecycle.

Inside ``transition_scope()`` (entered only by spoken.transitions) the service
may create items and events and change lifecycle columns. Everywhere else --
views, admin, shells, other apps -- the ordinary ORM paths refuse:

* creating a SpokenItem or a SpokenItemEvent;
* changing any lifecycle column of a SpokenItem through ``save()``,
  ``QuerySet.update()`` or ``bulk_update()`` (the instance is compared with the
  authoritative row, so a stale or deferred instance cannot smuggle a change);
* changing or deleting an event (also refused inside the scope: append-only).

Only the editorial text (title, notes) and Django's own ``SET_NULL`` of a
deleted user's foreign keys are free. Raw SQL is outside this boundary, as for
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


def refuse_queryset_update(model_name, columns: dict, *, allowed) -> None:
    if in_scope():
        return
    blocked = sorted(name for name, value in columns.items()
                     if name not in allowed and not (name in _USER_FKS and value is None))
    if blocked:
        raise SpokenIntegrityError(
            f"{model_name} {blocked} may only change through the spoken services (spoken.transitions)")


def _protected_item_fields(item) -> list:
    return [field for field in item._meta.concrete_fields
            if field.name not in ITEM_FREE_COLUMNS and not field.primary_key]


def check_item_save(item, update_fields) -> None:
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
