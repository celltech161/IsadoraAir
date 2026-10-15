"""Produced-content scope checks (server side, never trusted from a client).

Broad capabilities stay in ``authz``; this module answers only "which exact
type" and "is this user assigned to it". Delivery-specific services (e.g.
active_messages.services) combine the two.

The broad Produced Content capabilities are the established
``active_message.produce`` / ``active_message.control`` slugs (D1): D3C reuses
them for regular and test content rather than adding new ones. PRODUCE plus an
assignment to the exact enabled type lets a producer produce it; CONTROL works
across enabled types without an assignment.
"""
from __future__ import annotations

from .models import DeliveryMode, ProducedContentType, ProducedContentTypeAssignment

PRODUCE = "active_message.produce"
CONTROL = "active_message.control"
# The modes the D3C "My Content" workspace serves (urgent / public address stay on My Messages).
CONTENT_MODES = (DeliveryMode.REGULAR, DeliveryMode.TEST)


def can(user, capability: str) -> bool:
    if user is None or not getattr(user, "is_authenticated", False) or getattr(user, "pk", None) is None:
        return False
    from authz.evaluator import authorize
    return bool(authorize(user, capability))


def has_workspace(user) -> bool:
    """The broad capability: produce or control produced content."""
    return can(user, CONTROL) or can(user, PRODUCE)


def production_refusal(user, content_type) -> str:
    """Why ``user`` may NOT start or save new production of this exact type
    now ("" when they may): the type is enabled, and the user has CONTROL, or
    PRODUCE and an assignment to it."""
    if not content_type.enabled:
        return f"{content_type.name} is disabled."
    if can(user, CONTROL):
        return ""
    if not can(user, PRODUCE):
        return "Your account cannot produce content."
    if not is_assigned(user, content_type):
        return f"You are not assigned to {content_type.name}."
    return ""


def is_station_control(user) -> bool:
    """Station control of produced content: CONTROL, or a spoken manager (staff)."""
    from spoken.transitions import is_manager
    return can(user, CONTROL) or is_manager(user)


class ProductionRefused(Exception):
    """Refused BEFORE any side effect (no backend, link, item or take)."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def lock_for_production(user, content_type, *, mode) -> ProducedContentType:
    """THE production authorization, inside the caller's transaction and
    before any side effect: lock and re-read the exact type row (the lock a
    mode change and the backend bridge take too), require ``mode``, prove it
    enabled, then CONTROL -- or PRODUCE and an assignment to it (the assignment
    row is locked, so its removal waits for this transaction). Returns the
    locked row."""
    from django.db import transaction
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("lock_for_production must run inside transaction.atomic()")
    try:
        pk = resolve(content_type).pk
    except UnknownContentType:
        raise ProductionRefused("no such content type", code="unknown_type") from None
    row = ProducedContentType.objects.select_for_update(of=("self",)).filter(pk=pk).first()
    if row is None or row.delivery_mode != mode:
        raise ProductionRefused("no such content type", code="unknown_type")
    if not row.enabled:
        raise ProductionRefused(f"{row.name} is disabled.", code="type_disabled")
    if can(user, CONTROL):
        return row
    if not can(user, PRODUCE):
        raise ProductionRefused("Your account cannot produce content.", code="not_permitted")
    if not ProducedContentTypeAssignment.objects.select_for_update().filter(
            user_id=user.pk, content_type_id=row.pk).exists():
        raise ProductionRefused(f"You are not assigned to {row.name}.", code="not_assigned")
    return row


def controls_managed_type(user, spoken_content_type_id) -> bool:
    """spoken.transitions recall authority (registered in AppConfig.ready):
    CONTROL manages the items of a spoken type ONLY when it is the explicitly
    linked backend of a regular produced type -- never a standalone one."""
    return (ProducedContentType.objects.filter(spoken_content_type_id=spoken_content_type_id,
                                               delivery_mode=DeliveryMode.REGULAR).exists()
            and can(user, CONTROL))


def content_types_for(user):
    """The enabled regular and test types the user may produce: all of them
    for CONTROL, the assigned ones for a producer."""
    if can(user, CONTROL):
        rows = ProducedContentType.objects.filter(enabled=True)
    elif can(user, PRODUCE):
        rows = assigned_types(user)
    else:
        rows = ProducedContentType.objects.none()
    return rows.filter(delivery_mode__in=CONTENT_MODES).order_by("sort_order", "name", "key")


class UnknownContentType(LookupError):
    pass


def resolve(content_type) -> ProducedContentType:
    """The exact type named by an instance, a primary key or a durable key --
    always re-read from the database (a caller's instance may be stale)."""
    if isinstance(content_type, ProducedContentType):
        lookup = {"pk": content_type.pk}
    elif isinstance(content_type, int) and not isinstance(content_type, bool):
        lookup = {"pk": content_type}
    elif isinstance(content_type, str) and content_type:
        lookup = {"key": content_type}
    else:
        raise UnknownContentType("no such produced content type")
    found = ProducedContentType.objects.filter(**lookup).first()
    if found is None:
        raise UnknownContentType("no such produced content type")
    return found


def is_assigned(user, content_type) -> bool:
    if user is None or getattr(user, "pk", None) is None:
        return False
    return ProducedContentTypeAssignment.objects.filter(
        user_id=user.pk, content_type_id=getattr(content_type, "pk", content_type)).exists()


def assigned_types(user, *, enabled_only=True):
    """The types ``user`` is assigned to (for later producer-facing pages)."""
    if user is None or getattr(user, "pk", None) is None:
        return ProducedContentType.objects.none()
    rows = ProducedContentType.objects.filter(assignments__user_id=user.pk)
    return rows.filter(enabled=True) if enabled_only else rows
