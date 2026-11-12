"""r0134 immutable release contract (Weather FLAC and generated-speech loudness; Dedication peak limiter).

Published r0133 (55c69a3) plus two accepted linear commits, then R:
  14b1fce -- accepted implementation (Weather FLAC + calibrated gain/limiter, Dedication limiter);
  3b1634c -- accepted correction (Track adoption removed; FLAC publishes as an ordinary asset);
  R       -- metadata-only child of 3b1634c adding this contract, the r0134 manifest and the two release documents.
Tests refer solely to commits and objects reachable from R, so a fresh station clone never depends on an
unpublished feature or release-preparation ref.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
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
R0132_RELEASE = "653c5bfd09972858fc529d00a28deee7890607a3"
R0131_RELEASE = "c89ae56a15667a458b1cebb897c068b774256727"
R0133_RELEASE = "55c69a33393f0c5b00c3d10ad60c5e3a0ce1ae83"
R0133_TREE = "7cc4bb07f555defaa230864e1e02d54aea7f8b67"
IMPLEMENTATION = "14b1fce3cf70793756d4cffb93436a065268473b"
IMPLEMENTATION_TREE = "e4001b4d0e06cb1a1bb6c6f0146326e142531c47"
CORRECTION = "3b1634c9b0d70a3402a0df44a58c4d4da64890f0"
CORRECTION_TREE = "f6e840357e5b52ee2776c661a64de149e968f862"
ENGINEERING_ADDED = {
    "docs/WEATHER_SPEECH_FLAC.md",
    "isadoraair/tests/test_announcement_peak_ceiling.py",
    "weather/tests/test_flac_diagnostics.py",
    "weather/tests/test_flac_publication.py",
    "weather_ingest/lib/speech_audio.py",
    "weather_ingest/tests/test_speech_audio.py",
}
ENGINEERING_MODIFIED = {
    "isadoraair/announcements/renderer.py",
    "isadoraair/announcements/types.py",
    "weather/diagnostics.py",
    "weather/management/commands/publish_weather_asset.py",
    "weather/publication.py",
    "weather/setup_status.py",
    "weather/tests/test_diagnostics.py",
    "weather_ingest/amber_alert.py",
    "weather_ingest/amber_poll.py",
    "weather_ingest/current_temp.py",
    "weather_ingest/tests/test_alert_beep_trigger.py",
    "weather_ingest/tests/test_speech_paths.py",
    "weather_ingest/update_local_wx_data.py",
    "weather_ingest/wx_alert.py",
    "weather_ingest/wx_forecast.py",
    "webrequests/services.py",
    "webrequests/tests/test_dedication_intros.py",
}
ENGINEERING_FILES = ENGINEERING_ADDED | ENGINEERING_MODIFIED
MANIFEST_PATH = "deploy/releases/r0134.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0134.md",
    "docs/releases/r0134_installation.md",
    "updatecenter/tests/test_r0134_release.py",
}
RESTARTS = ("isadoraair-gunicorn", "isadoraair-monitoring")
CATCH_UP_FROM_R0131_RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring")
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
        raise AssertionError(f"r0134 manifest introduced more than once: {commits!r}")
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
    return parse_policy_dict(json.loads(_show(R0133_RELEASE, POLICY_PATH)), label="r0133 protected policy")


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


class R0134ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_gunicorn_then_monitoring_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0134.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0134", "r0133", 5),
        )
        self.assertEqual((tuple(manifest.migrations_required), manifest.migration_compatibility), ((), None))
        self.assertEqual(tuple(manifest.services_requiring_restart), RESTARTS)
        self.assertNotIn("isadoraair-engine", manifest.services_requiring_restart)
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
        self.assertEqual(list(data), list(json.loads(_show(R0133_RELEASE, "deploy/releases/r0133.json"))))
        self.assertEqual(list(RESTARTS), [unit for unit in planner.RESTART_ORDER if unit in RESTARTS])
        self.assertLessEqual(len(data["summary"]), 500)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0134")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0134", "r0133"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((), None))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertIsNone(parsed.protected_runtime)

    def test_release_notes_preserve_the_operator_facing_contract(self):
        notes = _artifact("docs/releases/r0134.md").decode()
        for required in (
            "`current_temp.flac`", "`current_obs.flac`", "`forecast.flac`", "`wx_alert.flac`", "Vorbis Comments",
            "44.1 kHz stereo 16-bit", "one-second silence", "+6.0 dB", "+4.75 dB", "+6.5 dB", "**provisional**",
            "`alimiter`", "0.84 linear ceiling", "ordinary new Track ID", "No\n  historical Track ID",
            "**Existing MP3 files and Tracks are not deleted, deactivated, re-pointed or rewritten",
            "**0.0 dB gain**", "native mono FLAC", "KanDrive's two-pass EBU R128", "first ready Track in the `WxAlert`",
            "no automatic fallback", "listening acceptance of the calibrated audio is still\n  outstanding",
            "`protected_runtime` is `null`", "`isadoraair-gunicorn`, then `isadoraair-monitoring`",
            "playout engine is **not** restarted", "`generate_dedication_intros`",
        ):
            self.assertIn(required, notes)

    def test_runbook_preserves_operational_boundaries(self):
        runbook = _artifact("docs/releases/r0134_installation.md").decode()
        for required in (
            R0133_RELEASE, IMPLEMENTATION, CORRECTION, CORRECTION_TREE, "## KOGR", "## WRJE", "actual installed release",
            "intervening release", "No engine restart", "Observation-only mode", "host unarmed",
            "residual PSA disabled", "evidence-collection user timers", "not an engine restart",
            "Publishing r0134 does not\nauthorize", "insert_urgent", "no ready alert Track", "no automatic fallback",
            "Playlists and rotations", "Already-approved logs", "no automatic source or Track\nrollback is authorized",
            "Do not presume it has the\nsame Weather configuration", "alimiter", "Claira",
            "Do not generate a test alert on the live station",
        ):
            self.assertIn(required, runbook)

    def test_documented_gains_match_the_calibrated_table(self):
        sys_path = str(PROJECT_ROOT / "weather_ingest" / "lib")
        sys.path.insert(0, sys_path)
        try:
            import speech_audio
        finally:
            sys.path.remove(sys_path)
        notes = _artifact("docs/releases/r0134.md").decode()
        expected = {
            ("Max_Weatherly", "forecast"): 6.0, ("Max_Weatherly", "obs"): 6.0, ("Max_Weatherly", "alert"): 6.0,
            ("Max_Weatherly", "temp"): 4.75, ("Claira_Sky", "forecast"): 6.5, ("Claira_Sky", "obs"): 6.5,
            ("Claira_Sky", "temp"): 4.75, ("Claira_Sky", "alert"): 6.5,
        }
        for (voice, product), gain in expected.items():
            self.assertEqual(speech_audio.VOICE_GAIN_DB[voice][product], gain, (voice, product))
        for text in ("+6.0 dB", "+4.75 dB", "+6.5 dB"):
            self.assertIn(text, notes)
        self.assertEqual(speech_audio.LIMITER_CEILING, 0.84)


class R0134HistoryAndScopeTests(SimpleTestCase):
    def test_accepted_engineering_is_exact_linear_and_content_identical(self):
        self.assertEqual(_git("rev-parse", f"{R0133_RELEASE}^{{tree}}"), R0133_TREE)
        self.assertEqual(_git("rev-list", "--reverse", f"{R0133_RELEASE}..{CORRECTION}").split(),
                         [IMPLEMENTATION, CORRECTION])
        self.assertEqual(_git("rev-list", "--merges", f"{R0133_RELEASE}..{CORRECTION}"), "")
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", IMPLEMENTATION).split()[1:], [R0133_RELEASE])
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", CORRECTION).split()[1:], [IMPLEMENTATION])
        self.assertEqual(_git("rev-parse", f"{IMPLEMENTATION}^{{tree}}"), IMPLEMENTATION_TREE)
        self.assertEqual(_git("rev-parse", f"{CORRECTION}^{{tree}}"), CORRECTION_TREE)

    def test_accepted_engineering_inventory_is_exactly_23_files(self):
        self.assertEqual(len(ENGINEERING_FILES), 23)
        self.assertEqual(_changed(R0133_RELEASE, CORRECTION), ENGINEERING_FILES)
        self.assertEqual(_changed(R0133_RELEASE, CORRECTION, "--diff-filter=A"), ENGINEERING_ADDED)
        self.assertEqual(_changed(R0133_RELEASE, CORRECTION, "--diff-filter=M"), ENGINEERING_MODIFIED)
        stat = _git("diff", "--shortstat", "--no-renames", R0133_RELEASE, CORRECTION)
        self.assertIn("23 files changed, 1197 insertions(+), 193 deletions(-)", stat)

    def test_release_is_one_metadata_only_child_of_the_accepted_correction(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [CORRECTION])
        self.assertEqual(_changed(CORRECTION, release), RELEASE_FILES)
        self.assertEqual(_changed(CORRECTION, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(CORRECTION, release, "--diff-filter=MDR"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0133_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0133_RELEASE}..{release}").split(),
                         [IMPLEMENTATION, CORRECTION, release])
        self.assertEqual(_changed(R0133_RELEASE, release), ENGINEERING_FILES | RELEASE_FILES)
        for path in ENGINEERING_FILES:
            self.assertEqual(_git("rev-parse", f"{release}:{path}"), _git("rev-parse", f"{CORRECTION}:{path}"), path)

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        release = _release_commit() or CORRECTION
        self.assertEqual(_changed(R0133_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0133_RELEASE, release, "--", "requirements*.txt", "*/requirements.txt"), set())
        self.assertEqual(_changed(R0133_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0133_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0133_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertFalse([path for path in _changed(R0133_RELEASE, release) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0133_RELEASE}:{prefix}"), _git("rev-parse", f"{release}:{prefix}"), prefix)
        # No engine, Hour Refill, library, updater or KanDrive code is touched.
        for untouched in ("library/", "road_conditions/", "updatecenter/", "isadoraair_updater/", "production/", "monitoring/"):
            changed = {path for path in _changed(R0133_RELEASE, release) if path.startswith(untouched)}
            self.assertEqual(changed - {"updatecenter/tests/test_r0134_release.py"}, set(), untouched)

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0134.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0133_RELEASE,
        )
        self.assertEqual(list(findings), [])

    def test_engine_does_not_call_the_changed_dedication_synthesis(self):
        """The reason no engine restart is declared: the engine imports webrequests.services only for scheduling
        helpers; the one changed function (synthesize_dedication_intro) is called solely by the separate
        generate_dedication_intros command."""
        release = _release_commit() or CORRECTION
        engine = _show(release, "library/services/engine.py").decode()
        self.assertNotIn("synthesize_dedication_intro", engine)
        self.assertNotIn("SpeechSpliceAnnouncement", engine)
        self.assertNotIn("DEDICATION_PEAK_CEILING", engine)


class R0134ChainAndCompanionTests(SimpleTestCase):
    def test_no_migration_companion_or_historical_manifest_change(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0134.json").exists())
            return
        self.assertEqual(_changed(R0133_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0133_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertEqual(_changed(R0133_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0134", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0134"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))

    def test_every_historical_manifest_is_byte_identical(self):
        release = _require_release(self)
        names = _git("ls-tree", "--name-only", R0133_RELEASE, "deploy/releases/").split()
        self.assertEqual(len(names), 133)
        for name in names:
            self.assertEqual(_git("rev-parse", f"{release}:{name}"), _git("rev-parse", f"{R0133_RELEASE}:{name}"), name)

    def test_chain_resolves_unique_r0133_to_r0134_identities(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain[-3:]], ["r0132", "r0133", "r0134"])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0132"], commits["r0133"], commits["r0134"]), (R0132_RELEASE, R0133_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))

    def test_complete_chain_r0001_through_r0134_is_contiguous_and_validates(self):
        release = _require_release(self)
        chain = release_chain.build_chain(release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases"))
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, 135)])
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn("FAIL", combined)
        self.assertIn("r0133 -> r0134", out.getvalue())

    def test_r0133_release_identity_and_manifest_are_preserved(self):
        release = _require_release(self)
        self.assertEqual(_introducing("deploy/releases/r0133.json"), [R0133_RELEASE])
        self.assertEqual(_show(release, "deploy/releases/r0133.json"), _show(R0133_RELEASE, "deploy/releases/r0133.json"))
        _git("merge-base", "--is-ancestor", R0133_RELEASE, release)  # raises unless r0133 is an ancestor of R


class R0134TrustedPlanTests(SimpleTestCase):
    def test_exact_plan_is_migration_free_gunicorn_then_monitoring_and_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0133", R0133_RELEASE), ("r0134", release)])
        plan = derive_plan(_repository(), release, R0133_RELEASE, "r0134", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0133", "r0134", release))
        self.assertEqual(plan.releases_in_plan, ("r0134",))
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
        plan = derive_plan(_repository(), release, R0131_RELEASE, "r0134", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, ("r0132", "r0133", "r0134"))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((), None))
        self.assertEqual(plan.services_requiring_restart, CATCH_UP_FROM_R0131_RESTARTS)
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=_known_units()), ())

    def test_catch_up_from_r0132_restarts_gunicorn_then_monitoring_without_the_engine(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0132_RELEASE, "r0134", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, ("r0133", "r0134"))
        self.assertEqual(plan.services_requiring_restart, RESTARTS)

    def test_restart_rehearsal_touches_only_gunicorn_then_monitoring(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
        runner = FakeSystemRunner()
        manager = SystemdManager(config, runner, enforce_root_ownership=False, signed_policy=_policy())
        self.assertEqual(manager.restart_declared(RESTARTS), list(RESTARTS))
        restarted = [call[2] for call in runner.calls if call[1] == "restart"]
        self.assertEqual(restarted, ["isadoraair-gunicorn.service", "isadoraair-monitoring.service"])
        touched = " ".join(" ".join(call) for call in runner.calls)
        for untouched in (
            "isadoraair-engine", "isadoraair-encoders", "isadoraair-rbds",
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


class R0134PlannerParityTests(TestCase):
    def test_application_and_protected_planners_match_action_fingerprint(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(R0133_RELEASE)
            built = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, R0133_RELEASE, "r0134",
                                    known_units=_known_units())
            self.assertEqual(built.safety_status, planner.SafetyStatus.READY_TO_PLAN, built.safety_detail)
            self.assertEqual((built.releases_in_plan, built.target_commit), (("r0134",), release))
            self.assertEqual(built.services_requiring_restart, RESTARTS)
            self.assertIsNone(built.migrations)
            self.assertEqual(built.fingerprint, protected.fingerprint)

    def test_catch_up_from_r0131_application_and_protected_planners_match(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(R0131_RELEASE)
            built = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, R0131_RELEASE, "r0134",
                                    known_units=_known_units())
            self.assertEqual(built.safety_status, planner.SafetyStatus.READY_TO_PLAN, built.safety_detail)
            self.assertEqual(built.releases_in_plan, ("r0132", "r0133", "r0134"))
            self.assertEqual(built.services_requiring_restart, CATCH_UP_FROM_R0131_RESTARTS)
            self.assertEqual(built.fingerprint, protected.fingerprint)
