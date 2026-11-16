"""r0141 immutable release contract (Past Overrides, NEXT relocation and live queue Track Swap).

Published r0140 (64e8620) plus eight accepted linear commits, then R:
  45ba3ff -- Schedule: Past Overrides list in Date Override mode;
  ab08101 -- Console queue: NEXT after the drag handle, new (inert) Swap column;
  e43481b -- Console queue Swap: guarded in-place replacement of an upcoming occurrence;
  9f230bf -- test pins (the Codex-reviewed candidate);
  7bfefa6 -- Codex F1/F2: claim adoption, contention, live-fill queue identity;
  a44d1d6 -- Codex F3-F5: protected origins, effective airtime, unknown outcomes;
  9b83a45 -- test-only query-parity setup (the re-reviewed candidate);
  d0aa0f2 -- F5 follow-up: an early read of the original track is not final (accepted);
  R       -- metadata-only child of d0aa0f2 adding this contract, the r0141 manifest and the two release documents.

r0141 declares no migration. Every object named by these tests is reachable from R alone (asserted against an isolated
repository that holds nothing but R's history), so a fresh station clone never depends on a feature branch, evidence
directory or unpublished release-preparation ref.
"""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase

from .phase_b_helpers import RUNTIME_ROOT, config_dict  # noqa: F401  (protected runtime on sys.path)
from .test_phase_b_systemd_daemon import FakeSystemRunner
from .test_r0137_release import LONG_RUNNING_ENTRIES, _ImportGraph
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
R0124_RELEASE = "69d53bc4f40e96a47359dce817375aa415cd068f"
R0130_RELEASE = "58e67cb247bd75ed489213dfc7e0bd6800596d3a"
R0136_RELEASE = "7c1df8f8d30c396fd01cfb6ded7d2240ecfa8fa3"
R0137_RELEASE = "ac22f4fd8445b22342fbaa334c5cf3f697ea644c"
R0138_RELEASE = "167a806ef97dd224d6f1182d2492ba3d908a5da5"
R0139_RELEASE = "52e764ed0a8dab63a308214b0e85bc3918c8bf07"
R0140_RELEASE = "64e8620ee5e90ffcd31bfca00369bd5cfb80fe3b"
R0140_TREE = "4f09c2cfda5c5e7dfac25e139d31eec2a2106744"
ENGINEERING = (
    ("45ba3ff6ccfc327cf04a29e030a378e47f8aa9c0", "69fd8da1766c0bd0afcb87e73b1a4dbe95a5dd6b"),
    ("ab08101f95493f75800cb6f9cd543c1d6ee121f9", "cc5e9b9eab77a199d496c1e78ec81bb91944d208"),
    ("e43481bb91853c075a80ff05d8d5405a8aded191", "84352267c4e730e56ddca898805db4fd4a49d203"),
    ("9f230bf14906a02d6aa32effeb703ae875c907df", "925d939fe3b1e417f4358798a511afb254ecb074"),
    ("7bfefa6d2e7a867b70d09f261d72edd0951109ba", "b12341446d070cc48735704dae5d51af559fa9a8"),
    ("a44d1d60594ae3e590972f81ca99b590ea855189", "256613f6679a407c06e77bdea121f2d11400a5ac"),
    ("9b83a45ce2783706651af76dfc1f969be6e18f5a", "dc75eadf48158ed4a414f01bf039a52f002b1d05"),
    ("d0aa0f212d1e5ef5ce6f4b0b2f4d7de80cbd4876", "e5c96b3e3891c260ec0aa2f12d634c1005994d48"),
)
ACCEPTED = ENGINEERING[-1][0]
ACCEPTED_TREE = ENGINEERING[-1][1]
ACCEPTED_PARENT = ENGINEERING[-2][0]
#: The final accepted correction touched exactly these files.
F5_FILES = {
    "docs/QUEUE_SWAP.md",
    "library/templates/library/dashboard.html",
    "library/tests/browser_console_queue_swap_check.py",
}
ENGINEERING_ADDED = {
    "docs/QUEUE_SWAP.md",
    "library/services/queue_replace.py",
    "library/tests/browser_console_queue_swap_check.py",
    "library/tests/browser_schedule_past_overrides_check.py",
    "library/tests/render_schedule_console_artifact.py",
    "library/tests/test_queue_swap.py",
    "library/tests/test_queue_swap_adversarial.py",
    "library/tests/test_queue_swap_airtime.py",
    "library/tests/test_queue_swap_claim.py",
    "library/tests/test_queue_swap_livefill.py",
    "library/tests/test_schedule_past_overrides.py",
}
ENGINEERING_MODIFIED = {
    "docs/SCHEDULE_PROFILES.md",
    "library/services/engine.py",
    "library/services/log_builder.py",
    "library/templates/library/dashboard.html",
    "library/templates/library/schedule.html",
    "library/tests/test_console_2_23_presentation.py",
    "library/tests/test_console_log_display_browser.py",
    "library/tests/test_live_fill_end_of_hour.py",
    "library/urls.py",
    "library/views.py",
}
ENGINEERING_FILES = ENGINEERING_ADDED | ENGINEERING_MODIFIED
#: Changed importable production modules (tests excluded; there is no migration).
PRODUCTION_MODULES = {
    "library.services.engine", "library.services.log_builder", "library.services.queue_replace",
    "library.urls", "library.views",
}
#: Changed Django templates; only Gunicorn renders them.
TEMPLATES = {"library/templates/library/dashboard.html", "library/templates/library/schedule.html"}
#: What the engine executes of the change: the claim / live-fill code and the effective-airtime helper.
ENGINE_MODULES = {"library.services.engine", "library.services.log_builder"}
MANIFEST_PATH = "deploy/releases/r0141.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0141.md",
    "docs/releases/r0141_installation.md",
    "updatecenter/tests/test_r0141_release.py",
}
LIBRARY_LEAF = ("library", "0103_remotedjconfig_auto_reconnect_enabled")
R0140_MIGRATION = "library.0103_remotedjconfig_auto_reconnect_enabled"
R0139_MIGRATION = "library.0102_uitheme_log_display_appearance"
R0138_MIGRATION = "library.0101_uitheme_show_played_items"
R0131_MIGRATION = "library.0100_dst_fallback_replay_pass"
RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine")
CATCH_UP_FROM_R0136_RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring")
CATCH_UP_FROM_R0124_RESTARTS = (
    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring", "isadoraair-active-message-worker",
)
#: installed release -> (releases in plan, migrations, compatibility, restarts)
PLANS = {
    "r0140": (R0140_RELEASE, ("r0141",), (), None, RESTARTS),
    "r0139": (R0139_RELEASE, ("r0140", "r0141"), (R0140_MIGRATION,), "additive", RESTARTS),
    "r0138": (R0138_RELEASE, ("r0139", "r0140", "r0141"), (R0139_MIGRATION, R0140_MIGRATION), "additive", RESTARTS),
    "r0136": (R0136_RELEASE, tuple(f"r{n:04d}" for n in range(137, 142)),
              (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", CATCH_UP_FROM_R0136_RESTARTS),
    "r0130": (R0130_RELEASE, tuple(f"r{n:04d}" for n in range(131, 142)),
              (R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive",
              CATCH_UP_FROM_R0136_RESTARTS),
    "r0124": (R0124_RELEASE, tuple(f"r{n:04d}" for n in range(125, 142)),
              (R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive",
              CATCH_UP_FROM_R0124_RESTARTS),
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
        ["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True, capture_output=True,
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
        raise AssertionError(f"r0141 manifest introduced more than once: {commits!r}")
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
    return parse_policy_dict(json.loads(_show(R0140_RELEASE, POLICY_PATH)), label="r0140 protected policy")


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


def _isolated_repository(root: Path, release: str) -> Path:
    """A bare repository holding only R's history (no alternates, no other refs): what a fresh clone of R gets."""
    bare = root / "isolated.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    _git("fetch", "-q", "--no-tags", str(PROJECT_ROOT), f"{release}:refs/heads/main", cwd=bare)
    return bare


class R0141ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_gunicorn_then_engine_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0141.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0141", "r0140", 5),
        )
        self.assertEqual((tuple(manifest.migrations_required), manifest.migration_compatibility), ((), None))
        self.assertEqual(tuple(manifest.services_requiring_restart), RESTARTS)
        self.assertEqual((manifest.collectstatic_required, manifest.protected_runtime), (False, None))
        self.assertEqual(data["minimum_supported_release_id"], "r0081")
        self.assertEqual(data["minimum_supported_release_id"],
                         json.loads(_show(R0140_RELEASE, "deploy/releases/r0140.json"))["minimum_supported_release_id"])
        for field in (
            "apt_packages_new", "systemd_units_changed", "systemd_units_new_required",
            "systemd_units_new_optional", "systemd_units_removed_or_renamed", "migrations_required",
        ):
            self.assertEqual(data[field], [], field)
        self.assertEqual(
            (data["manual_bootstrap_required"], data["python_requirements_changed"], data["requirements_sha256"],
             data["nginx_changed"], data["runtime_components_changed"], data["migration_compatibility"]),
            (False, False, None, False, False, None),
        )
        self.assertEqual(list(data), list(json.loads(_show(R0140_RELEASE, "deploy/releases/r0140.json"))))
        self.assertEqual(list(RESTARTS), [unit for unit in planner.RESTART_ORDER if unit in RESTARTS])
        self.assertLessEqual(len(data["summary"]), manifest_mod.SUMMARY_MAX_LEN)
        summary = " ".join(data["summary"].split())
        for required in ("Past Overrides", "NEXT beside the drag handle", "guarded live Swap",
                         "effective-airtime preview", "unknown-outcome reconciliation",
                         "the playback claim adopts a committed Swap atomically",
                         "live-fill proposals are revalidated under the log lock", "Restart Gunicorn, then the engine",
                         "interrupts program audio", "no migration"):
            self.assertIn(required, summary)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0141")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0141", "r0140"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((), None))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertFalse(parsed.collectstatic_required)
        self.assertIsNone(parsed.protected_runtime)

    def test_release_notes_preserve_the_feature_contract_and_limits(self):
        notes = " ".join(_artifact("docs/releases/r0141.md").decode().split())
        for required in (
            # A. Past Overrides
            "Schedule → **Date Override**", "**Past Overrides**", "**Schedule Profile's**", "**station timezone**",
            "by Rotation or Playlist name", "including transitions at minutes other than `:00`",
            "**Date-grouped pagination:**", "**Load older dates**", "**Show date / Show hour:**",
            "`DELETE /api/schedule/<id>/?profile=&date=`", "`schedule.edit`", "**archived profiles are refused**",
            "**weekly schedule**", "**generated logs**",
            "**Removing a date override does not delete its Rotation or Playlist.**",
            "`GET /api/schedule/past-overrides/",
            # B. Console queue
            "**Drag | NEXT | Swap | # | Artist | Title | Cat | Dur | Intro | Time On**",
            "**NEXT keeps its previous behavior**", "**existing drag-to-reorder mechanism is unchanged**",
            "keyboard focus now stays on the same row's control",
            # C. Swap
            "**Confirmation before mutation.**", "**same engine instance**", "**expected track**",
            "**category**", "**origin**", "**Normal queue reload.**", "idempotent no-op",
            "`generated_schedule`, `generated_fill` (build-time fill) and `operator_inserted`",
            "`live_fill`", "`refill_installed`", "`engine_inserted`", "`dst_replay`", "**unknown or missing origin**",
            "**claimed**, **played** and **on-deck**", "**reserved by a listener request**",
            "**adopts the committed track atomically with the claim**", "**contended, not failed**",
            "**planned effective-airtime**", "**additional material is not guaranteed**",
            "**Check again**", "**no automatic mutation retry**",
            "including the original track on a still-unclaimed row",
            # D, E
            "**a later eligible listener request can replace an operator-selected track before that occurrence is "
            "claimed**", "**not an operator reservation and carries no priority guarantee**",
            "**Swap is currently unavailable to Remote Hosts.**",
            "**NEXT, queue insert and reorder permissions are unchanged.**",
            "**no GroupAccess migration and no privilege expansion**",
            # F
            "`next_start - cue_in`", "**in the same transaction as the append and holding the `PlaylistLog` lock**",
            "**next-hour rollover**", "**No guaranteed filler.**", "`dry_run_only`", "`HOUR_REFILL_INSTALL_ARMED`",
            "does **not** activate Hour Refill", "introduces no new refill cause",
            "does not guarantee uninterrupted playout",
            # G
            "**Introduced by r0141 (accepted):**", "**Inherited, unchanged by r0141:**",
            "**8 consecutive contended attempts**", "Occurrence skipped after persistent claim contention",
            "**Queue-writer concurrency boundaries.**", "**Poison-row / live-fill.**",
            "**No real-engine / GStreamer field acceptance has been performed.**",
            "**The browser harnesses use simulated API responses**",
            # Qualification and deployment
            "fail 3 of 3 on `9f230bf` and pass 3 of 3", "7 of 14 on `9b83a45`", "26 of 26 on `d0aa0f2`",
            "Console 96 of 96", "35 of 35", "**not clean**", "452 failure/error identifiers",
            "451 identifiers", "**no candidate-only identifier**", "205 identical",
            "`migration_compatibility` is `null`", "1. `isadoraair-gunicorn` 2. `isadoraair-engine`",
            "`protected_runtime` is `null`",
            "**The engine restart interrupts program audio and ends any live Remote DJ session.**",
            "Publication does not authorize installation",
        ):
            self.assertIn(required, notes)
        for forbidden in ("activates Hour Refill", "guaranteed filler is added", "Remote Hosts can Swap",
                          "operator reservation is", "fully passing"):
            self.assertNotIn(forbidden, notes)

    def test_runbook_preserves_station_and_operational_boundaries(self):
        runbook = " ".join(_artifact("docs/releases/r0141_installation.md").decode().split())
        for required in (
            R0140_RELEASE, *(commit for commit, _tree in ENGINEERING), ACCEPTED_TREE,
            "Publication does not authorize deployment", "separate station-specific",
            "**This release restarts the audio engine.**", "**interrupts program audio**",
            "**may terminate any live Remote DJ session**", "operator-approved maintenance window",
            "There is **no migration**", "Gunicorn then engine restarts only", "trusted updater identity",
            "recovery payload", "action fingerprint", "`playback_claimed_at`", "Manual/Auto state",
            "Remote Mic (PTT) state", "Weather/AMBER", "Active Messages", "Hour Refill",
            "no critical quarantine", "restart-required finding",
            "## KOGR preflight and acceptance", "`dry_run_only` remains checked",
            "`HOUR_REFILL_INSTALL_ARMED` remains false", "every current Schedule Profile, Rotation and Playlist",
            "**not automatically remove the Jeff Memorial Rotation or any historical override**",
            "**separate operator action**", "## WRJE preflight and acceptance",
            "**independent installed-release check**", "**catch-up plan**", "**service-health preflight**",
            "**installation authorization**", "**operational acceptance**", "Do not assume it is on r0140",
            "Success at KOGR is neither authorization nor acceptance evidence for WRJE",
            "**Past Overrides** appears beside the date picker controls", "Name filtering finds actual historical "
            "assignments", "**only a safe test fixture**", "Do not remove a real historical override",
            "**NEXT** appears in the intended column", "Drag-to-reorder and NEXT still work",
            "Protected occurrences", "Network uncertainty is represented truthfully", "narrow-screen rendering",
            "Normal program audio resumes", "encoder connections remain healthy", "crossfades",
            "No unexpected track skip, duplicate occurrence or wrong-track history",
            "Standard live fill and the top-of-hour transition", "No unexpected Hour Refill activation",
            "**Do not induce live queue races, lock timeouts, media errors or deliberate playout gaps on an on-air "
            "station.**", "requires isolated testing", "Record KOGR and WRJE evidence independently",
            "Do not manually run `migrate`", "Stop and preserve", "Do not use manual Git resets",
            "not rollback authority",
        ):
            self.assertIn(required, runbook)
        self.assertNotIn("Apply the migration", runbook)

    def test_documents_match_the_manifest(self):
        data = json.loads(_artifact(MANIFEST_PATH))
        notes = " ".join(_artifact("docs/releases/r0141.md").decode().split())
        runbook = " ".join(_artifact("docs/releases/r0141_installation.md").decode().split())
        self.assertEqual((data["migrations_required"], data["migration_compatibility"]), ([], None))
        self.assertIn("The release has **no migration**", notes)
        self.assertIn("`migrations_required` is empty", notes)
        self.assertIn("No migration, apt package, Python requirement, systemd unit, static collection, nginx, "
                      "manual-bootstrap or protected-runtime operation is expected", runbook)
        order = " ".join(f"{n}. `{unit}`" for n, unit in enumerate(data["services_requiring_restart"], 1))
        self.assertIn(order, notes)
        self.assertIn("2. Restart `isadoraair-gunicorn`. 3. Restart `isadoraair-engine`", runbook)
        self.assertIn("A direct r0140 → r0141 plan advances source to R, then restarts `isadoraair-gunicorn`, then "
                      "`isadoraair-engine`. Nothing else.", runbook)
        self.assertEqual(data["services_requiring_restart"], list(RESTARTS))


class R0141HistoryAndScopeTests(SimpleTestCase):
    def test_accepted_engineering_is_exact_linear_and_content_identical(self):
        self.assertEqual(_git("rev-parse", f"{R0140_RELEASE}^{{tree}}"), R0140_TREE)
        self.assertEqual(_git("rev-list", "--reverse", f"{R0140_RELEASE}..{ACCEPTED}").split(),
                         [commit for commit, _tree in ENGINEERING])
        self.assertEqual(_git("rev-list", "--merges", f"{R0140_RELEASE}..{ACCEPTED}"), "")
        parent = R0140_RELEASE
        for commit, tree in ENGINEERING:
            self.assertEqual(_git("rev-list", "--parents", "-n", "1", commit).split()[1:], [parent], commit)
            self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}"), tree, commit)
            parent = commit

    def test_accepted_engineering_inventory_is_exact(self):
        self.assertEqual(_changed(R0140_RELEASE, ACCEPTED), ENGINEERING_FILES)
        self.assertEqual(_changed(R0140_RELEASE, ACCEPTED, "--diff-filter=A"), ENGINEERING_ADDED)
        self.assertEqual(_changed(R0140_RELEASE, ACCEPTED, "--diff-filter=M"), ENGINEERING_MODIFIED)
        self.assertIn("21 files changed, 3859 insertions(+), 36 deletions(-)",
                      _git("diff", "--shortstat", "--no-renames", R0140_RELEASE, ACCEPTED))

    def test_final_accepted_correction_touches_only_the_reported_files(self):
        self.assertEqual(_changed(ACCEPTED_PARENT, ACCEPTED), F5_FILES)
        self.assertEqual(_changed(ACCEPTED_PARENT, ACCEPTED, "--diff-filter=M"), F5_FILES)

    def test_release_is_one_metadata_only_child(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ACCEPTED])
        self.assertEqual(_changed(ACCEPTED, release), RELEASE_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=MDR"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0140_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0140_RELEASE}..{release}").split(),
                         [commit for commit, _tree in ENGINEERING] + [release])
        for path in ENGINEERING_FILES:
            self.assertEqual(_git("rev-parse", f"{release}:{path}"), _git("rev-parse", f"{ACCEPTED}:{path}"), path)
        # Everything outside the four release files is the accepted tree, object for object.
        accepted_entries = _git("ls-tree", "-r", ACCEPTED).splitlines()
        release_entries = [line for line in _git("ls-tree", "-r", release).splitlines()
                           if line.split("\t", 1)[1] not in RELEASE_FILES]
        self.assertEqual(release_entries, accepted_entries)

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        release = _release_commit() or ACCEPTED
        self.assertEqual(_changed(R0140_RELEASE, release, "--", "requirements*.txt", "*/requirements.txt"), set())
        self.assertEqual(_changed(R0140_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0140_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0140_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertEqual(_changed(R0140_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertFalse([path for path in _changed(R0140_RELEASE, release) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0140_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{release}:{prefix}"), prefix)
        touched = _changed(R0140_RELEASE, release)
        for untouched in ("isadoraair_updater/", "protected_bootstrap/", "monitoring/", "weather/", "weather_ingest/",
                          "active_messages/", "webrequests/", "production/", "encoders/", "rbds/", "spoken/",
                          "authz/", "deploy/updater", "templates/", "library/models.py", "library/migrations/",
                          "library/static/"):
            self.assertFalse({path for path in touched if path.startswith(untouched)}, untouched)
        self.assertFalse({path for path in touched if "hour_refill" in path})
        self.assertLessEqual({path for path in touched if not path.startswith(("library/", "docs/"))},
                             {MANIFEST_PATH, "updatecenter/tests/test_r0141_release.py"})
        # Earlier immutable release contracts are untouched.
        self.assertEqual({path for path in touched if path.startswith("updatecenter/")},
                         {"updatecenter/tests/test_r0141_release.py"} if _release_commit() else set())

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0141.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0140_RELEASE,
        )
        self.assertEqual(list(findings), [])

    def test_changed_modules_and_templates_are_exactly_the_audited_set(self):
        release = _release_commit() or ACCEPTED
        changed = _changed(R0140_RELEASE, release)
        changed_modules = {
            path[:-3].replace("/", ".") for path in changed
            if path.endswith(".py") and "/tests/" not in path and "/migrations/" not in path
        }
        self.assertEqual(changed_modules, PRODUCTION_MODULES)
        production_other = {
            path for path in changed
            if not path.endswith(".py") and not path.startswith("docs/") and path != MANIFEST_PATH
        }
        self.assertEqual(production_other, TEMPLATES)

    def test_declared_restarts_are_exactly_the_long_running_services_that_execute_the_changed_code(self):
        """Gunicorn loads the changed views and URLs, the new Swap service and (through the views) the engine-facing
        services, and renders the two changed templates; the engine runs the changed claim and live-fill code and the
        effective-airtime helper. No other long-running service reaches any changed module."""
        release = _release_commit() or ACCEPTED
        graph = _ImportGraph(release)
        for entry in LONG_RUNNING_ENTRIES.values():
            self.assertIsNotNone(graph.path(entry), entry)
        for module in PRODUCTION_MODULES:
            self.assertIsNotNone(graph.path(module), module)
        loading = {service for service in LONG_RUNNING_ENTRIES if graph.service_reach(service) & PRODUCTION_MODULES}
        self.assertEqual(loading, set(RESTARTS))
        self.assertEqual(graph.service_reach("isadoraair-gunicorn") & PRODUCTION_MODULES, PRODUCTION_MODULES)
        self.assertEqual(graph.service_reach("isadoraair-engine") & PRODUCTION_MODULES, ENGINE_MODULES)
        for service in set(LONG_RUNNING_ENTRIES) - set(RESTARTS):
            self.assertEqual(graph.service_reach(service) & PRODUCTION_MODULES, set(), service)
        # The long-running set is still every Type=simple/default isadoraair unit that runs project code.
        long_running = set()
        for unit in _git("ls-tree", "--name-only", release, "deploy/").split():
            name = Path(unit).name
            if not (name.startswith("isadoraair-") and name.endswith(".service")):
                continue
            text = _show(release, unit).decode()
            if "Type=oneshot" in text or "manage.py" not in text and "gunicorn" not in text:
                continue
            long_running.add(name[: -len(".service")])
        self.assertEqual(long_running, set(LONG_RUNNING_ENTRIES))


class R0141MigrationTests(TestCase):
    def test_no_migration_is_declared_changed_or_pending(self):
        release = _release_commit() or ACCEPTED
        self.assertEqual(_changed(R0140_RELEASE, release, "--", "*/migrations/*"), set())
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(loader.graph.leaf_nodes("library"), [LIBRARY_LEAF])
        executor = MigrationExecutor(connection)
        self.assertEqual(executor.migration_plan(executor.loader.graph.leaf_nodes()), [])

    def test_no_companion_exists_or_is_required(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0141.json").exists())
            return
        self.assertEqual(_changed(R0140_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0141", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0141"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))


class R0141ChainTests(SimpleTestCase):
    def test_historical_manifests_are_byte_identical_and_r0141_is_unique(self):
        release = _require_release(self)
        names = _git("ls-tree", "--name-only", R0140_RELEASE, "deploy/releases/").split()
        self.assertEqual(len(names), 140)
        for name in names:
            self.assertEqual(_git("rev-parse", f"{release}:{name}"), _git("rev-parse", f"{R0140_RELEASE}:{name}"), name)
        self.assertEqual(_changed(R0140_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertEqual(_introducing("deploy/releases/r0140.json"), [R0140_RELEASE])
        self.assertEqual(_introducing(), [release])

    def test_complete_chain_r0001_through_r0141_is_contiguous_and_validates(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, 142)])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0139"], commits["r0140"], commits["r0141"]),
                         (R0139_RELEASE, R0140_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        self.assertNotIn("FAIL", out.getvalue() + err.getvalue())
        self.assertIn("r0140 -> r0141", out.getvalue())


class R0141TrustedPlanTests(SimpleTestCase):
    def _assert_no_other_actions(self, plan):
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed, plan.runtime_components_changed,
                          plan.python_requirements_changed, plan.manual_bootstrap_required),
                         (False, False, False, False, False))
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed, plan.systemd_units_new_required,
                          plan.systemd_units_new_optional, plan.systemd_units_removed_or_renamed),
                         ((), (), (), (), ()))
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=_known_units()), ())

    def _assert_plan(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        plan = derive_plan(_repository(), release, installed, "r0141", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         (label, "r0141", release))
        self.assertEqual(plan.releases_in_plan, releases)
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), (migrations, compatibility))
        self.assertEqual(plan.services_requiring_restart, restarts)
        self._assert_no_other_actions(plan)
        return plan

    def test_direct_plan_is_migration_free_gunicorn_then_engine_and_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0140", R0140_RELEASE), ("r0141", release)])
        self._assert_plan("r0140")

    def test_catch_up_from_r0139_keeps_the_r0140_migration(self):
        self._assert_plan("r0139")

    def test_catch_up_from_r0138_keeps_the_r0139_and_r0140_migrations(self):
        self._assert_plan("r0138")

    def test_catch_up_from_r0136_keeps_the_r0137_monitoring_restart(self):
        self._assert_plan("r0136")

    def test_catch_up_from_r0130_keeps_all_four_migrations(self):
        self._assert_plan("r0130")

    def test_catch_up_from_r0124_preserves_all_intervening_actions(self):
        self._assert_plan("r0124")

    def test_restart_rehearsal_touches_only_gunicorn_then_engine(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
        runner = FakeSystemRunner()
        manager = SystemdManager(config, runner, enforce_root_ownership=False, signed_policy=_policy())
        self.assertEqual(manager.restart_declared(RESTARTS), list(RESTARTS))
        restarted = [call[2] for call in runner.calls if call[1] == "restart"]
        self.assertEqual(restarted, ["isadoraair-gunicorn.service", "isadoraair-engine.service"])
        touched = " ".join(" ".join(call) for call in runner.calls)
        for untouched in (
            "isadoraair-encoders", "isadoraair-monitoring", "isadoraair-rbds",
            "isadoraair-active-message-worker", "isadoraair-spoken-render-worker",
        ):
            self.assertNotIn(untouched, touched)


class R0141ReachabilityTests(SimpleTestCase):
    def test_every_named_object_is_reachable_from_r_alone(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            bare = _isolated_repository(Path(directory), release)
            self.assertEqual(_git("for-each-ref", "--format=%(refname)", cwd=bare), "refs/heads/main")
            self.assertFalse((bare / "objects/info/alternates").exists())
            self.assertEqual(_git("rev-parse", "main", cwd=bare), release)
            for commit, tree in ENGINEERING:
                self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}", cwd=bare), tree, commit)
                _git("merge-base", "--is-ancestor", commit, release, cwd=bare)
            for label, (installed, *_rest) in PLANS.items():
                _git("merge-base", "--is-ancestor", installed, release, cwd=bare)
            self.assertEqual(_git("rev-parse", f"{R0140_RELEASE}^{{tree}}", cwd=bare), R0140_TREE)
            for path in RELEASE_FILES:
                _git("cat-file", "-e", f"{release}:{path}", cwd=bare)
            # The protected planner derives the identical direct plan from the isolated repository.
            isolated = derive_plan(_repository(bare), release, R0140_RELEASE, "r0141", known_units=_known_units())
            shared = derive_plan(_repository(), release, R0140_RELEASE, "r0141", known_units=_known_units())
            self.assertEqual(isolated.fingerprint, shared.fingerprint)
            self.assertEqual(isolated.services_requiring_restart, RESTARTS)


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


class R0141PlannerParityTests(TestCase):
    def _assert_parity(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(installed)
            application = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, installed, "r0141",
                                    known_units=_known_units())
            self.assertEqual(application.safety_status, planner.SafetyStatus.READY_TO_PLAN,
                             application.safety_detail)
            self.assertEqual((application.releases_in_plan, application.target_commit), (releases, release))
            if migrations:
                self.assertIsNotNone(application.migrations)
                self.assertEqual(application.migrations.explicitly_required, migrations)
                self.assertEqual(application.migrations.compatibility, compatibility)
            else:
                self.assertIsNone(application.migrations)
            self.assertEqual(application.services_requiring_restart, restarts)
            self.assertEqual(application.fingerprint, protected.fingerprint)

    def test_direct_application_and_protected_planners_match_action_fingerprint(self):
        self._assert_parity("r0140")

    def test_catch_up_from_r0139_application_and_protected_planners_match(self):
        self._assert_parity("r0139")

    def test_catch_up_from_r0130_application_and_protected_planners_match(self):
        self._assert_parity("r0130")
