"""Run closed, read-only migration data checks for the protected updater."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import connection, transaction


GROUP_LIMIT = 25
ROW_ID_LIMIT = 20
# The profile library.0087 assigns every profile-less ScheduleBlock to
# (get_or_create by this exact name) before library.0088 enforces
# per-profile uniqueness. Kept equal to that migration's INITIAL_PROFILE_NAME.
DEFAULT_PROFILE_NAME = "Default Schedule"


def _column_exists(cursor, table, column):
    cursor.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
        [table, column],
    )
    return cursor.fetchone() is not None


def _schedule_block_duplicate_times(cursor):
    """Rows that are, or will become, duplicates under library.0088's
    per-profile constraints:

        UNIQUE (profile, day_of_week, start_time)   WHERE day_of_week IS NOT NULL
        UNIQUE (profile, specific_date, start_time) WHERE specific_date IS NOT NULL

    The effective profile is the one each row will belong to once the
    target transition finishes:

    * before library.0086 (no profile column), every block is assigned
      to the single 0087 default profile, so ALL blocks share one
      effective profile (exactly 0087's own pre-check);
    * after 0086, a block keeps its profile; a profile-less block joins
      the existing "Default Schedule" profile (0087's get_or_create), or
      a brand-new one if none exists yet.

    Two blocks at the same day/time in DIFFERENT profiles are valid and
    never reported.
    """
    has_profile = _column_exists(cursor, "library_scheduleblock", "profile_id")
    if has_profile:
        cursor.execute("SELECT id FROM library_scheduleprofile WHERE name = %s", [DEFAULT_PROFILE_NAME])
        row = cursor.fetchone()
        default_profile_id = row[0] if row else None
        # -1 is never a real primary key: profile-less rows with no existing
        # default profile form one new (future default) profile.
        effective = "COALESCE(profile_id, %s, -1)"
        effective_params = [default_profile_id]
    else:
        effective = "0"
        effective_params = []
    groups = []
    total = 0
    for kind, column in (("recurring", "day_of_week"), ("specific-date", "specific_date")):
        base = (
            f"SELECT {effective} AS effective_profile, {column}, start_time, COUNT(*) AS n, "
            f"(array_agg(id ORDER BY id))[1:{ROW_ID_LIMIT}] AS ids "
            f"FROM library_scheduleblock WHERE {column} IS NOT NULL "
            f"GROUP BY 1, {column}, start_time HAVING COUNT(*) > 1"
        )
        cursor.execute(f"SELECT COUNT(*) FROM ({base}) AS duplicate_groups", effective_params)
        total += cursor.fetchone()[0]
        cursor.execute(f"{base} ORDER BY 1, {column}, start_time LIMIT {GROUP_LIMIT}", effective_params)
        for effective_profile, key, start_time, count, ids in cursor.fetchall():
            groups.append({
                "kind": kind,
                "profile": (
                    None if not has_profile
                    else ("new default (unassigned rows)" if effective_profile == -1 else effective_profile)
                ),
                "identity": [str(key), str(start_time)],
                "count": count,
                "schedule_block_ids": list(ids),
            })
    return {
        "ok": total == 0,
        "offending_group_count": total,
        "offending_groups": groups,
        "offending_groups_truncated": total > len(groups),
        "profile_scoped": has_profile,
    }


CHECKS = {
    "library.schedule_block_duplicate_times": _schedule_block_duplicate_times,
}


def run_preflights(check_ids: list[str]) -> dict:
    unknown = [check_id for check_id in check_ids if check_id not in CHECKS]
    if unknown:
        return {
            "schema_version": 1, "status": "failed",
            "checks": [{"id": value, "status": "unknown", "evidence": {}} for value in unknown],
        }
    results = []
    with transaction.atomic():
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute("SET TRANSACTION READ ONLY")
            for check_id in check_ids:
                evidence = CHECKS[check_id](cursor)
                results.append({
                    "id": check_id,
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
    help = "Run allowlisted read-only migration preflight checks."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="append", dest="checks", required=True)

    def handle(self, *args, **options):
        payload = run_preflights(options["checks"])
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(raw.encode("utf-8")) > 65536:
            raise RuntimeError("migration preflight output exceeds 64 KiB")
        self.stdout.write(raw)
