"""r0113 (D3C native regular produced content + TEST / NO AIR): the release
manifest says exactly what the repository does, and the trusted migration-
authorization companion -- once it exists -- authorizes exactly the manual
operations the real classifier finds, pinned to the migration bytes at the
release commit, and the protected runtime's own validator and coverage rule
accept exactly that set and nothing less.

* no protected-runtime change (generation 14 stays; "protected_runtime": null);
* the four D3C migrations, in the probe's target order, and no other;
* classifier: exactly five manual operations (produced_content.0003 #0/#1/#2,
  produced_content.0004 #0, production.0003 #0 -- the deliberate database
  default that keeps still-running r0112 writers working);
* Gunicorn + spoken render worker only; no collectstatic/unit/package/nginx;
* the native-airing gate is a source-level False.

Historical assertions are bounded by the unique commit that introduces the
r0113 manifest, never by HEAD, so later releases cannot break them.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401  (puts the protected runtime on sys.path)
from isadoraair_updater.migration_authorization import (
    central_authorization_covers, manual_operation_identity, validate_release_companion,
)
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import CORE_SERVICES, TrustedRepository, load_chain

from updatecenter import manifest as manifest_mod
from updatecenter import migration_authorization, planner
from updatecenter.management.commands import updatecenter_probe as probe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "deploy" / "releases" / "r0113.json"
COMPANION = PROJECT_ROOT / "deploy" / "migration_authorizations" / "r0113.json"
R0112 = "02b10fe43728acde954dd2e151dde2ea06ae1ad0"        # the installed r0112 release commit
ACCEPTED = "652aa0facc6353960d50e2b59879301f6c98ab86"     # the independently accepted r0113 candidate
TEST_CORRECTION = "2b5168bc79e268c9b578707cff6abea4442de07e"  # release preparation's one stale-test correction
PLAN = [
    "produced_content.0002_d3c_spoken_backend_and_test_take",
    "produced_content.0003_recipe_key_leading_digit",
    "produced_content.0004_backend_regular_only",
    "production.0003_media_airing_prohibited",
]
MIGRATION_SHA256 = {
    "produced_content.0002_d3c_spoken_backend_and_test_take":
        "5efdbdc1c5c9ed279aeaae492794d8870cf4e5829d710e0cbd6b133cdde96d52",
    "produced_content.0003_recipe_key_leading_digit":
        "4d699999e8e30b891352c41126b7527a2946262495b19a22db252c324b3b7d78",
    "produced_content.0004_backend_regular_only":
        "ae11abee01664d779b9643210d6a61fdde333810fa3313a870beef76f5a66c7b",
    "production.0003_media_airing_prohibited":
        "bf8b9cb037d2f8703279b6b12e2330af61f27a2991753d40704413b4e145ec89",
}
MANUAL = [
    ("produced_content.0003_recipe_key_leading_digit", 0, "RemoveConstraint"),
    ("produced_content.0003_recipe_key_leading_digit", 1, "AlterField"),
    ("produced_content.0003_recipe_key_leading_digit", 2, "AddConstraint"),
    ("produced_content.0004_backend_regular_only", 0, "AddConstraint"),
    ("production.0003_media_airing_prohibited", 0, "AddField"),
]
RESTARTS = ["isadoraair-gunicorn", "isadoraair-spoken-render-worker"]
RELEASE_COMMIT_FILES = {"deploy/releases/r0113.json", "docs/releases/r0113.md",
                        "updatecenter/tests/test_r0113_release.py"}


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True,
                          capture_output=True).stdout


def _release_commit() -> str:
    """r0113's immutable introducing commit: the unique addition of its manifest."""
    commits = _git("log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--",
                   "deploy/releases/r0113.json").split()
    if len(commits) != 1:
        raise AssertionError(f"r0113.json must have exactly one introducing commit, found {commits!r}")
    return commits[0]


def _migration_path(ref: str) -> str:
    app, name = ref.split(".", 1)
    return f"{app}/migrations/{name}.py"


def _exact_plan():
    return probe._serialize_exact_plan(MigrationLoader(None, ignore_no_migrations=True), PLAN)


class R0113ManifestTests(SimpleTestCase):
    def setUp(self):
        self.raw = _show(_release_commit(), "deploy/releases/r0113.json")
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(self.data, source_label="r0113.json")

    def test_the_manifest_is_exact_with_no_protected_runtime_change(self):
        m = self.manifest
        self.assertEqual((m.release_id, m.previous_release_id, m.minimum_updater_protocol_version),
                         ("r0113", "r0112", 5))
        self.assertIsNone(m.protected_runtime)
        self.assertEqual(m.migration_compatibility, "additive")
        self.assertEqual(list(m.migrations_required), PLAN)
        self.assertFalse(m.collectstatic_required)
        self.assertEqual(list(m.services_requiring_restart), RESTARTS)
        for empty in ("apt_packages_new", "systemd_units_changed", "systemd_units_new_required",
                      "systemd_units_new_optional", "systemd_units_removed_or_renamed"):
            self.assertEqual(self.data[empty], [], empty)
        self.assertEqual((self.data["python_requirements_changed"], self.data["requirements_sha256"],
                          self.data["nginx_changed"], self.data["runtime_components_changed"],
                          self.data["manual_bootstrap_required"]), (False, None, False, False, False))
        self.assertEqual(self.data["minimum_supported_release_id"], "r0081")
        self.assertEqual(self.raw, (json.dumps(self.data, indent=2) + "\n").encode())
        self.assertEqual(MANIFEST.read_bytes(), self.raw)                       # never edited afterwards

    def test_the_release_commit_fast_forwards_r0112_and_adds_only_release_metadata(self):
        release = _release_commit()
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [TEST_CORRECTION])
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", TEST_CORRECTION).split()[1:], [ACCEPTED])
        _git("merge-base", "--is-ancestor", R0112, release)                     # raises if not a fast-forward
        self.assertEqual(set(_git("diff", "--name-only", TEST_CORRECTION, release).split()), RELEASE_COMMIT_FILES)
        # the accepted application source is untouched: the only other change is that one test file
        self.assertEqual(_git("diff", "--name-only", ACCEPTED, TEST_CORRECTION).split(),
                         ["production/tests/test_migration.py"])

    def test_the_four_migrations_are_exactly_those_introduced_since_r0112(self):
        added = _git("diff", "--name-only", "--diff-filter=A", R0112, _release_commit(), "--",
                     "*/migrations/0*.py").split()
        self.assertEqual(sorted(added), sorted(_migration_path(ref) for ref in PLAN))
        for ref, sha in MIGRATION_SHA256.items():
            self.assertEqual(hashlib.sha256(_show(_release_commit(), _migration_path(ref))).hexdigest(), sha, ref)

    def test_the_real_classifier_finds_exactly_the_five_manual_operations(self):
        plan = _exact_plan()
        self.assertEqual([item["ref"] for item in plan], PLAN)
        self.assertEqual({item["ref"]: item["migration_file_sha256"] for item in plan}, MIGRATION_SHA256)
        manual = [(entry["ref"], entry["operation_index"], entry["operation"])
                  for entry in probe.extract_manual_operations(plan)]
        self.assertEqual(manual, MANUAL)
        self.assertEqual([op["classification"] for op in plan[0]["operations"]], ["additive", "additive"])
        self.assertEqual(plan[3]["operations"][0]["detail"], "non-null AddField uses database default")

    def test_production_0003_keeps_the_database_default_old_writers_need(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        (operation,) = loader.disk_migrations[("production", "0003_media_airing_prohibited")].operations
        field = operation.field
        self.assertEqual((field.null, field.default, field.db_default), (False, False, False))
        self.assertTrue(all(loader.disk_migrations[tuple(ref.split(".", 1))].atomic for ref in PLAN))

    def test_no_undeclared_runtime_static_unit_or_package_surface_changed(self):
        changed = set(_git("diff", "--name-only", R0112, _release_commit()).split())
        for forbidden in ("deploy/updater_runtime/", "deploy/updater_attestations/", "deploy/updater_bootstrap/",
                          "requirements.txt", "deploy/isadoraair.nginx", "ogremote/", "library/services/engine.py"):
            self.assertFalse([path for path in changed if path.startswith(forbidden)], forbidden)
        self.assertFalse([path for path in changed if path.endswith((".service", ".timer"))])
        self.assertFalse([path for path in changed if "/static/" in path])         # hence no collectstatic
        self.assertEqual({path for path in changed if path.startswith("deploy/")}, {"deploy/releases/r0113.json"})

    def test_the_restart_set_is_declared_in_protected_order_and_nothing_else(self):
        self.assertTrue(set(RESTARTS) <= CORE_SERVICES)
        self.assertEqual(RESTARTS, [name for name in planner.RESTART_ORDER if name in RESTARTS])
        changed = set(_git("diff", "--name-only", R0112, _release_commit()).split())
        self.assertIn("spoken/management/commands/spoken_render_worker.py", changed)   # the worker's own code

    def test_native_airing_is_unavailable_in_source(self):
        self.assertIn(b"\nNATIVE_AIRING_AVAILABLE = False\n", _show(_release_commit(), "active_messages/services.py"))
        from active_messages import services
        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)


class R0113CompanionTests(SimpleTestCase):
    """The companion is introduced AFTER the release commit (its own
    metadata-only commit). Before that commit exists this only proves nothing
    stray is there; once present it must be exact for BOTH validators, and the
    runtime's coverage rule must need every one of the five operations."""

    def companion(self):
        if not COMPANION.exists():
            self.skipTest("the r0113 companion is introduced after the release commit")
        return migration_authorization.parse_companion(COMPANION.read_bytes())

    def station(self):
        plan = _exact_plan()
        return {"plan_items": [{"ref": item["ref"], "migration_file_sha256": item["migration_file_sha256"]}
                               for item in plan],
                "manual_operations": probe.extract_manual_operations(plan)}

    def test_the_companion_binds_the_release_and_authorizes_exactly_the_five(self):
        record = self.companion()
        release = _release_commit()
        self.assertEqual((record["release_id"], record["target_commit"]), ("r0113", release))
        self.assertEqual(record["manifest_sha256"],
                         hashlib.sha256(_show(release, "deploy/releases/r0113.json")).hexdigest())
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in record["authorized_manual_operations"]], MANUAL)
        for entry in record["authorized_manual_operations"]:
            self.assertEqual((entry["migration_file_sha256"], entry["classification"]),
                             (MIGRATION_SHA256[entry["ref"]], "manual"))
        self.assertEqual(COMPANION.read_bytes(), (json.dumps(record, indent=2) + "\n").encode())

    def test_both_validators_accept_it_at_the_trusted_tip(self):
        self.companion()
        tip, release = _git("rev-parse", "HEAD"), _release_commit()
        expected = frozenset((ref, MIGRATION_SHA256[ref], index, op, "manual") for ref, index, op in MANUAL)
        django = migration_authorization.validate_release_companion(
            PROJECT_ROOT, tip, release_id="r0113", release_commit=release, migrations_required=PLAN)
        repository = TrustedRepository(Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir")),
                                       "unused", "main", CommandRunner())
        entry = {item.manifest.release_id: item for item in load_chain(repository, tip)}["r0113"]
        self.assertEqual(entry.commit, release)
        runtime = validate_release_companion(repository, tip, entry)
        self.assertEqual((django, runtime), (expected, expected))
        with self.assertRaises(migration_authorization.CompanionError):        # bound to THIS release commit only
            migration_authorization.validate_release_companion(
                PROJECT_ROOT, tip, release_id="r0113", release_commit=ACCEPTED, migrations_required=PLAN)

    def test_runtime_coverage_needs_all_five_exact_operations(self):
        record = self.companion()
        station = self.station()
        full = frozenset(manual_operation_identity(entry, entry["migration_file_sha256"])
                         for entry in record["authorized_manual_operations"])
        self.assertTrue(central_authorization_covers({"r0113": full}, **station))
        variants = {
            "missing production.0003": {i for i in full if i[0] != "production.0003_media_airing_prohibited"},
            "only four (pc.0003 #2 missing)": {i for i in full if (i[0], i[2]) != (
                "produced_content.0003_recipe_key_leading_digit", 2)},
            "different migration bytes": {(i[0], "0" * 64, *i[2:]) if i[0].startswith("production.") else i
                                          for i in full},
            "changed operation index": {(i[0], i[1], 1, *i[3:]) if i[0].startswith("production.") else i
                                        for i in full},
            "another release's set": set(frozenset().union(*[
                frozenset(manual_operation_identity(e, e["migration_file_sha256"])
                          for e in migration_authorization.parse_companion(
                              (PROJECT_ROOT / "deploy/migration_authorizations" / f"{rid}.json").read_bytes())
                          ["authorized_manual_operations"]) for rid in ("r0110", "r0111")])),
        }
        for label, identities in variants.items():
            with self.subTest(label):
                self.assertNotEqual(frozenset(identities), full)
                self.assertFalse(central_authorization_covers({"r0113": frozenset(identities)}, **station))
        unexpected = dict(station, manual_operations=station["manual_operations"] + [
            {"ref": "produced_content.0004_backend_regular_only", "operation_index": 1,
             "operation": "RunPython", "classification": "manual", "detail": "unexpected"}])
        self.assertFalse(central_authorization_covers({"r0113": full}, **unexpected))   # an unauthorized op
