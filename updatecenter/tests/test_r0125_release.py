"""r0125 release contract: the accepted roadmap 3.11 Library work (Track-deletion
integrity, bulk editing, reconciliation/duplicate review/relink, audit events)
and its documentation corrections, linearly on published r0124, with no
migration companion and a Gunicorn + active-message-worker trusted plan.

Why the active-message worker: library/models.py (path-registration locks in
Track.save/update/bulk_create/update_or_create), library/apps.py (Track
pre/post_delete waveform receivers), sync_track_file (registration under the
path lock) and waveform_cache (deletion fence) are imported in-process by that
long-running worker (active_messages.playout._place_and_ingest, retention's
Collector.delete). The engine, encoders, Monitoring, RBDS and spoken-render
worker never register, relocate or delete a Track or write a waveform, so no
restart of theirs is declared and audio is not interrupted.

Assertions resolve the unique commit introducing r0125.json, so later
releases cannot silently redefine this release's identity. The trusted-plan
tests run the Django planner on a disposable clone (the candidate as an
isolated origin/main) and compare it, by execution fingerprint, with the
protected updater's own independent derivation.
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TestCase

from .phase_b_helpers import RUNTIME_ROOT, config_dict  # noqa: F401  (protected runtime on sys.path)
from .test_phase_b_systemd_daemon import FakeSystemRunner
from isadoraair_updater import MANIFEST_PROTOCOL_VERSION
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.migration_authorization import validate_release_companion
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import (
    ReleaseError,
    TrustedRepository,
    derive_plan,
    load_chain,
    manual_blockers,
    parse_manifest,
    resolve_known_managed_units,
)
from isadoraair_updater.systemd import SystemdError, SystemdManager
from protected_bootstrap.policy import parse_policy_dict

from updatecenter import cross_check
from updatecenter import manifest as manifest_mod
from updatecenter import migration_authorization, planner, release_chain


PROJECT_ROOT = Path(__file__).resolve().parents[2]
R0115_RELEASE = "fd3ac805e774f6039636f3f8178f5770ea038572"
R0122_RELEASE = "83cb322bfe36d644b12f1c16e6c1aeeedb1dfcf4"
R0123_RELEASE = "1a7c5b113a31f619ab20a7361be3eecf2f285559"
R0123_COMPANION = "e9e182d9a21b2d52d72ac2b0042cff51d2bb95bd"
R0124_RELEASE = "69d53bc4f40e96a47359dce817375aa415cd068f"
ACCEPTED = "bfcd4f7cc7e0fe55c3e7b4b45aaef766e754721c"
ACCEPTED_TREE = "8fabed47427768e2a4f6175aa395b4900875ec60"
DEVELOPMENT = [
    "307a16160a78c49f87222e157d3bed8aed550c62",
    "3e52b9effff38a61a485e95e57445040de7a5e71",
    "2f5eeb0bf220a712f9ac92306d26fc0ebb7bf8c3",
    "b111cff7ac1ba6f6db09f799043a9dd39579c27f",
    "6815076811d9630f79d21b0cfc0fa960c718ce4c",
    "1cfd5626794983ddfa62c4dafd4498820b0b87df",
    "1ae1835360071956d5febdc48ab83a0e1c14ac1e",
    "4ea3acac71b235554c851730fcc687b3b59c2e9d",
    "0e78380b6b0c29744729433d5c3d27bb67851bc8",
    "341699bc4c37dad287001061ce7501b9480dbdcb",
    "7f324d1ce7a43591d545266e927bace1238e1c1f",
    "1e5152624ef6126f7b1ec3559206476bfea9d403",
    "a04002e089d739bd85fce9115117382cec6514b4",
    "f9eea69e1a43006944119982e06bb06c82ddfe49",
    "09a26853387264b87cfad397926108d6fa6a6e29",
    "1740e0e5efa73f5646ffc6e5f953c4ff55332e49",
    "de32512f7e54ceffc385363e00f535f69d20f9fb",
    ACCEPTED,
]
#: Every path the accepted development work changed relative to r0124 (the release-impact inventory).
DEVELOPMENT_FILES = {
    "README.md", "docs/LIBRARY_RECONCILIATION.md", "docs/releases/r0109.md",
    "docs/roadmap/2_27_PHASE_D_ROLLOUT_PACKET.md",
    "library/admin.py", "library/apps.py", "library/models.py", "library/urls.py", "library/views.py",
    "library/management/commands/apply_duplicate_resolutions.py",
    "library/management/commands/find_orphaned_tracks.py",
    "library/management/commands/reconcile_library.py",
    "library/management/commands/remove_track_file.py",
    "library/management/commands/sync_track_file.py",
    "library/services/library_audit.py", "library/services/library_reconciliation.py",
    "library/services/library_relink.py", "library/services/source_paths.py",
    "library/services/track_bulk_edit.py", "library/services/track_deletion.py",
    "library/services/waveform_cache.py",
    "library/templates/library/library.html", "library/templates/library/reconciliation.html",
    "library/tests/measure_bulk_edit_performance.py", "library/tests/measure_reconciliation_performance.py",
    "library/tests/render_library_bulk_artifact.py", "library/tests/render_reconciliation_artifact.py",
    "library/tests/test_authz_category_track_enforcement.py", "library/tests/test_library_audit_r0125.py",
    "library/tests/test_library_bulk_edit_r0125.py", "library/tests/test_library_reconciliation_b.py",
    "library/tests/test_track_deletion_integrity.py",
    "webrequests/services.py", "webrequests/tests/test_dedication_intros.py",
}
MANIFEST_PATH = "deploy/releases/r0125.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0125.md",
    "docs/releases/r0125_installation.md",
    "updatecenter/tests/test_r0125_release.py",
}
PROTECTED_PREFIXES = (
    "deploy/updater_runtime",
    "deploy/updater_bootstrap",
    "deploy/updater_attestations",
)
RESTARTS = ("isadoraair-gunicorn", "isadoraair-active-message-worker")
POLICY_PATH = "deploy/updater_runtime/protected-policy.json"


def _git(*args: str, cwd: Path = PROJECT_ROOT) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
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
        raise AssertionError(f"r0125 manifest introduced more than once: {commits!r}")
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


def _repository(git_dir: Path | None = None) -> TrustedRepository:
    git_dir = git_dir or Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir"))
    return TrustedRepository(git_dir, "unused", "main", CommandRunner())


def _policy(commit: str):
    return parse_policy_dict(json.loads(_show(commit, POLICY_PATH)), label=f"protected policy at {commit[:12]}")


def _known_units(commit: str = R0124_RELEASE) -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy(commit))


def _require_release(test) -> str:
    release = _release_commit()
    if release is None:
        test.skipTest("R has not been committed yet")
    return release


class R0125ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_and_restarts_gunicorn_then_the_worker(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0125.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0125", "r0124", 5),
        )
        self.assertEqual(list(manifest.migrations_required), [])
        self.assertIsNone(manifest.migration_compatibility)
        self.assertEqual(tuple(manifest.services_requiring_restart), RESTARTS)
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
             data["requirements_sha256"], data["nginx_changed"], data["runtime_components_changed"]),
            (False, False, None, False, False),
        )
        self.assertLessEqual(len(data["summary"]), 500)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        self.assertEqual(list(data), list(json.loads(_show(R0124_RELEASE, "deploy/releases/r0124.json"))))
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_the_protected_parser_reads_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0125")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0125", "r0124"))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertEqual((parsed.migrations_required, parsed.protected_runtime), ((), None))

    def test_tampered_declarations_are_refused_by_both_parsers(self):
        base = json.loads(_artifact(MANIFEST_PATH))
        tampered = {
            "unsupported_restart": {**base, "services_requiring_restart": [*RESTARTS, "isadoraair-render"]},
            "unit_name_restart": {**base, "services_requiring_restart": ["isadoraair-gunicorn.service"]},
            "hook": {**base, "post_update_hooks": ["manage.py collectstatic"]},
            "self_reference": {**base, "release_commit": "0" * 40},
            "unknown_field": {**base, "restart_engine": True},
            "bad_migration_ref": {**base, "migrations_required": ["library/0099"]},
        }
        for name, data in tampered.items():
            with self.subTest(name=name):
                with self.assertRaises(manifest_mod.ManifestError):
                    manifest_mod.validate_manifest_dict(data, source_label=name)
                with self.assertRaises(ReleaseError):
                    parse_manifest(data, label=name)


class R0125IntegrationTests(SimpleTestCase):
    def test_accepted_history_is_linear_on_published_r0124(self):
        self.assertEqual(_git("rev-list", "--reverse", f"{R0124_RELEASE}..{ACCEPTED}").split(), DEVELOPMENT)
        self.assertEqual(_git("rev-list", "--merges", f"{R0124_RELEASE}..{ACCEPTED}"), "")
        self.assertEqual(_git("rev-parse", f"{ACCEPTED}^{{tree}}"), ACCEPTED_TREE)
        self.assertEqual(_changed(R0124_RELEASE, ACCEPTED), DEVELOPMENT_FILES)

    def test_no_migration_unit_dependency_static_nginx_or_protected_surface_changed(self):
        self.assertEqual(_changed(R0124_RELEASE, ACCEPTED, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0124_RELEASE, ACCEPTED, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0124_RELEASE, ACCEPTED, "--", "requirements*.txt"), set())
        self.assertEqual(_changed(R0124_RELEASE, ACCEPTED, "--", "deploy/isadoraair.nginx", "deploy/*nginx*"), set())
        self.assertEqual(_changed(R0124_RELEASE, ACCEPTED, "--", "deploy/releases", "deploy/migration_authorizations"),
                         set())
        self.assertFalse([path for path in _changed(R0124_RELEASE, ACCEPTED) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0124_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{ACCEPTED}:{prefix}"), prefix)

    def test_release_commit_is_the_metadata_only_child_of_the_accepted_tip(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ACCEPTED])
        self.assertEqual(_changed(ACCEPTED, release), RELEASE_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=MDR"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0124_RELEASE}..{release}"), "")

    def test_manifest_claims_agree_with_repository_content(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(json.loads(_show(release, MANIFEST_PATH)),
                                                       source_label="r0125.json")
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0124_RELEASE,
        )
        self.assertEqual(list(findings), [])


class R0125MigrationAndHistoryTests(SimpleTestCase):
    def test_no_migration_operation_and_no_r0125_companion(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0125.json").exists())
            return
        self.assertEqual(_changed(R0124_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertEqual(_git("log", "--full-history", "--format=%H", release, "--",
                              "deploy/migration_authorizations/r0125.json"), "")
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0125", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0125"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))

    def test_historical_manifests_and_companions_are_unchanged(self):
        release = _require_release(self)
        self.assertEqual(_changed(R0124_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertEqual(_changed(R0124_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertEqual(_git("rev-parse", f"{release}:deploy/releases/r0124.json"),
                         _git("rev-parse", f"{R0124_RELEASE}:deploy/releases/r0124.json"))

    def test_application_chain_resolves_unique_identities_on_the_isolated_candidate(self):
        """The candidate itself as the canonical tip: one introducing commit per release, r0125 = R."""
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([c.manifest.release_id for c in chain[-3:]], ["r0123", "r0124", "r0125"])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0124"], commits["r0125"]), (R0124_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))
        installed = release_chain.resolve_installed_release(chain, PROJECT_ROOT, R0124_RELEASE, commits)
        self.assertEqual(installed.manifest.release_id, "r0124")


class R0125ProtectedPlanTests(SimpleTestCase):
    def plan(self, installed, target="r0125", *, known=None):
        release = _require_release(self)
        return derive_plan(_repository(), release, installed, target, known_units=known or _known_units())

    def test_exact_plan_from_r0124(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0124", R0124_RELEASE), ("r0125", release)])
        plan = self.plan(R0124_RELEASE)
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0124", "r0125", release))
        self.assertEqual(plan.releases_in_plan, ("r0125",))
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

    def test_multi_release_catch_up_plans(self):
        from_r0123 = self.plan(R0123_RELEASE)
        self.assertEqual(from_r0123.releases_in_plan, ("r0124", "r0125"))
        self.assertEqual((from_r0123.migrations_required, from_r0123.services_requiring_restart), ((), RESTARTS))
        from_r0122 = self.plan(R0122_RELEASE)
        self.assertEqual(from_r0122.releases_in_plan, ("r0123", "r0124", "r0125"))
        self.assertEqual(from_r0122.migrations_required, ("library.0099_track_cue_marker_authority",))
        self.assertEqual((from_r0122.migration_compatibility, from_r0122.services_requiring_restart),
                         ("additive", RESTARTS))
        r0123 = {item.manifest.release_id: item for item in load_chain(_repository(), _release_commit())}["r0123"]
        self.assertIsNotNone(validate_release_companion(_repository(), _release_commit(), r0123))
        for plan in (from_r0123, from_r0122):
            self.assertIsNone(plan.protected_runtime_transition)
            self.assertEqual(manual_blockers(plan, known_units=_known_units()), ())
        # A station that has not crossed r0116 also crosses a protected-runtime handoff: its plan is a
        # different, separately reviewed transition, never the one-release r0125 plan.
        old = self.plan(R0115_RELEASE, known=_known_units(R0115_RELEASE))
        self.assertEqual(old.releases_in_plan[0], "r0116")
        self.assertEqual(old.protected_runtime_transition.release_id, "r0116")
        self.assertIn("isadoraair-engine", old.services_requiring_restart)

    def test_non_release_divergent_current_and_wrong_target_heads_are_refused(self):
        release = _require_release(self)
        divergent = _git("rev-parse", f"{R0123_RELEASE}^")             # an unreleased pre-r0123 commit
        for name, head, target in (
            ("accepted development tip", ACCEPTED, "r0125"),
            ("mid-history development commit", DEVELOPMENT[8], "r0125"),
            ("non-release ancestor", divergent, "r0125"),
            ("already current", release, "r0125"),
            ("target not the latest trusted release", R0124_RELEASE, "r0126"),
            ("target older than installed", R0124_RELEASE, "r0124"),
        ):
            with self.subTest(name=name), self.assertRaises(ReleaseError):
                derive_plan(_repository(), release, head, target, known_units=_known_units())

    def test_unsupported_updater_protocol_is_a_manual_blocker(self):
        plan = self.plan(R0124_RELEASE)
        future = dataclasses.replace(plan, minimum_updater_protocol_version=MANIFEST_PROTOCOL_VERSION + 1)
        self.assertIn("UPDATER_UPGRADE_REQUIRED", manual_blockers(future, known_units=_known_units()))


class R0125RestartRehearsalTests(SimpleTestCase):
    """The protected executor's restart step with a recording systemctl: exactly the declared services, in
    the closed RESTART_ORDER, each verified; a refused service is never restarted. No real unit is started.
    (restart_declared stops at the first refused name, so a station whose signed policy lacked the worker
    would restart Gunicorn and then fail the job for manual attention -- which is why such a station's
    plan must first cross r0116's protected handoff; see test_multi_release_catch_up_plans.)"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(root, str(root / "upstream.git")),
                                           allow_local_repository=True)

    def manager(self, runner, commit=R0124_RELEASE):
        return SystemdManager(self.config, runner, enforce_root_ownership=False, signed_policy=_policy(commit))

    def test_declared_restarts_run_in_order_and_are_verified(self):
        runner = FakeSystemRunner()
        self.assertEqual(self.manager(runner).restart_declared(RESTARTS), list(RESTARTS))
        self.assertEqual([call[1:3] for call in runner.calls], [
            ["restart", "isadoraair-gunicorn.service"], ["show", "isadoraair-gunicorn.service"],
            ["restart", "isadoraair-active-message-worker.service"],
            ["show", "isadoraair-active-message-worker.service"],
        ])
        touched = " ".join(" ".join(call) for call in runner.calls)
        for untouched in ("isadoraair-engine", "isadoraair-encoders", "isadoraair-rbds", "isadoraair-monitoring",
                          "isadoraair-spoken-render-worker", "isadoraair-render", "isadoraair-validation"):
            self.assertNotIn(untouched, touched)

    def test_policy_mismatch_order_mismatch_and_missing_service_are_refused(self):
        for name, services, commit, message in (
            ("reversed order", tuple(reversed(RESTARTS)), R0124_RELEASE, "closed deterministic"),
            ("worker unknown to the generation-14 policy", RESTARTS, R0115_RELEASE, "outside the restart allowlist"),
            ("unmanaged service", ("isadoraair-render",), R0124_RELEASE, ""),
        ):
            with self.subTest(name=name):
                runner = FakeSystemRunner()
                manager = self.manager(runner, commit)
                with self.assertRaisesRegex(SystemdError, message):
                    manager.restart_declared(services)
                self.assertNotIn(["restart", "isadoraair-active-message-worker.service"],
                                 [call[1:3] for call in runner.calls])


class _Clone:
    """A disposable station checkout whose origin/main is the isolated candidate R. Nothing here touches
    the source repository's refs or any station checkout."""

    def __init__(self, root: Path, release: str):
        self.root, self.release = root, release
        self.origin = root / "origin.git"
        self.work = root / "station"
        subprocess.run(["git", "clone", "--quiet", "--shared", "--bare", str(PROJECT_ROOT), str(self.origin)],
                       check=True, capture_output=True)
        _git("update-ref", "refs/heads/main", release, cwd=self.origin)
        subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(self.origin), str(self.work)],
                       check=True, capture_output=True)
        self.git("config", "user.email", "release-test@invalid")
        self.git("config", "user.name", "release test")

    def git(self, *args):
        return _git(*args, cwd=self.work)

    def at(self, commit):
        self.git("checkout", "-q", "-f", "-B", "main", commit)
        self.git("branch", "-q", "--set-upstream-to=origin/main", "main")
        self.git("clean", "-q", "-fdx")
        return self

    def publish_tampered(self, path, data):
        """origin/main gains a child of R that rewrites one manifest (plumbing only, in the bare origin)."""
        env = {**os.environ, "GIT_INDEX_FILE": str(self.root / "tamper.index")}
        run = lambda *a, **k: subprocess.run(["git", "-C", str(self.origin), *a], check=True, capture_output=True,
                                             text=True, env=env, **k).stdout.strip()
        blob = run("hash-object", "-w", "--stdin", input=json.dumps(data, indent=2) + "\n")
        run("read-tree", self.release)
        run("update-index", "--cacheinfo", f"100644,{blob},{path}")
        tree = run("write-tree")
        commit = run("-c", "user.email=t@invalid", "-c", "user.name=t", "commit-tree", tree, "-p", self.release,
                     "-m", "tampered manifest")
        run("update-ref", "refs/heads/main", commit)
        self.git("fetch", "-q", "origin")
        return commit


class R0125ApplicationPlannerTests(TestCase):
    """The application planner (build_plan) on a disposable clone, against the protected derivation."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.release = _release_commit()
        if cls.release is None:
            return
        cls.temp = tempfile.TemporaryDirectory()
        cls.clone = _Clone(Path(cls.temp.name), cls.release)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "temp", None) is not None:
            cls.temp.cleanup()
        super().tearDownClass()

    def setUp(self):
        _require_release(self)

    def build(self, commit=None):
        if commit is not None:
            self.clone.at(commit)
        return planner.build_plan(self.clone.work, "deploy/releases")

    def protected(self, installed):
        return derive_plan(_repository(self.clone.origin), self.release, installed, "r0125",
                           known_units=_known_units())

    def test_exact_and_catch_up_plans_match_the_protected_fingerprint(self):
        for installed, releases in ((R0124_RELEASE, ("r0125",)), (R0123_RELEASE, ("r0124", "r0125"))):
            with self.subTest(installed=installed):
                built = self.build(installed)
                self.assertEqual(built.safety_status, planner.SafetyStatus.READY_TO_PLAN, built.safety_detail)
                self.assertEqual((built.releases_in_plan, built.target_commit), (releases, self.release))
                self.assertEqual(built.services_requiring_restart, RESTARTS)
                self.assertIsNone(built.migrations)
                self.assertEqual((built.collectstatic_required, built.python_requirements_changed,
                                  built.nginx_changed, built.runtime_components_changed,
                                  built.systemd_units_changed, built.systemd_units_new_required),
                                 (False, False, False, False, (), ()))
                self.assertEqual(built.fingerprint, self.protected(installed).fingerprint)

    def test_up_to_date_non_release_dirty_detached_and_local_commit_checkouts_are_refused(self):
        S = planner.SafetyStatus
        self.assertEqual(self.build(self.release).safety_status, S.UP_TO_DATE)
        self.assertEqual(self.build(ACCEPTED).safety_status, S.CHECKOUT_NOT_AT_RELEASE_COMMIT)
        self.build(R0124_RELEASE)
        (self.clone.work / "README.md").write_text("locally edited\n")
        self.assertEqual(self.build().safety_status, S.DIRTY_CHECKOUT)
        self.clone.at(R0124_RELEASE).git("checkout", "-q", "--detach")
        self.assertEqual(self.build().safety_status, S.DETACHED_HEAD)
        self.clone.at(self.release).git("commit", "-q", "--allow-empty", "-m", "local only")
        self.assertEqual(self.build().safety_status, S.LOCAL_COMMITS_NOT_ON_ORIGIN)
        self.clone.at(R0124_RELEASE).git("commit", "-q", "--allow-empty", "-m", "diverged")
        self.assertEqual(self.build().safety_status, S.DIVERGED_FROM_ORIGIN)
        self.assertEqual(self.build(R0124_RELEASE).safety_status, S.READY_TO_PLAN)   # clean again: planable

    def test_unexpected_migration_state_is_schema_drift_not_a_plan(self):
        MigrationRecorder.Migration.objects.filter(app="library", name="0099_track_cue_marker_authority").delete()
        built = self.build(R0124_RELEASE)
        self.assertEqual(built.safety_status, planner.SafetyStatus.SCHEMA_DRIFT_DETECTED)
        self.assertNotIn(built.safety_status, planner.SafetyStatus.ACTIONABLE)

    def test_a_tampered_published_declaration_is_refused_by_both_planners(self):
        clone = _Clone(Path(self.temp.name) / "tamper-target", self.release)
        data = json.loads(_show(self.release, MANIFEST_PATH))
        data["services_requiring_restart"] = [*RESTARTS, "isadoraair-render"]
        tip = clone.publish_tampered(MANIFEST_PATH, data)
        clone.at(R0124_RELEASE)
        built = planner.build_plan(clone.work, "deploy/releases")
        self.assertEqual(built.safety_status, planner.SafetyStatus.INVALID_RELEASE_MANIFEST)
        with self.assertRaises(ReleaseError):
            derive_plan(_repository(clone.origin), tip, R0124_RELEASE, "r0125", known_units=_known_units())

    def test_a_rewritten_historical_manifest_makes_identity_unresolvable(self):
        clone = _Clone(Path(self.temp.name) / "tamper-history", self.release)
        data = json.loads(_show(self.release, "deploy/releases/r0124.json"))
        data["summary"] = "r0124, rewritten after publication."     # still a valid manifest
        manifest_mod.validate_manifest_dict(data, source_label="rewritten r0124")
        tip = clone.publish_tampered("deploy/releases/r0124.json", data)
        clone.at(R0124_RELEASE)
        built = planner.build_plan(clone.work, "deploy/releases")
        self.assertEqual(built.safety_status, planner.SafetyStatus.TARGET_COMMIT_UNKNOWN, built.safety_detail)
        self.assertNotIn(built.safety_status, planner.SafetyStatus.ACTIONABLE)
        with self.assertRaises(ReleaseError):
            derive_plan(_repository(clone.origin), tip, R0124_RELEASE, "r0125", known_units=_known_units())


class R0125ManifestValidatorTests(SimpleTestCase):
    def test_complete_historical_manifest_chain_validates(self):
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn("FAIL", combined)
        self.assertIn("r0124 -> r0125", out.getvalue())
