"""PRIVATE writer for active_messages.services -- not an application API.

Writes go through Django's base ``QuerySet`` (no model signals, never
``Model.save()``), so nothing between the service's decision and the SQL can
change what is written. Every update is a compare-and-write (the row must
still hold ``match``), and every write is read back and must equal what was
planned, or the transaction rolls back.
"""
from __future__ import annotations

from django.db import models

from .guard import ActiveMessageIntegrityError
from .models import POLICY_ID, ActiveBroadcastPolicy


def _base(model) -> models.QuerySet:
    return models.QuerySet(model=model)


def _check(row, values: dict) -> None:
    for name, expected in values.items():
        field = row._meta.get_field(name)
        actual = getattr(row, field.attname)
        expected = getattr(expected, "pk", expected) if field.is_relation else expected
        if actual != expected:
            raise ActiveMessageIntegrityError(f"{row._meta.label}.{name} read back {actual!r}, planned {expected!r}")


def insert(model, **values):
    row = model(**values)
    _base(model).bulk_create([row])
    stored = _base(model).get(pk=row.pk)
    _check(stored, values)
    return stored


def write(model, pk, *, match: dict, values: dict):
    """UPDATE ... WHERE pk AND match; exactly one row or the change is refused."""
    if _base(model).filter(pk=pk, **match).update(**values) != 1:
        raise ActiveMessageIntegrityError(f"{model._meta.label} {pk} no longer matches {sorted(match)}")
    stored = _base(model).get(pk=pk)
    _check(stored, values)
    return stored


def ensure_policy() -> None:
    """Create the singleton on first use (no data migration). Concurrent
    first uses are both safe: the insert ignores the conflict."""
    _base(ActiveBroadcastPolicy).bulk_create([ActiveBroadcastPolicy(id=POLICY_ID)], ignore_conflicts=True)
