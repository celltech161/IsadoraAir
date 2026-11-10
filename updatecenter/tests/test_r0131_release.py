"""r0131 immutable release contract.

The accepted DST history is published verbatim and the accepted importer patch is
one content-preserving linear cherry-pick. R adds only this contract, its manifest
and the two release documents. Tests intentionally refer only to objects reachable
from R so a fresh station clone does not depend on unpublished feature refs.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase

from .phase_b_helpers import RUNTIME_ROOT, config_dict  # noqa: F401
from .test_phase_b_systemd_daemon import FakeSystemRunner
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.migration_authorization import validate_release_companion
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import (
    TrustedRepository,
    derive_plan,
    load_chain,
    manual_blockers,
    parse_manifest,
    resolve_known_managed_units,
)
from isadoraair_updater.systemd import SystemdManager
from protected_bootstrap.policy import parse_policy_dict

from updatecenter import cross_check, manifest as manifest_mod, migration_authorization, planner, release_chain
from updatecenter.management.commands import updatecenter_probe as probe


PROJECT_ROOT = Path(__file__).resolve().parents[2]
R0130_RELEASE = "58e67cb247bd75ed489213dfc7e0bd6800596d3a"
DST_TIP = "9e940c34cb931334d478f139f7256d8edc782fa0"
DST_TREE = "108d6182e8705575c172e779095da93e486da361"
ENGINEERING = "6ac39d1acf5cea9aa497781a6a18e9e8c49246bd"
ENGINEERING_TREE = "830a7e5782f70c5fbcc4ec291f0e6d9d04e50ad8"
DEVELOPMENT = [
    "dc8432bae09a471cc4594df59f5cafdac572def4",
    "1c3ae4622ff621c5e5e03a758938ac8cebfe7bbb",
    "cca10b6684659ceddf86c94d2e42dec6e562c918",
    "f17901af63fa6531c2eceee35e7d1328b0d308d0",
    "0aab2a7469709c03c9e7a6af75078bda53c5e123",
    "2a534e93053807911c1726ecbd574259d9fb9199",
    DST_TIP,
    ENGINEERING,
]
DEVELOPMENT_FILES = {
    "docs/DST_FALLBACK_CONTINUITY.md",
    "docs/PLAYBACK_ACCOUNTING_SEMANTICS_PHASE_B.md",
    "library/management/commands/import_songs.py",
    "library/migrations/0100_dst_fallback_replay_pass.py",
    "library/models.py",
    "library/services/dst_fallback.py",
    "library/services/engine.py",
    "library/tests/test_dst_fallback_continuity_1_21.py",
    "library/tests/test_dst_fallback_migration_1_21.py",
    "library/tests/test_dst_fallback_replay_identity_1_21.py",
    "library/tests/test_dst_fallback_transition_1_21.py",
    "library/tests/test_import_songs.py",
    "library/tests/test_import_songs_hardening_r0131.py",
    "library/tests/test_schedule_profiles_3_1a.py",
    "library/tests/test_voicetrack_production_media.py",
}
IMPORT_FILES = {
    "library/management/commands/import_songs.py",
    "library/tests/test_import_songs.py",
    "library/tests/test_import_songs_hardening_r0131.py",
}
MANIFEST_PATH = "deploy/releases/r0131.json"
MIGRATION = "library.0100_dst_fallback_replay_pass"
MIGRATION_PATH = "library/migrations/0100_dst_fallback_replay_pass.py"
MIGRATION_SHA256 = "7b5559d6b05865bef42bae1e66c34359bb67f02a00fd54a0542262359ee08b94"
RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine")
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0131.md",
    "docs/releases/r0131_installation.md",
    "updatecenter/tests/test_r0131_release.py",
}
POLICY_PATH = "deploy/updater_runtime/protected-policy.json"
PROTECTED_PREFIXES = (
    "deploy/updater_runtime",
    "deploy/updater_bootstrap",
    "deploy/updater_attestations",
)


def _git(*args: str, cwd: Path = PROJECT_ROOT) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"],
        check=True, capture_output=True,
    ).stdout


def _changed(base: str, commit: str, *filters: str) -> set[str]:
    options, paths = list(filters), []
    if "--" in options:
        cut = options.index("--")
        options, paths = options[:cut], options[cut:]
    return set(_git("diff", "--name-only", "--no-renames", *options, base, commit, *paths).split())


def _introducing() -> list[str]:
    return _git(
        "log", "--full-history", "--no-renames", "--diff-filter=A",
        "--format=%H", "HEAD", "--", MANIFEST_PATH,
    ).split()


def _release_commit() -> str | None:
    commits = _introducing()
    if len(commits) > 1:
        raise AssertionError(f"r0131 manifest introduced more than once: {commits!r}")
    return commits[0] if commits else None


def _require_release(test) -> str:
    release = _release_commit()
    if release is None:
        test.skipTest("R has not been committed yet")
    return release


def _artifact(path: str) -> bytes:
    release = _release_commit()
    return _show(release, path) if release else (PROJECT_ROOT / path).read_bytes()


def _repository(git_dir: Path | None = None) -> TrustedRepository:
    git_dir = git_dir or Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir"))
    return TrustedRepository(git_dir, "unused", "main", CommandRunner())


def _policy():
    return parse_policy_dict(
        json.loads(_show(R0130_RELEASE, POLICY_PATH)), label="r0130 protected policy",
    )


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


def _exact_plan() -> list[dict]:
    return probe._serialize_exact_plan(MigrationLoader(None, ignore_no_migrations=True), [MIGRATION])


class R0131ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_additive_application_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0131.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0131", "r0130", 5),
        )
        self.assertEqual((tuple(manifest.migrations_required), manifest.migration_compatibility),
                         ((MIGRATION,), "additive"))
        self.assertEqual(tuple(manifest.services_requiring_restart), RESTARTS)
        self.assertEqual((manifest.collectstatic_required, manifest.protected_runtime), (False, None))
        self.assertEqual(data["minimum_supported_release_id"], "r0081")
        for field in (
            "apt_packages_new", "systemd_units_changed", "systemd_units_new_required",
            "systemd_units_new_optional", "systemd_units_removed_or_renamed",
        ):
            self.assertEqual(data[field], [], field)
        self.assertEqual(
            (data["manual_bootstrap_required"], data["python_requirements_changed"],
             data["requirements_sha256"], data["nginx_changed"], data["runtime_components_changed"]),
            (False, False, None, False, False),
        )
        self.assertEqual(list(data), list(json.loads(_show(R0130_RELEASE, "deploy/releases/r0130.json"))))
        self.assertEqual(list(RESTARTS), [unit for unit in planner.RESTART_ORDER if unit in RESTARTS])
        self.assertLessEqual(len(data["summary"]), 500)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        manifest = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0131")
        self.assertEqual((manifest.release_id, manifest.previous_release_id), ("r0131", "r0130"))
        self.assertEqual((manifest.migrations_required, manifest.migration_compatibility),
                         ((MIGRATION,), "additive"))
        self.assertEqual(manifest.services_requiring_restart, RESTARTS)
        self.assertIsNone(manifest.protected_runtime)

    def test_release_documents_preserve_operational_boundaries(self):
        notes = _artifact("docs/releases/r0131.md").decode()
        runbook = _artifact("docs/releases/r0131_installation.md").decode()
        for required in (
            "first audio buffer", "requests, one-shot and late inserts", "normal-day behavior",
            "AppleDouble", "no manual operation", "KOGR and WRJE",
        ):
            self.assertIn(required, notes)
        for required in (
            R0130_RELEASE, MIGRATION, MIGRATION_SHA256, "90-second systemd timeout", "SIGKILL",
            "--dry-run", "temporary directory outside the media", "Do not manually run `migrate`",
            "separately authorized installation", "Do not fabricate a clock transition",
        ):
            self.assertIn(required, runbook)


class R0131HistoryAndScopeTests(SimpleTestCase):
    def test_accepted_linear_history_and_exact_engineering_tree_are_preserved(self):
        self.assertEqual(_git("rev-parse", f"{DST_TIP}^{{tree}}"), DST_TREE)
        self.assertEqual(_git("rev-list", "--reverse", f"{R0130_RELEASE}..{ENGINEERING}").split(),
                         DEVELOPMENT)
        self.assertEqual(_git("rev-list", "--merges", f"{R0130_RELEASE}..{ENGINEERING}"), "")
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", ENGINEERING).split()[1:], [DST_TIP])
        self.assertEqual(_git("rev-parse", f"{ENGINEERING}^{{tree}}"), ENGINEERING_TREE)
        self.assertEqual(_changed(R0130_RELEASE, ENGINEERING), DEVELOPMENT_FILES)
        self.assertEqual(_changed(DST_TIP, ENGINEERING), IMPORT_FILES)

    def test_release_is_one_metadata_only_child(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ENGINEERING])
        self.assertEqual(_changed(ENGINEERING, release), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=MDR"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0130_RELEASE}..{release}"), "")

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        self.assertEqual(_changed(R0130_RELEASE, ENGINEERING, "--", "requirements.txt"), set())
        self.assertEqual(_changed(R0130_RELEASE, ENGINEERING, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0130_RELEASE, ENGINEERING, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertFalse([path for path in _changed(R0130_RELEASE, ENGINEERING) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0130_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{ENGINEERING}:{prefix}"), prefix)

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0131.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0130_RELEASE,
        )
        self.assertEqual(list(findings), [])


class R0131MigrationTests(SimpleTestCase):
    def test_exact_hash_dependency_and_no_manual_operation(self):
        plan = _exact_plan()
        self.assertEqual([item["ref"] for item in plan], [MIGRATION])
        self.assertEqual(plan[0]["migration_file_sha256"], MIGRATION_SHA256)
        self.assertEqual(plan[0]["dependencies"], ["library.0099_track_cue_marker_authority"])
        self.assertEqual([(op["operation"], op["classification"]) for op in plan[0]["operations"]],
                         [("AddField", "additive"), ("AlterField", "additive")])
        self.assertEqual(probe.extract_manual_operations(plan), [])
        self.assertEqual(hashlib.sha256((PROJECT_ROOT / MIGRATION_PATH).read_bytes()).hexdigest(),
                         MIGRATION_SHA256)

    def test_no_companion_exists_or_is_required(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0131.json").exists())
            return
        self.assertEqual(_changed(R0130_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0131", release_commit=release,
            migrations_required=[MIGRATION],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0131"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))


class R0131TrustedPlanTests(SimpleTestCase):
    def test_chain_and_exact_plan_are_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0130", R0130_RELEASE), ("r0131", release)])
        plan = derive_plan(_repository(), release, R0130_RELEASE, "r0131", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0130", "r0131", release))
        self.assertEqual(plan.releases_in_plan, ("r0131",))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility),
                         ((MIGRATION,), "additive"))
        self.assertEqual(plan.services_requiring_restart, RESTARTS)
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed,
                          plan.runtime_components_changed, plan.python_requirements_changed,
                          plan.manual_bootstrap_required), (False, False, False, False, False))
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed,
                          plan.systemd_units_new_required, plan.systemd_units_new_optional,
                          plan.systemd_units_removed_or_renamed), ((), (), (), (), ()))
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=_known_units()), ())

    def test_restart_rehearsal_touches_only_gunicorn_then_engine(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
        runner = FakeSystemRunner()
        manager = SystemdManager(config, runner, enforce_root_ownership=False, signed_policy=_policy())
        self.assertEqual(manager.restart_declared(RESTARTS), list(RESTARTS))
        self.assertEqual([call[1:3] for call in runner.calls], [
            ["restart", "isadoraair-gunicorn.service"], ["show", "isadoraair-gunicorn.service"],
            ["restart", "isadoraair-engine.service"], ["show", "isadoraair-engine.service"],
        ])
        touched = " ".join(" ".join(call) for call in runner.calls)
        for untouched in (
            "isadoraair-encoders", "isadoraair-monitoring", "isadoraair-rbds",
            "isadoraair-active-message-worker", "isadoraair-spoken-render-worker",
        ):
            self.assertNotIn(untouched, touched)


class _Clone:
    def __init__(self, root: Path, release: str):
        self.origin = root / "origin.git"
        self.work = root / "station"
        subprocess.run(["git", "clone", "--quiet", "--shared", "--bare", str(PROJECT_ROOT), str(self.origin)],
                       check=True, capture_output=True)
        _git("update-ref", "refs/heads/main", release, cwd=self.origin)
        subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(self.origin), str(self.work)],
                       check=True, capture_output=True)
        self.git("config", "user.email", "release-test@invalid")
        self.git("config", "user.name", "release test")

    def git(self, *args: str) -> str:
        return _git(*args, cwd=self.work)

    def at(self, commit: str) -> None:
        self.git("checkout", "-q", "-f", "-B", "main", commit)
        self.git("branch", "-q", "--set-upstream-to=origin/main", "main")
        self.git("clean", "-q", "-fdx")


class R0131PlannerParityTests(TestCase):
    def test_application_and_protected_planners_match_action_fingerprint(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(R0130_RELEASE)
            application = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, R0130_RELEASE, "r0131",
                                    known_units=_known_units())
            self.assertEqual(application.safety_status, planner.SafetyStatus.READY_TO_PLAN,
                             application.safety_detail)
            self.assertEqual((application.releases_in_plan, application.target_commit),
                             (("r0131",), release))
            self.assertIsNotNone(application.migrations)
            self.assertEqual(application.migrations.explicitly_required, (MIGRATION,))
            self.assertEqual(application.migrations.compatibility, "additive")
            self.assertEqual(application.services_requiring_restart, RESTARTS)
            self.assertEqual(application.fingerprint, protected.fingerprint)


class R0131ChainValidatorTests(SimpleTestCase):
    def test_chain_resolves_unique_r0130_to_r0131_identities(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain[-3:]], ["r0129", "r0130", "r0131"])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0130"], commits["r0131"]), (R0130_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))

    def test_complete_manifest_chain_validates(self):
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn("FAIL", combined)
        self.assertIn("r0130 -> r0131", out.getvalue())
