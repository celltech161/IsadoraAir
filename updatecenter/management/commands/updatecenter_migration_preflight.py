"""Run closed, read-only migration data checks for the protected updater."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import connection, transaction


def _schedule_block_duplicate_times(cursor):
    groups = []
    for kind, columns, predicate in (
        ("recurring", ("day_of_week", "start_time"), "day_of_week IS NOT NULL"),
        ("specific-date", ("specific_date", "start_time"), "specific_date IS NOT NULL"),
    ):
        names = ", ".join(columns)
        cursor.execute(
            f"SELECT {names}, COUNT(*) FROM library_scheduleblock "
            f"WHERE {predicate} GROUP BY {names} HAVING COUNT(*) > 1 "
            f"ORDER BY {names} LIMIT 25"
        )
        for row in cursor.fetchall():
            groups.append({
                "kind": kind,
                "identity": [str(value) for value in row[:-1]],
                "count": row[-1],
            })
    return {
        "ok": not groups,
        "offending_group_count": len(groups),
        "offending_groups": groups,
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
