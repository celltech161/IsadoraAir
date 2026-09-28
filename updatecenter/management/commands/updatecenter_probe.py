"""Strict machine-readable schema probe executed as ISA_USER by Phase B.

Roadmap: reviewed migration approval. When invoked with --release-id and
--target-commit (only the protected executor's TARGET-schema probe call
does this -- see executor.py's _validate_target_schema), this command
also computes a canonical migration_plan_digest over the exact plan it
just derived and independently looks up a matching
updatecenter.models.MigrationPlanApproval row. This keeps the ENTIRE
trust-sensitive recomputation inside this one process: it is always
invoked by root, as ISA_USER, against a root-staged, provenance-verified
target commit's OWN copy of this exact file -- never against the
currently-running Gunicorn process's code, and never influenced by an
ordinary Django request. See docs/UPDATE_CENTER.md's "Reviewed migration
approval" section for the complete trust-boundary reasoning."""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import NOT_PROVIDED


def _ref(key):
    return f"{key[0]}.{key[1]}"


_SIMPLE_LITERAL_DEFAULT_TYPES = (bool, int, float, str, bytes)
# help_text: pure admin-form display text, never reaches the database.
# choices (P1 2.4 Pass G): Django does not enforce `choices` at the
# database level for an ordinary CharField/PositiveSmallIntegerField/
# etc -- no CHECK constraint or native ENUM type is generated; it is
# purely an application/ModelForm validation concern (see Django's own
# BaseDatabaseSchemaEditor.alter_field(), whose db_parameters()
# comparison never inspects `choices`). Adding a new choice (e.g.
# MonitorCheck.KIND_CHOICES gaining a "weather" entry) is therefore the
# exact same kind of approved non-database metadata change as a
# help_text edit, not a schema change -- see
# ActualMonitoringWeatherKindMigrationClassificationTests in
# updatecenter/tests/test_updatecenter_probe.py for the actual on-disk
# migration this was proven against.
_NON_DATABASE_FIELD_METADATA = frozenset({"help_text", "choices"})


def _classify_add_field(operation):
    field = getattr(operation, "field", None)
    if field is None:
        return {
            "operation": "AddField",
            "classification": "manual",
            "detail": "AddField has no inspectable field definition",
        }
    if field.null:
        return {
            "operation": "AddField",
            "classification": "additive",
            "detail": "nullable field",
        }

    unsafe_reason = None
    if field.primary_key:
        unsafe_reason = "primary-key field"
    elif field.unique:
        unsafe_reason = "unique field"
    elif field.is_relation or field.remote_field is not None:
        unsafe_reason = "relational field"
    elif field.db_index:
        unsafe_reason = "indexed field"
    elif getattr(field, "generated", False):
        unsafe_reason = "generated field"
    elif getattr(field, "db_default", NOT_PROVIDED) is not NOT_PROVIDED:
        unsafe_reason = "database default"
    elif not field.has_default():
        unsafe_reason = "no explicit default"
    elif field.default is None:
        unsafe_reason = "None default"
    elif callable(field.default):
        unsafe_reason = "callable default"
    elif type(field.default) not in _SIMPLE_LITERAL_DEFAULT_TYPES:
        unsafe_reason = "non-scalar default"

    if unsafe_reason is not None:
        return {
            "operation": "AddField",
            "classification": "manual",
            "detail": f"non-null AddField uses {unsafe_reason}",
        }
    return {
        "operation": "AddField",
        "classification": "additive",
        "detail": "non-null field with explicit simple literal default",
    }


def _field_definition_without_metadata(field):
    name, path, args, kwargs = field.deconstruct()
    database_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key not in _NON_DATABASE_FIELD_METADATA
    }
    return name, path, args, database_kwargs


def _state_field(project_state, app_label, operation):
    if project_state is None or app_label is None:
        return None
    model_state = project_state.models.get(
        (app_label, operation.model_name_lower)
    )
    if model_state is None:
        return None
    return model_state.fields.get(operation.name)


def _classify_alter_field(operation, *, app_label, before_state, after_state):
    before_field = _state_field(before_state, app_label, operation)
    after_field = _state_field(after_state, app_label, operation)
    if before_field is None or after_field is None:
        return {
            "operation": "AlterField",
            "classification": "manual",
            "detail": "AlterField before/after state could not be proven",
        }
    try:
        unchanged = (
            _field_definition_without_metadata(before_field)
            == _field_definition_without_metadata(after_field)
        )
    except Exception:
        unchanged = False
    if unchanged:
        return {
            "operation": "AlterField",
            "classification": "additive",
            "detail": "field definition differs only in approved non-database metadata",
        }
    return {
        "operation": "AlterField",
        "classification": "manual",
        "detail": "AlterField changes database-affecting or unapproved field attributes",
    }


def _classify_operation(
    operation, *, app_label=None, before_state=None, after_state=None
):
    name = operation.__class__.__name__
    if name == "CreateModel":
        return {"operation": name, "classification": "additive", "detail": "new table/model"}
    if name == "AddField":
        return _classify_add_field(operation)
    if name == "AlterField":
        return _classify_alter_field(
            operation,
            app_label=app_label,
            before_state=before_state,
            after_state=after_state,
        )
    return {"operation": name, "classification": "manual", "detail": "operation is outside the Phase B v1 automatic allowlist"}


def _migration_file_sha256(migration) -> str:
    module_name = type(migration).__module__
    module = sys.modules.get(module_name) or importlib.import_module(module_name)
    with open(module.__file__, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def extract_manual_operations(plan: list[dict]) -> list[dict]:
    """Every non-additive operation across the whole plan, in plan order
    -- not just the first one encountered (unlike the executor's actual
    go/no-go loop, which the caller is responsible for). Used both to
    populate UpdateJob.migration_plan_review for operator review and as
    part of the canonical digest input below."""
    manual = []
    for item in plan:
        for index, operation in enumerate(item["operations"]):
            if operation["classification"] != "additive":
                manual.append({
                    "ref": item["ref"],
                    "operation_index": index,
                    "operation": operation["operation"],
                    "classification": operation["classification"],
                    "detail": operation["detail"],
                })
    return manual


def compute_migration_plan_digest(*, release_id: str, target_commit: str, manifest_sha256: str, plan: list[dict]) -> str:
    """Deterministic digest binding release_id, target_commit, the
    manifest's own bytes, and the COMPLETE ordered plan (every migration,
    every operation -- not just the manual subset) including each
    migration FILE's own content hash. Any change anywhere in this --
    a different target commit, a different manifest, a different
    migration's bytes, an added/removed/reordered/reclassified operation
    -- produces a different digest, so a stale approval simply stops
    matching rather than needing bespoke invalidation logic. See
    docs/UPDATE_CENTER.md's "Reviewed migration approval" section."""
    canonical = {
        "digest_schema_version": 1,
        "release_id": release_id,
        "target_commit": target_commit,
        "manifest_sha256": manifest_sha256,
        "plan": [
            {
                "ref": item["ref"],
                "migration_file_sha256": item["migration_file_sha256"],
                "operations": [
                    {
                        "operation": operation["operation"],
                        "classification": operation["classification"],
                        "detail": operation["detail"],
                    }
                    for operation in item["operations"]
                ],
            }
            for item in plan
        ],
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _lookup_approval(*, release_id: str, digest: str) -> dict:
    from updatecenter.models import MigrationPlanApproval

    row = (
        MigrationPlanApproval.objects
        .filter(target_release_id=release_id, migration_plan_digest=digest)
        .order_by("-approved_at")
        .first()
    )
    if row is None:
        return {"found": False}
    return {
        "found": True,
        "id": str(row.id),
        "approved_by": row.approved_by_username,
        "approved_at": row.approved_at.isoformat(),
    }


def build_probe_payload(*, release_id: str | None = None, target_commit: str | None = None):
    executor = MigrationExecutor(connection)
    loader = executor.loader
    conflicts = loader.detect_conflicts()
    targets = loader.graph.leaf_nodes()
    raw_plan = executor.migration_plan(targets)
    # This is the same applied-migration state Django's executor uses before
    # running a forward plan. Advancing it operation-by-operation lets the
    # read-only probe compare AlterField definitions without touching schema.
    project_state = executor._create_project_state(with_applied_migrations=True)
    plan = []
    for migration, backwards in raw_plan:
        if backwards:
            raise RuntimeError("backward migration appeared in a forward leaf plan")
        node = loader.graph.node_map[(migration.app_label, migration.name)]
        operations = []
        for operation in migration.operations:
            before_state = (
                project_state.clone()
                if operation.__class__.__name__ == "AlterField"
                else None
            )
            operation.state_forwards(migration.app_label, project_state)
            operations.append(
                _classify_operation(
                    operation,
                    app_label=migration.app_label,
                    before_state=before_state,
                    after_state=project_state,
                )
            )
        plan.append({
            "ref": _ref((migration.app_label, migration.name)),
            "dependencies": sorted(_ref(parent.key) for parent in node.parents),
            "migration_file_sha256": _migration_file_sha256(migration),
            "operations": operations,
        })
    nodes = {}
    for key, node in loader.graph.node_map.items():
        nodes[_ref(key)] = sorted(_ref(parent.key) for parent in node.parents)
    replacements = sorted(
        _ref(key) for key, migration in loader.disk_migrations.items()
        if getattr(migration, "replaces", None)
    )
    payload = {
        "schema_version": 1,
        "status": "ok",
        "plan": plan,
        "nodes": nodes,
        "applied": sorted(_ref(key) for key in loader.applied_migrations),
        "conflicts": {app: sorted(names) for app, names in sorted(conflicts.items())},
        "replacements": replacements,
        "release_id": release_id,
        "target_commit": target_commit,
        "manifest_sha256": None,
        "migration_plan_digest": None,
        # Deliberately gated on full release context below, same as the
        # digest -- "manual operations were found" is only meaningful
        # alongside "for which release," and every real consumer
        # (the executor's approval gate) only ever calls this WITH both.
        "manual_operations": [],
        "approval": None,
    }
    if release_id is not None and target_commit is not None:
        manifest_path = Path("deploy") / "releases" / f"{release_id}.json"
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        digest = compute_migration_plan_digest(
            release_id=release_id, target_commit=target_commit,
            manifest_sha256=manifest_sha256, plan=plan,
        )
        payload["manifest_sha256"] = manifest_sha256
        payload["migration_plan_digest"] = digest
        payload["manual_operations"] = extract_manual_operations(plan)
        if payload["manual_operations"]:
            payload["approval"] = _lookup_approval(release_id=release_id, digest=digest)
    return payload


class Command(BaseCommand):
    help = "Emit the read-only migration graph/plan contract used by the protected Phase B updater."

    def add_arguments(self, parser):
        parser.add_argument(
            "--release-id", default=None,
            help="Target release id (e.g. r0089). Only the protected executor's TARGET-schema "
                 "probe call supplies this; enables migration_plan_digest/approval lookup.",
        )
        parser.add_argument(
            "--target-commit", default=None,
            help="Target release's trusted commit SHA, as already independently resolved by the "
                 "caller. Embedded in the digest verbatim; this command does not itself verify it.",
        )

    def handle(self, *args, **options):
        payload = build_probe_payload(
            release_id=options.get("release_id"), target_commit=options.get("target_commit"),
        )
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(raw.encode("utf-8")) > 1024 * 1024:
            raise RuntimeError("migration probe output exceeds 1 MiB")
        self.stdout.write(raw)
