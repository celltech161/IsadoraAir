"""r0115 (D3D Active Messages operator board): the release manifest says exactly
what the repository does, and the trusted migration-authorization companion --
once it exists -- authorizes exactly the two manual operations the real
classifier finds, pinned to the migration bytes at the release commit, and
the protected runtime's own validator and coverage rule accept exactly that
set and nothing less.

* no protected-runtime change (generation 14 stays; "protected_runtime": null);
* one migration, active_messages.0005, and no other;
* classifier: exactly two manual operations (0005 #0 and #1, both AddIndex);
* Gunicorn only; no collectstatic/unit/package/nginx;
* the native-airing gate is a source-level False;
* the read-only database preflight shipped with the release runs, reads only.

Historical assertions are bounded by the unique commit that introduces the
r0115 manifest, never by HEAD, so later releases cannot break them.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase

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
MANIFEST = PROJECT_ROOT / "deploy" / "releases" / "r0115.json"
COMPANION = PROJECT_ROOT / "deploy" / "migration_authorizations" / "r0115.json"
PREFLIGHT = PROJECT_ROOT / "docs" / "releases" / "r0115_preflight.sql"
R0114 = "9da1b4ff74d3389fbfd3955e7ed3dcd0adca341d"        # the published r0114 release commit (no companion)
ACCEPTED = "4a9e943c8b36ba90f11ef276d94e034145aa7448"     # the independently accepted D3D candidate
REF = "active_messages.0005_execution_history_indexes"
PLAN = [REF]
MIGRATION_SHA256 = {REF: "2faa6b4aac94f7aaaa2dfede9c64cb1987a8153d9b737275c3e2ed8219d7af8e"}
MANUAL = [(REF, 0, "AddIndex"), (REF, 1, "AddIndex")]
INDEXES = ["active_execution_recent", "active_execution_members"]
RESTARTS = ["isadoraair-gunicorn"]
RELEASE_COMMIT_FILES = {"deploy/releases/r0115.json", "docs/releases/r0115.md", "docs/releases/r0115_preflight.sql",
                        "updatecenter/tests/test_r0115_release.py"}


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True,
                          capture_output=True).stdout


def _release_commit() -> str:
    """r0115's immutable introducing commit: the unique addition of its manifest."""
    commits = _git("log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--",
                   "deploy/releases/r0115.json").split()
    if len(commits) != 1:
        raise AssertionError(f"r0115.json must have exactly one introducing commit, found {commits!r}")
    return commits[0]


def _migration_path(ref: str) -> str:
    app, name = ref.split(".", 1)
    return f"{app}/migrations/{name}.py"


def _exact_plan():
    return probe._serialize_exact_plan(MigrationLoader(None, ignore_no_migrations=True), PLAN)


class R0115ManifestTests(SimpleTestCase):
    def setUp(self):
        self.raw = _show(_release_commit(), "deploy/releases/r0115.json")
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(self.data, source_label="r0115.json")

    def test_the_manifest_is_exact_with_no_protected_runtime_change(self):
        m = self.manifest
        self.assertEqual((m.release_id, m.previous_release_id, m.minimum_updater_protocol_version),
                         ("r0115", "r0114", 5))
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

    def test_the_release_commit_fast_forwards_r0114_and_adds_only_release_metadata(self):
        release = _release_commit()
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ACCEPTED])
        _git("merge-base", "--is-ancestor", R0114, release)                     # raises if not a fast-forward
        self.assertEqual(set(_git("diff", "--name-only", ACCEPTED, release).split()), RELEASE_COMMIT_FILES)
        self.assertEqual(_git("log", "--format=%H", f"{R0114}..{ACCEPTED}").split(),
                         [ACCEPTED, "0625b6d96c13a3ddfd3b45b442dc01eeb6009975",
                          "f0e9d5b524a5d54df1d063dce26245f1bfc2f302"])          # the accepted D3D history

    def test_the_one_migration_is_exactly_what_was_introduced_since_r0114(self):
        added = _git("diff", "--name-only", "--diff-filter=A", R0114, _release_commit(), "--",
                     "*/migrations/0*.py").split()
        self.assertEqual(added, [_migration_path(REF)])
        self.assertEqual(_git("diff", "--name-only", "--diff-filter=MD", R0114, _release_commit(), "--",
                              "*/migrations/*.py").split(), [])                 # no existing migration touched
        for ref, sha in MIGRATION_SHA256.items():
            self.assertEqual(hashlib.sha256(_show(_release_commit(), _migration_path(ref))).hexdigest(), sha, ref)

    def test_the_real_classifier_finds_exactly_the_two_manual_operations(self):
        plan = _exact_plan()
        self.assertEqual([item["ref"] for item in plan], PLAN)
        self.assertEqual({item["ref"]: item["migration_file_sha256"] for item in plan}, MIGRATION_SHA256)
        self.assertEqual(plan[0]["dependencies"], ["active_messages.0004_message_content_type_identity"])
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in probe.extract_manual_operations(plan)], MANUAL)

    def test_the_migration_is_two_atomic_index_builds_and_nothing_else(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.disk_migrations[("active_messages", "0005_execution_history_indexes")]
        self.assertTrue(migration.atomic)
        self.assertEqual([(type(op).__name__, op.model_name, op.index.name) for op in migration.operations],
                         [("AddIndex", "activebroadcastexecution", name) for name in INDEXES])
        recent, members = (op.index for op in migration.operations)
        self.assertEqual((type(recent).__name__, list(recent.fields)), ("Index", ["created_at", "id"]))
        self.assertEqual((type(members).__name__, list(members.fields), list(members.opclasses)),
                         ("GinIndex", ["composition_snapshot"], ["jsonb_path_ops"]))

    def test_no_undeclared_runtime_static_unit_or_package_surface_changed(self):
        changed = set(_git("diff", "--name-only", R0114, _release_commit()).split())
        for forbidden in ("deploy/updater_runtime/", "deploy/updater_attestations/", "deploy/updater_bootstrap/",
                          "requirements.txt", "deploy/isadoraair.nginx", "ogremote/", "library/services/engine.py",
                          "active_messages/playout.py", "active_messages/worker.py", "spoken/", "wx",
                          "hardware/", "monitoring/"):
            # runtime code only: D3D updated test-only migration-plan pins (e.g. spoken/tests/)
            self.assertFalse([path for path in changed if path.startswith(forbidden) and "/tests/" not in path],
                             forbidden)
        self.assertEqual({path for path in changed if "/tests/" in path and not path.startswith(
            ("active_messages/", "updatecenter/"))},
                         {"library/tests/test_voicetrack_production_media.py", "production/tests/test_migration.py",
                          "spoken/tests/test_migration.py"})
        self.assertFalse([path for path in changed if path.endswith((".service", ".timer"))])
        self.assertFalse([path for path in changed if "/static/" in path])         # hence no collectstatic
        self.assertEqual({path for path in changed if path.startswith("deploy/")}, {"deploy/releases/r0115.json"})

    def test_the_restart_set_is_gunicorn_only(self):
        self.assertTrue(set(RESTARTS) <= CORE_SERVICES)
        self.assertEqual(RESTARTS, [name for name in planner.RESTART_ORDER if name in RESTARTS])
        changed = set(_git("diff", "--name-only", R0114, _release_commit()).split())
        # The engine loads active_messages only through playout, which is unchanged and calls none of
        # the changed service functions; the model change is index metadata only.
        playout = _show(_release_commit(), "active_messages/playout.py").decode()
        for changed_function in ("activate_message", "set_message_expiry", "request_manual_replay"):
            self.assertNotIn(changed_function, playout)
        self.assertIn("active_messages/board.py", changed)

    def test_native_airing_is_unavailable_in_source(self):
        self.assertIn(b"\nNATIVE_AIRING_AVAILABLE = False\n", _show(_release_commit(), "active_messages/services.py"))
        from active_messages import services
        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)


class R0115PreflightTests(TestCase):
    """The shipped read-only preflight reads only: its frame is one
    BEGIN TRANSACTION READ ONLY ... ROLLBACK (checked as text), and every
    statement inside it runs here -- inside the test's own transaction -- and
    reports the post-install state of this migrated database."""

    def statements(self):
        lines = [line for line in PREFLIGHT.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith(("--", "\\"))]
        return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]

    def test_it_only_reads_inside_one_read_only_transaction(self):
        statements = self.statements()
        self.assertEqual(statements[0], "BEGIN TRANSACTION READ ONLY")
        self.assertEqual(statements[-1], "ROLLBACK")
        for statement in statements[1:-1]:
            self.assertRegex(statement, r"^(SELECT\b|SET LOCAL (statement|lock)_timeout = '\d+s'$)", statement)
        # Every statement is a SELECT or a timeout (above); nothing inside one may act on another session
        # or take a lock of its own.
        self.assertFalse(re.search(r"pg_(terminate|cancel)_backend|pg_(try_)?advisory|for (update|share)",
                                   "\n".join(statements), re.IGNORECASE))


    def test_it_runs_and_reports_the_post_install_state_here(self):
        results = {}
        with connection.cursor() as cursor:
            for statement in self.statements()[1:-1]:           # inside the BEGIN ... ROLLBACK frame
                cursor.execute(statement)
                if statement.startswith("SELECT"):
                    results[statement.split("\n", 1)[0]] = cursor.fetchall()
        self.assertEqual(len(results), 10)                                     # every query ran
        migrations = next(rows for key, rows in results.items() if "FROM django_migrations" in key)
        self.assertEqual(migrations[-1][0], "0005_execution_history_indexes")   # this test database is migrated
        indexes = next(rows for key, rows in results.items() if key.startswith("SELECT c.relname AS index"))
        state = {row[0]: (row[1], row[2]) for row in indexes}
        for name in INDEXES:
            self.assertEqual(state[name], (True, True), name)
        present = next(rows for key, rows in results.items() if "r0115_name_already_present" in key)
        self.assertEqual(sorted(row[0] for row in present), sorted(INDEXES))


class R0115CompanionTests(SimpleTestCase):
    """The companion is introduced AFTER the release commit (its own
    metadata-only commit). Before that commit exists this only proves nothing
    stray is there; once present it must be exact for BOTH validators, and the
    runtime's coverage rule must need both operations."""

    def companion(self):
        if not COMPANION.exists():
            self.skipTest("the r0115 companion is introduced after the release commit")
        return migration_authorization.parse_companion(COMPANION.read_bytes())

    def station(self):
        plan = _exact_plan()
        return {"plan_items": [{"ref": item["ref"], "migration_file_sha256": item["migration_file_sha256"]}
                               for item in plan],
                "manual_operations": probe.extract_manual_operations(plan)}

    def test_the_companion_binds_the_release_and_authorizes_exactly_the_two(self):
        record = self.companion()
        release = _release_commit()
        self.assertEqual((record["release_id"], record["target_commit"]), ("r0115", release))
        self.assertNotIn(record["target_commit"], (ACCEPTED, _git("rev-parse", "HEAD")))
        self.assertEqual(record["manifest_sha256"],
                         hashlib.sha256(_show(release, "deploy/releases/r0115.json")).hexdigest())
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in record["authorized_manual_operations"]], MANUAL)
        for entry in record["authorized_manual_operations"]:
            self.assertEqual((entry["migration_file_sha256"], entry["classification"]),
                             (MIGRATION_SHA256[entry["ref"]], "manual"))
        self.assertEqual(set(record), {"schema_version", "release_id", "target_commit", "manifest_sha256",
                                       "authorized_manual_operations"})
        self.assertEqual(COMPANION.read_bytes(), (json.dumps(record, indent=2) + "\n").encode())

    def test_the_companion_commit_is_single_parent_metadata_only_after_the_release(self):
        self.companion()
        introduced = _git("log", "--full-history", "--diff-filter=A", "--format=%H", "HEAD", "--",
                          "deploy/migration_authorizations/r0115.json").split()
        self.assertEqual(len(introduced), 1)
        parents = _git("rev-list", "--parents", "-n", "1", introduced[0]).split()[1:]
        self.assertEqual(parents, [_release_commit()])
        self.assertEqual(_git("diff", "--name-only", parents[0], introduced[0]).split(),
                         ["deploy/migration_authorizations/r0115.json"])

    def test_both_validators_accept_it_at_the_trusted_tip(self):
        self.companion()
        tip, release = _git("rev-parse", "HEAD"), _release_commit()
        expected = frozenset((ref, MIGRATION_SHA256[ref], index, op, "manual") for ref, index, op in MANUAL)
        django = migration_authorization.validate_release_companion(
            PROJECT_ROOT, tip, release_id="r0115", release_commit=release, migrations_required=PLAN)
        repository = TrustedRepository(Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir")),
                                       "unused", "main", CommandRunner())
        entry = {item.manifest.release_id: item for item in load_chain(repository, tip)}["r0115"]
        self.assertEqual(entry.commit, release)
        runtime = validate_release_companion(repository, tip, entry)
        self.assertEqual((django, runtime), (expected, expected))
        with self.assertRaises(migration_authorization.CompanionError):        # bound to THIS release commit only
            migration_authorization.validate_release_companion(
                PROJECT_ROOT, tip, release_id="r0115", release_commit=ACCEPTED, migrations_required=PLAN)

    def test_runtime_coverage_needs_both_exact_operations(self):
        record = self.companion()
        station = self.station()
        full = frozenset(manual_operation_identity(entry, entry["migration_file_sha256"])
                         for entry in record["authorized_manual_operations"])
        self.assertTrue(central_authorization_covers({"r0115": full}, **station))
        variants = {
            "only #0": {i for i in full if i[2] == 0},
            "only #1": {i for i in full if i[2] == 1},
            "different migration bytes": {(i[0], "0" * 64, *i[2:]) for i in full},
            "renamed operation": {(*i[:3], "AddConstraint", i[4]) for i in full},
            "another release's set": set(frozenset(manual_operation_identity(e, e["migration_file_sha256"])
                                                   for e in migration_authorization.parse_companion(
                                                       (PROJECT_ROOT / "deploy/migration_authorizations/r0113.json")
                                                       .read_bytes())["authorized_manual_operations"])),
        }
        for label, identities in variants.items():
            with self.subTest(label):
                self.assertNotEqual(frozenset(identities), full)
                self.assertFalse(central_authorization_covers({"r0115": frozenset(identities)}, **station))
        unexpected = dict(station, manual_operations=station["manual_operations"] + [
            {"ref": REF, "operation_index": 2, "operation": "RunSQL", "classification": "manual",
             "detail": "unexpected"}])
        self.assertFalse(central_authorization_covers({"r0115": full}, **unexpected))   # an unauthorized op
