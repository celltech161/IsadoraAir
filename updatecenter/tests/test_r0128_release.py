"""r0128 release contract for verified audio-equivalent duplicate resolution.

The accepted implementation is two linear commits on published r0127. R is a metadata-only child
that introduces this test, the manifest and the two release documents. The application and protected
planners must independently derive the same migration-free, Gunicorn-only action plan.
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
R0127_RELEASE = "0eb739f6d2420d128a2548aa83a855d47bd475c7"
ACCEPTED = "919c0dd2509486d097fd85b17602b62253908094"
ACCEPTED_TREE = "1a82bc288d8e1c551f72c0bf07622a309a760da4"
DEVELOPMENT = [
    "9a7b9528cee3e98ea0fa8c4dfbfd8ae5b63971a7",
    ACCEPTED,
]
DEVELOPMENT_FILES = {
    "docs/LIBRARY_RECONCILIATION.md",
    "library/management/commands/reconcile_library.py",
    "library/services/audio_identity.py",
    "library/services/library_duplicate_resolution.py",
    "library/services/library_reconciliation.py",
    "library/services/library_reconciliation_launch.py",
    "library/templates/library/reconciliation.html",
    "library/tests/test_library_audio_equivalent_resolution.py",
    "library/views.py",
}
MANIFEST_PATH = "deploy/releases/r0128.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0128.md",
    "docs/releases/r0128_installation.md",
    "updatecenter/tests/test_r0128_release.py",
}
RESTARTS = ("isadoraair-gunicorn",)
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
        raise AssertionError(f"r0128 manifest introduced more than once: {commits!r}")
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


def _policy(commit: str = R0127_RELEASE):
    return parse_policy_dict(json.loads(_show(commit, POLICY_PATH)), label=f"protected policy at {commit[:12]}")


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


class R0128ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_ordinary_application_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0128.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0128", "r0127", 5),
        )
        self.assertEqual(tuple(manifest.services_requiring_restart), RESTARTS)
        self.assertEqual((tuple(manifest.migrations_required), manifest.migration_compatibility), ((), None))
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
        self.assertLessEqual(len(data["summary"]), 500)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        self.assertEqual(list(data), list(json.loads(_show(R0127_RELEASE, "deploy/releases/r0127.json"))))
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0128")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0128", "r0127"))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertEqual((parsed.migrations_required, parsed.protected_runtime), ((), None))

    def test_release_docs_distinguish_evidence_bounds_and_production_boundary(self):
        notes = _artifact("docs/releases/r0128.md").decode()
        install = _artifact("docs/releases/r0128_installation.md").decode()
        for required in ("Identical source-file bytes", "Verified identical decoded audio",
                         "Similar metadata without proven audio equality", "`/proc/self/fd/N`",
                         "Off", "25", "50", "100", "Gunicorn"):
            self.assertIn(required, notes)
        self.assertIn("fresh reconciliation scan with audio verification enabled", notes)
        self.assertIn("`protected_runtime` is `null`", notes)
        self.assertIn("Publishing r0128 does", install)
        self.assertIn("not authorize any step on KOGR or WRJE", install)
        self.assertIn("Existing report transition", install)


class R0128ScopeAndIdentityTests(SimpleTestCase):
    def test_accepted_history_is_exact_linear_and_scoped(self):
        self.assertEqual(_git("rev-list", "--reverse", f"{R0127_RELEASE}..{ACCEPTED}").split(), DEVELOPMENT)
        self.assertEqual(_git("rev-list", "--merges", f"{R0127_RELEASE}..{ACCEPTED}"), "")
        self.assertEqual(_git("rev-parse", f"{ACCEPTED}^{{tree}}"), ACCEPTED_TREE)
        self.assertEqual(_changed(R0127_RELEASE, ACCEPTED), DEVELOPMENT_FILES)

    def test_accepted_implementation_changes_no_excluded_surface(self):
        self.assertEqual(_changed(R0127_RELEASE, ACCEPTED, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0127_RELEASE, ACCEPTED, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0127_RELEASE, ACCEPTED, "--", "requirements*.txt"), set())
        self.assertEqual(_changed(R0127_RELEASE, ACCEPTED, "--", "deploy/releases", "deploy/migration_authorizations"), set())
        self.assertEqual(_git("rev-parse", f"{R0127_RELEASE}:library/services/engine.py"),
                         _git("rev-parse", f"{ACCEPTED}:library/services/engine.py"))
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0127_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{ACCEPTED}:{prefix}"), prefix)

    def test_accepted_audio_equivalence_contract_is_present(self):
        identity = _show(ACCEPTED, "library/services/audio_identity.py").decode()
        scanner = _show(ACCEPTED, "library/services/library_reconciliation.py").decode()
        resolution = _show(ACCEPTED, "library/services/library_duplicate_resolution.py").decode()
        page = _show(ACCEPTED, "library/templates/library/reconciliation.html").decode()
        launcher = _show(ACCEPTED, "library/services/library_reconciliation_launch.py").decode()
        for required in ('return _identify(f"/proc/self/fd/{fd}", (fd,), deadline, timeout)',
                         'pass_fds=pass_fds', '_remaining(deadline, "decoding")'):
            self.assertIn(required, identity)
        for required in ("VERIFY_AUDIO_PAIR_CHOICES = (25, 50, 100)", "MAX_VERIFY_AUDIO_PAIRS = 100",
                         "VERIFY_AUDIO_MAX_SECONDS = 30 * 60", "VERIFY_DECODE_TIMEOUT_SECONDS = 300",
                         "self.verify_pairs_attempted >= self.verify_audio_pairs",
                         "audio_identity.compute_fd_or_none(fd, timeout=timeout)"):
            self.assertIn(required, scanner)
        self.assertIn('return {EXACT: "exact_bytes", AUDIO_EQUIVALENT: "decoded_audio"}', resolution)
        self.assertIn('record.get("status") != "verified"', resolution)
        self.assertIn("const VERIFY_AUDIO_CHOICES = [[0, 'Off'], [25, 'Up to 25 pairs'], [50, 'Up to 50 pairs'], [100, 'Up to 100 pairs']]", page)
        self.assertIn('return ["--verify-audio-pairs", str(pairs)]', launcher)

    def test_release_is_one_metadata_only_child_of_the_accepted_tip(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ACCEPTED])
        self.assertEqual(_changed(ACCEPTED, release), RELEASE_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=MDR"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0127_RELEASE}..{release}"), "")

    def test_manifest_claims_match_the_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0128.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0127_RELEASE,
        )
        self.assertEqual(list(findings), [])


class R0128ChainAndMigrationTests(SimpleTestCase):
    def test_no_migration_companion_or_historical_manifest_change(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0128.json").exists())
            return
        self.assertEqual(_changed(R0127_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0127_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertEqual(_changed(R0127_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0128", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0128"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))

    def test_chain_resolves_unique_r0127_to_r0128_identities(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([c.manifest.release_id for c in chain[-3:]], ["r0126", "r0127", "r0128"])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0127"], commits["r0128"]), (R0127_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))


class R0128ProtectedPlanTests(SimpleTestCase):
    def test_exact_plan_is_migration_free_gunicorn_only_and_unblocked(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0127_RELEASE, "r0128", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0127", "r0128", release))
        self.assertEqual(plan.releases_in_plan, ("r0128",))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((), None))
        self.assertEqual(plan.services_requiring_restart, RESTARTS)
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed, plan.systemd_units_new_required,
                          plan.systemd_units_new_optional, plan.systemd_units_removed_or_renamed),
                         ((), (), (), (), ()))
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed, plan.runtime_components_changed,
                          plan.python_requirements_changed, plan.manual_bootstrap_required),
                         (False, False, False, False, False))
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
        for untouched in ("isadoraair-engine", "isadoraair-encoders", "isadoraair-rbds",
                          "isadoraair-monitoring", "isadoraair-active-message-worker",
                          "isadoraair-spoken-render-worker"):
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


class R0128PlannerParityTests(TestCase):
    def test_application_and_protected_planners_match_action_fingerprint(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(R0127_RELEASE)
            built = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, R0127_RELEASE, "r0128",
                                    known_units=_known_units())
            self.assertEqual(built.safety_status, planner.SafetyStatus.READY_TO_PLAN, built.safety_detail)
            self.assertEqual((built.releases_in_plan, built.target_commit), (("r0128",), release))
            self.assertEqual(built.services_requiring_restart, RESTARTS)
            self.assertIsNone(built.migrations)
            self.assertEqual(built.fingerprint, protected.fingerprint)


class R0128ManifestValidatorTests(SimpleTestCase):
    def test_complete_manifest_chain_validates(self):
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn("FAIL", combined)
        self.assertIn("r0127 -> r0128", out.getvalue())
