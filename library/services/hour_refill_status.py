"""Roadmap 2.27 Phase D -- read-only operator status for the admin page.

Runs in the web process only. Reads the policy row, the schema (is the Phase
A dry-run constraint still present?), the runner's tmpfs status file, the
HourRefillInstall ledger and existing PlayEvent rows. Never writes and never
creates the policy row. Internal ids and numbers only: no requester data.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from django.db import connection
from django.utils import timezone

from library.services.hour_refill_runner import STATUS_PATH, host_armed

DRY_RUN_CONSTRAINT = "hour_refill_policy_dry_run_only_phase_a"
STATUS_STALE_SECONDS = 180.0

MODE_LABELS = {
    "off": "Off: nothing is captured, evaluated or installed.",
    "observation": "Observation (dry run): advisory proposals only; never writes to a log.",
    "install_not_armed": "Install requested, but this host is not armed (HOUR_REFILL_INSTALL_ARMED): "
                         "the engine stays in observation.",
    "install": "Install permitted: the runner may install guarded refills; Phase C re-checks every write.",
}


def dry_run_gate_present():
    """True while the Phase A check constraint (dry_run_only must be True)
    exists, i.e. migration 0096 has not been applied here."""
    from library.models import HourRefillPolicy

    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, HourRefillPolicy._meta.db_table)
    return DRY_RUN_CONSTRAINT in constraints


def mode_code(policy):
    """Mode as THIS web process sees it (its own HOUR_REFILL_INSTALL_ARMED)."""
    if policy is None or not policy.enabled:
        return "off"
    if policy.dry_run_only:
        return "observation"
    return "install" if host_armed() else "install_not_armed"


def read_runner_status(path=None, now=None):
    """(document or None, freshness): fresh / stale / missing / unreadable."""
    path = STATUS_PATH if path is None else path
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "unreadable"
    if not isinstance(doc, dict):
        return None, "unreadable"
    try:
        written = datetime.fromisoformat(doc.get("written_at"))
    except (TypeError, ValueError):
        return doc, "stale"
    now = now or timezone.now()
    if now - written > timedelta(seconds=STATUS_STALE_SECONDS):
        return doc, "stale"
    return doc, "fresh"


def recent_installs(limit=10):
    """Latest committed ledger rows with any EXISTING PlayEvent evidence per
    installed LogItem. Committed is never reported as aired."""
    from library.models import HourRefillInstall, PlayEvent

    rows = list(HourRefillInstall.objects.order_by("-committed_at", "-id")[:limit])
    item_ids = {item_id for row in rows for item_id in (row.installed_log_item_ids or [])}
    aired = {}
    for item_id, started in (PlayEvent.objects.filter(log_item_id_snapshot__in=item_ids)
                             .order_by("started_at").values_list("log_item_id_snapshot", "started_at")):
        aired.setdefault(item_id, started)
    return [{
        "id": row.id,
        "committed_at": row.committed_at,
        "log_id": row.playlist_log_id_snapshot,
        "log_date": row.log_date,
        "log_hour": row.log_hour,
        "placement_mode": row.placement_mode,
        "cause_ids": list(row.cause_ids or []),
        "net_before_seconds": row.net_before_seconds,
        "residual_after_seconds": row.residual_after_seconds,
        "safety_budget_seconds": row.safety_budget_seconds,
        "items": [{"log_item_id": item_id, "track_id": track_id, "aired_at": aired.get(item_id)}
                  for item_id, track_id in zip(row.installed_log_item_ids or [], row.track_ids or [])],
    } for row in rows]
