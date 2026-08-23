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
graph, executes nothing, writes nothing."""
from __future__ import annotations

import dataclasses

from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class SchemaHealthStatus:
    SCHEMA_CURRENT = "schema_current"
    UNAPPLIED_MIGRATIONS_DETECTED = "unapplied_migrations_detected"
    MIGRATION_STATE_INDETERMINATE = "migration_state_indeterminate"


@dataclasses.dataclass(frozen=True)
class SchemaHealth:
    status: str
    pending_migrations: tuple[str, ...]  # "app_label.migration_name", in plan order
    detail: str


def check_schema_health() -> SchemaHealth:
    """Never raises -- any failure to even compute the plan (unusual
    DB connectivity issue, a genuinely broken migration graph) yields
    MIGRATION_STATE_INDETERMINATE with the pending list empty, never
    SCHEMA_CURRENT by default. An indeterminate answer must never be
    silently treated as "fine" by any caller -- see planner.py's own
    handling."""
    try:
        executor = MigrationExecutor(connection)
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
