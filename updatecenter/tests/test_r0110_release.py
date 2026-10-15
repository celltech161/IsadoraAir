"""r0110 (Phase D active messages, dormant): the release manifest says
exactly what the repository does, and the trusted migration-authorization
companion -- once it exists -- authorizes exactly the manual operations the
real classifier finds, pinned to the migration bytes at the release commit.

* no protected-runtime change (generation 13 stays; "protected_runtime": null);
* the six Phase-D migrations, in the probe's target order, and no other;
* classifier: exactly five manual operations (authz.0008 #0 RunPython,
  active_messages.0003 #1 RunPython, active_messages.0004 #0 AlterField /
  #1 RemoveConstraint / #2 AddConstraint);
* collectstatic, Gunicorn + engine only, no unit/package/nginx change;
* the native-airing gate is a source-level False.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase

from updatecenter import manifest as manifest_mod
from updatecenter import migration_authorization
from updatecenter.management.commands import updatecenter_probe as probe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "deploy" / "releases" / "r0110.json"
COMPANION = PROJECT_ROOT / "deploy" / "migration_authorizations" / "r0110.json"
R0109 = "6694b15672f520815b73e2b0e054639f9263d2ba"
PHASE_D_PLAN = [
    "produced_content.0001_initial",
    "active_messages.0001_initial",
    "active_messages.0002_playout_correlation",
    "active_messages.0003_message_content_type",
    "active_messages.0004_message_content_type_identity",
    "authz.0008_active_message_capabilities",
]
MANUAL = [
    ("active_messages.0003_message_content_type", 1, "RunPython"),
    ("active_messages.0004_message_content_type_identity", 0, "AlterField"),
    ("active_messages.0004_message_content_type_identity", 1, "RemoveConstraint"),
    ("active_messages.0004_message_content_type_identity", 2, "AddConstraint"),
    ("authz.0008_active_message_capabilities", 0, "RunPython"),
]


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _release_commit() -> str:
    """r0110's immutable introducing commit: the unique addition of its
    manifest. Historical assertions are bounded by it, never by HEAD, so later
    releases cannot break them."""
    commits = _git("log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--",
                   "deploy/releases/r0110.json").split()
    if len(commits) != 1:
        raise AssertionError(f"r0110.json must have exactly one introducing commit, found {commits!r}")
    return commits[0]


def _migration_path(ref: str) -> str:
    app, name = ref.split(".", 1)
    return f"{app}/migrations/{name}.py"


class R0110ManifestTests(SimpleTestCase):
    def setUp(self):
        self.raw = MANIFEST.read_bytes()
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(self.data, source_label="r0110.json")

    def test_the_manifest_is_dormant_phase_d_with_no_protected_runtime_change(self):
        m = self.manifest
        self.assertEqual((m.release_id, m.previous_release_id, m.minimum_updater_protocol_version),
                         ("r0110", "r0109", 5))
        self.assertIsNone(m.protected_runtime)
        self.assertEqual(m.migration_compatibility, "additive")
        self.assertEqual(list(m.migrations_required), PHASE_D_PLAN)
        self.assertTrue(m.collectstatic_required)
        self.assertEqual(list(m.services_requiring_restart), ["isadoraair-gunicorn", "isadoraair-engine"])
        for empty in ("apt_packages_new", "systemd_units_changed", "systemd_units_new_required",
                      "systemd_units_new_optional", "systemd_units_removed_or_renamed"):
            self.assertEqual(self.data[empty], [], empty)
        self.assertEqual((self.data["python_requirements_changed"], self.data["nginx_changed"],
                          self.data["runtime_components_changed"], self.data["manual_bootstrap_required"]),
                         (False, False, False, False))
        self.assertEqual(self.data["minimum_supported_release_id"], "r0081")
        self.assertNotIn("migration_authorization", self.data)
        self.assertEqual(self.raw, (json.dumps(self.data, indent=2) + "\n").encode())

    def test_the_six_migrations_are_exactly_those_introduced_by_r0110(self):
        added = _git("diff", "--name-only", "--diff-filter=A", R0109, _release_commit(), "--",
                     "*/migrations/0*.py").split()
        self.assertEqual(sorted(added), sorted(_migration_path(ref) for ref in PHASE_D_PLAN))

    def test_the_classifier_finds_exactly_the_five_manual_operations(self):
        plan = probe._serialize_exact_plan(MigrationLoader(None), PHASE_D_PLAN)
        self.assertEqual([item["ref"] for item in plan], PHASE_D_PLAN)
        manual = [(entry["ref"], entry["operation_index"], entry["operation"])
                  for entry in probe.extract_manual_operations(plan)]
        self.assertEqual(sorted(manual), sorted(MANUAL))
        self.assertEqual(plan[3]["operations"][0]["operation"], "AddField")       # 0003 #0: nullable, additive
        self.assertEqual(plan[3]["operations"][0]["classification"], "additive")

    def test_no_protected_runtime_unit_static_or_package_surface_changed_beyond_the_declared(self):
        changed = set(_git("diff", "--name-only", R0109, _release_commit()).split())
        for forbidden in ("deploy/updater_runtime/", "deploy/updater_attestations/", "requirements.txt",
                          "deploy/isadoraair.nginx", "ogremote/"):
            self.assertFalse([path for path in changed if path.startswith(forbidden)], forbidden)
        self.assertFalse([path for path in changed if path.startswith("deploy/") and path.endswith(
            (".service", ".timer"))])
        static = sorted(path for path in changed if "/static/" in path)
        self.assertEqual(static, ["production/static/production/iportal/workstation.js"])  # hence collectstatic

    def test_native_airing_is_unavailable_in_source(self):
        source = (PROJECT_ROOT / "active_messages" / "services.py").read_text()
        self.assertIn("\nNATIVE_AIRING_AVAILABLE = False\n", source)
        from active_messages import services
        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)


class R0110CompanionTests(SimpleTestCase):
    """The companion is introduced AFTER the release commit (its own
    metadata-only commit), so before that commit exists this only proves
    nothing stray is there. Its provenance (never modified, deleted or
    re-added) is validated against the current trusted tip, HEAD, by design."""

    def test_the_companion_authorizes_exactly_the_classifiers_manual_operations(self):
        if not COMPANION.exists():
            self.assertFalse(list(COMPANION.parent.glob("*")) if COMPANION.parent.exists() else [])
            self.skipTest("the r0110 companion is introduced after the release commit")
        release = _release_commit()
        record = migration_authorization.parse_companion(COMPANION.read_bytes())
        self.assertEqual((record["release_id"], record["target_commit"]), ("r0110", release))
        manifest_at_release = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "show", f"{release}:deploy/releases/r0110.json"],
            check=True, capture_output=True).stdout
        self.assertEqual(record["manifest_sha256"], hashlib.sha256(manifest_at_release).hexdigest())
        plan = probe._serialize_exact_plan(MigrationLoader(None), PHASE_D_PLAN)
        classified = {(entry["ref"], entry["operation_index"], entry["operation"], entry["classification"])
                      for entry in probe.extract_manual_operations(plan)}
        authorized = {(entry["ref"], entry["operation_index"], entry["operation"], entry["classification"])
                      for entry in record["authorized_manual_operations"]}
        self.assertEqual(authorized, classified)
        for entry in record["authorized_manual_operations"]:
            at_release = subprocess.run(
                ["git", "-C", str(PROJECT_ROOT), "show", f"{release}:{_migration_path(entry['ref'])}"],
                check=True, capture_output=True).stdout
            self.assertEqual(entry["migration_file_sha256"], hashlib.sha256(at_release).hexdigest(), entry["ref"])
        validated = migration_authorization.validate_release_companion(
            PROJECT_ROOT, _git("rev-parse", "HEAD"), release_id="r0110", release_commit=release,
            migrations_required=PHASE_D_PLAN)
        self.assertEqual({(ref, index, op, cls) for ref, _sha, index, op, cls in validated}, classified)
