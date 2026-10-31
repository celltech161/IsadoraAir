"""r0123 release contract: immutable metadata, library.0099 preservation,
two exact manual AddField authorizations, unchanged protected runtime and
trusted plans from r0122 and r0121.

Historical assertions resolve the unique commit introducing the manifest;
future releases therefore cannot silently redefine r0123's identity.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
from pathlib import Path

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
from isadoraair_updater.release import (
    TrustedRepository,
    derive_plan,
    load_chain,
    manual_blockers,
    resolve_known_managed_units,
)
from protected_bootstrap.policy import parse_policy_dict

from library.models import Track
from updatecenter import manifest as manifest_mod
from updatecenter import migration_authorization, planner
from updatecenter.management.commands import updatecenter_probe as probe


PROJECT_ROOT = Path(__file__).resolve().parents[2]
R0121_RELEASE = "5eeefa6707b607692ac70a9752a536022ab26378"
R0122_RELEASE = "83cb322bfe36d644b12f1c16e6c1aeeedb1dfcf4"
ENGINEERING = "15e0d8326b6745de6410268ebeaf27902816f91c"
ENGINEERING_TREE = "7d36da19266f1665c8c7232457d2cb365df82a13"
ENGINEERING_COMMITS = [
    "400e694927dbda31b615a8e7b3555bbd511dd2b7",
    "5a380bb43ef97a3a8e31bcd6200bfc10fb0230b1",
    "8289b145d59ec2b08eea5e58aac07513359dd2ea",
    "38681a4a8ba2326edaaf0544a9a0776afd90051b",
    "20677cb5b74690315d35cd6931dc67bdeef195c8",
    "475ba34e15e8a4b9ef607149ab1914b259c1c0d1",
    "51df811714d08b27ebcaf3753bdbddb9010918d0",
    "846eb292173ef2bc15a100e75fd659c8a81e16a9",
    "65a1ab64711f5c29fc758cad1e2d44c08b82dd44",
    "6e97e5ecdff2d25e1c4293859c30ab8744ccb89d",
    "10738cffa60a577d0711e5ab30bd66d42794d0f8",
    ENGINEERING,
]
MANIFEST_PATH = "deploy/releases/r0123.json"
COMPANION_PATH = "deploy/migration_authorizations/r0123.json"
VERIFY_PATH = "docs/releases/r0123_verify.sql"
MIGRATION = "library.0099_track_cue_marker_authority"
MIGRATION_PATH = "library/migrations/0099_track_cue_marker_authority.py"
MIGRATION_SHA256 = "2a806488920d88f4ece07af1a8d796453fd521fae4bb28f6fe7c5bde1d90db95"
OPERATIONS = [("AddField", "manual"), ("AddField", "manual"), ("AddField", "additive")]
MANUAL = [(MIGRATION, 0, "AddField"), (MIGRATION, 1, "AddField")]
RESTARTS = ["isadoraair-gunicorn", "isadoraair-active-message-worker"]
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0123.md",
    "docs/releases/r0123_installation.md",
    VERIFY_PATH,
    "updatecenter/tests/test_r0123_release.py",
}
PROTECTED_PREFIXES = (
    "deploy/updater_runtime",
    "deploy/updater_bootstrap",
    "deploy/updater_attestations",
)
MARKERS = (
    "cue_in_seconds", "cue_out_seconds", "next_start_seconds", "intro_until_seconds",
    "sweep_start_seconds", "outro_starts_seconds", "hook_in_seconds", "hook_out_seconds",
)


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
        raise AssertionError(f"r0123 manifest introduced more than once: {commits!r}")
    return commits[0] if commits else None


def _companion_commit() -> str | None:
    commits = _introducing(COMPANION_PATH)
    if len(commits) > 1:
        raise AssertionError(f"r0123 companion introduced more than once: {commits!r}")
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


def _repository() -> TrustedRepository:
    return TrustedRepository(
        Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir")),
        "unused", "main", CommandRunner(),
    )


def _known_units() -> frozenset[str]:
    policy = parse_policy_dict(
        json.loads(_show(R0122_RELEASE, "deploy/updater_runtime/protected-policy.json")),
        label="r0122 protected policy",
    )
    return resolve_known_managed_units(active_policy=policy)


def _exact_plan() -> list[dict]:
    return probe._serialize_exact_plan(MigrationLoader(None, ignore_no_migrations=True), [MIGRATION])


def _identities() -> frozenset:
    return frozenset((ref, MIGRATION_SHA256, index, operation, "manual")
                     for ref, index, operation in MANUAL)


class R0123ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_and_application_only(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0123.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0123", "r0122", 5),
        )
        self.assertEqual(list(manifest.migrations_required), [MIGRATION])
        self.assertEqual(manifest.migration_compatibility, "additive")
        self.assertEqual(list(manifest.services_requiring_restart), RESTARTS)
        self.assertFalse(manifest.collectstatic_required)
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
        self.assertEqual(RESTARTS, [unit for unit in planner.RESTART_ORDER if unit in RESTARTS])
        self.assertLessEqual(len(data["summary"]), 500)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        self.assertNotIn(ENGINEERING, raw.decode())

    def test_release_commit_is_a_metadata_only_child_of_the_accepted_engineering_tip(self):
        release = _release_commit()
        if release is None:
            self.skipTest("R has not been committed yet")
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ENGINEERING])
        self.assertEqual(_git("rev-parse", f"{ENGINEERING}^{{tree}}"), ENGINEERING_TREE)
        self.assertEqual(_git("rev-list", "--reverse", f"{R0122_RELEASE}..{ENGINEERING}").split(),
                         ENGINEERING_COMMITS)
        self.assertEqual(_git("rev-list", "--merges", f"{R0122_RELEASE}..{release}"), "")
        self.assertEqual(_changed(ENGINEERING, release), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=MDR"), set())

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        self.assertEqual(_changed(R0122_RELEASE, ENGINEERING, "--", "requirements.txt"), set())
        self.assertEqual(_changed(R0122_RELEASE, ENGINEERING, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0122_RELEASE, ENGINEERING, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertFalse([path for path in _changed(R0122_RELEASE, ENGINEERING) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0122_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{ENGINEERING}:{prefix}"), prefix)


class R0123MigrationTests(SimpleTestCase):
    def test_exact_hash_operations_dependency_and_two_manual_identities(self):
        plan = _exact_plan()
        self.assertEqual([item["ref"] for item in plan], [MIGRATION])
        self.assertEqual(plan[0]["migration_file_sha256"], MIGRATION_SHA256)
        self.assertEqual([(op["operation"], op["classification"]) for op in plan[0]["operations"]],
                         OPERATIONS)
        self.assertEqual([(item["ref"], item["operation_index"], item["operation"])
                          for item in probe.extract_manual_operations(plan)], MANUAL)
        self.assertEqual(plan[0]["dependencies"], ["library.0098_hour_refill_automatic_failure_cause"])
        self.assertEqual(hashlib.sha256((PROJECT_ROOT / MIGRATION_PATH).read_bytes()).hexdigest(),
                         MIGRATION_SHA256)

    def test_migration_is_schema_only_with_compatible_database_defaults(self):
        source = (PROJECT_ROOT / MIGRATION_PATH).read_text()
        self.assertNotIn("RunPython", source)
        self.assertNotIn("RunSQL", source)
        self.assertIn("db_default={}", source)
        self.assertIn("db_default=0", source)


class R0123MigrationApplicationTests(TransactionTestCase):
    databases = {"default"}
    BEFORE = [("library", "0098_hour_refill_automatic_failure_cause")]
    AFTER = [("library", "0099_track_cue_marker_authority")]

    def tearDown(self):
        call_command("migrate", verbosity=0)
        super().tearDown()

    def test_existing_data_and_old_writer_insert_survive_0099(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.BEFORE)
        historical_apps = executor.loader.project_state(self.BEFORE).apps
        Artist = historical_apps.get_model("library", "Artist")
        Category = historical_apps.get_model("library", "Category")
        CategoryKind = historical_apps.get_model("library", "CategoryKind")
        HistoricalTrack = historical_apps.get_model("library", "Track")
        artist = Artist.objects.create(name="r0123 migration artist")
        kind = CategoryKind.objects.create(code="r0123_kind", name="r0123 kind")
        category = Category.objects.create(code="R0123", name="r0123", kind=kind)
        other = Category.objects.create(code="R0123OTHER", name="r0123 other", kind=kind)
        values = dict(zip(MARKERS, (1.5, 198.0, 195.0, 9.0, None, 180.0, 60.0, 70.0)))
        row = HistoricalTrack.objects.create(
            filepath="/tmp/r0123-migration.flac", filename="r0123-migration.flac",
            title="r0123 migration", artist=artist, category=category,
            duration_seconds=200.0, ready2air=True, **values,
        )
        row.additional_categories.add(other)

        MigrationExecutor(connection).migrate(self.AFTER)
        migrated = Track.objects.get(pk=row.pk)
        self.assertEqual({name: getattr(migrated, name) for name in MARKERS}, values)
        self.assertEqual((migrated.ready2air, migrated.artist_id, migrated.category_id),
                         (True, artist.pk, category.pk))
        self.assertEqual(set(migrated.additional_categories.values_list("pk", flat=True)), {other.pk})
        self.assertEqual((migrated.cue_marker_state, migrated.cue_marker_revision,
                          migrated.analysis_requested_at), ({}, 0, None))

        old_writer = HistoricalTrack.objects.create(
            filepath="/tmp/r0123-old-writer.flac", filename="r0123-old-writer.flac",
            title="r0123 old writer", artist=artist, category=category,
        )
        inserted = Track.objects.get(pk=old_writer.pk)
        self.assertEqual((inserted.cue_marker_state, inserted.cue_marker_revision,
                          inserted.analysis_requested_at), ({}, 0, None))


class R0123VerifySqlTests(TransactionTestCase):
    databases = {"default"}
    BEFORE = [("library", "0098_hour_refill_automatic_failure_cause")]
    AFTER = [("library", "0099_track_cue_marker_authority")]

    def tearDown(self):
        call_command("migrate", verbosity=0)
        super().tearDown()

    def statements(self):
        lines = [line for line in _artifact(VERIFY_PATH).decode().splitlines()
                 if line.strip() and not line.lstrip().startswith(("--", "\\"))]
        return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]

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
        results = []
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
                if statement.startswith("SELECT"):
                    results.append(cursor.fetchall())
        return results

    def test_sql_executes_read_only_before_and_after_0099(self):
        MigrationExecutor(connection).migrate(self.BEFORE)
        before = self.execute_script()
        self.assertEqual(before[0], [])
        self.assertEqual(before[1], [])
        self.assertEqual(before[2][0][1:4], (0, 0, 0))

        MigrationExecutor(connection).migrate(self.AFTER)
        after = self.execute_script()
        self.assertEqual(after[0], [("library", "0099_track_cue_marker_authority")])
        self.assertEqual({row[0] for row in after[1]},
                         {"cue_marker_state", "cue_marker_revision", "analysis_requested_at"})
        self.assertEqual(after[2][0][1:4], (after[2][0][0],) * 3)


class R0123CompanionTests(SimpleTestCase):
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
        self.assertEqual((record["release_id"], record["target_commit"]), ("r0123", release))
        self.assertEqual(record["manifest_sha256"],
                         hashlib.sha256(_show(release, MANIFEST_PATH)).hexdigest())
        self.assertEqual([(item["ref"], item["operation_index"], item["operation"])
                          for item in record["authorized_manual_operations"]], MANUAL)

    def test_both_trusted_validators_accept_exactly_the_two_operations(self):
        tip = self.companion()
        release = _release_commit()
        expected = _identities()
        django = migration_authorization.validate_release_companion(
            PROJECT_ROOT, tip, release_id="r0123", release_commit=release,
            migrations_required=[MIGRATION],
        )
        repository = _repository()
        entry = {item.manifest.release_id: item for item in load_chain(repository, tip)}["r0123"]
        runtime = runtime_validate_companion(repository, tip, entry)
        self.assertEqual((django, runtime), (expected, expected))

        plan = _exact_plan()
        station = {
            "plan_items": [{"ref": item["ref"], "migration_file_sha256": item["migration_file_sha256"]}
                           for item in plan],
            "manual_operations": probe.extract_manual_operations(plan),
        }
        self.assertTrue(central_authorization_covers({"r0123": expected}, **station))
        self.assertFalse(central_authorization_covers({"r0123": frozenset(list(expected)[:1])}, **station))


class R0123TrustedPlanTests(SimpleTestCase):
    def tip(self):
        return _companion_commit() or _release_commit()

    def test_chain_and_plan_from_r0122_are_exact(self):
        tip = self.tip()
        if tip is None:
            self.skipTest("R has not been committed yet")
        repository = _repository()
        chain = load_chain(repository, tip)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0122", R0122_RELEASE), ("r0123", _release_commit())])
        plan = derive_plan(repository, tip, R0122_RELEASE, "r0123", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0122", "r0123", _release_commit()))
        self.assertEqual(plan.releases_in_plan, ("r0123",))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility),
                         ((MIGRATION,), "additive"))
        self.assertEqual(plan.services_requiring_restart, tuple(RESTARTS))
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed,
                          plan.runtime_components_changed, plan.python_requirements_changed,
                          plan.manual_bootstrap_required), (False, False, False, False, False))
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed,
                          plan.systemd_units_new_required, plan.systemd_units_new_optional,
                          plan.systemd_units_removed_or_renamed), ((), (), (), (), ()))
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=_known_units()), ())

    def test_plan_from_r0121_crosses_r0122_without_adding_actions(self):
        tip = self.tip()
        if tip is None:
            self.skipTest("R has not been committed yet")
        plan = derive_plan(_repository(), tip, R0121_RELEASE, "r0123", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, ("r0122", "r0123"))
        self.assertEqual(plan.migrations_required, (MIGRATION,))
        self.assertEqual(plan.services_requiring_restart, tuple(RESTARTS))
        self.assertFalse(plan.collectstatic_required)


class R0123ManifestValidatorTests(SimpleTestCase):
    def test_validate_release_manifests_passes(self):
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn("FAIL", combined)
        self.assertIn("r0122 -> r0123", out.getvalue())
