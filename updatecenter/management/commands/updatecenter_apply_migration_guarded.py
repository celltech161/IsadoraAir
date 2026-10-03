"""Apply exactly ONE atomic migration under a PostgreSQL ownership guard.

Called only by the protected updater (runtime 11), once per migration. In one
transaction on ONE PostgreSQL session it:

1. takes `LOCK TABLE django_migrations IN ACCESS EXCLUSIVE MODE` (with
   `SET LOCAL lock_timeout`, bounding every lock wait of the transaction),
   so every other reader/writer of django_migrations -- in
   particular an ordinary `manage.py migrate`, which must SELECT it before it
   can plan anything -- blocks until this transaction ends;
2. proves the live recorder rows of the target transition are EXACTLY the
   owned prefix the updater supplied (exact id/applied), that the requested
   migration is the first non-owned one and is absent, and that the nonce
   was never used;
3. requires Django's own plan to that target to be exactly that one forward
   migration, and that the migration is atomic;
4. applies it with the real `migrate` command, in-process, on the same
   connection (its schema_editor atomic block becomes a savepoint);
5. proves exactly one new recorder row (that migration) appeared and nothing
   else changed;
6. inserts a receipt binding nonce, job, migration and that recorder row.

Schema change, recorder row and receipt commit together, or not at all. A
refusal is raised INSIDE the transaction so everything -- including a
first-time receipt-table creation -- rolls back, and is reported outside it.

Output: {"schema_version": 1, "status": "applied", "migration", "nonce",
"row": {"id", "applied"}} or {"schema_version": 1, "status": "refused",
"reason"} (exit 0). Any other failure -- the migration itself raising, the
receipt insert failing -- rolls everything back and exits non-zero.

The receipt table is created on demand inside the guarded transaction (so it
exists only once a guarded apply has COMMITTED). Runtime code only creates,
inserts and reads receipts; nothing updates or deletes them. The protected runtime never trusts it alone: it re-observes the
recorder row and the receipt through its own database path.
"""
from __future__ import annotations

import io
import json
import re

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

RECEIPT_TABLE = "updatecenter_guarded_migration_receipt"
LOCK_TIMEOUT = "120s"
_REF = re.compile(r"^[a-z][a-z0-9_]*\.[0-9]{4}_[a-z0-9_]+$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_APPLIED = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$")
_ROWS_SQL = (
    "SELECT id, app, name, "
    "to_char(applied AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') "
    "FROM django_migrations ORDER BY id"
)


class GuardRefusal(Exception):
    """Raised inside the guarded transaction so it rolls back completely."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_owned(values) -> dict:
    owned = {}
    for value in values:
        ref, separator, identity = value.partition("=")
        row_id, comma, applied = identity.partition(",")
        if (not separator or not comma or not _REF.fullmatch(ref) or not row_id.isdigit()
                or not _APPLIED.fullmatch(applied) or ref in owned):
            raise CommandError(f"invalid --owned value {value!r}")
        owned[ref] = {"id": int(row_id), "applied": applied}
    return owned


def _backend_pid(cursor) -> int:
    cursor.execute("SELECT pg_backend_pid()")
    return cursor.fetchone()[0]


def _rows(cursor) -> list[tuple]:
    cursor.execute(_ROWS_SQL)
    return [(row_id, f"{app}.{name}", applied) for row_id, app, name, applied in cursor.fetchall()]


def apply_guarded(*, job_id: str, nonce: str, migration: str, transition: list[str], owned: dict,
                  after_proof=None, lock_timeout: str = LOCK_TIMEOUT) -> dict:
    """The guarded apply. Returns the applied result plus `session` diagnostics
    (the PostgreSQL backend pid observed at each step). Raises GuardRefusal
    after a full rollback; any other exception (a failing migration) also
    rolls everything back and propagates. `after_proof` is an internal test
    seam called while the lock is held, after the proof, before mutation."""
    if (not _UUID.fullmatch(job_id) or not _UUID4.fullmatch(nonce) or not _REF.fullmatch(migration)
            or not transition or len(set(transition)) != len(transition)
            or any(not _REF.fullmatch(ref) for ref in transition)):
        raise GuardRefusal("invalid_request")
    if list(owned) != transition[:len(owned)]:
        raise GuardRefusal("owned_entries_are_not_the_exact_transition_prefix")
    if len(owned) >= len(transition) or transition[len(owned)] != migration:
        raise GuardRefusal("migration_is_not_the_first_unowned_transition_migration")
    if connection.vendor != "postgresql":
        raise GuardRefusal("postgresql_required")
    if not re.fullmatch(r"[0-9]{1,6}(ms|s)", lock_timeout):
        raise GuardRefusal("invalid_lock_timeout")
    session = {}
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                session["begin"] = _backend_pid(cursor)
                cursor.execute(f"SET LOCAL lock_timeout = '{lock_timeout}'")
                cursor.execute("LOCK TABLE django_migrations IN ACCESS EXCLUSIVE MODE")
                session["locked"] = _backend_pid(cursor)
                before = _rows(cursor)
                transition_rows = [row for row in before if row[1] in set(transition)]
                present = [row[1] for row in transition_rows]
                if len(present) != len(set(present)):
                    raise GuardRefusal("duplicate_transition_recorder_rows")
                if migration in present:
                    raise GuardRefusal("next_migration_already_applied")
                if set(present) != set(owned):
                    raise GuardRefusal("applied_transition_differs_from_owned_prefix")
                for row_id, ref, applied in transition_rows:
                    if owned[ref] != {"id": row_id, "applied": applied}:
                        raise GuardRefusal("owned_recorder_row_identity_changed")
                cursor.execute(
                    f"CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} ("
                    "nonce uuid PRIMARY KEY, job_id uuid NOT NULL, migration text NOT NULL, "
                    "django_migration_id bigint NOT NULL UNIQUE, applied_utc text NOT NULL, "
                    "created_at timestamptz NOT NULL DEFAULT now())"
                )
                cursor.execute(f"SELECT 1 FROM {RECEIPT_TABLE} WHERE nonce = %s", [nonce])
                if cursor.fetchone() is not None:
                    raise GuardRefusal("nonce_already_used")

            app_label, name = migration.split(".", 1)
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan([(app_label, name)])
            if (len(plan) != 1 or plan[0][1] is not False
                    or (plan[0][0].app_label, plan[0][0].name) != (app_label, name)):
                raise GuardRefusal("django_plan_is_not_exactly_the_requested_migration")
            if plan[0][0].atomic is not True:
                raise GuardRefusal("NON_ATOMIC_UNSUPPORTED")
            if after_proof is not None:
                after_proof()

            # The REAL migrate command, in-process, on this same connection.
            call_command("migrate", app_label, name, interactive=False, verbosity=0,
                         skip_checks=True, stdout=io.StringIO(), stderr=io.StringIO())

            with connection.cursor() as cursor:
                session["after_migrate"] = _backend_pid(cursor)
                after = _rows(cursor)
                added = sorted(set(after) - set(before))
                if set(before) - set(after) or len(added) != 1 or added[0][1] != migration:
                    raise GuardRefusal("migrate_changed_more_than_the_requested_migration")
                row_id, _ref, applied = added[0]
                cursor.execute(
                    f"INSERT INTO {RECEIPT_TABLE} (nonce, job_id, migration, django_migration_id, applied_utc) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [nonce, job_id, migration, row_id, applied],
                )
                session["receipt"] = _backend_pid(cursor)
    except OperationalError as exc:
        # The whole transaction rolled back: nothing changed, no receipt. A
        # bounded lock wait (lock_timeout applies to EVERY lock this
        # transaction waits for, including the migration's own DDL) or being
        # chosen as a deadlock victim is retryable, never ownership.
        pgcode = getattr(getattr(exc, "__cause__", None), "pgcode", None)
        if pgcode == "55P03":
            raise GuardRefusal("lock_timeout") from exc
        if pgcode == "40P01":
            raise GuardRefusal("deadlock_victim") from exc
        raise
    return {
        "schema_version": 1, "status": "applied", "migration": migration, "nonce": nonce,
        "row": {"id": row_id, "applied": applied}, "session": session,
    }


class Command(BaseCommand):
    help = "Protected-updater only: apply exactly one atomic migration under a PostgreSQL ownership guard."

    def add_arguments(self, parser):
        parser.add_argument("--job-id", required=True)
        parser.add_argument("--nonce", required=True)
        parser.add_argument("--migration", required=True)
        parser.add_argument("--transition", action="append", required=True)
        parser.add_argument("--owned", action="append", default=[])

    def handle(self, *args, **options):
        try:
            result = apply_guarded(
                job_id=options["job_id"], nonce=options["nonce"], migration=options["migration"],
                transition=options["transition"], owned=parse_owned(options["owned"]),
            )
            payload = {key: result[key] for key in ("schema_version", "status", "migration", "nonce", "row")}
        except GuardRefusal as refusal:
            payload = {"schema_version": 1, "status": "refused", "reason": refusal.reason}
        self.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
