"""r0118 candidate release and trusted migration-companion contract.

Historical assertions are bounded by the unique commit introducing the r0118
manifest. The companion is optional at that commit and, once added, must be a
separate metadata-only successor that authorizes exactly authz.0010 operation 0.
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

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401
from isadoraair_updater.migration_authorization import (
    central_authorization_covers, manual_operation_identity, validate_release_companion,
)
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import CORE_SERVICES, TrustedRepository, load_chain

from updatecenter import manifest as manifest_mod
from updatecenter import migration_authorization, planner
from updatecenter.management.commands import updatecenter_probe as probe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "deploy" / "releases" / "r0118.json"
COMPANION = PROJECT_ROOT / "deploy" / "migration_authorizations" / "r0118.json"
VERIFY_SQL = PROJECT_ROOT / "docs" / "releases" / "r0118_verify.sql"
R0117_TIP = "9d0e517f7233e686ef77351128f28395abd0392c"
ACCEPTED = "e95cf5731968a0928c8b811e2eca3b7295d24d64"
AUTHZ = "authz.0010_remote_dj_admin_capabilities"
PLAN = [AUTHZ]
MIGRATION_SHA256 = {AUTHZ: "d674a54a6bd88fed02e36036df662933535b129060a2f1769bb95d31695e1825"}
MANUAL = [(AUTHZ, 0, "RunPython")]
RESTARTS = ["isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring"]
RELEASE_COMMIT_FILES = {
    "deploy/releases/r0118.json", "docs/releases/r0118.md", "docs/releases/r0118_installation.md",
    "docs/releases/r0118_verify.sql", "updatecenter/tests/test_r0118_release.py",
}


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True,
                          capture_output=True).stdout


def _release_commit() -> str:
    commits = _git("log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--",
                   "deploy/releases/r0118.json").split()
    if len(commits) != 1:
        raise AssertionError(f"r0118.json must have exactly one introducing commit, found {commits!r}")
    return commits[0]


def _exact_plan():
    return probe._serialize_exact_plan(MigrationLoader(None, ignore_no_migrations=True), PLAN)


class R0118ManifestTests(SimpleTestCase):
    def setUp(self):
        self.raw = _show(_release_commit(), "deploy/releases/r0118.json")
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(self.data, source_label="r0118.json")

    def test_manifest_is_exact_and_application_only(self):
        m = self.manifest
        self.assertEqual((m.release_id, m.previous_release_id, m.minimum_updater_protocol_version),
                         ("r0118", "r0117", 5))
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
        self.assertEqual(MANIFEST.read_bytes(), self.raw)

    def test_release_commit_is_linear_and_metadata_only_after_engineering_tip(self):
        release = _release_commit()
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ACCEPTED])
        _git("merge-base", "--is-ancestor", R0117_TIP, release)
        self.assertEqual(set(_git("diff", "--name-only", ACCEPTED, release).split()), RELEASE_COMMIT_FILES)
        self.assertEqual(_git("rev-list", "--merges", f"{R0117_TIP}..{release}"), "")

    def test_authz_0010_is_the_only_new_migration_since_r0117(self):
        release = _release_commit()
        added = _git("diff", "--name-only", "--diff-filter=A", R0117_TIP, release, "--",
                     "*/migrations/0*.py").split()
        self.assertEqual(added, ["authz/migrations/0010_remote_dj_admin_capabilities.py"])
        self.assertEqual(_git("diff", "--name-only", "--diff-filter=MD", R0117_TIP, release, "--",
                              "*/migrations/*.py"), "")
        self.assertEqual(hashlib.sha256(_show(release, "authz/migrations/0010_remote_dj_admin_capabilities.py"))
                         .hexdigest(), MIGRATION_SHA256[AUTHZ])

    def test_real_classifier_finds_exactly_one_manual_operation(self):
        plan = _exact_plan()
        self.assertEqual([item["ref"] for item in plan], PLAN)
        self.assertEqual({item["ref"]: item["migration_file_sha256"] for item in plan}, MIGRATION_SHA256)
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in probe.extract_manual_operations(plan)], MANUAL)
        migration = MigrationLoader(None, ignore_no_migrations=True).disk_migrations[
            ("authz", "0010_remote_dj_admin_capabilities")]
        self.assertEqual([type(operation).__name__ for operation in migration.operations], ["RunPython"])
        self.assertTrue(migration.operations[0].reversible)

    def test_no_runtime_unit_package_nginx_static_or_dependency_surface_changed(self):
        changed = set(_git("diff", "--name-only", R0117_TIP, _release_commit()).split())
        self.assertEqual({path for path in changed if path.startswith("deploy/")},
                         {"deploy/releases/r0118.json"})
        for forbidden in ("requirements.txt", "deploy/isadoraair.nginx", "deploy/updater_runtime/",
                          "deploy/updater_attestations/", "deploy/updater_bootstrap/"):
            self.assertFalse([path for path in changed if path.startswith(forbidden)], forbidden)
        self.assertFalse([path for path in changed if path.endswith((".service", ".timer"))])
        self.assertFalse([path for path in changed if "/static/" in path])

    def test_restart_set_is_gunicorn_engine_monitoring_in_protected_order(self):
        self.assertTrue(set(RESTARTS) <= CORE_SERVICES)
        self.assertEqual(RESTARTS, [name for name in planner.RESTART_ORDER if name in RESTARTS])
        changed = set(_git("diff", "--name-only", R0117_TIP, _release_commit()).split())
        self.assertIn("library/views.py", changed)
        self.assertIn("library/services/engine.py", changed)
        self.assertIn("monitoring/services/monitor.py", changed)


class R0118VerifySqlTests(TestCase):
    def statements(self):
        lines = [line for line in VERIFY_SQL.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith(("--", "\\"))]
        return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]

    def test_sql_only_reads_inside_one_read_only_transaction(self):
        statements = self.statements()
        self.assertEqual(statements[0], "BEGIN TRANSACTION READ ONLY")
        self.assertEqual(statements[-1], "ROLLBACK")
        for statement in statements[1:-1]:
            self.assertRegex(statement, r"^(SELECT\b|SET LOCAL (statement|lock)_timeout = '\d+s'$)", statement)
        self.assertFalse(re.search(r"pg_(terminate|cancel)_backend|pg_(try_)?advisory|for (update|share)",
                                   "\n".join(statements), re.IGNORECASE))

    def test_sql_reports_expected_migration_capabilities_and_grants(self):
        results = []
        with connection.cursor() as cursor:
            for statement in self.statements()[1:-1]:
                cursor.execute(statement)
                if statement.startswith("SELECT"):
                    results.append(cursor.fetchall())
        migration, capabilities, grants, forbidden = results
        self.assertEqual(migration, [("authz", "0010_remote_dj_admin_capabilities")])
        self.assertEqual(capabilities, [("remote_dj.emergency_mute", False),
                                        ("remote_dj.force_disconnect", False)])
        self.assertEqual(grants, [("remote_dj.emergency_mute", "Station Administrator"),
                                  ("remote_dj.force_disconnect", "Station Administrator")])
        self.assertEqual(forbidden, [])


class R0118CompanionTests(SimpleTestCase):
    def companion(self):
        if not COMPANION.exists():
            self.skipTest("the r0118 companion is introduced after the release commit")
        return migration_authorization.parse_companion(COMPANION.read_bytes())

    def station(self):
        plan = _exact_plan()
        return {"plan_items": [{"ref": item["ref"], "migration_file_sha256": item["migration_file_sha256"]}
                                for item in plan],
                "manual_operations": probe.extract_manual_operations(plan)}

    def test_companion_binds_release_and_authorizes_exactly_authz_0010(self):
        record = self.companion()
        release = _release_commit()
        self.assertEqual((record["release_id"], record["target_commit"]), ("r0118", release))
        self.assertNotEqual(record["target_commit"], _git("rev-parse", "HEAD"))
        self.assertEqual(record["manifest_sha256"],
                         hashlib.sha256(_show(release, "deploy/releases/r0118.json")).hexdigest())
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in record["authorized_manual_operations"]], MANUAL)
        self.assertEqual(record["authorized_manual_operations"][0]["migration_file_sha256"],
                         MIGRATION_SHA256[AUTHZ])

    def test_companion_commit_is_single_parent_metadata_only_successor(self):
        self.companion()
        introduced = _git("log", "--full-history", "--diff-filter=A", "--format=%H", "HEAD", "--",
                          "deploy/migration_authorizations/r0118.json").split()
        self.assertEqual(len(introduced), 1)
        parents = _git("rev-list", "--parents", "-n", "1", introduced[0]).split()[1:]
        self.assertEqual(parents, [_release_commit()])
        self.assertEqual(_git("diff", "--name-only", parents[0], introduced[0]).split(),
                         ["deploy/migration_authorizations/r0118.json"])

    def test_django_and_protected_runtime_accept_exact_companion(self):
        self.companion()
        tip, release = _git("rev-parse", "HEAD"), _release_commit()
        expected = frozenset((AUTHZ, MIGRATION_SHA256[AUTHZ], 0, "RunPython", "manual") for _ in [0])
        django = migration_authorization.validate_release_companion(
            PROJECT_ROOT, tip, release_id="r0118", release_commit=release, migrations_required=PLAN)
        repository = TrustedRepository(Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir")),
                                       "unused", "main", CommandRunner())
        entry = {item.manifest.release_id: item for item in load_chain(repository, tip)}["r0118"]
        runtime = validate_release_companion(repository, tip, entry)
        self.assertEqual((django, runtime), (expected, expected))

    def test_exact_operation_is_necessary_and_sufficient_for_central_coverage(self):
        record = self.companion()
        station = self.station()
        full = frozenset(manual_operation_identity(entry, entry["migration_file_sha256"])
                         for entry in record["authorized_manual_operations"])
        self.assertTrue(central_authorization_covers({"r0118": full}, **station))
        self.assertFalse(central_authorization_covers({"r0118": frozenset()}, **station))
        wrong = {(AUTHZ, "0" * 64, 0, "RunPython", "manual")}
        self.assertFalse(central_authorization_covers({"r0118": frozenset(wrong)}, **station))
