"""Keep ``active_messages.services`` (with ``active_messages.playout`` for the
D2B playout correlation) the ONLY writer of active-message authority.

Every ordinary ORM write path of the four domain models refuses:

* ``Model.save()`` / ``Model.delete()`` -- always (so ``objects.create()``,
  ``get_or_create()``/``update_or_create()``'s creating branch and admin
  saves refuse too);
* ``QuerySet.bulk_create()`` / ``bulk_update()`` / ``delete()`` -- always;
* ``QuerySet.update()`` -- except Django's own SET_NULL of a deleted user
  (exactly the user foreign keys, each set to None): the username snapshots
  keep the history readable.

The guarded querysets are the default AND base managers
(``Meta.base_manager_name``), and every reverse related manager is built from
them. The service writes through ``active_messages._store`` instead: Django's
base ``QuerySet`` (no model signals, no ``save()``), compare-and-write on the
locked source state, then a read-back that must equal what was planned.

Importing ``_store`` from anywhere else, or calling Django's base QuerySet
methods directly, is equivalent to raw SQL and outside this boundary -- the
same convention as ProductionMedia and the spoken domain.
"""
from __future__ import annotations

from django.db import models


class ActiveMessageIntegrityError(Exception):
    """An ordinary write path tried to bypass the active-message services."""


def refuse(message: str):
    raise ActiveMessageIntegrityError(message)


class GuardedQuerySet(models.QuerySet):
    """Subclasses name the model (for messages) and its user foreign keys."""

    model_label = "active-message rows"
    user_foreign_keys: frozenset = frozenset()

    def update(self, **kwargs):
        # Django's Collector nulls SET_NULL foreign keys of a deleted user with
        # QuerySet.update(); that one write is allowed, nothing else.
        if kwargs and set(kwargs) <= self.user_foreign_keys and all(v is None for v in kwargs.values()):
            return super().update(**kwargs)
        refuse(f"{self.model_label} change only through active_messages.services; "
               f"refusing a generic update of {sorted(kwargs)}")

    def bulk_update(self, objs, fields, batch_size=None):
        refuse(f"{self.model_label} change only through active_messages.services; bulk_update is refused")

    def bulk_create(self, objs, *args, **kwargs):
        refuse(f"{self.model_label} are created only by active_messages.services; bulk_create is refused")

    def delete(self):
        refuse(f"{self.model_label} are never deleted")

    delete.queryset_only = True


def user_fk_names(*fields: str) -> frozenset:
    """The attribute AND field names Django may pass for a SET_NULL update."""
    return frozenset(fields) | frozenset(f"{name}_id" for name in fields)
