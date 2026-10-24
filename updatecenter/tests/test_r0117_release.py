"""r0117 (roadmap 2.23 Track A: Start Next, Eject/VT safety, Console refinements):
the release manifest says exactly what the repository does, and the trusted
migration-authorization companion -- once it exists -- authorizes exactly the
two manual operations the real classifier finds, pinned to the migration
bytes at the release commit, and the protected runtime's own validator and
coverage rule accept exactly that set and nothing less.

* application-only: no protected-runtime change (generation 15 stays;
  "protected_runtime": null), no nginx/unit/package/static/requirements change;
* three migrations: authz.0009 (RunPython, manual), library.0091 (CreateModel,
  additive), library.0092 (RunPython, manual);
* Gunicorn and the engine restart; nothing else;
* the shipped read-only verification SQL reads only and reports the
  post-install state.

Historical assertions are bounded by the unique commit that introduces the
r0117 manifest, never by HEAD, so later releases cannot break them.
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
MANIFEST = PROJECT_ROOT / "deploy" / "releases" / "r0117.json"
COMPANION = PROJECT_ROOT / "deploy" / "migration_authorizations" / "r0117.json"
VERIFY_SQL = PROJECT_ROOT / "docs" / "releases" / "r0117_verify.sql"
R0116 = "a873eb91f8c03ecd095a4195b179509111e6a2c0"        # the r0116 release commit
R0116_TIP = "4179ca3a77e86660ca3b501471a7e26bd9d7a3d4"    # published main: r0116's companion
ACCEPTED = "ba305d8b4fc416a7187f4b35fdd4c0248c2cbe6f"  # the Track A engineering tip
AUTHZ, CONFIG, ACCESS = ("authz.0009_playout_start_next_capability", "library.0091_playouttransportconfig",
                         "library.0092_remote_dj_start_next_access")
PLAN = [AUTHZ, CONFIG, ACCESS]
MIGRATION_SHA256 = {
    AUTHZ: "e6c6dfb8b14771b22b89d40722d1fc77b183b573ed75288d87f15da01de79309",
    CONFIG: "8385c5e08450a42eb2166adf430b1b8fd2f9c64f5dc1e57395eb9ead4ae164d7",
    ACCESS: "af6eca694f248d38b440f287c23a3793b2235895265afa4b92a9d8c7f85a7752",
}
MANUAL = [(AUTHZ, 0, "RunPython"), (ACCESS, 0, "RunPython")]
RESTARTS = ["isadoraair-gunicorn", "isadoraair-engine"]
RELEASE_COMMIT_FILES = {"deploy/releases/r0117.json", "docs/releases/r0117.md", "docs/releases/r0117_installation.md",
                        "docs/releases/r0117_verify.sql", "updatecenter/tests/test_r0117_release.py"}


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True,
                          capture_output=True).stdout


def _release_commit() -> str:
    """r0117's immutable introducing commit: the unique addition of its manifest."""
    commits = _git("log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--",
                   "deploy/releases/r0117.json").split()
    if len(commits) != 1:
        raise AssertionError(f"r0117.json must have exactly one introducing commit, found {commits!r}")
    return commits[0]


def _migration_path(ref: str) -> str:
    app, name = ref.split(".", 1)
    return f"{app}/migrations/{name}.py"


def _exact_plan():
    return probe._serialize_exact_plan(MigrationLoader(None, ignore_no_migrations=True), PLAN)


class R0117ManifestTests(SimpleTestCase):
    def setUp(self):
        self.raw = _show(_release_commit(), "deploy/releases/r0117.json")
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(self.data, source_label="r0117.json")

    def test_the_manifest_is_exact_and_application_only(self):
        m = self.manifest
        self.assertEqual((m.release_id, m.previous_release_id, m.minimum_updater_protocol_version),
                         ("r0117", "r0116", 5))
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
        self.assertFalse(set(self.data) - {
            "schema_version", "release_id", "previous_release_id", "minimum_updater_protocol_version", "summary",
            "migrations_required", "migration_compatibility", "manual_bootstrap_required",
            "python_requirements_changed", "requirements_sha256", "apt_packages_new", "systemd_units_changed",
            "systemd_units_new_required", "systemd_units_new_optional", "systemd_units_removed_or_renamed",
            "collectstatic_required", "services_requiring_restart", "nginx_changed", "runtime_components_changed",
            "minimum_supported_release_id", "protected_runtime"})          # no D-18 / host_prerequisites field
        self.assertEqual(self.raw, (json.dumps(self.data, indent=2) + "\n").encode())
        self.assertEqual(MANIFEST.read_bytes(), self.raw)                       # never edited afterwards

    def test_the_release_commit_fast_forwards_r0116_and_adds_only_release_metadata(self):
        release = _release_commit()
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ACCEPTED])
        _git("merge-base", "--is-ancestor", R0116_TIP, release)                 # raises if not a fast-forward
        self.assertEqual(set(_git("diff", "--name-only", ACCEPTED, release).split()), RELEASE_COMMIT_FILES)
        self.assertEqual(_git("rev-list", "--merges", f"{R0116_TIP}..{release}"), "")   # linear history

    def test_the_three_migrations_are_exactly_what_was_introduced_since_r0116(self):
        added = _git("diff", "--name-only", "--diff-filter=A", R0116, _release_commit(), "--",
                     "*/migrations/0*.py").split()
        self.assertEqual(sorted(added), sorted(_migration_path(ref) for ref in PLAN))
        self.assertEqual(_git("diff", "--name-only", "--diff-filter=MD", R0116, _release_commit(), "--",
                              "*/migrations/*.py").split(), [])                 # no existing migration touched
        for ref, sha in MIGRATION_SHA256.items():
            self.assertEqual(hashlib.sha256(_show(_release_commit(), _migration_path(ref))).hexdigest(), sha, ref)

    def test_the_real_classifier_finds_exactly_the_two_manual_operations(self):
        plan = _exact_plan()
        self.assertEqual([item["ref"] for item in plan], PLAN)
        self.assertEqual({item["ref"]: item["migration_file_sha256"] for item in plan}, MIGRATION_SHA256)
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in probe.extract_manual_operations(plan)], MANUAL)
        config = next(item for item in plan if item["ref"] == CONFIG)
        self.assertEqual([(op["operation"], op["classification"]) for op in config["operations"]],
                         [("CreateModel", "additive")])

    def test_the_data_migrations_are_reversible_runpython_only(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        for app, name in (("authz", "0009_playout_start_next_capability"),
                          ("library", "0092_remote_dj_start_next_access")):
            migration = loader.disk_migrations[(app, name)]
            self.assertEqual([type(op).__name__ for op in migration.operations], ["RunPython"], name)
            self.assertTrue(migration.operations[0].reversible, name)

    def test_no_runtime_unit_package_nginx_or_static_surface_changed(self):
        changed = set(_git("diff", "--name-only", R0116_TIP, _release_commit()).split())
        self.assertEqual({path for path in changed if path.startswith("deploy/")}, {"deploy/releases/r0117.json"})
        for forbidden in ("requirements.txt", "deploy/isadoraair.nginx", "deploy/updater_runtime/",
                          "deploy/updater_attestations/", "deploy/updater_bootstrap/"):
            self.assertFalse([path for path in changed if path.startswith(forbidden)], forbidden)
        self.assertFalse([path for path in changed if path.endswith((".service", ".timer"))])
        self.assertFalse([path for path in changed if "/static/" in path])         # hence no collectstatic
        self.assertFalse([path for path in changed if "remote_dj_signaling" in path or "signaling" in path])

    def test_the_restart_set_is_gunicorn_and_engine(self):
        self.assertTrue(set(RESTARTS) <= CORE_SERVICES)
        self.assertEqual(RESTARTS, [name for name in planner.RESTART_ORDER if name in RESTARTS])
        changed = set(_git("diff", "--name-only", R0116_TIP, _release_commit()).split())
        self.assertIn("library/services/engine.py", changed)                    # hence the engine restart
        self.assertIn("library/views.py", changed)                              # hence Gunicorn

    def test_authz_0009_is_the_only_authz_0009(self):
        names = [path for path in _git("ls-tree", "--name-only", _release_commit(), "authz/migrations/").split()
                 if Path(path).name.startswith("0009")]
        self.assertEqual(names, ["authz/migrations/0009_playout_start_next_capability.py"])


class R0117VerifySqlTests(TestCase):
    """The shipped post-install check reads only: one BEGIN TRANSACTION READ
    ONLY ... ROLLBACK frame (checked as text), and every statement inside runs
    here, inside the test's own transaction, against this migrated database."""

    def statements(self):
        lines = [line for line in VERIFY_SQL.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith(("--", "\\"))]
        return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]

    def test_it_only_reads_inside_one_read_only_transaction(self):
        statements = self.statements()
        self.assertEqual(statements[0], "BEGIN TRANSACTION READ ONLY")
        self.assertEqual(statements[-1], "ROLLBACK")
        for statement in statements[1:-1]:
            self.assertRegex(statement, r"^(SELECT\b|SET LOCAL (statement|lock)_timeout = '\d+s'$)", statement)
        self.assertFalse(re.search(r"pg_(terminate|cancel)_backend|pg_(try_)?advisory|for (update|share)",
                                   "\n".join(statements), re.IGNORECASE))

    def test_it_reports_the_post_install_state_here(self):
        results = []
        with connection.cursor() as cursor:
            for statement in self.statements()[1:-1]:
                cursor.execute(statement)
                if statement.startswith("SELECT"):
                    results.append(cursor.fetchall())
        migrations, capability, roles, forbidden, access, config = results
        self.assertEqual(len(migrations), 3)
        self.assertEqual(capability, [("playout.start_next", True)])
        self.assertEqual([row[0] for row in roles], ["Remote Host", "Station Administrator"])
        self.assertEqual(forbidden, [])
        self.assertEqual(access, [("remote_dj", True)])
        self.assertEqual(config[0][0], True)


class R0117CompanionTests(SimpleTestCase):
    """The companion is introduced AFTER the release commit (its own
    metadata-only commit). Once present it must be exact for BOTH validators,
    and the runtime's coverage rule must need both operations."""

    def companion(self):
        if not COMPANION.exists():
            self.skipTest("the r0117 companion is introduced after the release commit")
        return migration_authorization.parse_companion(COMPANION.read_bytes())

    def station(self):
        plan = _exact_plan()
        return {"plan_items": [{"ref": item["ref"], "migration_file_sha256": item["migration_file_sha256"]}
                               for item in plan],
                "manual_operations": probe.extract_manual_operations(plan)}

    def test_the_companion_binds_the_release_and_authorizes_exactly_the_two(self):
        record = self.companion()
        release = _release_commit()
        self.assertEqual((record["release_id"], record["target_commit"]), ("r0117", release))
        self.assertNotIn(record["target_commit"], (ACCEPTED, _git("rev-parse", "HEAD")))
        self.assertEqual(record["manifest_sha256"],
                         hashlib.sha256(_show(release, "deploy/releases/r0117.json")).hexdigest())
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
                          "deploy/migration_authorizations/r0117.json").split()
        self.assertEqual(len(introduced), 1)
        parents = _git("rev-list", "--parents", "-n", "1", introduced[0]).split()[1:]
        self.assertEqual(parents, [_release_commit()])
        self.assertEqual(_git("diff", "--name-only", parents[0], introduced[0]).split(),
                         ["deploy/migration_authorizations/r0117.json"])

    def test_both_validators_accept_it_at_the_trusted_tip(self):
        self.companion()
        tip, release = _git("rev-parse", "HEAD"), _release_commit()
        expected = frozenset((ref, MIGRATION_SHA256[ref], index, op, "manual") for ref, index, op in MANUAL)
        django = migration_authorization.validate_release_companion(
            PROJECT_ROOT, tip, release_id="r0117", release_commit=release, migrations_required=PLAN)
        repository = TrustedRepository(Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir")),
                                       "unused", "main", CommandRunner())
        entry = {item.manifest.release_id: item for item in load_chain(repository, tip)}["r0117"]
        self.assertEqual(entry.commit, release)
        runtime = validate_release_companion(repository, tip, entry)
        self.assertEqual((django, runtime), (expected, expected))
        with self.assertRaises(migration_authorization.CompanionError):        # bound to THIS release commit only
            migration_authorization.validate_release_companion(
                PROJECT_ROOT, tip, release_id="r0117", release_commit=ACCEPTED, migrations_required=PLAN)

    def test_runtime_coverage_needs_both_exact_operations(self):
        record = self.companion()
        station = self.station()
        full = frozenset(manual_operation_identity(entry, entry["migration_file_sha256"])
                         for entry in record["authorized_manual_operations"])
        self.assertTrue(central_authorization_covers({"r0117": full}, **station))
        variants = {
            "authz.0009 only": {i for i in full if i[0] == AUTHZ},
            "library.0092 only": {i for i in full if i[0] == ACCESS},
            "different migration bytes": {(i[0], "0" * 64, *i[2:]) for i in full},
            "renamed operation": {(*i[:3], "RunSQL", i[4]) for i in full},
        }
        for label, identities in variants.items():
            with self.subTest(label):
                self.assertNotEqual(frozenset(identities), full)
                self.assertFalse(central_authorization_covers({"r0117": frozenset(identities)}, **station))
        unexpected = dict(station, manual_operations=station["manual_operations"] + [
            {"ref": CONFIG, "operation_index": 1, "operation": "RunSQL", "classification": "manual",
             "detail": "unexpected"}])
        self.assertFalse(central_authorization_covers({"r0117": full}, **unexpected))   # an unauthorized op
