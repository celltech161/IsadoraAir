"""r0133 immutable release contract (Roadmap 1.22: Update Center migration-history consistency hardening).

Published r0132 (653c5bf) plus three linear commits, then R:
  8724da9 -- accepted test correction (r0125 drift test uses library's current leaf migration);
  8dd2abd -- accepted Roadmap 1.22 implementation (schema_health + updatecenter_probe consistency check);
  D       -- documentation-only commit (docs/UPDATE_CENTER.md documents the inconsistent status);
  R       -- metadata-only child of D adding this contract, the r0133 manifest and the two release documents.
Tests refer solely to commits and objects reachable from R, so a fresh station clone never depends on an
unpublished feature or release-preparation ref.
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
R0132_RELEASE = "653c5bfd09972858fc529d00a28deee7890607a3"
R0132_TREE = "8017824a02deb54c3350bd4b29affed905077287"
ACCEPTED_FIX = "8724da92a85104a9a7ea914b22cbabd4e5c45ebd"
ACCEPTED_FIX_TREE = "dfb14d842c69d070d0c1127e1a96d69af238ac84"
IMPLEMENTATION = "8dd2abdc6edf86a51e543e610f8fb53a6410bfae"
IMPLEMENTATION_TREE = "f093fe9fdbeca4cbe547bb55d0dc257f01e93b02"
DOCUMENTATION = "eec82168a11e45fdabeac7a700e095e86c054b61"
DOCUMENTATION_TREE = "74458dafd48fc5e1ed7683b619a18b5bf90160be"
ENGINEERING_FILES = {
    "updatecenter/schema_health.py",
    "updatecenter/management/commands/updatecenter_probe.py",
    "updatecenter/tests/test_planner.py",
    "updatecenter/tests/test_r0125_release.py",
    "updatecenter/tests/test_schema_health.py",
    "updatecenter/tests/test_updatecenter_probe_history_consistency.py",
}
DOCUMENTATION_FILES = {"docs/UPDATE_CENTER.md"}
MANIFEST_PATH = "deploy/releases/r0133.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0133.md",
    "docs/releases/r0133_installation.md",
    "updatecenter/tests/test_r0133_release.py",
}
RESTARTS = ("isadoraair-gunicorn",)
CATCH_UP_RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine")  # r0132 (Gunicorn+engine) then r0133
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


def _introducing(path: str = MANIFEST_PATH) -> list[str]:
    return _git(
        "log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--", path,
    ).split()


def _release_commit() -> str | None:
    commits = _introducing()
    if len(commits) > 1:
        raise AssertionError(f"r0133 manifest introduced more than once: {commits!r}")
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
    return parse_policy_dict(json.loads(_show(R0132_RELEASE, POLICY_PATH)), label="r0132 protected policy")


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


class R0133ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_gunicorn_only_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0133.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0133", "r0132", 5),
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
        # Same field set and order as the preceding accepted manifest: no new manifest field.
        self.assertEqual(list(data), list(json.loads(_show(R0132_RELEASE, "deploy/releases/r0132.json"))))
        self.assertEqual(list(RESTARTS), [unit for unit in planner.RESTART_ORDER if unit in RESTARTS])
        self.assertLessEqual(len(data["summary"]), 500)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0133")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0133", "r0132"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((), None))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertIsNone(parsed.protected_runtime)

    def test_release_documents_preserve_operational_boundaries(self):
        notes = _artifact("docs/releases/r0133.md").decode()
        runbook = _artifact("docs/releases/r0133_installation.md").decode()
        for required in (
            "`inconsistent_migration_history`", "`check_consistent_history()`", "Nothing is repaired automatically",
            "`PROBE_FAILED`", "`migration_state_indeterminate`", "A healthy migration history stays eligible",
            "trusted plan\n  digest are unchanged", "no new operator-facing configuration",
            "`protected_runtime` is `null`", "only restart is `isadoraair-gunicorn`",
        ):
            self.assertIn(required, notes)
        for required in (
            R0132_RELEASE, ACCEPTED_FIX, IMPLEMENTATION, "## KOGR", "## WRJE", "actual installed release",
            "intervening release", "restart `isadoraair-gunicorn` only", "Observation-only mode",
            "host unarmed", "residual PSA disabled", "evidence-collection user timers",
            "not an Hour Refill event", "Do not deliberately corrupt", "Publishing r0133 does not\nauthorize",
        ):
            self.assertIn(required, runbook)


class R0133HistoryAndScopeTests(SimpleTestCase):
    def test_accepted_engineering_is_exact_linear_and_content_identical(self):
        self.assertEqual(_git("rev-parse", f"{R0132_RELEASE}^{{tree}}"), R0132_TREE)
        self.assertEqual(_git("rev-list", "--reverse", f"{R0132_RELEASE}..{IMPLEMENTATION}").split(),
                         [ACCEPTED_FIX, IMPLEMENTATION])
        self.assertEqual(_git("rev-list", "--merges", f"{R0132_RELEASE}..{IMPLEMENTATION}"), "")
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", ACCEPTED_FIX).split()[1:], [R0132_RELEASE])
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", IMPLEMENTATION).split()[1:], [ACCEPTED_FIX])
        self.assertEqual(_git("rev-parse", f"{ACCEPTED_FIX}^{{tree}}"), ACCEPTED_FIX_TREE)
        self.assertEqual(_git("rev-parse", f"{IMPLEMENTATION}^{{tree}}"), IMPLEMENTATION_TREE)
        self.assertEqual(_changed(R0132_RELEASE, IMPLEMENTATION), ENGINEERING_FILES)
        self.assertEqual(_changed(R0132_RELEASE, IMPLEMENTATION, "--diff-filter=A"),
                         {"updatecenter/tests/test_updatecenter_probe_history_consistency.py"})

    def test_documentation_commit_is_one_docs_only_child(self):
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", DOCUMENTATION).split()[1:], [IMPLEMENTATION])
        self.assertEqual(_git("rev-parse", f"{DOCUMENTATION}^{{tree}}"), DOCUMENTATION_TREE)
        self.assertEqual(_changed(IMPLEMENTATION, DOCUMENTATION), DOCUMENTATION_FILES)
        self.assertEqual(_changed(IMPLEMENTATION, DOCUMENTATION, "--diff-filter=M"), DOCUMENTATION_FILES)
        text = _show(DOCUMENTATION, "docs/UPDATE_CENTER.md").decode()
        for status in (
            "schema_current", "unapplied_migrations_detected", "inconsistent_migration_history",
            "migration_state_indeterminate",
        ):
            self.assertIn(f"`{status}`", text)
        for required in ("`check_consistent_history()`", "*before* forward\nmigration planning", "SCHEMA_DRIFT_DETECTED"):
            self.assertIn(required, text)

    def test_release_is_one_metadata_only_child_of_the_documentation_commit(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [DOCUMENTATION])
        self.assertEqual(_changed(DOCUMENTATION, release), RELEASE_FILES)
        self.assertEqual(_changed(DOCUMENTATION, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(DOCUMENTATION, release, "--diff-filter=MDR"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0132_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0132_RELEASE}..{release}").split(),
                         [ACCEPTED_FIX, IMPLEMENTATION, DOCUMENTATION, release])
        self.assertEqual(_changed(R0132_RELEASE, release), ENGINEERING_FILES | DOCUMENTATION_FILES | RELEASE_FILES)
        for path in ENGINEERING_FILES:
            self.assertEqual(_git("rev-parse", f"{release}:{path}"), _git("rev-parse", f"{IMPLEMENTATION}:{path}"), path)

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        release = _release_commit() or DOCUMENTATION
        self.assertEqual(_changed(R0132_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0132_RELEASE, release, "--", "requirements*.txt"), set())
        self.assertEqual(_changed(R0132_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0132_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0132_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertFalse([path for path in _changed(R0132_RELEASE, release) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0132_RELEASE}:{prefix}"), _git("rev-parse", f"{release}:{prefix}"), prefix)
        # No engine/playout/Hour Refill code is touched: nothing outside the declared updatecenter and docs files.
        outside = {path for path in _changed(R0132_RELEASE, release)
                   if not path.startswith(("updatecenter/", "docs/", "deploy/releases/"))}
        self.assertEqual(outside, set())

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0133.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0132_RELEASE,
        )
        self.assertEqual(list(findings), [])


class R0133ChainAndCompanionTests(SimpleTestCase):
    def test_no_migration_companion_or_historical_manifest_change(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0133.json").exists())
            return
        self.assertEqual(_changed(R0132_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0132_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertEqual(_changed(R0132_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0133", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0133"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))

    def test_chain_resolves_unique_r0132_to_r0133_identities(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain[-3:]], ["r0131", "r0132", "r0133"])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0132"], commits["r0133"]), (R0132_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))

    def test_complete_chain_r0001_through_r0133_is_contiguous_and_validates(self):
        release = _require_release(self)
        chain = release_chain.build_chain(release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases"))
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, 134)])
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn("FAIL", combined)
        self.assertIn("r0132 -> r0133", out.getvalue())

    def test_r0132_release_identity_and_manifest_are_preserved(self):
        release = _require_release(self)
        self.assertEqual(_introducing("deploy/releases/r0132.json"), [R0132_RELEASE])
        self.assertEqual(_show(release, "deploy/releases/r0132.json"), _show(R0132_RELEASE, "deploy/releases/r0132.json"))
        _git("merge-base", "--is-ancestor", R0132_RELEASE, release)  # raises unless r0132 is an ancestor of R


class R0133TrustedPlanTests(SimpleTestCase):
    def test_exact_plan_is_migration_free_gunicorn_only_and_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0132", R0132_RELEASE), ("r0133", release)])
        plan = derive_plan(_repository(), release, R0132_RELEASE, "r0133", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0132", "r0133", release))
        self.assertEqual(plan.releases_in_plan, ("r0133",))
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

    def test_multi_release_catch_up_from_r0131_aggregates_in_order(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0131_RELEASE, "r0133", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, ("r0132", "r0133"))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((), None))
        self.assertEqual(plan.services_requiring_restart, CATCH_UP_RESTARTS)
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=_known_units()), ())

    def test_restart_rehearsal_touches_only_gunicorn(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
        runner = FakeSystemRunner()
        manager = SystemdManager(config, runner, enforce_root_ownership=False, signed_policy=_policy())
        self.assertEqual(manager.restart_declared(RESTARTS), list(RESTARTS))
        self.assertEqual([call[1:3] for call in runner.calls], [
            ["restart", "isadoraair-gunicorn.service"], ["show", "isadoraair-gunicorn.service"],
        ])
        touched = " ".join(" ".join(call) for call in runner.calls)
        for untouched in (
            "isadoraair-engine", "isadoraair-encoders", "isadoraair-monitoring", "isadoraair-rbds",
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


class R0133PlannerParityTests(TestCase):
    def test_application_and_protected_planners_match_action_fingerprint(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(R0132_RELEASE)
            built = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, R0132_RELEASE, "r0133",
                                    known_units=_known_units())
            self.assertEqual(built.safety_status, planner.SafetyStatus.READY_TO_PLAN, built.safety_detail)
            self.assertEqual((built.releases_in_plan, built.target_commit), (("r0133",), release))
            self.assertEqual(built.services_requiring_restart, RESTARTS)
            self.assertIsNone(built.migrations)
            self.assertEqual(built.fingerprint, protected.fingerprint)

    def test_catch_up_from_r0131_application_and_protected_planners_match(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(R0131_RELEASE)
            built = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, R0131_RELEASE, "r0133",
                                    known_units=_known_units())
            self.assertEqual(built.safety_status, planner.SafetyStatus.READY_TO_PLAN, built.safety_detail)
            self.assertEqual(built.releases_in_plan, ("r0132", "r0133"))
            self.assertEqual(built.services_requiring_restart, CATCH_UP_RESTARTS)
            self.assertEqual(built.fingerprint, protected.fingerprint)
