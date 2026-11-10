"""r0132 immutable release contract.

Two independently accepted packages, integrated as two linear commits on published r0131:
  integration A d8fa959 -- net diff of Package A (fair four-track end-of-hour music lookahead), accepted
                           source 326b13b3b71d2706f9e87d0cc99106949168ffa9 (tree 97c8e501...);
  integration B c82fc57 -- net diff of Package B (Remote DJ/WebRTC failure teardown, incl. B1), accepted
                           source 3a5cb2a5d5b02c57f1dd06349f5e78805900814f (tree 28e120cb...).
R is a metadata-only child of integration B adding this contract, its manifest and the two release
documents. The package source commits are recorded here and as blob identities only: tests refer solely to
objects reachable from R, so a fresh station clone never depends on unpublished feature refs.
"""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from .phase_b_helpers import RUNTIME_ROOT, config_dict  # noqa: F401  (protected runtime on sys.path)
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


PROJECT_ROOT = Path(__file__).resolve().parents[2]
R0131_RELEASE = "c89ae56a15667a458b1cebb897c068b774256727"
INTEGRATION_A = "d8fa959533d75274fd5672688e851a5693aa94eb"
INTEGRATION_A_TREE = "97c8e501a8935b50743e5e24b2b0c66f27d8f526"  # == accepted Package A tree
INTEGRATION_B = "c82fc57af8dc1feb5f2a283401d72e0084eeebf5"
INTEGRATION_B_TREE = "bb291a24614f447aa81f43489c91df51ec8a8d45"
PACKAGE_A_BLOBS = {
    "library/services/log_builder.py": "4d78878440658171147311e1e7811cb40cd358ba",
    "library/tests/test_holiday_and_persistence.py": "34da4e327d37b8473559a9ee21ac9eb3d7f763f5",
    "library/tests/test_landing_lookahead_r0132.py": "f0ec400c30841d5bbd6093d69cbc2f4fba44decf",
    "library/tests/test_rotation_pair_integration.py": "f92fe5d5a0bdbaa83ae5afd04666a97d173cb2df",
    "webrequests/tests/test_matched_pair_replaceability.py": "5cc6b7b7263a7f7c3fa8293fc28857d64ff38ca0",
}
PACKAGE_B_BLOBS = {
    "library/services/engine.py": "7cab2deb18bd1d0d72e1f34128c86bdb1332a6c3",
    "library/tests/test_engine_remote_dj_connection_failures.py": "537f7a39b9544c0ca7c0ba37a90b893b6198f6af",
    "library/tests/test_remote_dj_teardown_r0132b.py": "0e78919ca7cb74c7c6763379f4b93d707e0b544d",
}
MANIFEST_PATH = "deploy/releases/r0132.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0132.md",
    "docs/releases/r0132_installation.md",
    "updatecenter/tests/test_r0132_release.py",
}
RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine")
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
        "log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--", MANIFEST_PATH,
    ).split()


def _release_commit() -> str | None:
    commits = _introducing()
    if len(commits) > 1:
        raise AssertionError(f"r0132 manifest introduced more than once: {commits!r}")
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
    return parse_policy_dict(json.loads(_show(R0131_RELEASE, POLICY_PATH)), label="r0131 protected policy")


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


class R0132ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_application_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0132.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0132", "r0131", 5),
        )
        self.assertEqual((tuple(manifest.migrations_required), manifest.migration_compatibility), ((), None))
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
        self.assertEqual(list(data), list(json.loads(_show(R0131_RELEASE, "deploy/releases/r0131.json"))))
        self.assertEqual(list(RESTARTS), [unit for unit in planner.RESTART_ORDER if unit in RESTARTS])
        self.assertLessEqual(len(data["summary"]), 500)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0132")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0132", "r0131"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((), None))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertIsNone(parsed.protected_runtime)

    def test_release_documents_preserve_operational_boundaries(self):
        notes = _artifact("docs/releases/r0132.md").decode()
        runbook = _artifact("docs/releases/r0132_installation.md").decode()
        for required in (
            "up to four consecutive music slots", "effective rotation weight",
            "keep r0131's matched-pair behavior", "Web Requests, which may still replace",
            "**A quarantine never restarts the station automatically.**",
            "Remote DJ cleanup failed; Remote DJ unavailable until engine restart",
            "Program audio\n  is unaffected", "`protected_runtime` is `null`", "Restarting the engine briefly interrupts",
        ):
            self.assertIn(required, notes)
        for required in (
            R0131_RELEASE, "## KOGR", "## WRJE", "actual installed release", "intervening release",
            "90-second", "SIGKILL", "futex_do_wait", "does not restart itself",
            "Never deliberately induce", "Publishing r0132 does not\nauthorize",
        ):
            self.assertIn(required, runbook)


class R0132HistoryAndScopeTests(SimpleTestCase):
    def test_accepted_integration_is_exact_linear_and_content_identical(self):
        self.assertEqual(_git("rev-list", "--reverse", f"{R0131_RELEASE}..{INTEGRATION_B}").split(),
                         [INTEGRATION_A, INTEGRATION_B])
        self.assertEqual(_git("rev-list", "--merges", f"{R0131_RELEASE}..{INTEGRATION_B}"), "")
        self.assertEqual(_git("rev-parse", f"{INTEGRATION_A}^{{tree}}"), INTEGRATION_A_TREE)
        self.assertEqual(_git("rev-parse", f"{INTEGRATION_B}^{{tree}}"), INTEGRATION_B_TREE)
        self.assertEqual(_changed(R0131_RELEASE, INTEGRATION_A), set(PACKAGE_A_BLOBS))
        self.assertEqual(_changed(INTEGRATION_A, INTEGRATION_B), set(PACKAGE_B_BLOBS))
        for path, blob in {**PACKAGE_A_BLOBS, **PACKAGE_B_BLOBS}.items():
            self.assertEqual(_git("rev-parse", f"{INTEGRATION_B}:{path}"), blob, path)

    def test_release_is_one_metadata_only_child(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [INTEGRATION_B])
        self.assertEqual(_changed(INTEGRATION_B, release), RELEASE_FILES)
        self.assertEqual(_changed(INTEGRATION_B, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(INTEGRATION_B, release, "--diff-filter=MDR"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0131_RELEASE}..{release}"), "")
        for path, blob in {**PACKAGE_A_BLOBS, **PACKAGE_B_BLOBS}.items():
            self.assertEqual(_git("rev-parse", f"{release}:{path}"), blob, path)

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        self.assertEqual(_changed(R0131_RELEASE, INTEGRATION_B, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0131_RELEASE, INTEGRATION_B, "--", "requirements*.txt"), set())
        self.assertEqual(_changed(R0131_RELEASE, INTEGRATION_B, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0131_RELEASE, INTEGRATION_B, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertFalse([path for path in _changed(R0131_RELEASE, INTEGRATION_B) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0131_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{INTEGRATION_B}:{prefix}"), prefix)

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0132.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0131_RELEASE,
        )
        self.assertEqual(list(findings), [])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0131_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{release}:{prefix}"), prefix)


class R0132ChainAndCompanionTests(SimpleTestCase):
    def test_no_migration_companion_or_historical_manifest_change(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0132.json").exists())
            return
        self.assertEqual(_changed(R0131_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0131_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertEqual(_changed(R0131_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0132", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0132"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))

    def test_chain_resolves_unique_r0131_to_r0132_identities(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain[-3:]], ["r0130", "r0131", "r0132"])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0131"], commits["r0132"]), (R0131_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))

    def test_complete_manifest_chain_validates(self):
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn("FAIL", combined)
        self.assertIn("r0131 -> r0132", out.getvalue())


class R0132TrustedPlanTests(SimpleTestCase):
    def test_exact_plan_is_migration_free_gunicorn_then_engine_and_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0131", R0131_RELEASE), ("r0132", release)])
        plan = derive_plan(_repository(), release, R0131_RELEASE, "r0132", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0131", "r0132", release))
        self.assertEqual(plan.releases_in_plan, ("r0132",))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((), None))
        self.assertEqual(plan.services_requiring_restart, RESTARTS)
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed, plan.runtime_components_changed,
                          plan.python_requirements_changed, plan.manual_bootstrap_required),
                         (False, False, False, False, False))
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed, plan.systemd_units_new_required,
                          plan.systemd_units_new_optional, plan.systemd_units_removed_or_renamed),
                         ((), (), (), (), ()))
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


class R0132PlannerParityTests(TestCase):
    def test_application_and_protected_planners_match_action_fingerprint(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(R0131_RELEASE)
            built = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, R0131_RELEASE, "r0132",
                                    known_units=_known_units())
            self.assertEqual(built.safety_status, planner.SafetyStatus.READY_TO_PLAN, built.safety_detail)
            self.assertEqual((built.releases_in_plan, built.target_commit), (("r0132",), release))
            self.assertEqual(built.services_requiring_restart, RESTARTS)
            self.assertIsNone(built.migrations)
            self.assertEqual(built.fingerprint, protected.fingerprint)
