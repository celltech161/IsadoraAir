"""r0120 (roadmap 2.27 v1, hour refill, default Off): the release manifest says
exactly what the repository does, and the trusted migration-authorization
companion authorizes exactly the two manual operations the real classifier
finds in library.0096, pinned to its bytes at the release commit.

* application-only: no protected-runtime change ("protected_runtime": null),
  no nginx/unit/package/static/requirements change;
* four library migrations, 0093-0096; only 0096's RemoveConstraint and
  AlterField are manual;
* Gunicorn, then the engine, restart; nothing else;
* the protected executor, fed a REAL probe of a database at the r0119 schema,
  chooses central authorization at the companion tip and refuses (manual) at
  the release commit itself or with any narrower or altered companion;
* the shipped read-only verification SQL reads only and reports the
  post-install state.

Historical assertions are bounded by the unique commit that introduces the
r0120 manifest (and, for the companion, by the unique commit introducing it),
never by HEAD, so later releases cannot break them.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from django.core.management import call_command
from django.db import IntegrityError, connection
from django.db.migrations.loader import MigrationLoader
from django.db.models import NOT_PROVIDED
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from .phase_b_helpers import RUNTIME_ROOT, config_dict  # noqa: F401  (puts the protected runtime on sys.path)
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import ExecutionError, Executor, _strict_probe
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.migration_authorization import (
    central_authorization_covers, load_plan_authorizations, manual_operation_identity, validate_release_companion,
)
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import (
    CORE_SERVICES, ReleaseError, TrustedRepository, derive_plan, load_chain, manual_blockers,
    resolve_known_managed_units,
)
from protected_bootstrap.policy import parse_policy_dict

from updatecenter import manifest as manifest_mod
from updatecenter import migration_authorization, planner
from updatecenter.management.commands import updatecenter_probe as probe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "deploy" / "releases" / "r0120.json"
MANIFEST_PATH = "deploy/releases/r0120.json"
COMPANION_PATH = "deploy/migration_authorizations/r0120.json"
VERIFY_SQL = PROJECT_ROOT / "docs" / "releases" / "r0120_verify.sql"
R0119 = "9e7adc035e8eecb9ba474077afca1e3d3cd065cb"           # published r0119 release commit and tip
R0118_RELEASE = "3cbe9ce4bc1e134040a3d9571b3ac6523cbb535f"
ACCEPTED = "9413d4aba04610e4d3a966179fac4a1f4655dc0a"         # accepted Phase A-D + journal guard
MILESTONES = {
    "phase_a": "172c5240163cebb15c1b287309a33528ee82e9b0",
    "phase_b": "eef0e6225ad76f92b01f16ea5c9c6687f8ac4a52",
    "phase_c": "9bb844cc1a94a41c5017e53e9a8dc3feaa2652ae",
    "phase_d": "6727912d1cc52260a713c14bf5e22b715b731cd5",
    "f1": "631ac7464e58b7ea8143bef1eaaa473ca51639ed",
}
CAUSE, ORIGIN, INSTALL, GATE = ("library.0093_hour_refill_cause_policy", "library.0094_logitem_origin",
                                "library.0095_hour_refill_install", "library.0096_hour_refill_install_gate")
PLAN = [CAUSE, ORIGIN, INSTALL, GATE]
MIGRATION_SHA256 = {
    CAUSE: "06e67c175a8b2f703dd1b513664e45d2ccfa202eff45ac4fa9e2a13809396ce5",
    ORIGIN: "018238e1ebfddcc77ff95f406059dcfc3fb1011c8e1c14fcc6e53c56de3d0e8c",
    INSTALL: "2fbdc02a31b2179c28c1b0c135f951ce55e5a7772807319d401e740901834eae",
    GATE: "635ee001f385d5088952c8c201c1b3f7f4016a56d0abc5f06a71370ce917727f",
}
OPERATIONS = {
    CAUSE: [("CreateModel", "additive"), ("CreateModel", "additive")],
    ORIGIN: [("AddField", "additive")],
    INSTALL: [("AlterField", "additive"), ("CreateModel", "additive")],
    GATE: [("RemoveConstraint", "manual"), ("AlterField", "manual")],
}
MANUAL = [(GATE, 0, "RemoveConstraint"), (GATE, 1, "AlterField")]
RESTARTS = ["isadoraair-gunicorn", "isadoraair-engine"]
RELEASE_COMMIT_FILES = {
    MANIFEST_PATH, "docs/releases/r0120.md", "docs/releases/r0120_installation.md",
    "docs/releases/r0120_verify.sql", "updatecenter/tests/test_r0120_release.py",
}
ENGINEERING_SURFACE = {
    "docs/roadmap/2_27_PHASE_A_CAUSALITY_CONTRACT.md", "docs/roadmap/2_27_PHASE_B_PROPOSAL_CONTRACT.md",
    "docs/roadmap/2_27_PHASE_C_GUARDED_INSTALL_CONTRACT.md", "docs/roadmap/2_27_PHASE_D_OPERATIONAL_CONTRACT.md",
    "docs/roadmap/2_27_PHASE_D_ROLLOUT_PACKET.md", "isadoraair/settings.py", "library/admin.py",
    "library/migrations/0093_hour_refill_cause_policy.py", "library/migrations/0094_logitem_origin.py",
    "library/migrations/0095_hour_refill_install.py", "library/migrations/0096_hour_refill_install_gate.py",
    "library/models.py", "library/services/engine.py", "library/services/hour_refill_causality.py",
    "library/services/hour_refill_evidence.py", "library/services/hour_refill_install.py",
    "library/services/hour_refill_projection.py", "library/services/hour_refill_proposal.py",
    "library/services/hour_refill_runner.py", "library/services/hour_refill_status.py",
    "library/services/log_builder.py", "library/views.py",
    "webrequests/management/commands/refresh_song_request_statuses.py", "webrequests/services.py",
}
PROTECTED_PREFIXES = ("deploy/updater_runtime/", "deploy/updater_bootstrap/", "deploy/updater_attestations/")
CONSTRAINT = "hour_refill_policy_dry_run_only_phase_a"
R0118_ONLY = "authz.0010_remote_dj_admin_capabilities"      # a real migration r0120 does not declare


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True,
                          capture_output=True).stdout


def _introducing(path: str) -> list[str]:
    return _git("log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--",
                path).split()


def _release_commit() -> str:
    """r0120's immutable introducing commit: the unique addition of its manifest."""
    commits = _introducing(MANIFEST_PATH)
    if len(commits) != 1:
        raise AssertionError(f"r0120.json must have exactly one introducing commit, found {commits!r}")
    return commits[0]


def _companion_commit() -> str | None:
    """The trusted tip at which r0120 is the newest release and its companion
    exists: the companion's own introducing commit (None before it exists)."""
    commits = _introducing(COMPANION_PATH)
    if len(commits) > 1:
        raise AssertionError(f"r0120 companion must be introduced once, found {commits!r}")
    return commits[0] if commits else None


def _changed(base: str, commit: str, *filters: str) -> set[str]:
    """Paths changed between two commits. Options come before the commits
    and any pathspec (after a literal "--") after them, so git never reads a
    commit as a path."""
    options, paths = list(filters), []
    if "--" in options:
        cut = options.index("--")
        options, paths = options[:cut], options[cut:]
    return set(_git("diff", "--name-only", "--no-renames", *options, base, commit, *paths).split())


def _migration_path(ref: str) -> str:
    app, name = ref.split(".", 1)
    return f"{app}/migrations/{name}.py"


def _common_dir() -> Path:
    return Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir"))


def _repository(git_dir: Path | None = None) -> TrustedRepository:
    return TrustedRepository(git_dir or _common_dir(), "unused", "main", CommandRunner())


def _known_units():
    policy = parse_policy_dict(json.loads(_show(R0119, "deploy/updater_runtime/protected-policy.json")),
                               label="installed generation 15")
    return resolve_known_managed_units(active_policy=policy)


def _exact_plan():
    return probe._serialize_exact_plan(MigrationLoader(None, ignore_no_migrations=True), PLAN)


def _station(plan=None):
    plan = plan or _exact_plan()
    return {"plan_items": [{"ref": item["ref"], "migration_file_sha256": item["migration_file_sha256"]}
                           for item in plan],
            "manual_operations": probe.extract_manual_operations(plan)}


def _expected_identities():
    return frozenset((ref, MIGRATION_SHA256[ref], index, operation, "manual") for ref, index, operation in MANUAL)


def _companion_record(release: str) -> dict:
    """The exact companion content the release-time review authorized."""
    return {
        "schema_version": 1,
        "release_id": "r0120",
        "target_commit": release,
        "manifest_sha256": hashlib.sha256(_show(release, MANIFEST_PATH)).hexdigest(),
        "authorized_manual_operations": [
            {"ref": ref, "migration_file_sha256": MIGRATION_SHA256[ref], "operation_index": index,
             "operation": operation, "classification": "manual"}
            for ref, index, operation in MANUAL
        ],
    }


def _encode(record: dict) -> bytes:
    return (json.dumps(record, indent=2) + "\n").encode()


class SyntheticHistory:
    """A throwaway bare repository whose objects borrow (alternates) from this
    one, so variant companion commits can be built on top of the REAL r0120
    release commit with plumbing only -- no checkout, no network, never the
    canonical refs."""

    def __init__(self, root: Path):
        self.git_dir = root / "variants.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.git_dir)], check=True)
        (self.git_dir / "objects" / "info" / "alternates").write_text(str(_common_dir() / "objects") + "\n")
        self.index = root / "index"
        self.env = {**os.environ, "GIT_INDEX_FILE": str(self.index), "GIT_AUTHOR_NAME": "r0120 variant",
                    "GIT_AUTHOR_EMAIL": "variant@example.invalid", "GIT_COMMITTER_NAME": "r0120 variant",
                    "GIT_COMMITTER_EMAIL": "variant@example.invalid"}

    def git(self, *args, data: bytes | None = None) -> str:
        return subprocess.run(["git", f"--git-dir={self.git_dir}", *args], check=True, capture_output=True,
                              input=data, env=self.env).stdout.decode().strip()

    def commit(self, parent: str, files: dict[str, bytes | None], message: str = "variant") -> str:
        self.git("read-tree", parent)
        for path, data in files.items():
            if data is None:   # mode 0 removes the entry; works without a work tree
                self.git("update-index", "--index-info", data=f"0 {'0' * 40}\t{path}\n".encode())
            else:
                blob = self.git("hash-object", "-w", "--stdin", data=data)
                self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        return self.git("commit-tree", self.git("write-tree"), "-p", parent, "-m", message)

    def verdicts(self, tip: str, release: str) -> tuple:
        """(Django, protected runtime) companion verdicts at tip: identities,
        None (absent) or the string 'rejected'."""
        try:
            django = migration_authorization.validate_release_companion(
                self.git_dir, tip, release_id="r0120", release_commit=release, migrations_required=PLAN)
        except migration_authorization.CompanionError:
            django = "rejected"
        repository = _repository(self.git_dir)
        try:
            entry = {item.manifest.release_id: item for item in load_chain(repository, tip)}["r0120"]
            runtime = validate_release_companion(repository, tip, entry)
        except ReleaseError:
            runtime = "rejected"
        return django, runtime


# ---------------------------------------------------------------------------
# Release commit R: manifest, ancestry, surface, migrations
# ---------------------------------------------------------------------------

class R0120ManifestTests(SimpleTestCase):
    def setUp(self):
        self.raw = _show(_release_commit(), MANIFEST_PATH)
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(self.data, source_label="r0120.json")

    def test_the_manifest_is_exact_and_application_only(self):
        m = self.manifest
        self.assertEqual((m.release_id, m.previous_release_id, m.minimum_updater_protocol_version),
                         ("r0120", "r0119", 5))
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
        self.assertNotIn("migration_authorization", self.raw.decode())         # manifests never point at companions
        self.assertEqual(self.raw, _encode(self.data))
        self.assertEqual(MANIFEST.read_bytes(), self.raw)                       # never edited afterwards
        self.assertNotIn(_release_commit(), self.raw.decode())                  # no self-reference

    def test_restarts_are_exactly_gunicorn_then_engine_in_protected_order(self):
        self.assertTrue(set(RESTARTS) <= CORE_SERVICES)
        self.assertEqual(RESTARTS, [name for name in planner.RESTART_ORDER if name in RESTARTS])
        changed = _changed(R0119, _release_commit())
        # Why each restart is needed -- and why nothing else is.
        self.assertTrue({"library/views.py", "library/admin.py", "isadoraair/settings.py"} <= changed)
        self.assertIn("library/services/engine.py", changed)
        self.assertFalse([path for path in changed if path.startswith(
            ("monitoring/", "encoders/", "rbds/", "active_messages/", "spoken/", "produced_content/"))])


class R0120AncestryTests(SimpleTestCase):
    def test_release_commit_is_the_single_parent_child_of_the_accepted_tip(self):
        release = _release_commit()
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ACCEPTED])
        for name, commit in {"r0119": R0119, **MILESTONES}.items():
            with self.subTest(name):
                _git("merge-base", "--is-ancestor", commit, release)
        self.assertEqual(_git("rev-list", "--merges", f"{R0119}..{release}"), "")
        self.assertEqual(len(_git("rev-list", f"{R0119}..{ACCEPTED}").split()), 28)

    def test_release_commit_is_metadata_only(self):
        release = _release_commit()
        self.assertEqual(_changed(ACCEPTED, release), RELEASE_COMMIT_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=A"), RELEASE_COMMIT_FILES)

    def test_engineering_surface_is_exactly_the_reviewed_source(self):
        source = {path for path in _changed(R0119, ACCEPTED)
                  if not path.startswith(("library/tests/", "webrequests/tests/"))}
        self.assertEqual(source, ENGINEERING_SURFACE)

    def test_historical_release_artifacts_are_untouched(self):
        history = _changed(R0119, _release_commit(), "--diff-filter=MDR", "--", "deploy/releases",
                           "deploy/migration_authorizations", "docs/releases", "updatecenter/tests")
        self.assertEqual(history, set())

    def test_no_package_unit_nginx_static_dependency_or_protected_runtime_change(self):
        changed = _changed(R0119, _release_commit())
        self.assertEqual({path for path in changed if path.startswith("deploy/")}, {MANIFEST_PATH})
        self.assertFalse([path for path in changed if path.startswith(PROTECTED_PREFIXES)])
        self.assertFalse(changed & {"requirements.txt", "deploy/isadoraair.nginx"})
        self.assertFalse([path for path in changed if path.endswith((".service", ".timer"))])
        self.assertFalse([path for path in changed if "/static/" in path or "/templates/" in path])


class R0120MigrationTests(SimpleTestCase):
    def test_the_four_migrations_are_exactly_what_was_introduced_since_r0119(self):
        release = _release_commit()
        self.assertEqual(sorted(_changed(R0119, release, "--diff-filter=A", "--", "*/migrations/0*.py")),
                         [_migration_path(ref) for ref in PLAN])
        self.assertEqual(_changed(R0119, release, "--diff-filter=MDR", "--", "*/migrations/*.py"), set())
        for ref in PLAN:
            with self.subTest(ref):
                self.assertEqual(hashlib.sha256(_show(release, _migration_path(ref))).hexdigest(),
                                 MIGRATION_SHA256[ref])
                self.assertEqual((PROJECT_ROOT / _migration_path(ref)).read_bytes(),
                                 _show(release, _migration_path(ref)))

    def test_the_real_classifier_finds_exactly_the_two_0096_manual_operations(self):
        plan = _exact_plan()
        self.assertEqual([item["ref"] for item in plan], PLAN)
        self.assertEqual({item["ref"]: item["migration_file_sha256"] for item in plan}, MIGRATION_SHA256)
        self.assertEqual({item["ref"]: [(op["operation"], op["classification"]) for op in item["operations"]]
                          for item in plan}, OPERATIONS)
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in probe.extract_manual_operations(plan)], MANUAL)
        self.assertEqual([item["dependencies"] for item in plan],
                         [["library.0092_remote_dj_start_next_access"], [CAUSE], [ORIGIN], [INSTALL]])

    def test_0096_is_exactly_the_reviewed_constraint_removal_and_metadata_change(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).disk_migrations[
            ("library", "0096_hour_refill_install_gate")]
        remove, alter = migration.operations
        self.assertEqual((type(remove).__name__, remove.model_name, remove.name),
                         ("RemoveConstraint", "hourrefillpolicy", CONSTRAINT))
        self.assertEqual((type(alter).__name__, alter.model_name, alter.name),
                         ("AlterField", "hourrefillpolicy", "dry_run_only"))
        self.assertIs(alter.field.default, True)
        self.assertTrue(alter.field.editable)
        self.assertFalse(alter.field.null)
        self.assertIs(alter.field.db_default, NOT_PROVIDED)              # no database default is added
        source = _show(_release_commit(), _migration_path(GATE)).decode()
        body = source.split("operations = [", 1)[1]
        for forbidden in ("RunPython", "RunSQL", "update(", "objects", "enabled"):
            self.assertNotIn(forbidden, body, forbidden)


class R0120SqlTests(TestCase):
    def test_0096_forward_sql_only_drops_the_constraint_and_reverse_only_adds_it(self):
        forward, backward = io.StringIO(), io.StringIO()
        call_command("sqlmigrate", "library", "0096_hour_refill_install_gate", stdout=forward)
        call_command("sqlmigrate", "library", "0096_hour_refill_install_gate", backwards=True, stdout=backward)

        def statements(text):
            return [line for line in text.splitlines() if line.strip() and not line.startswith("--")
                    and line not in ("BEGIN;", "COMMIT;")]

        self.assertEqual(statements(forward.getvalue()),
                         [f'ALTER TABLE "library_hourrefillpolicy" DROP CONSTRAINT "{CONSTRAINT}";'])
        self.assertEqual(statements(backward.getvalue()),
                         [f'ALTER TABLE "library_hourrefillpolicy" ADD CONSTRAINT "{CONSTRAINT}" '
                          f'CHECK ("dry_run_only");'])
        for ref in (CAUSE, ORIGIN, INSTALL):   # 0093-0095 never touch an existing table except origin's ADD
            out = io.StringIO()
            call_command("sqlmigrate", "library", ref.split(".", 1)[1], stdout=out)
            sql = "\n".join(statements(out.getvalue()))
            self.assertNotRegex(sql, r"\b(DROP|DELETE|UPDATE|TRUNCATE|RENAME)\b")
            altered = re.findall(r'ALTER TABLE "(\w+)" (.*?);', sql)
            self.assertEqual(altered, [("library_logitem", 'ADD COLUMN "origin" varchar(24) NULL')]
                             if ref == ORIGIN else [])


class R0120GateMigrationBehaviourTests(TransactionTestCase):
    """Disposable development test database only: 0096 forward and reverse
    against a realistic pre-migration policy row, and with no row at all."""

    def tearDown(self):
        call_command("migrate", verbosity=0)

    def constraint_present(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_constraint WHERE conname = %s", [CONSTRAINT])
            return cursor.fetchone()[0] == 1

    def column_default(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT column_default, is_nullable FROM information_schema.columns "
                           "WHERE table_name = 'library_hourrefillpolicy' AND column_name = 'dry_run_only'")
            return cursor.fetchone()

    def rows(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, enabled, capture_request_substitution, capture_operator_eject, "
                           "dry_run_only, updated_at FROM library_hourrefillpolicy ORDER BY id")
            return cursor.fetchall()

    def test_absent_row_stays_absent_and_the_constraint_is_removed(self):
        call_command("migrate", "library", "0095", verbosity=0)
        self.assertTrue(self.constraint_present())
        before_default = self.column_default()
        call_command("migrate", "library", "0096", verbosity=0)
        self.assertFalse(self.constraint_present())
        self.assertEqual(self.rows(), [])                       # nothing creates a policy row
        self.assertEqual(self.column_default(), before_default)
        self.assertEqual(before_default, (None, "NO"))          # no DB default before or after; Django supplies True

    def test_a_realistic_row_is_preserved_byte_for_byte_and_reverse_needs_dry_run(self):
        call_command("migrate", "library", "0095", verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO library_hourrefillpolicy (id, enabled, capture_request_substitution, "
                           "capture_operator_eject, dry_run_only, updated_at) "
                           "VALUES (1, false, true, false, true, '2026-10-01T12:00:00Z')")
        before = self.rows()
        call_command("migrate", "library", "0096", verbosity=0)
        self.assertEqual(self.rows(), before)
        self.assertFalse(self.constraint_present())
        # Reverse with every row in dry run: the constraint returns, the row is unchanged.
        call_command("migrate", "library", "0095", verbosity=0)
        self.assertTrue(self.constraint_present())
        self.assertEqual(self.rows(), before)
        # Forward again, leave dry run: reverse now refuses and changes nothing.
        call_command("migrate", "library", "0096", verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE library_hourrefillpolicy SET dry_run_only = false WHERE id = 1")
        with self.assertRaises(IntegrityError):
            call_command("migrate", "library", "0095", verbosity=0)
        self.assertFalse(self.constraint_present())
        with connection.cursor() as cursor:
            cursor.execute("UPDATE library_hourrefillpolicy SET dry_run_only = true WHERE id = 1")


class R0120VerifySqlTests(TestCase):
    """The shipped post-install check reads only: one BEGIN TRANSACTION READ
    ONLY ... ROLLBACK frame (checked as text), and every statement inside runs
    here, inside the test's own transaction, against this migrated database."""

    def statements(self):
        lines = [line for line in _show(_release_commit(), "docs/releases/r0120_verify.sql").decode().splitlines()
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

    def test_it_reports_the_post_install_feature_off_state_here(self):
        results = []
        with connection.cursor() as cursor:
            for statement in self.statements()[1:-1]:
                cursor.execute(statement)
                if statement.startswith("SELECT"):
                    results.append(cursor.fetchall())
        migrations, constraint, policy, counts = results
        self.assertEqual(migrations, [("library", ref.split(".", 1)[1]) for ref in PLAN])
        self.assertEqual(constraint, [])
        self.assertEqual(policy, [])
        self.assertEqual(counts, [(0, 0, 0)])


# ---------------------------------------------------------------------------
# Companion commit C and both validators
# ---------------------------------------------------------------------------

class R0120CompanionTests(SimpleTestCase):
    """The companion is introduced AFTER the release commit (its own
    metadata-only commit). Once present it must be exact for BOTH validators,
    and the runtime's coverage rule must need both operations."""

    def companion(self):
        tip = _companion_commit()
        if tip is None:
            self.skipTest("the r0120 companion is introduced after the release commit")
        return tip, migration_authorization.parse_companion(_show(tip, COMPANION_PATH))

    def test_the_companion_binds_the_release_and_authorizes_exactly_the_two(self):
        tip, record = self.companion()
        release = _release_commit()
        raw = _show(tip, COMPANION_PATH)
        self.assertEqual(raw, _encode(_companion_record(release)))
        self.assertEqual((record["release_id"], record["target_commit"]), ("r0120", release))
        self.assertNotIn(record["target_commit"], (ACCEPTED, tip))
        self.assertEqual(record["manifest_sha256"], hashlib.sha256(_show(release, MANIFEST_PATH)).hexdigest())
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in record["authorized_manual_operations"]], MANUAL)
        self.assertNotIn(tip, raw.decode())                                     # no reference to C itself

    def test_the_companion_commit_is_single_parent_metadata_only_after_the_release(self):
        tip, _record = self.companion()
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", tip).split()[1:], [_release_commit()])
        self.assertEqual(_git("diff", "--name-only", "--no-renames", _release_commit(), tip).split(),
                         [COMPANION_PATH])
        self.assertEqual(_git("log", "--full-history", "--format=%H", "HEAD", "--", COMPANION_PATH).split(),
                         [tip])                                                 # never modified afterwards
        self.assertEqual(sorted(_git("ls-tree", "--name-only", tip, "deploy/migration_authorizations/").split()),
                         sorted(_git("ls-tree", "--name-only", R0119, "deploy/migration_authorizations/").split()
                                + [COMPANION_PATH]))

    def test_both_validators_accept_it_at_the_trusted_tip(self):
        tip, _record = self.companion()
        release = _release_commit()
        django = migration_authorization.validate_release_companion(
            PROJECT_ROOT, tip, release_id="r0120", release_commit=release, migrations_required=PLAN)
        repository = _repository()
        entry = {item.manifest.release_id: item for item in load_chain(repository, tip)}["r0120"]
        self.assertEqual(entry.commit, release)
        runtime = validate_release_companion(repository, tip, entry)
        self.assertEqual((django, runtime), (_expected_identities(), _expected_identities()))
        with self.assertRaises(migration_authorization.CompanionError):        # bound to THIS release commit only
            migration_authorization.validate_release_companion(
                PROJECT_ROOT, tip, release_id="r0120", release_commit=ACCEPTED, migrations_required=PLAN)

    def test_runtime_coverage_needs_both_exact_operations(self):
        self.companion()
        full = _expected_identities()
        station = _station()
        self.assertTrue(central_authorization_covers({"r0120": full}, **station))
        variants = {
            "RemoveConstraint only": {i for i in full if i[2] == 0},
            "AlterField only": {i for i in full if i[2] == 1},
            "different 0096 bytes": {(i[0], "0" * 64, *i[2:]) for i in full},
            "wrong operation index": {(*i[:2], i[2] + 1, *i[3:]) for i in full},
            "wrong operation type": {(*i[:3], "RunSQL", i[4]) for i in full},
        }
        for label, identities in variants.items():
            with self.subTest(label):
                self.assertNotEqual(frozenset(identities), full)
                self.assertFalse(central_authorization_covers({"r0120": frozenset(identities)}, **station))
        self.assertFalse(central_authorization_covers({}, **station))           # no companion at all
        # The station's own 0096 bytes differ from the reviewed bytes.
        altered = dict(station, plan_items=[dict(item, migration_file_sha256="f" * 64) if item["ref"] == GATE
                                            else item for item in station["plan_items"]])
        self.assertFalse(central_authorization_covers({"r0120": full}, **altered))
        # An unexpected extra manual operation anywhere in the plan.
        unexpected = dict(station, manual_operations=station["manual_operations"] + [
            {"ref": INSTALL, "operation_index": 1, "operation": "RunSQL", "classification": "manual",
             "detail": "unexpected"}])
        self.assertFalse(central_authorization_covers({"r0120": full}, **unexpected))


class R0120CompanionNegativeHistoryTests(SimpleTestCase):
    """Variant companions committed (in a throwaway repository) directly on
    the REAL r0120 release commit: each must fail closed in BOTH validators,
    or validate as a narrower set that no longer covers the station's plan."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._temp = tempfile.TemporaryDirectory()
        cls.history = SyntheticHistory(Path(cls._temp.name))
        cls.release = _release_commit()

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()
        super().tearDownClass()

    def variant(self, **changes) -> bytes:
        record = _companion_record(self.release)
        operations = changes.pop("operations", None)
        if operations is not None:
            record["authorized_manual_operations"] = operations
        record.update(changes)
        return _encode(record)

    def test_the_exact_companion_is_accepted_by_both(self):
        tip = self.history.commit(self.release, {COMPANION_PATH: self.variant()})
        self.assertEqual(self.history.verdicts(tip, self.release), (_expected_identities(),) * 2)

    def test_no_companion_contributes_nothing_in_both(self):
        self.assertEqual(self.history.verdicts(self.release, self.release), (None, None))

    def test_wrong_release_commit_wrong_manifest_and_wrong_0096_bytes_are_rejected_by_both(self):
        good = _companion_record(self.release)["authorized_manual_operations"]
        cases = {
            "wrong R": self.variant(target_commit=ACCEPTED),
            "wrong manifest sha": self.variant(manifest_sha256="0" * 64),
            "altered 0096 bytes": self.variant(operations=[dict(op, migration_file_sha256="1" * 64) for op in good]),
            "undeclared migration": self.variant(operations=good + [dict(good[0], ref=R0118_ONLY)]),
        }
        for label, raw in cases.items():
            with self.subTest(label):
                tip = self.history.commit(self.release, {COMPANION_PATH: raw})
                self.assertEqual(self.history.verdicts(tip, self.release), ("rejected", "rejected"))

    def test_missing_or_misidentified_operations_validate_but_never_cover_the_plan(self):
        good = _companion_record(self.release)["authorized_manual_operations"]
        station = _station()
        cases = {
            "missing RemoveConstraint": [good[1]],
            "missing AlterField": [good[0]],
            "wrong index": [good[0], dict(good[1], operation_index=2)],
            "wrong type": [good[0], dict(good[1], operation="AddField")],
        }
        for label, operations in cases.items():
            with self.subTest(label):
                tip = self.history.commit(self.release, {COMPANION_PATH: self.variant(operations=operations)})
                django, runtime = self.history.verdicts(tip, self.release)
                self.assertEqual(django, runtime)
                self.assertNotEqual(runtime, _expected_identities())
                self.assertFalse(central_authorization_covers({"r0120": runtime}, **station))

    def test_bad_provenance_is_rejected_by_both(self):
        exact = self.variant()
        introduced = self.history.commit(self.release, {COMPANION_PATH: exact})
        cases = {
            "modified after introduction": self.history.commit(
                introduced, {COMPANION_PATH: exact.replace(b'"r0120"', b'"r0120" ')}),
            "deleted after introduction": self.history.commit(introduced, {COMPANION_PATH: None}),
            "introduced together with another file": self.history.commit(
                self.release, {COMPANION_PATH: exact, "README.md": b"not metadata\n"}),
        }
        for label, tip in cases.items():
            with self.subTest(label):
                self.assertEqual(self.history.verdicts(tip, self.release), ("rejected", "rejected"))
        # A companion inside the release commit itself is never "after" it.
        files = {path: _show(self.release, path) for path in RELEASE_COMMIT_FILES}
        same = self.history.commit(ACCEPTED, {COMPANION_PATH: exact, **files})
        self.assertEqual(self.history.verdicts(same, same), ("rejected", "rejected"))


# ---------------------------------------------------------------------------
# Trusted plan and the protected executor's authorization decision
# ---------------------------------------------------------------------------

class R0120TrustedPlanTests(SimpleTestCase):
    def setUp(self):
        self.tip = _companion_commit()
        if self.tip is None:
            self.skipTest("the trusted tip is the companion commit")
        self.repository = _repository()
        self.known = _known_units()

    def test_chain_ends_r0118_r0119_r0120_singly_linked(self):
        chain = load_chain(self.repository, self.tip)
        tail = [(entry.manifest.release_id, entry.commit) for entry in chain[-3:]]
        self.assertEqual(tail, [("r0118", R0118_RELEASE), ("r0119", R0119), ("r0120", _release_commit())])
        self.assertEqual(chain[-1].manifest.previous_release_id, "r0119")

    def test_trusted_plan_from_r0119_targets_r_with_central_authorization(self):
        plan = derive_plan(self.repository, self.tip, R0119, "r0120", known_units=self.known)
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0119", "r0120", _release_commit()))
        self.assertNotEqual(plan.target_commit, self.tip)                     # stations install R, not C
        self.assertEqual(plan.releases_in_plan, ("r0120",))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), (tuple(PLAN), "additive"))
        self.assertEqual(plan.services_requiring_restart, tuple(RESTARTS))
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed, plan.systemd_units_new_required,
                          plan.systemd_units_new_optional, plan.systemd_units_removed_or_renamed),
                         ((), (), (), (), ()))
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed, plan.runtime_components_changed,
                          plan.python_requirements_changed, plan.manual_bootstrap_required),
                         (False, False, False, False, False))
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=self.known), ())
        self.assertEqual(load_plan_authorizations(self.repository, self.tip, plan),
                         {"r0120": _expected_identities()})

    def test_trusted_plan_from_r0118_crosses_r0119_and_needs_only_the_r0120_companion(self):
        plan = derive_plan(self.repository, self.tip, R0118_RELEASE, "r0120", known_units=self.known)
        self.assertEqual(plan.releases_in_plan, ("r0119", "r0120"))
        self.assertEqual(plan.migrations_required, tuple(PLAN))
        self.assertEqual(plan.services_requiring_restart, tuple(RESTARTS))
        self.assertEqual(manual_blockers(plan, known_units=self.known), ())
        self.assertEqual(load_plan_authorizations(self.repository, self.tip, plan),
                         {"r0120": _expected_identities()})          # r0119 has no companion


class R0120ProtectedExecutorTests(TransactionTestCase):
    """The protected executor's own _validate_target_schema, fed the REAL
    updatecenter_probe payload of a development test database rolled back to
    the r0119 schema (library at 0092), with the REAL trusted repository and
    plan. Limit: the current-schema probe is modelled as the r0119 graph
    (this graph without the four r0120 nodes) rather than produced by r0119
    source; the target probe, classifier, digest and authorization are real."""

    def setUp(self):
        self.tip = _companion_commit()
        if self.tip is None:
            self.skipTest("the trusted tip is the companion commit")
        self.release = _release_commit()
        self.repository = _repository()
        self.plan = derive_plan(self.repository, self.tip, R0119, "r0120", known_units=_known_units())
        self.assertEqual(MANIFEST.read_bytes(), _show(self.release, MANIFEST_PATH))  # probe hashes the working copy
        self.addCleanup(call_command, "migrate", verbosity=0)    # registered first, so it always runs last
        call_command("migrate", "library", "0092", verbosity=0)
        cwd = os.getcwd()
        os.chdir(PROJECT_ROOT)
        try:
            raw = json.dumps(probe.build_probe_payload(release_id="r0120", target_commit=self.release),
                             sort_keys=True, separators=(",", ":")).encode()
        finally:
            os.chdir(cwd)
        self.payload = _strict_probe(raw, review_context=True)
        self.current = {"nodes": {ref: deps for ref, deps in self.payload["nodes"].items() if ref not in PLAN},
                        "applied": list(self.payload["applied"])}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
        self.store = JobStore(config.jobs_root, config.logs_root, acquire_daemon_lock=False)
        self.addCleanup(self.store.close)
        self.executor = Executor(config, self.store, CommandRunner())
        self.executor.repository = self.repository

    def validate(self, trusted_tip, plan=None):
        plan = plan or self.plan
        job_id = str(uuid.uuid4())
        self.store.accept(job_id, "r0120", plan.fingerprint)
        actual = self.executor._validate_target_schema(
            plan, self.payload, self.current, job_id, migration_already_started=False, trusted_tip=trusted_tip)
        return actual, job_id

    def test_the_real_probe_plan_is_exactly_the_reviewed_plan(self):
        self.assertEqual([item["ref"] for item in self.payload["plan"]], PLAN)
        self.assertEqual({item["ref"]: item["migration_file_sha256"] for item in self.payload["plan"]},
                         MIGRATION_SHA256)
        self.assertEqual({item["ref"]: [(op["operation"], op["classification"]) for op in item["operations"]]
                          for item in self.payload["plan"]}, OPERATIONS)
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in self.payload["manual_operations"]], MANUAL)
        self.assertEqual((self.payload["release_id"], self.payload["target_commit"], self.payload["manifest_sha256"]),
                         ("r0120", self.release, hashlib.sha256(_show(self.release, MANIFEST_PATH)).hexdigest()))
        self.assertEqual(self.payload["conflicts"], {})
        self.assertEqual(self.payload["replacements"], [])

    def test_the_companion_tip_is_authorized_centrally(self):
        actual, job_id = self.validate(self.tip)
        self.assertEqual(list(actual), PLAN)
        self.assertEqual(self.executor.last_authorization_source, "central")
        self.assertIn("trusted companion authorization(s) for ['r0120'] cover every one of this plan's 2 "
                      "manual operation(s); proceeding", self.store.tail_log(job_id, 65536))

    def test_without_the_companion_the_release_stops_for_exact_local_review(self):
        with self.assertRaises(ExecutionError) as caught:
            self.validate(self.release)
        self.assertEqual(caught.exception.classification, "MIGRATION_OPERATION_MANUAL")
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in caught.exception.migration_plan_review["manual_operations"]], MANUAL)

    def test_a_narrower_companion_stops_for_exact_local_review(self):
        with tempfile.TemporaryDirectory() as directory:
            history = SyntheticHistory(Path(directory))
            record = _companion_record(self.release)
            record["authorized_manual_operations"] = record["authorized_manual_operations"][:1]
            tip = history.commit(self.release, {COMPANION_PATH: _encode(record)})
            self.executor.repository = _repository(history.git_dir)
            plan = derive_plan(self.executor.repository, tip, R0119, "r0120", known_units=_known_units())
            with self.assertRaises(ExecutionError) as caught:
                self.validate(tip, plan)
        self.assertEqual(caught.exception.classification, "MIGRATION_OPERATION_MANUAL")

    def test_a_badly_provenanced_companion_fails_the_job_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            history = SyntheticHistory(Path(directory))
            exact = _encode(_companion_record(self.release))
            introduced = history.commit(self.release, {COMPANION_PATH: exact})
            tip = history.commit(introduced, {COMPANION_PATH: exact + b"\n"})
            self.executor.repository = _repository(history.git_dir)
            plan = derive_plan(self.executor.repository, tip, R0119, "r0120", known_units=_known_units())
            with self.assertRaises(ReleaseError):
                self.validate(tip, plan)


class R0120ManifestValidatorTests(SimpleTestCase):
    def test_validate_release_manifests_passes_at_head(self):
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        self.assertNotIn("FAIL", out.getvalue() + err.getvalue())
        self.assertIn("r0119 -> r0120", out.getvalue())
