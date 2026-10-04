"""The ONLY legitimate state changes of an existing ProductionMedia row.

Every other ORM write is refused by production.models (see its docstring).
Each transition is a single conditional UPDATE whose WHERE clause carries the
state rule, so it is race-safe without a lock and simply affects 0 rows when the
rule does not hold. Callers inspect the returned count. These are internal to
the production services (validation, retention); consuming domains never call
them.

There is no purged -> present transition and no way to rewrite identity, bytes
identity, custody, display metadata or provenance.
"""
from __future__ import annotations

from django.db.models import QuerySet

from .models import ProductionMedia

FACT_FIELDS = frozenset({
    "container", "codec", "sample_rate", "channels", "decoded_duration_seconds",
    "header_duration_seconds", "probe",
})
VERDICT_FIELDS = FACT_FIELDS | {"validation_state", "validation_code", "validated_at"}


def _apply(queryset, **columns) -> int:
    # The guarded ProductionMediaQuerySet.update refuses generic writes; these
    # vetted transitions call the plain QuerySet implementation directly.
    return QuerySet.update(queryset, **columns)


def record_verdict(media_id, columns: dict) -> int:
    """Record a validation verdict ONCE: only on a present, still-unvalidated
    row, and only the fact/verdict columns."""
    illegal = sorted(set(columns) - VERDICT_FIELDS)
    if illegal:
        raise ValueError(f"a verdict may not write {illegal}")
    if columns.get("validation_state") not in (ProductionMedia.VALIDATION_VALID, ProductionMedia.VALIDATION_INVALID):
        raise ValueError("a verdict must be valid or invalid")
    if columns.get("validated_at") is None or not columns.get("validation_code"):
        raise ValueError("a verdict needs validated_at and a validation_code")
    return _apply(
        ProductionMedia.objects.filter(
            pk=media_id, validation_state=ProductionMedia.VALIDATION_UNVALIDATED,
            retention_state=ProductionMedia.RETENTION_PRESENT,
        ),
        **columns,
    )


def record_infrastructure_attempt(media_id, code: str) -> int:
    """Note why the latest validation attempt could not judge the media. Only
    on a present, still-unvalidated row; touches validation_code alone."""
    if not code:
        raise ValueError("an infrastructure attempt needs a code")
    return _apply(
        ProductionMedia.objects.filter(
            pk=media_id, validation_state=ProductionMedia.VALIDATION_UNVALIDATED,
            retention_state=ProductionMedia.RETENTION_PRESENT,
        ),
        validation_code=code[:48],
    )


def mark_purged(media_id, when) -> int:
    """present -> purged (with purged_at). The only retention transition."""
    if when is None:
        raise ValueError("purged_at is required")
    return _apply(
        ProductionMedia.objects.filter(pk=media_id, retention_state=ProductionMedia.RETENTION_PRESENT),
        retention_state=ProductionMedia.RETENTION_PURGED, purged_at=when,
    )
