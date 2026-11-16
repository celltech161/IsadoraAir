"""r0143 immutable release contract (console VU meter staleness window).

Published r0142 (6b8d4b8) plus one engineering commit, then R:
  2fb09c8 -- both console VU meters share VU_STALE_SECONDS = 1.0 (was 0.75); template + regression test only.
             Independent review waived by the operator (template-only change);
  R       -- metadata-only child of 2fb09c8 adding this contract, the r0143 manifest and the two release documents.

r0143 declares no migration and restarts Gunicorn only. Every object named by these tests is reachable from R alone
(asserted against an isolated repository that holds nothing but R's history).
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
R0138_RELEASE = "167a806ef97dd224d6f1182d2492ba3d908a5da5"
R0139_RELEASE = "52e764ed0a8dab63a308214b0e85bc3918c8bf07"
R0140_RELEASE = "64e8620ee5e90ffcd31bfca00369bd5cfb80fe3b"
R0141_RELEASE = "529b84b79e154a0d370c9a87c78484716d4e6518"
R0142_RELEASE = "6b8d4b844de385e4f149b90cf30079d790d19fc1"
R0142_TREE = "381636dde9e738580ef699f0e4b522dbec60b290"
ENGINEERING = "2fb09c824aa87e34ce6b0d677d89e06b94a8a9df"
ENGINEERING_TREE = "2bd3bfd62e99ac346fd882d4419d9b9071c84269"
TEMPLATE = "library/templates/library/dashboard.html"
REGRESSION = "library/tests/test_vu_staleness_window.py"
CHANGE_FILES = {TEMPLATE, REGRESSION}
#: The exact template change: one shared constant replacing the two 0.75 s literals.
TEMPLATE_REMOVED = [
    "  if (!remoteDj.ts || (now - remoteDj.ts) > 0.75) {",
    "    const stale = !data.ts || (now - data.ts) > 0.75;",
]
TEMPLATE_ADDED = [
    "// A level sample older than this (viewer clock minus the engine's",
    "// timestamp) renders as silence. The age includes any difference between",
    "// the viewing device's clock and the station's, so the window allows a",
    "// device clock up to about a second ahead (Windows time sync commonly",
    "// leaves ~0.8 s). Shared by Program Level and the Remote Mic PTT meter.",
    "const VU_STALE_SECONDS = 1.0;",
    "  if (!remoteDj.ts || (now - remoteDj.ts) > VU_STALE_SECONDS) {",
    "    const stale = !data.ts || (now - data.ts) > VU_STALE_SECONDS;",
]
MANIFEST_PATH = "deploy/releases/r0143.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0143.md",
    "docs/releases/r0143_installation.md",
    "updatecenter/tests/test_r0143_release.py",
}
LIBRARY_LEAF = ("library", "0103_remotedjconfig_auto_reconnect_enabled")
R0140_MIGRATION = "library.0103_remotedjconfig_auto_reconnect_enabled"
R0139_MIGRATION = "library.0102_uitheme_log_display_appearance"
R0138_MIGRATION = "library.0101_uitheme_show_played_items"
R0131_MIGRATION = "library.0100_dst_fallback_replay_pass"
RESTARTS = ("isadoraair-gunicorn",)
CATCH_UP_FROM_R0140_RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine")
CATCH_UP_FROM_R0136_RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring")
CATCH_UP_FROM_R0124_RESTARTS = (
    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring", "isadoraair-active-message-worker",
)
#: installed release -> (releases in plan, migrations, compatibility, restarts)
PLANS = {
    "r0142": (R0142_RELEASE, ("r0143",), (), None, RESTARTS),
    "r0141": (R0141_RELEASE, ("r0142", "r0143"), (), None, RESTARTS),
    "r0140": (R0140_RELEASE, ("r0141", "r0142", "r0143"), (), None, CATCH_UP_FROM_R0140_RESTARTS),
    "r0139": (R0139_RELEASE, tuple(f"r{n:04d}" for n in range(140, 144)), (R0140_MIGRATION,), "additive",
              CATCH_UP_FROM_R0140_RESTARTS),
    "r0138": (R0138_RELEASE, tuple(f"r{n:04d}" for n in range(139, 144)), (R0139_MIGRATION, R0140_MIGRATION),
              "additive", CATCH_UP_FROM_R0140_RESTARTS),
    "r0136": (R0136_RELEASE, tuple(f"r{n:04d}" for n in range(137, 144)),
              (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", CATCH_UP_FROM_R0136_RESTARTS),
    "r0130": (R0130_RELEASE, tuple(f"r{n:04d}" for n in range(131, 144)),
              (R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive",
              CATCH_UP_FROM_R0136_RESTARTS),
    "r0124": (R0124_RELEASE, tuple(f"r{n:04d}" for n in range(125, 144)),
              (R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive",
              CATCH_UP_FROM_R0124_RESTARTS),
}
POLICY_PATH = "deploy/updater_runtime/protected-policy.json"
PROTECTED_PREFIXES = (
    "deploy/updater_runtime",
    "deploy/updater_bootstrap",
    "deploy/updater_attestations",
    "deploy/migration_authorizations",
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
        raise AssertionError(f"r0143 manifest introduced more than once: {commits!r}")
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
    return parse_policy_dict(json.loads(_show(R0142_RELEASE, POLICY_PATH)), label="r0142 protected policy")


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


def _isolated_repository(root: Path, release: str) -> Path:
    """A bare repository holding only R's history (no alternates, no other refs): what a fresh clone of R gets."""
    bare = root / "isolated.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    _git("fetch", "-q", "--no-tags", str(PROJECT_ROOT), f"{release}:refs/heads/main", cwd=bare)
    return bare


def _template_diff_lines(base: str, commit: str) -> tuple[list[str], list[str]]:
    removed, added = [], []
    for line in _git("diff", "-U0", "--no-renames", base, commit, "--", TEMPLATE).splitlines():
        if line.startswith(("+++ ", "--- ")):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
    return removed, added


class R0143ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_gunicorn_only_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0143.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0143", "r0142", 5),
        )
        self.assertEqual((tuple(manifest.migrations_required), manifest.migration_compatibility), ((), None))
        self.assertEqual(tuple(manifest.services_requiring_restart), RESTARTS)
        self.assertEqual((manifest.collectstatic_required, manifest.protected_runtime), (False, None))
        r0142 = json.loads(_show(R0142_RELEASE, "deploy/releases/r0142.json"))
        self.assertEqual(data["minimum_supported_release_id"], "r0081")
        self.assertEqual(data["minimum_supported_release_id"], r0142["minimum_supported_release_id"])
        self.assertEqual(data["schema_version"], r0142["schema_version"])
        for field in (
            "apt_packages_new", "systemd_units_changed", "systemd_units_new_required",
            "systemd_units_new_optional", "systemd_units_removed_or_renamed", "migrations_required",
        ):
            self.assertEqual(data[field], [], field)
        self.assertEqual(
            (data["manual_bootstrap_required"], data["python_requirements_changed"], data["requirements_sha256"],
             data["nginx_changed"], data["runtime_components_changed"], data["migration_compatibility"],
             data["collectstatic_required"], data["protected_runtime"]),
            (False, False, None, False, False, None, False, None),
        )
        self.assertEqual(data["services_requiring_restart"], ["isadoraair-gunicorn"])
        self.assertEqual(list(data), list(r0142))
        self.assertLessEqual(len(data["summary"]), manifest_mod.SUMMARY_MAX_LEN)
        summary = " ".join(data["summary"].split())
        for required in ("Program Level and Remote Mic PTT VU meters", "up to 1.0 s old", "(was 0.75 s)",
                         "no longer shows a blank meter", "Template-only", "Restart Gunicorn only", "no migration",
                         "engine restart", "protected-runtime change"):
            self.assertIn(required, summary)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0143")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0143", "r0142"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((), None))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertFalse(parsed.collectstatic_required)
        self.assertIsNone(parsed.protected_runtime)

    def test_release_notes_document_cause_change_limits_and_review_waiver(self):
        notes = " ".join(_artifact("docs/releases/r0143.md").decode().split())
        for required in (
            "**Program Level**", "**viewer's clock**", "**0.75 s**", "**0.84 s ahead**", "`VU_STALE_SECONDS = 1.0`",
            "(`pollLevels`)", "(`renderRemoteDjVu`)", "a stopped engine or lost connection still blanks the meter",
            "still depends on the viewing device's clock", "**52 of 52**", "**silence at 0.87 s**",
            "the operator waived independent Codex review", "`migrations_required` is empty",
            "`migration_compatibility` is `null`", "`protected_runtime` is `null`", "1. `isadoraair-gunicorn`",
            "**the audio engine and encoders remain running**", "reload open console pages",
            "Publication does not authorize installation",
        ):
            self.assertIn(required, notes)
        for forbidden in ("fully passing", "Codex accepted", "2. `isadoraair-engine`"):
            self.assertNotIn(forbidden, notes)

    def test_runbook_preserves_station_and_operational_boundaries(self):
        runbook = " ".join(_artifact("docs/releases/r0143_installation.md").decode().split())
        for required in (
            R0142_RELEASE, ENGINEERING, ENGINEERING_TREE,
            "Publication does not authorize deployment", "separate station-specific", "**restarts Gunicorn only**",
            "There is **no migration**", "Nothing else.", "no engine, encoder or Monitoring restart",
            "stop and investigate", "a station on r0140 also restarts the engine for r0141",
            "`library.0103_remotedjconfig_auto_reconnect_enabled`", "restarts Monitoring for r0137",
            "Do not manually run `migrate`", "Do not assume the station is on r0142", "action fingerprint",
            "Verify the engine PID and service activation time **did not change**", "**Reload** each open console page",
            "**Program Level** meter moves", "do not start a session or key the microphone on air",
            "A value above 1.0 means that device's clock is more than a second ahead",
            "**A successful installation at one station is not authorization to install at the other.**",
            "Record KOGR and WRJE evidence independently", "Stop and preserve", "Do not use manual Git resets",
            "not rollback authority",
        ):
            self.assertIn(required, runbook)
        self.assertNotIn("Restart `isadoraair-engine`", runbook)

    def test_documents_match_the_manifest(self):
        data = json.loads(_artifact(MANIFEST_PATH))
        notes = " ".join(_artifact("docs/releases/r0143.md").decode().split())
        runbook = " ".join(_artifact("docs/releases/r0143_installation.md").decode().split())
        self.assertIn("There is no migration: `migrations_required` is empty", notes)
        self.assertIn("No migration, apt package, Python requirement, systemd unit, static collection, nginx, "
                      "manual-bootstrap or protected-runtime operation is expected", runbook)
        order = " ".join(f"{n}. `{unit}`" for n, unit in enumerate(data["services_requiring_restart"], 1))
        self.assertIn(f"Restart exactly: {order}", notes)
        self.assertIn("1. Advance source to the exact canonical r0143 release commit R. 2. Restart "
                      "`isadoraair-gunicorn`. No migration", runbook)
        self.assertIn("A direct r0142 → r0143 plan advances source to R, then restarts `isadoraair-gunicorn`. "
                      "Nothing else.", runbook)
        self.assertEqual(data["services_requiring_restart"], list(RESTARTS))


class R0143HistoryAndScopeTests(SimpleTestCase):
    def test_engineering_commit_is_the_exact_linear_child_of_r0142(self):
        self.assertEqual(_git("rev-parse", f"{R0142_RELEASE}^{{tree}}"), R0142_TREE)
        self.assertEqual(_git("rev-list", f"{R0142_RELEASE}..{ENGINEERING}").split(), [ENGINEERING])
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", ENGINEERING).split()[1:], [R0142_RELEASE])
        self.assertEqual(_git("rev-parse", f"{ENGINEERING}^{{tree}}"), ENGINEERING_TREE)
        self.assertEqual(_introducing("deploy/releases/r0142.json"), [R0142_RELEASE])

    def test_change_inventory_is_exact(self):
        self.assertEqual(_changed(R0142_RELEASE, ENGINEERING), CHANGE_FILES)
        self.assertEqual(_changed(R0142_RELEASE, ENGINEERING, "--diff-filter=A"), {REGRESSION})
        self.assertEqual(_changed(R0142_RELEASE, ENGINEERING, "--diff-filter=M"), {TEMPLATE})
        self.assertIn("2 files changed, 47 insertions(+), 2 deletions(-)",
                      _git("diff", "--shortstat", "--no-renames", R0142_RELEASE, ENGINEERING))

    def test_the_only_production_change_is_the_vu_window(self):
        self.assertEqual(_template_diff_lines(R0142_RELEASE, ENGINEERING), (TEMPLATE_REMOVED, TEMPLATE_ADDED))

    def test_release_is_one_metadata_only_child(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ENGINEERING])
        self.assertEqual(_changed(ENGINEERING, release), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=MDRT"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0142_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0142_RELEASE}..{release}").split(), [ENGINEERING, release])
        engineering_entries = _git("ls-tree", "-r", ENGINEERING).splitlines()
        release_entries = [line for line in _git("ls-tree", "-r", release).splitlines()
                           if line.split("\t", 1)[1] not in RELEASE_FILES]
        self.assertEqual(release_entries, engineering_entries)

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0142_RELEASE, release, "--", "requirements*.txt", "*/requirements.txt"), set())
        self.assertEqual(_changed(R0142_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0142_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0142_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertFalse([path for path in _changed(R0142_RELEASE, release) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0142_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{release}:{prefix}"), prefix)
        self.assertEqual(_git("rev-parse", f"{R0142_RELEASE}:{POLICY_PATH}"),
                         _git("rev-parse", f"{release}:{POLICY_PATH}"))
        touched = _changed(R0142_RELEASE, release)
        self.assertEqual({path for path in touched if path.startswith("deploy/")},
                         {MANIFEST_PATH} if _release_commit() else set())
        self.assertEqual({path for path in touched if path.startswith("updatecenter/")},
                         {"updatecenter/tests/test_r0143_release.py"} if _release_commit() else set())
        self.assertEqual(touched - RELEASE_FILES, CHANGE_FILES)

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0143.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0142_RELEASE,
        )
        self.assertEqual(list(findings), [])

    def test_no_python_module_changed_only_the_console_template(self):
        release = _release_commit() or ENGINEERING
        changed = _changed(R0142_RELEASE, release)
        self.assertEqual({path for path in changed if path.endswith(".py") and "/tests/" not in path}, set())
        production_other = {path for path in changed
                            if not path.endswith(".py") and not path.startswith("docs/") and path != MANIFEST_PATH}
        self.assertEqual(production_other, {TEMPLATE})

    def test_declared_restart_is_exactly_gunicorn(self):
        """The console template is rendered by library.views (Gunicorn). No other long-running service reaches
        library.views, so only Gunicorn restarts (Django caches compiled templates per process)."""
        release = _release_commit() or ENGINEERING
        self.assertIn(b'"library/dashboard.html"', _show(release, "library/views.py"))
        graph = _ImportGraph(release)
        self.assertIsNotNone(graph.path("library.views"))
        renderers = {service for service in LONG_RUNNING_ENTRIES if "library.views" in graph.service_reach(service)}
        self.assertEqual(renderers, set(RESTARTS))


class R0143MigrationTests(TestCase):
    def test_no_migration_is_declared_changed_or_pending(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0142_RELEASE, release, "--", "*/migrations/*"), set())
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(loader.graph.leaf_nodes("library"), [LIBRARY_LEAF])
        executor = MigrationExecutor(connection)
        self.assertEqual(executor.migration_plan(executor.loader.graph.leaf_nodes()), [])

    def test_no_companion_exists_or_is_required(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0143.json").exists())
            return
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0143", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0143"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))


class R0143ChainTests(SimpleTestCase):
    def test_historical_manifests_are_byte_identical_and_r0143_is_unique(self):
        release = _require_release(self)
        names = _git("ls-tree", "--name-only", R0142_RELEASE, "deploy/releases/").split()
        self.assertEqual(len(names), 142)
        for name in names:
            self.assertEqual(_git("rev-parse", f"{release}:{name}"), _git("rev-parse", f"{R0142_RELEASE}:{name}"), name)
        self.assertEqual(_changed(R0142_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertEqual(_introducing(), [release])

    def test_complete_chain_r0001_through_r0143_is_contiguous_and_validates(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, 144)])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0141"], commits["r0142"], commits["r0143"]),
                         (R0141_RELEASE, R0142_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        self.assertNotIn("FAIL", out.getvalue() + err.getvalue())
        self.assertIn("r0142 -> r0143", out.getvalue())


class R0143TrustedPlanTests(SimpleTestCase):
    def _assert_plan(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        plan = derive_plan(_repository(), release, installed, "r0143", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         (label, "r0143", release))
        self.assertEqual(plan.releases_in_plan, releases)
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), (migrations, compatibility))
        self.assertEqual(plan.services_requiring_restart, restarts)
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed, plan.runtime_components_changed,
                          plan.python_requirements_changed, plan.manual_bootstrap_required),
                         (False, False, False, False, False))
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed, plan.systemd_units_new_required,
                          plan.systemd_units_new_optional, plan.systemd_units_removed_or_renamed),
                         ((), (), (), (), ()))
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=_known_units()), ())

    def test_direct_plan_is_migration_free_gunicorn_only_and_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0142", R0142_RELEASE), ("r0143", release)])
        self._assert_plan("r0142")

    def test_catch_up_from_r0141_stays_gunicorn_only(self):
        self._assert_plan("r0141")

    def test_catch_up_from_r0140_keeps_the_r0141_engine_restart(self):
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

    def test_restart_rehearsal_touches_only_gunicorn(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
        runner = FakeSystemRunner()
        manager = SystemdManager(config, runner, enforce_root_ownership=False, signed_policy=_policy())
        self.assertEqual(manager.restart_declared(RESTARTS), list(RESTARTS))
        self.assertEqual([call[2] for call in runner.calls if call[1] == "restart"], ["isadoraair-gunicorn.service"])
        touched = " ".join(" ".join(call) for call in runner.calls)
        for untouched in ("isadoraair-engine", "isadoraair-encoders", "isadoraair-monitoring", "isadoraair-rbds",
                          "isadoraair-active-message-worker", "isadoraair-spoken-render-worker"):
            self.assertNotIn(untouched, touched)


class R0143ReachabilityTests(SimpleTestCase):
    def test_every_named_object_is_reachable_from_r_alone(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            bare = _isolated_repository(Path(directory), release)
            self.assertEqual(_git("for-each-ref", "--format=%(refname)", cwd=bare), "refs/heads/main")
            self.assertFalse((bare / "objects/info/alternates").exists())
            self.assertEqual(_git("rev-parse", "main", cwd=bare), release)
            self.assertEqual(_git("rev-parse", f"{ENGINEERING}^{{tree}}", cwd=bare), ENGINEERING_TREE)
            self.assertEqual(_git("rev-parse", f"{R0142_RELEASE}^{{tree}}", cwd=bare), R0142_TREE)
            for _label, (installed, *_rest) in PLANS.items():
                _git("merge-base", "--is-ancestor", installed, release, cwd=bare)
            for path in RELEASE_FILES | CHANGE_FILES:
                _git("cat-file", "-e", f"{release}:{path}", cwd=bare)
            self.assertIn("const VU_STALE_SECONDS = 1.0;", _git("show", f"{release}:{TEMPLATE}", cwd=bare))
            isolated = derive_plan(_repository(bare), release, R0142_RELEASE, "r0143", known_units=_known_units())
            shared = derive_plan(_repository(), release, R0142_RELEASE, "r0143", known_units=_known_units())
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


class R0143PlannerParityTests(TestCase):
    def _assert_parity(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(installed)
            application = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, installed, "r0143",
                                    known_units=_known_units())
            self.assertEqual(application.safety_status, planner.SafetyStatus.READY_TO_PLAN,
                             application.safety_detail)
            self.assertEqual((application.releases_in_plan, application.target_commit), (releases, release))
            if migrations:
                self.assertEqual(application.migrations.explicitly_required, migrations)
                self.assertEqual(application.migrations.compatibility, compatibility)
            else:
                self.assertIsNone(application.migrations)
            self.assertEqual(application.services_requiring_restart, restarts)
            self.assertEqual(application.fingerprint, protected.fingerprint)

    def test_direct_application_and_protected_planners_match_action_fingerprint(self):
        self._assert_parity("r0142")

    def test_catch_up_from_r0141_application_and_protected_planners_match(self):
        self._assert_parity("r0141")

    def test_catch_up_from_r0140_application_and_protected_planners_match(self):
        self._assert_parity("r0140")

    def test_catch_up_from_r0130_application_and_protected_planners_match(self):
        self._assert_parity("r0130")
