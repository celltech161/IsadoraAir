"""Strict machine-readable schema probe executed as ISA_USER by Phase B."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


def _ref(key):
    return f"{key[0]}.{key[1]}"


def _classify_operation(operation):
    name = operation.__class__.__name__
    if name == "CreateModel":
        return {"operation": name, "classification": "additive", "detail": "new table/model"}
    if name == "AddField":
        field = getattr(operation, "field", None)
        if field is not None and getattr(field, "null", False):
            return {"operation": name, "classification": "additive", "detail": "nullable field"}
        return {"operation": name, "classification": "manual", "detail": "non-null AddField is not automatically proven safe"}
    return {"operation": name, "classification": "manual", "detail": "operation is outside the Phase B v1 automatic allowlist"}


def build_probe_payload():
    executor = MigrationExecutor(connection)
    loader = executor.loader
    conflicts = loader.detect_conflicts()
    targets = loader.graph.leaf_nodes()
    raw_plan = executor.migration_plan(targets)
    plan = []
    for migration, backwards in raw_plan:
        if backwards:
            raise RuntimeError("backward migration appeared in a forward leaf plan")
        node = loader.graph.node_map[(migration.app_label, migration.name)]
        plan.append({
            "ref": _ref((migration.app_label, migration.name)),
            "dependencies": sorted(_ref(parent.key) for parent in node.parents),
            "operations": [_classify_operation(operation) for operation in migration.operations],
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
