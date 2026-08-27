"""Strict machine-readable schema probe executed as ISA_USER by Phase B."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import NOT_PROVIDED


def _ref(key):
    return f"{key[0]}.{key[1]}"


_SIMPLE_LITERAL_DEFAULT_TYPES = (bool, int, float, str, bytes)
_NON_DATABASE_FIELD_METADATA = frozenset({"help_text"})


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


def build_probe_payload():
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
            "operations": operations,
        })
    nodes = {}
    for key, node in loader.graph.node_map.items():
        nodes[_ref(key)] = sorted(_ref(parent.key) for parent in node.parents)
    replacements = sorted(
        _ref(key) for key, migration in loader.disk_migrations.items()
        if getattr(migration, "replaces", None)
    )
    return {
        "schema_version": 1,
        "status": "ok",
        "plan": plan,
        "nodes": nodes,
        "applied": sorted(_ref(key) for key in loader.applied_migrations),
        "conflicts": {app: sorted(names) for app, names in sorted(conflicts.items())},
        "replacements": replacements,
    }


class Command(BaseCommand):
    help = "Emit the read-only migration graph/plan contract used by the protected Phase B updater."

    def handle(self, *args, **options):
        payload = build_probe_payload()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(raw.encode("utf-8")) > 1024 * 1024:
            raise RuntimeError("migration probe output exceeds 1 MiB")
        self.stdout.write(raw)
