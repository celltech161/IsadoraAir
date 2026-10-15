"""Produced-content scope checks (server side, never trusted from a client).

Broad capabilities stay in ``authz``; this module answers only "which exact
type" and "is this user assigned to it". Delivery-specific services (e.g.
active_messages.services) combine the two.
"""
from __future__ import annotations

from .models import ProducedContentType, ProducedContentTypeAssignment


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
