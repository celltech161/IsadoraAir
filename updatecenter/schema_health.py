"""Read-only database schema health check -- [P0] 1.1 Phase A correction.

Direct consequence of a real production incident: at commit 5a0cb0e,
`webrequests.0008_webrequestconfig_dedication_tts` was left deliberately
unapplied on the theory that its FEATURE (shared-TTS dedication
synthesis) hadn't been cut over yet. But `0008` is a plain `AddField`
on `WebRequestConfig` -- an EXISTING singleton config model, already
declared with the new fields in `webrequests/models.py` at that same
commit, and already loaded unconditionally every ~20s by the
`web-requests-ingest` timer (`WebRequestConfig.load()`, an ordinary
`.first()`-style ORM read that selects every non-deferred column by
default). The moment that commit was deployed and the timer/process
restarted, every such read failed with `column
webrequests_webrequestconfig.dedication_tts_voice_id does not exist`
-- Django doesn't know or care that the FEATURE built on those columns
was inert; it selects the columns the model class declares, always.

The corrected principle, load-bearing for this whole module:

    SCHEMA required by the deployed Django model state may NOT be
    left unapplied indefinitely. Only FEATURE ACTIVATION (a config
    flag, a null FK, an unpopulated table) may be deferred.

This module answers one question, cheaply and without mutating
anything: **is this station's database schema fully synchronized with
whatever code is CURRENTLY LOADED in this process, right now?** It
does not care about the release-manifest chain at all -- that's a
separate question `planner.py` layers on top. The planner treats ANY
pending migration returned here as unhealthy CURRENT schema; neither
an installed-release manifest nor a future target plan may explain it
away. This module's own check would have caught the WebRequestConfig incident
immediately, the moment 5a0cb0e was deployed, with zero dependency on
any release manifest existing or being accurate.

Uses `django.db.migrations.executor.MigrationExecutor.migration_plan()`
-- the exact mechanism `manage.py migrate --check`/`showmigrations`
already use internally to answer "what would `migrate` do right now."
Pure computation: reads `django_migrations` and the loaded migration
graph, executes nothing, writes nothing.

Roadmap 1.22: a plan to the graph's LEAF nodes skips a leaf Django already
believes applied without looking at that leaf's dependencies, so a missing
`django_migrations` row for an INTERIOR migration whose dependent is still
recorded as applied reads as "nothing pending" -- a healthy schema -- while
`manage.py migrate` itself refuses the same database with
InconsistentMigrationHistory. Django's own authoritative validator,
`MigrationLoader.check_consistent_history()`, is therefore consulted BEFORE the
plan. A violation is its own status (INCONSISTENT_MIGRATION_HISTORY), never a
"pending migration" (nothing is to be applied: a recorder row is missing) and
never SCHEMA_CURRENT. Nothing here repairs the recorder."""
from __future__ import annotations

import dataclasses

from django.db import connection
from django.db.migrations.exceptions import InconsistentMigrationHistory
from django.db.migrations.executor import MigrationExecutor


class SchemaHealthStatus:
    SCHEMA_CURRENT = "schema_current"
    UNAPPLIED_MIGRATIONS_DETECTED = "unapplied_migrations_detected"
    MIGRATION_STATE_INDETERMINATE = "migration_state_indeterminate"
    # An applied migration whose prerequisite has no applied record (Django's
    # InconsistentMigrationHistory). Not pending work and never healthy.
    INCONSISTENT_MIGRATION_HISTORY = "inconsistent_migration_history"


@dataclasses.dataclass(frozen=True)
class SchemaHealth:
    status: str
    pending_migrations: tuple[str, ...]  # "app_label.migration_name", in plan order
    detail: str


def check_schema_health() -> SchemaHealth:
    """Never raises -- any failure to even compute the plan (unusual
    DB connectivity issue, a genuinely broken migration graph, an
    unexpected failure of the consistency check itself) yields
    MIGRATION_STATE_INDETERMINATE with the pending list empty, never
    SCHEMA_CURRENT by default. An indeterminate answer must never be
    silently treated as "fine" by any caller -- see planner.py's own
    handling. An inconsistent recorder history yields
    INCONSISTENT_MIGRATION_HISTORY (empty pending list, Django's own
    description in `detail`); the planner already maps every non-current
    status to the non-actionable SCHEMA_DRIFT_DETECTED."""
    try:
        executor = MigrationExecutor(connection)
        try:
            executor.loader.check_consistent_history(connection)
        except InconsistentMigrationHistory as exc:
            return SchemaHealth(
                status=SchemaHealthStatus.INCONSISTENT_MIGRATION_HISTORY,
                pending_migrations=(),
                detail=(
                    f"the Django migration history recorded in the database is inconsistent with the "
                    f"migration graph: {exc} A missing recorder row is not a pending migration; no repair "
                    f"was attempted."
                ),
            )
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
    except Exception as exc:  # noqa: BLE001 -- any failure here must fail safe, not crash /updates/
        return SchemaHealth(
            status=SchemaHealthStatus.MIGRATION_STATE_INDETERMINATE,
            pending_migrations=(),
            detail=f"could not compute the actual Django migration plan: {exc}",
        )

    pending = tuple(f"{migration.app_label}.{migration.name}" for migration, backwards in plan if not backwards)
    if not pending:
        return SchemaHealth(
            status=SchemaHealthStatus.SCHEMA_CURRENT,
            pending_migrations=(),
            detail="database schema is fully synchronized with the currently-loaded code.",
        )
    return SchemaHealth(
        status=SchemaHealthStatus.UNAPPLIED_MIGRATIONS_DETECTED,
        pending_migrations=pending,
        detail=(
            f"{len(pending)} migration(s) are unapplied against the currently-loaded "
            f"code's own model state -- schema is NOT current, regardless of whether "
            f"the feature(s) built on it are activated."
        ),
    )
