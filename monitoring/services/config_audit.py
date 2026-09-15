"""Commit-safe emission of explicitly selected configuration audit events.

Callers remain responsible for deciding what is consequential and for passing
only values that are safe to retain.  This module deliberately does not inspect
models, infer secrets, or alter the runtime application path for a setting.
"""

from copy import deepcopy
import sys
from uuid import uuid4

from django.db import transaction

from monitoring.models import emit_event


def _request_username(request):
    """Return the real request username when available; never invent one."""

    user = getattr(request, "user", None)
    get_username = getattr(user, "get_username", None)
    if not callable(get_username):
        return None
    try:
        username = get_username()
    except Exception:  # Audit metadata must never break the configuration save.
        return None
    return str(username) if username else None


def emit_config_change_event(
    *,
    category,
    title,
    action,
    object_type,
    object_id,
    object_name,
    changed_fields,
    changes=None,
    redacted_fields=None,
    request=None,
    change_source="django_admin",
    apply_modes=None,
    restart_required=False,
):
    """Queue one already-decided configuration audit event for commit.

    ``changes`` must contain only caller-approved safe values.  Fields named in
    ``redacted_fields`` are removed defensively even if a caller accidentally
    supplied them in ``changes``.  A random transaction key prevents the normal
    SystemEvent fault-coalescing window from merging separate operator saves.

    Returns ``True`` when a callback was registered (or ran immediately in
    autocommit mode), and ``False`` when there were no changed fields or callback
    registration itself failed.  Event emission errors are intentionally
    swallowed so audit availability cannot determine whether configuration is
    saved.
    """

    fields = list(changed_fields or ())
    if not fields:
        return False

    redacted = list(redacted_fields or ())
    redacted_set = set(redacted)
    safe_changes = {
        field: value
        for field, value in (changes or {}).items()
        if field not in redacted_set
    }
    detail = {
        "event_type": "configuration_change",
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "object_name": object_name,
        "changed_fields": fields,
        "changes": safe_changes,
        "redacted_fields": redacted,
        "change_source": change_source,
        "apply_modes": dict(apply_modes or {}),
        "restart_required": bool(restart_required),
    }
    username = _request_username(request)
    if username is not None:
        detail["changed_by"] = username

    # Freeze every caller-owned container before deferring beyond the current
    # transaction.  The key contains no object values (private or otherwise).
    frozen_detail = deepcopy(detail)
    dedupe_key = f"configuration-change|{uuid4().hex}"

    def _emit_after_commit():
        try:
            emit_event(
                category=category,
                title=title,
                level="info",
                detail=frozen_detail,
                source=change_source,
                dedupe_key=dedupe_key,
            )
        except Exception as exc:  # Also protect against patched/alternate emitters.
            print(
                f"  [config_audit] failed to record event ({category}: {title}): {exc}",
                file=sys.stderr,
            )

    try:
        transaction.on_commit(_emit_after_commit)
    except Exception as exc:
        print(
            f"  [config_audit] failed to queue event ({category}: {title}): {exc}",
            file=sys.stderr,
        )
        return False
    return True
