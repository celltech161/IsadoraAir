"""r0121 release contract: exact artifacts, two reviewed migrations, seven
manual constraint operations, default-Off behavior, and a later immutable
trusted companion. Historical assertions anchor to R, never to a future HEAD.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TransactionTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401  (protected runtime on sys.path)
from isadoraair_updater.migration_authorization import (
    central_authorization_covers,
    validate_release_companion as runtime_validate_companion,
)
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import CORE_SERVICES, TrustedRepository, load_chain

from library.models import HourRefillPolicy
from updatecenter import manifest as manifest_mod
from updatecenter import migration_authorization, planner
from updatecenter.management.commands import updatecenter_probe as probe


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Linear snapshot of the qualified (unpublished, merge-built) candidate
# e431c8035ea38cf656a58cd8e04fe3e474de6e7c: same tree, single parent r0120.
ENGINEERING = "83e95f8cfbfc86efd8ac4d83a003d0d078742e4c"
ENGINEERING_TREE = "beedcc3531f2a84ecdbc9d9177d46d8bd97132b7"
R0120 = "07fd80bf44406b9219dc91d144f8a336c50e1796"
TRUSTED_R0120_TIP = "877a123f5721e796513555d8de389d925c6d17f3"
# Exact reviewed historical-test bytes, as blobs: the fix commits
# e505933 (r0119) and 65ae049 (r0120) are not in the published history.
R0119_TEST_FIX_BLOB = "243fee6f846e1adc2ec4fb493cd57882176470f1"
R0120_TEST_FIX_BLOB = "d092e089d922dcdd4e271fc1e1bafd76d5a93645"
MANIFEST_PATH = "deploy/releases/r0121.json"
COMPANION_PATH = "deploy/migration_authorizations/r0121.json"
VERIFY_PATH = "docs/releases/r0121_verify.sql"
START_NEXT = "library.0097_hour_refill_start_next_cause"
AUTOMATIC = "library.0098_hour_refill_automatic_failure_cause"
PLAN = [START_NEXT, AUTOMATIC]
MIGRATION_SHA256 = {
    START_NEXT: "a9efb9a3933b5c11cf53d511d4939f6c4e892c5938d09877bfab62a926cd05e2",
    AUTOMATIC: "7963d09dc9185e2864fd2d8270cd18dc866e6cdc855e442615430172cd4d3fe0",
}
OPERATIONS = {
    START_NEXT: [
        ("AddField", "additive"), ("AddField", "additive"),
        ("AddField", "additive"), ("AlterField", "additive"),
        ("RemoveConstraint", "manual"), ("AddConstraint", "manual"),
        ("AddConstraint", "manual"),
    ],
    AUTOMATIC: [
        ("AddField", "additive"), ("AddField", "additive"),
        ("AddField", "additive"), ("AddField", "additive"),
        ("AddField", "additive"), ("AlterField", "additive"),
        ("RemoveConstraint", "manual"), ("AddConstraint", "manual"),
        ("AddConstraint", "manual"), ("AddConstraint", "manual"),
    ],
}
MANUAL = [
    (START_NEXT, 4, "RemoveConstraint"),
    (START_NEXT, 5, "AddConstraint"),
    (START_NEXT, 6, "AddConstraint"),
    (AUTOMATIC, 6, "RemoveConstraint"),
    (AUTOMATIC, 7, "AddConstraint"),
    (AUTOMATIC, 8, "AddConstraint"),
    (AUTOMATIC, 9, "AddConstraint"),
]
RESTARTS = ["isadoraair-gunicorn", "isadoraair-engine"]
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0121.md",
    "docs/releases/r0121_installation.md",
    VERIFY_PATH,
    "updatecenter/tests/test_r0121_release.py",
}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"],
        check=True, capture_output=True,
    ).stdout


def _introducing(path: str) -> list[str]:
    return _git(
        "log", "--full-history", "--no-renames", "--diff-filter=A",
        "--format=%H", "HEAD", "--", path,
    ).split()


def _release_commit() -> str | None:
    commits = _introducing(MANIFEST_PATH)
    if len(commits) > 1:
        raise AssertionError(f"r0121 manifest introduced more than once: {commits!r}")
    return commits[0] if commits else None


def _companion_commit() -> str | None:
    commits = _introducing(COMPANION_PATH)
    if len(commits) > 1:
        raise AssertionError(f"r0121 companion introduced more than once: {commits!r}")
    return commits[0] if commits else None


def _artifact(path: str) -> bytes:
    release = _release_commit()
    return _show(release, path) if release else (PROJECT_ROOT / path).read_bytes()


def _changed(base: str, commit: str, *filters: str) -> set[str]:
    options, paths = list(filters), []
    if "--" in options:
        cut = options.index("--")
        options, paths = options[:cut], options[cut:]
    return set(_git("diff", "--name-only", "--no-renames", *options, base, commit, *paths).split())


def _migration_path(ref: str) -> str:
    app, name = ref.split(".", 1)
    return f"{app}/migrations/{name}.py"


def _exact_plan():
    return probe._serialize_exact_plan(MigrationLoader(None, ignore_no_migrations=True), PLAN)


def _identities() -> frozenset:
    return frozenset(
        (ref, MIGRATION_SHA256[ref], index, operation, "manual")
        for ref, index, operation in MANUAL
    )


class R0121ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_and_application_only(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0121.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id,
             manifest.minimum_updater_protocol_version),
            ("r0121", "r0120", 5),
        )
        self.assertEqual(list(manifest.migrations_required), PLAN)
        self.assertEqual(manifest.migration_compatibility, "additive")
        self.assertTrue(manifest.collectstatic_required)
        self.assertEqual(list(manifest.services_requiring_restart), RESTARTS)
        self.assertIsNone(manifest.protected_runtime)
        self.assertEqual(data["minimum_supported_release_id"], "r0081")
        for field in (
            "apt_packages_new", "systemd_units_changed", "systemd_units_new_required",
            "systemd_units_new_optional", "systemd_units_removed_or_renamed",
        ):
            self.assertEqual(data[field], [], field)
        self.assertEqual(
            (data["manual_bootstrap_required"], data["python_requirements_changed"],
             data["requirements_sha256"], data["nginx_changed"],
             data["runtime_components_changed"]),
            (False, False, None, False, False),
        )
        self.assertNotIn("migration_authorization", raw.decode())
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        self.assertTrue(set(RESTARTS) <= CORE_SERVICES)
        self.assertEqual(RESTARTS, [unit for unit in planner.RESTART_ORDER if unit in RESTARTS])

    def test_release_commit_is_exactly_five_new_files_after_engineering(self):
        release = _release_commit()
        if release is None:
            self.skipTest("R has not been committed yet")
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ENGINEERING])
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", ENGINEERING).split()[1:], [TRUSTED_R0120_TIP])
        self.assertEqual(_git("rev-parse", f"{ENGINEERING}^{{tree}}"), ENGINEERING_TREE)
        self.assertEqual(_git("rev-list", "--merges", f"{TRUSTED_R0120_TIP}..{release}"), "")
        self.assertEqual(_changed(ENGINEERING, release), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=MDR"), set())
        self.assertEqual(_show(release, MANIFEST_PATH), (PROJECT_ROOT / MANIFEST_PATH).read_bytes())

    def test_engineering_preserves_release_and_infrastructure_covenants(self):
        changed = _changed(TRUSTED_R0120_TIP, ENGINEERING)
        self.assertEqual(
            [path for path in changed if "/static/" in path],
            ["production/static/production/iportal/workstation.js"],
        )
        self.assertFalse(changed & {"requirements.txt", "deploy/isadoraair.nginx"})
        self.assertFalse([path for path in changed if path.endswith((".service", ".timer"))])
        self.assertFalse([path for path in changed if path.startswith(
            ("deploy/updater_runtime/", "deploy/updater_bootstrap/", "deploy/updater_attestations/"))])
        self.assertEqual(
            _git("rev-parse", f"{ENGINEERING}:updatecenter/tests/test_r0119_release.py"),
            R0119_TEST_FIX_BLOB,
        )
        self.assertEqual(
            _git("rev-parse", f"{ENGINEERING}:updatecenter/tests/test_r0120_release.py"),
            R0120_TEST_FIX_BLOB,
        )


class R0121MigrationTests(SimpleTestCase):
    def test_exact_hashes_operations_and_seven_manual_identities(self):
        plan = _exact_plan()
        self.assertEqual([item["ref"] for item in plan], PLAN)
        self.assertEqual({item["ref"]: item["migration_file_sha256"] for item in plan}, MIGRATION_SHA256)
        self.assertEqual(
            {item["ref"]: [(op["operation"], op["classification"]) for op in item["operations"]]
             for item in plan},
            OPERATIONS,
        )
        self.assertEqual(
            [(item["ref"], item["operation_index"], item["operation"])
             for item in probe.extract_manual_operations(plan)],
            MANUAL,
        )
        self.assertEqual([item["dependencies"] for item in plan],
                         [["library.0096_hour_refill_install_gate"], [START_NEXT]])
        for ref in PLAN:
            self.assertEqual(hashlib.sha256((PROJECT_ROOT / _migration_path(ref)).read_bytes()).hexdigest(),
                             MIGRATION_SHA256[ref])

    def test_migrations_have_no_data_operation_and_all_switches_default_off(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        for name in ("0097_hour_refill_start_next_cause", "0098_hour_refill_automatic_failure_cause"):
            operations = loader.disk_migrations[("library", name)].operations
            self.assertNotIn("RunPython", {type(op).__name__ for op in operations})
            self.assertNotIn("RunSQL", {type(op).__name__ for op in operations})
        for field in (
            "enabled", "capture_request_substitution", "capture_operator_eject",
            "capture_start_next", "capture_automatic_skip", "capture_media_failure",
        ):
            self.assertIs(HourRefillPolicy._meta.get_field(field).default, False)
        self.assertIs(HourRefillPolicy._meta.get_field("dry_run_only").default, True)
        self.assertFalse(settings.HOUR_REFILL_INSTALL_ARMED)


class R0121VerifySqlTests(TransactionTestCase):
    databases = {"default"}

    def statements(self):
        lines = [
            line for line in _artifact(VERIFY_PATH).decode().splitlines()
            if line.strip() and not line.lstrip().startswith(("--", "\\"))
        ]
        return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]

    def migrate_library(self, name):
        executor = MigrationExecutor(connection)
        target = ("library", name)
        executor.migrate([target])
        return executor.loader.project_state([target]).apps

    def tearDown(self):
        # Every case restores the disposable schema to the current leaf, even
        # if an assertion fails after exercising the genuine 0096 state.
        self.migrate_library("0098_hour_refill_automatic_failure_cause")
        super().tearDown()

    def execute_script(self):
        statements = self.statements()
        self.assertEqual(statements[0], "BEGIN TRANSACTION READ ONLY")
        self.assertEqual(statements[-1], "ROLLBACK")
        for statement in statements[1:-1]:
            self.assertRegex(statement, r"^(SELECT\b|SET LOCAL (statement|lock)_timeout = '\d+s'$)")
        self.assertFalse(re.search(
            r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|TRUNCATE|GRANT|REVOKE)\b|"
            r"pg_(terminate|cancel)_backend|pg_(try_)?advisory|FOR (UPDATE|SHARE)",
            "\n".join(statements), re.IGNORECASE,
        ))
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM library_hourrefillpolicy")
            policy_count_before = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM library_hourrefillcause")
            cause_count_before = cursor.fetchone()[0]
        results = []
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
                if statement.startswith("SELECT"):
                    results.append(cursor.fetchall())
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM library_hourrefillpolicy")
            self.assertEqual(cursor.fetchone()[0], policy_count_before)
            cursor.execute("SELECT count(*) FROM library_hourrefillcause")
            self.assertEqual(cursor.fetchone()[0], cause_count_before)
        return results

    def policy_object(self, results):
        policy = results[2][0][0]
        return json.loads(policy) if isinstance(policy, str) else policy

    def test_read_only_sql_executes_against_genuine_0096_preflight_schema(self):
        historical_apps = self.migrate_library("0096_hour_refill_install_gate")
        policy_model = historical_apps.get_model("library", "HourRefillPolicy")
        policy_model.objects.create()
        results = self.execute_script()
        self.assertEqual(results[0], [])
        self.assertEqual({row[0] for row in results[1]}, {"hour_refill_cause_known_type"})
        policy = self.policy_object(results)
        self.assertFalse(policy["enabled"])
        self.assertFalse(policy["capture_request_substitution"])
        self.assertFalse(policy["capture_operator_eject"])
        self.assertTrue(policy["dry_run_only"])
        for unavailable in ("capture_start_next", "capture_automatic_skip", "capture_media_failure"):
            self.assertNotIn(unavailable, policy)

    def test_read_only_sql_executes_against_genuine_0098_postinstall_schema(self):
        self.migrate_library("0098_hour_refill_automatic_failure_cause")
        HourRefillPolicy.objects.create()
        results = self.execute_script()
        self.assertEqual(results[0], [("library", ref.split(".", 1)[1]) for ref in PLAN])
        self.assertEqual({row[0] for row in results[1]}, {
            "hour_refill_cause_known_type", "hour_refill_cause_start_next_fields",
            "hour_refill_cause_automatic_skip_fields", "hour_refill_cause_media_failure_fields",
        })
        policy = self.policy_object(results)
        self.assertFalse(policy["enabled"])
        self.assertTrue(policy["dry_run_only"])
        for field in (
            "capture_request_substitution", "capture_operator_eject", "capture_start_next",
            "capture_automatic_skip", "capture_media_failure",
        ):
            self.assertFalse(policy[field])


class R0121CompanionTests(SimpleTestCase):
    def companion(self):
        tip = _companion_commit()
        if tip is None:
            self.skipTest("C is intentionally created only after R is immutable")
        return tip

    def test_companion_is_direct_child_metadata_only_and_exact(self):
        tip = self.companion()
        release = _release_commit()
        self.assertIsNotNone(release)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", tip).split()[1:], [release])
        self.assertEqual(_changed(release, tip), {COMPANION_PATH})
        record = migration_authorization.parse_companion(_show(tip, COMPANION_PATH))
        self.assertEqual((record["release_id"], record["target_commit"]), ("r0121", release))
        self.assertEqual(record["manifest_sha256"], hashlib.sha256(_show(release, MANIFEST_PATH)).hexdigest())
        self.assertEqual(
            [(item["ref"], item["operation_index"], item["operation"])
             for item in record["authorized_manual_operations"]],
            MANUAL,
        )

    def test_both_trusted_validators_accept_all_seven(self):
        tip = self.companion()
        release = _release_commit()
        expected = _identities()
        django = migration_authorization.validate_release_companion(
            PROJECT_ROOT, tip, release_id="r0121", release_commit=release,
            migrations_required=PLAN,
        )
        repository = TrustedRepository(
            Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir")),
            "unused", "main", CommandRunner(),
        )
        entry = {item.manifest.release_id: item for item in load_chain(repository, tip)}["r0121"]
        runtime = runtime_validate_companion(repository, tip, entry)
        self.assertEqual((django, runtime), (expected, expected))
        with self.assertRaises(migration_authorization.CompanionError):
            migration_authorization.validate_release_companion(
                PROJECT_ROOT, tip, release_id="r0121", release_commit=ENGINEERING,
                migrations_required=PLAN,
            )

    def test_missing_or_altered_authorization_never_covers_the_plan(self):
        self.companion()
        plan = _exact_plan()
        station = {
            "plan_items": [
                {"ref": item["ref"], "migration_file_sha256": item["migration_file_sha256"]}
                for item in plan
            ],
            "manual_operations": probe.extract_manual_operations(plan),
        }
        exact = _identities()
        self.assertTrue(central_authorization_covers({"r0121": exact}, **station))
        for variant in (
            frozenset(list(exact)[:-1]),
            frozenset((ref, "0" * 64, index, operation, classification)
                      for ref, _sha, index, operation, classification in exact),
            frozenset((ref, sha, index + 1, operation, classification)
                      for ref, sha, index, operation, classification in exact),
        ):
            self.assertFalse(central_authorization_covers({"r0121": variant}, **station))
        self.assertFalse(central_authorization_covers({}, **station))


class R0121ManifestValidatorTests(SimpleTestCase):
    def test_validate_release_manifests_passes(self):
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        self.assertNotIn("FAIL", out.getvalue() + err.getvalue())
        self.assertIn("r0120 -> r0121", out.getvalue())
