"""Read-only migration data preflights for the protected updater (runtime 11).

The protected executor passes the trusted PENDING migration refs of the
staged target (`--pending <ref>`, repeated). This command owns an explicit
registry keyed by migration ref: each registered check models exactly what
that pending migration will reject, given the known effects of migrations
pending before it. Unregistered refs contribute no check. Nothing comes from
a release manifest, and nothing is imported or executed dynamically.

Every check runs inside one PostgreSQL READ ONLY transaction that is always
rolled back, and returns bounded, structured evidence.

r0107: production.0001_initial -- the migration that first gives a station
ProductionMedia -- also carries two read-only HOST checks
(production.services.host_capability): the isadoraair-validation boundary's
kernel/systemd facilities, and that PRODUCTION_MEDIA_ROOT can be established.
They write nothing; they move a refusal on an unsupported host ahead of every
mutation (checkpoint, migration, checkout advance, units, starts).
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import connection, transaction

GROUP_LIMIT = 25
ROW_ID_LIMIT = 20
# library.0087 get_or_creates this profile and assigns every profile-less
# ScheduleBlock to it (its INITIAL_PROFILE_NAME); regression-tested equal.
DEFAULT_PROFILE_NAME = "Default Schedule"
M0087 = "library.0087_backfill_default_schedule_profile"
M0088 = "library.0088_enforce_schedule_profile_integrity"


def _column_exists(cursor, table, column):
    cursor.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
        [table, column],
    )
    return cursor.fetchone() is not None


def _table_exists(cursor, table):
    cursor.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = %s",
        [table],
    )
    return cursor.fetchone() is not None


def _duplicate_groups(cursor, *, scope_expression, scope_params, scope_label):
    """Duplicate (scope, key, start_time) groups for both slot kinds:
    recurring (day_of_week IS NOT NULL) and dated (specific_date IS NOT NULL)."""
    groups, total = [], 0
    for kind, column in (("recurring", "day_of_week"), ("specific-date", "specific_date")):
        base = (
            f"SELECT {scope_expression} AS scope, {column}, start_time, COUNT(*) AS n, "
            f"(array_agg(id ORDER BY id))[1:{ROW_ID_LIMIT}] AS ids "
            f"FROM library_scheduleblock WHERE {column} IS NOT NULL "
            f"GROUP BY 1, {column}, start_time HAVING COUNT(*) > 1"
        )
        cursor.execute(f"SELECT COUNT(*) FROM ({base}) AS duplicate_groups", scope_params)
        total += cursor.fetchone()[0]
        cursor.execute(f"{base} ORDER BY 1, {column}, start_time LIMIT {GROUP_LIMIT}", scope_params)
        for scope, key, start_time, count, ids in cursor.fetchall():
            groups.append({
                "kind": kind,
                "scope": scope_label(scope),
                "key": [str(key), str(start_time)],
                "count": count,
                "schedule_block_ids": list(ids),
                "row_ids_truncated": count > len(ids),
            })
    return groups, total


def _check_0087_global_slot_ambiguity(cursor, pending):
    """Mirrors library.0087's own pre-check exactly: it aborts on ANY
    duplicate (day_of_week, start_time) among rows with a day, or
    (specific_date, start_time) among rows with a date -- globally,
    IGNORING profile, before it assigns anything."""
    groups, total = _duplicate_groups(
        cursor, scope_expression="'global'", scope_params=[], scope_label=lambda _scope: "global",
    )
    return {
        "ok": total == 0,
        "rule": "library.0087 global (day_of_week|specific_date, start_time) ambiguity",
        "offending_group_count": total,
        "offending_groups": groups,
        "offending_groups_truncated": total > len(groups),
    }


def _check_0088_profile_integrity(cursor, pending):
    """Mirrors what library.0088 enforces, on the state it will see:

    * UNIQUE (profile, day_of_week, start_time)   WHERE day_of_week IS NOT NULL
    * UNIQUE (profile, specific_date, start_time) WHERE specific_date IS NOT NULL
    * ScheduleBlock.profile NOT NULL
    * ScheduleProfileState.active_profile / default_profile NOT NULL

    If 0087 is still pending, rows are evaluated as 0087 will leave them:
    profile-less blocks join the existing "Default Schedule" profile (or a
    new one), and the singleton state row (pk=1) gets both pointers set.
    Before 0086 there is no profile at all, so every block shares the one
    future default profile.
    """
    after_0087 = M0087 not in pending
    problems = []
    has_profile = _column_exists(cursor, "library_scheduleblock", "profile_id")
    if has_profile:
        default_id = None
        if not after_0087:
            cursor.execute("SELECT id FROM library_scheduleprofile WHERE name = %s", [DEFAULT_PROFILE_NAME])
            row = cursor.fetchone()
            default_id = row[0] if row else None
        if after_0087:
            scope_expression, scope_params = "profile_id", []
            cursor.execute(
                f"SELECT COUNT(*), (array_agg(id ORDER BY id))[1:{ROW_ID_LIMIT}] "
                "FROM library_scheduleblock WHERE profile_id IS NULL"
            )
            null_count, null_ids = cursor.fetchone()
            if null_count:
                problems.append({
                    "kind": "schedule_block_profile_null",
                    "count": null_count, "schedule_block_ids": list(null_ids or []),
                    "row_ids_truncated": null_count > len(null_ids or []),
                })
        else:
            # -1 is never a primary key: profile-less rows with no existing
            # default profile become one brand-new profile.
            scope_expression, scope_params = "COALESCE(profile_id, %s, -1)", [default_id]

        def scope_label(scope):
            return "new default profile (unassigned rows)" if scope == -1 else {"profile_id": scope}
    else:
        scope_expression, scope_params = "0", []

        def scope_label(_scope):
            return "single future default profile (pre-0086)"

    if _table_exists(cursor, "library_scheduleprofilestate"):
        pointer_filter = "(active_profile_id IS NULL OR default_profile_id IS NULL)"
        if not after_0087:
            pointer_filter += " AND id <> 1"     # 0087 repairs only the singleton row
        cursor.execute(f"SELECT COUNT(*), array_agg(id ORDER BY id) FROM library_scheduleprofilestate WHERE {pointer_filter}")
        state_count, state_ids = cursor.fetchone()
        if state_count:
            problems.append({
                "kind": "schedule_profile_state_pointer_null",
                "count": state_count, "schedule_profile_state_ids": list(state_ids or [])[:ROW_ID_LIMIT],
                "row_ids_truncated": state_count > ROW_ID_LIMIT,
            })

    groups, total = _duplicate_groups(
        cursor, scope_expression=scope_expression, scope_params=scope_params, scope_label=scope_label,
    )
    return {
        "ok": total == 0 and not problems,
        "rule": "library.0088 per-profile uniqueness and NOT NULL profile pointers",
        "evaluated_after_pending_0087": not after_0087,
        "offending_group_count": total,
        "offending_groups": groups,
        "offending_groups_truncated": total > len(groups),
        "null_profile_problems": problems,
    }


# Explicit registry: pending migration ref -> [(check id, check)].
M_PRODUCTION_0001 = "production.0001_initial"


def _check_validation_host(cursor, pending):
    from production.services import host_capability
    return host_capability.check_validation_host()


def _check_media_root(cursor, pending):
    from production.services import host_capability
    return host_capability.check_media_root()


REGISTRY = {
    M0087: [("library.0087.global_slot_ambiguity", _check_0087_global_slot_ambiguity)],
    M0088: [("library.0088.profile_integrity", _check_0088_profile_integrity)],
    M_PRODUCTION_0001: [
        ("production.0001.validation_host_capability", _check_validation_host),
        ("production.0001.media_root_establishable", _check_media_root),
    ],
}


def _applied(cursor, refs):
    cursor.execute("SELECT app, name FROM django_migrations")
    recorded = {f"{app}.{name}" for app, name in cursor.fetchall()}
    return [ref for ref in refs if ref in recorded]


def run_preflights(pending_refs: list[str]) -> dict:
    pending = list(dict.fromkeys(pending_refs))
    results = []
    with transaction.atomic():
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute("SET TRANSACTION READ ONLY")
            already = _applied(cursor, pending)
            if already:
                # The trusted plan says these are pending, the recorder says
                # otherwise: the stage is not what any check would model.
                results.append({
                    "id": "updater.pending_stage_consistency", "migration": already[0], "status": "failed",
                    "evidence": {"applied_but_reported_pending": already},
                })
            else:
                pending_set = frozenset(pending)
                for ref in pending:
                    for check_id, check in REGISTRY.get(ref, ()):
                        evidence = check(cursor, pending_set)
                        results.append({
                            "id": check_id, "migration": ref,
                            "status": "passed" if evidence.pop("ok") else "failed",
                            "evidence": evidence,
                        })
        transaction.set_rollback(True)
    return {
        "schema_version": 1,
        "status": "ok" if all(item["status"] == "passed" for item in results) else "failed",
        "checks": results,
    }


class Command(BaseCommand):
    help = "Run the registered read-only data preflights for the given pending migrations."

    def add_arguments(self, parser):
        parser.add_argument("--pending", action="append", dest="pending", required=True)

    def handle(self, *args, **options):
        payload = run_preflights(options["pending"])
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(raw.encode("utf-8")) > 65536:
            raise RuntimeError("migration preflight output exceeds 64 KiB")
        self.stdout.write(raw)
