"""Operator audit events for Library bulk editing and reconciliation (roadmap 3.11 / 2.5).

Recorded through the existing ``monitoring.models.emit_event`` (``SystemEvent``, category ``library``), so they
appear in /monitoring/ Recent Events and Django admin.  That log is operational, not an audit archive: rows are
pruned after about 30 days (``prune_systemevents``) and writes are best effort -- a failed write is printed to
stderr and dropped.  Nothing here is a permanent, guaranteed or append-only record.

* **After commit.**  ``record`` registers the event with ``transaction.on_commit``: inside a transaction it is
  written only once that transaction commits (a rollback writes nothing); in autocommit it is written at once,
  after the work it describes has already committed.  It never raises into the caller: a committed Library
  operation is never failed or replayed because its audit could not be written.
* **One event per action.**  Each action carries its own ``action_id`` (random), which is also its
  ``dedupe_key``, so SystemEvent's 60-second coalescing never merges two different operations.
* **Bounded and private.**  The operator's username and id, outcome and counts; at most ``ID_SAMPLE`` Track ids
  per list (with the full count); file paths relative to ``LIBRARY_ROOT``; never a preview token, cookie,
  request body or credential.
"""
from __future__ import annotations

import logging
import os
import secrets

from django.conf import settings
from django.db import transaction

log = logging.getLogger(__name__)

ID_SAMPLE = 20


def new_action_id() -> str:
    return secrets.token_hex(8)


def library_relative(path) -> str:
    """A Library-relative path for the event log (absolute station paths are not exposed)."""
    if not path:
        return ""
    root = os.path.realpath(getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music"))
    absolute = os.path.abspath(str(path))
    if absolute == root or absolute.startswith(root + os.sep):
        return os.path.relpath(absolute, root)
    return f"(outside the Library)/{os.path.basename(absolute)}"


def id_sample(ids) -> dict:
    ids = list(ids)
    return {"count": len(ids), "ids": ids[:ID_SAMPLE], "truncated": len(ids) > ID_SAMPLE}


def record(action: str, *, user, title: str, outcome: str, level: str = "info", detail: dict | None = None,
           action_id: str | None = None) -> str:
    """Write one audit event after the current transaction commits (at once in autocommit).  Returns its id."""
    action_id = action_id or new_action_id()
    payload = {
        "action": action, "action_id": action_id, "outcome": outcome,
        "operator": getattr(user, "username", "") or "", "operator_id": getattr(user, "pk", None),
        **(detail or {}),
    }

    def write():
        try:
            from monitoring.models import emit_event
            emit_event(category="library", level=level, title=title, detail=payload,
                       dedupe_key=f"library|audit|{action}|{action_id}")
        except Exception as exc:          # best effort: never surface into the committed operation
            log.warning("library audit event %s/%s not recorded: %s", action, action_id, type(exc).__name__)

    try:
        transaction.on_commit(write, robust=True)
    except Exception as exc:              # e.g. a broken transaction: the operation itself decides its outcome
        log.warning("library audit event %s/%s not scheduled: %s", action, action_id, type(exc).__name__)
    return action_id
