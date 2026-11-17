"""r0144 immutable release contract (engine restart safeguard, Library unique Artist count, Bulk Edit Related Artists).

Published r0143 (538ed7e) plus five linear engineering commits, then R:
  9e70035, cdd23d4 -- A: roadmap 1.20 (revised) engine restart safeguard (accepted candidate, kept verbatim);
  36f1376, fe781a9 -- B: Library unique Artist count (cherry-picked with -x from bdc8b9a, cb8dc33);
  8137200          -- C: Bulk Edit Related Artists (cherry-picked with -x from 38008ef);
  R                -- metadata-only child of 8137200 adding this contract, the r0144 manifest and two release documents.
The engineering tip's tree is the independently reviewed combined tree 6d71d23a.

r0144 declares no migration and restarts Gunicorn only: every changed production module and template is reachable only
from the Gunicorn service. Every object named by these tests is reachable from R alone (asserted against an isolated
repository that holds nothing but R's history).
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
R0143_RELEASE = "538ed7e1c4a9a178e5d8186dc40ff27510dc0ce5"
R0143_TREE = "8686889e83a78ebf4e5587b533366749f38276ff"
#: (commit, tree, accepted candidate it reproduces) in first-parent order on r0143.
ENGINEERING_CHAIN = (
    ("9e70035092ae1041d189ece0d0af6f3b995f8c43", "af03e934d247d19bc8033abb4c7833cab5d0876a", None),
    ("cdd23d452ab622a58b40558b819a9d82f2eb2efe", "6033870f6bb7a6537ec4c35b77ad041587250a86", None),
    ("36f1376b46541e9eb7586414f03316c260aa64c1", "dbe8333a848d441fef6d5bdaba2695b7c655c412",
     "bdc8b9aebce2c7d57d96c57677d1d4f37bb2e709"),
    ("fe781a9fe69463b839c6876ef8ec2a7c50709af7", "dd42f0974dedb609086651850b605daea3771ea5",
     "cb8dc33c05c9604a9d488f216077b6a36c28182a"),
    ("8137200dde516466e3bafca036887c3ba1e615db", "6d71d23a6e40b2ed123a55796bbd2e94594b613c",
     "38008ef5d280e21e654c0a23efea7299918dc5e3"),
)
#: Accepted candidate A tip (kept verbatim) and the independently reviewed combined tree.
CANDIDATE_A = "cdd23d452ab622a58b40558b819a9d82f2eb2efe"
CANDIDATE_A_TREE = "6033870f6bb7a6537ec4c35b77ad041587250a86"
REVIEWED_COMBINED_TREE = "6d71d23a6e40b2ed123a55796bbd2e94594b613c"
ENGINEERING = ENGINEERING_CHAIN[-1][0]
ENGINEERING_TREE = ENGINEERING_CHAIN[-1][1]
ADDED_FILES = {
    "docs/ENGINE_RESTART_SAFEGUARD.md",
    "hardware/tests/test_engine_restart_safety_1_20.py",
    "library/services/engine_restart_safety.py",
    "library/tests/engine_restart_state_support.py",
    "library/tests/render_library_artist_count_artifact.py",
    "library/tests/render_library_bulk_related_artifact.py",
    "library/tests/test_engine_restart_safety_1_20.py",
    "library/tests/test_engine_restart_safety_identity_1_20.py",
    "library/tests/test_library_bulk_related_artists.py",
    "library/tests/test_library_track_list_response_order.py",
    "library/tests/test_library_unique_artist_count.py",
    "monitoring/tests/test_engine_restart_safety_1_20.py",
    "updatecenter/tests/test_engine_restart_safety_1_20.py",
}
MODIFIED_FILES = {
    "README.md",
    "hardware/admin.py",
    "hardware/tests/test_config_audit_admin.py",
    "library/services/track_bulk_edit.py",
    "library/services/track_filters.py",
    "library/templates/library/library.html",
    "library/views.py",
    "monitoring/templates/monitoring/dashboard.html",
    "monitoring/tests/test_authz_restart_service.py",
    "monitoring/views.py",
    "updatecenter/templates/updatecenter/updates.html",
    "updatecenter/tests/test_phase_c_integration.py",
    "updatecenter/views.py",
}
CHANGE_FILES = ADDED_FILES | MODIFIED_FILES
#: Production (non-test) Python modules the engineering changes; each must be reachable only from Gunicorn.
PRODUCTION_MODULES = {
    "hardware.admin", "library.services.engine_restart_safety", "library.services.track_bulk_edit",
    "library.services.track_filters", "library.views", "monitoring.views", "updatecenter.views",
}
#: Changed template -> the only (Gunicorn) view module that renders it.
TEMPLATES = {
    "library/templates/library/library.html": ("library/library.html", "library/views.py"),
    "monitoring/templates/monitoring/dashboard.html": ("monitoring/dashboard.html", "monitoring/views.py"),
    "updatecenter/templates/updatecenter/updates.html": ("updatecenter/updates.html", "updatecenter/views.py"),
}
MANIFEST_PATH = "deploy/releases/r0144.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0144.md",
    "docs/releases/r0144_installation.md",
    "updatecenter/tests/test_r0144_release.py",
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
#: installed release -> (installed commit, releases in plan, migrations, compatibility, restarts)
PLANS = {
    "r0143": (R0143_RELEASE, ("r0144",), (), None, RESTARTS),
    "r0142": (R0142_RELEASE, ("r0143", "r0144"), (), None, RESTARTS),
    "r0141": (R0141_RELEASE, ("r0142", "r0143", "r0144"), (), None, RESTARTS),
    "r0140": (R0140_RELEASE, ("r0141", "r0142", "r0143", "r0144"), (), None, CATCH_UP_FROM_R0140_RESTARTS),
    "r0139": (R0139_RELEASE, tuple(f"r{n:04d}" for n in range(140, 145)), (R0140_MIGRATION,), "additive",
              CATCH_UP_FROM_R0140_RESTARTS),
    "r0138": (R0138_RELEASE, tuple(f"r{n:04d}" for n in range(139, 145)), (R0139_MIGRATION, R0140_MIGRATION),
              "additive", CATCH_UP_FROM_R0140_RESTARTS),
    "r0136": (R0136_RELEASE, tuple(f"r{n:04d}" for n in range(137, 145)),
              (R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive", CATCH_UP_FROM_R0136_RESTARTS),
    "r0130": (R0130_RELEASE, tuple(f"r{n:04d}" for n in range(131, 145)),
              (R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, R0140_MIGRATION), "additive",
              CATCH_UP_FROM_R0136_RESTARTS),
    "r0124": (R0124_RELEASE, tuple(f"r{n:04d}" for n in range(125, 145)),
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
        raise AssertionError(f"r0144 manifest introduced more than once: {commits!r}")
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
    return parse_policy_dict(json.loads(_show(R0143_RELEASE, POLICY_PATH)), label="r0143 protected policy")


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


def _isolated_repository(root: Path, release: str) -> Path:
    """A bare repository holding only R's history (no alternates, no other refs): what a fresh clone of R gets."""
    bare = root / "isolated.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    _git("fetch", "-q", "--no-tags", str(PROJECT_ROOT), f"{release}:refs/heads/main", cwd=bare)
    return bare


class R0144ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_gunicorn_only_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0144.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0144", "r0143", 5),
        )
        self.assertEqual((tuple(manifest.migrations_required), manifest.migration_compatibility), ((), None))
        self.assertEqual(tuple(manifest.services_requiring_restart), RESTARTS)
        self.assertEqual((manifest.collectstatic_required, manifest.protected_runtime), (False, None))
        r0143 = json.loads(_show(R0143_RELEASE, "deploy/releases/r0143.json"))
        self.assertEqual(data["minimum_supported_release_id"], "r0081")
        self.assertEqual(data["minimum_supported_release_id"], r0143["minimum_supported_release_id"])
        self.assertEqual(data["schema_version"], r0143["schema_version"])
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
        self.assertEqual(list(data), list(r0143))
        self.assertLessEqual(len(data["summary"]), manifest_mod.SUMMARY_MAX_LEN)
        summary = " ".join(data["summary"].split())
        for required in ("Engine restart safeguard", "refused while a microphone or Remote DJ is live",
                         "engine status is unknown", "superuser overrides with a reason (audited)",
                         "unique Artist count", "Bulk Edit Related Artists", "Restart Gunicorn only", "no migration",
                         "engine restart", "protected-runtime change"):
            self.assertIn(required, summary)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0144")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0144", "r0143"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((), None))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertFalse(parsed.collectstatic_required)
        self.assertIsNone(parsed.protected_runtime)

    def test_release_notes_document_changes_reviews_and_limitations(self):
        notes = " ".join(_artifact("docs/releases/r0144.md").decode().split())
        for required in (
            REVIEWED_COMBINED_TREE, "Codex ACCEPTED", "Codex ACCEPTED with one nonblocking finding",
            "**Unknown is never treated as safe.**", "**superuser**", "category `restart_safety`",
            "never claims the engine restarted", "`docs/ENGINE_RESTART_SAFEGUARD.md`",
            "a Gunicorn-only plan reads no engine status and is never gated",
            "`sample_rate` or `program_gain_db`", "`artist_total`", "not only the visible page",
            "a late answer for an earlier filter is discarded", "**Related Artists** row immediately below Artist",
            "**Leave unchanged**, **Set to**, **Append** or **Clear**", "`library.services.related_artists`",
            "**resulting** primary Artist", "without truncation", "Database metadata only",
            "previews made before the update are reported stale",
            "**The engine restart safeguard checks admission, not eventual restart execution.**",
            "particularly during a multi-step Update Center installation",
            "**A concurrent rename of a cached target Artist during Bulk Edit Related Artists may leave the renamed "
            "primary Artist represented in Related Artists.**",
            "does not compromise set-based scheduling identity calculations",
            "durable Manual-mode restoration scope of roadmap 1.20 remains **deferred**",
            "Roadmap 2.25 (evergreen Voice Track transitions) is **not** part of r0144",
            "21 Monitoring/Hardware test failures reproduce identically on unmodified r0143",
            "`migrations_required` is empty", "`migration_compatibility` is `null`", "`protected_runtime` is `null`",
            "1. `isadoraair-gunicorn`", "**the audio engine and encoders remain running**",
            "Publication does not authorize installation",
        ):
            self.assertIn(required, notes)
        for forbidden in ("fully passing", "2. `isadoraair-engine`", "Manual-mode restoration is included"):
            self.assertNotIn(forbidden, notes)

    def test_runbook_preserves_station_and_operational_boundaries(self):
        runbook = " ".join(_artifact("docs/releases/r0144_installation.md").decode().split())
        for required in (
            R0143_RELEASE, ENGINEERING, ENGINEERING_TREE,
            "Publication does not authorize deployment", "separate station-specific", "**restarts Gunicorn only**",
            "There is **no migration**", "Nothing else.", "no engine, encoder, Monitoring or RBDS restart",
            "stop and investigate", "a station on r0140 also restarts the engine for r0141",
            "`library.0103_remotedjconfig_auto_reconnect_enabled`", "restarts Monitoring for r0137",
            "Do not manually run `migrate`", "Do not assume the station is on r0143", "action fingerprint",
            "Do **not** initiate an engine restart, interrupt or start a Remote DJ session",
            "kept their recorded PIDs and activation times", "**Restart safeguard (read-only).**",
            "`engine_session_id` is present and equals `state_revision.instance`", "**Engine restart safeguard**",
            "**Do not press it**", "no `restart_safety` event", "`N tracks · M artists`",
            "**Bulk Edit Related Artists (preview only).**", "**Append**", "**Clear**",
            "do **not** Apply on production metadata", "## KOGR acceptance", "## WRJE acceptance",
            "Do not reuse KOGR's results",
            "**A successful installation at one station is not authorization to install at the other.**",
            "Record KOGR and WRJE evidence independently", "Stop and preserve", "Do not use manual Git resets",
            "not rollback authority",
        ):
            self.assertIn(required, runbook)
        self.assertNotIn("Restart `isadoraair-engine`", runbook)

    def test_documents_match_the_manifest(self):
        data = json.loads(_artifact(MANIFEST_PATH))
        notes = " ".join(_artifact("docs/releases/r0144.md").decode().split())
        runbook = " ".join(_artifact("docs/releases/r0144_installation.md").decode().split())
        self.assertIn("There is no migration: `migrations_required` is empty", notes)
        self.assertIn("No migration, apt package, Python requirement, systemd unit, static collection, nginx, "
                      "manual-bootstrap or protected-runtime operation is expected", runbook)
        order = " ".join(f"{n}. `{unit}`" for n, unit in enumerate(data["services_requiring_restart"], 1))
        self.assertIn(f"Restart exactly: {order}", notes)
        self.assertIn("1. Advance source to the exact canonical r0144 release commit R. 2. Restart "
                      "`isadoraair-gunicorn`. No migration", runbook)
        self.assertIn("A direct r0143 → r0144 plan advances source to R, then restarts `isadoraair-gunicorn`. "
                      "Nothing else.", runbook)
        self.assertEqual(data["services_requiring_restart"], list(RESTARTS))


class R0144HistoryAndScopeTests(SimpleTestCase):
    def test_engineering_is_the_exact_linear_chain_on_r0143(self):
        self.assertEqual(_git("rev-parse", f"{R0143_RELEASE}^{{tree}}"), R0143_TREE)
        commits = [commit for commit, _tree, _origin in ENGINEERING_CHAIN]
        self.assertEqual(_git("rev-list", "--reverse", f"{R0143_RELEASE}..{ENGINEERING}").split(), commits)
        parent = R0143_RELEASE
        for commit, tree, _origin in ENGINEERING_CHAIN:
            self.assertEqual(_git("rev-list", "--parents", "-n", "1", commit).split()[1:], [parent], commit)
            self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}"), tree, commit)
            parent = commit
        self.assertEqual(_git("rev-list", "--merges", f"{R0143_RELEASE}..{ENGINEERING}"), "")
        self.assertEqual(_introducing("deploy/releases/r0143.json"), [R0143_RELEASE])

    def test_integration_reproduces_the_accepted_candidates_and_the_reviewed_tree(self):
        self.assertEqual(_git("rev-parse", f"{CANDIDATE_A}^{{tree}}"), CANDIDATE_A_TREE)
        self.assertEqual(ENGINEERING_TREE, REVIEWED_COMBINED_TREE)
        self.assertEqual(_git("rev-parse", f"{ENGINEERING}^{{tree}}"), REVIEWED_COMBINED_TREE)
        for commit, _tree, origin in ENGINEERING_CHAIN:
            message = _git("log", "-1", "--format=%B", commit)
            if origin is None:
                self.assertNotIn("cherry picked from commit", message, commit)
            else:
                self.assertIn(f"(cherry picked from commit {origin})", message, commit)

    def test_change_inventory_is_exact(self):
        self.assertEqual(_changed(R0143_RELEASE, ENGINEERING), CHANGE_FILES)
        self.assertEqual(_changed(R0143_RELEASE, ENGINEERING, "--diff-filter=A"), ADDED_FILES)
        self.assertEqual(_changed(R0143_RELEASE, ENGINEERING, "--diff-filter=M"), MODIFIED_FILES)
        self.assertEqual(_changed(R0143_RELEASE, ENGINEERING, "--diff-filter=DRTC"), set())
        self.assertIn("26 files changed, 3255 insertions(+), 26 deletions(-)",
                      _git("diff", "--shortstat", "--no-renames", R0143_RELEASE, ENGINEERING))

    def test_release_is_one_metadata_only_child(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ENGINEERING])
        self.assertEqual(_changed(ENGINEERING, release), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ENGINEERING, release, "--diff-filter=MDRT"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0143_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0143_RELEASE}..{release}").split(),
                         [commit for commit, _tree, _origin in ENGINEERING_CHAIN] + [release])
        engineering_entries = _git("ls-tree", "-r", ENGINEERING).splitlines()
        release_entries = [line for line in _git("ls-tree", "-r", release).splitlines()
                           if line.split("\t", 1)[1] not in RELEASE_FILES]
        self.assertEqual(release_entries, engineering_entries)

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0143_RELEASE, release, "--", "requirements*.txt", "*/requirements.txt"), set())
        self.assertEqual(_changed(R0143_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0143_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0143_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertFalse([path for path in _changed(R0143_RELEASE, release) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0143_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{release}:{prefix}"), prefix)
        self.assertEqual(_git("rev-parse", f"{R0143_RELEASE}:{POLICY_PATH}"),
                         _git("rev-parse", f"{release}:{POLICY_PATH}"))
        touched = _changed(R0143_RELEASE, release)
        self.assertEqual({path for path in touched if path.startswith("deploy/")},
                         {MANIFEST_PATH} if _release_commit() else set())
        self.assertEqual(touched - RELEASE_FILES, CHANGE_FILES)

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0144.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0143_RELEASE,
        )
        self.assertEqual(list(findings), [])

    def test_production_change_is_exactly_these_modules_and_templates(self):
        release = _release_commit() or ENGINEERING
        changed = _changed(R0143_RELEASE, release)
        modules = {path[:-3].replace("/", ".") for path in changed
                   if path.endswith(".py") and "/tests/" not in path}
        self.assertEqual(modules, PRODUCTION_MODULES)
        production_other = {path for path in changed if not path.endswith(".py")
                            and not path.startswith("docs/") and path not in (MANIFEST_PATH, "README.md")}
        self.assertEqual(production_other, set(TEMPLATES))

    def test_declared_restart_is_exactly_gunicorn(self):
        """Every changed production module is loaded only by Gunicorn (the over-approximating import graph finds no
        other long-running service that can import it), and every changed template is rendered only by a Gunicorn
        view module, so no other service holds old code or compiled templates."""
        release = _release_commit() or ENGINEERING
        graph = _ImportGraph(release)
        reach = {service: graph.service_reach(service) for service in LONG_RUNNING_ENTRIES}
        for module in sorted(PRODUCTION_MODULES):
            with self.subTest(module=module):
                self.assertIsNotNone(graph.path(module))
                self.assertEqual({service for service, modules in reach.items() if module in modules}, set(RESTARTS))
        for template, (name, renderer) in TEMPLATES.items():
            with self.subTest(template=template):
                users = {path for path in graph.files
                         if "/tests/" not in path and name.encode() in _show(release, path)}
                self.assertEqual(users, {renderer})
                renderer_module = renderer[:-3].replace("/", ".")
                self.assertEqual({service for service, modules in reach.items() if renderer_module in modules},
                                 set(RESTARTS))


class R0144MigrationTests(TestCase):
    def test_no_migration_is_declared_changed_or_pending(self):
        release = _release_commit() or ENGINEERING
        self.assertEqual(_changed(R0143_RELEASE, release, "--", "*/migrations/*"), set())
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(loader.graph.leaf_nodes("library"), [LIBRARY_LEAF])
        executor = MigrationExecutor(connection)
        self.assertEqual(executor.migration_plan(executor.loader.graph.leaf_nodes()), [])

    def test_no_companion_exists_or_is_required(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0144.json").exists())
            return
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0144", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0144"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))


class R0144ChainTests(SimpleTestCase):
    def test_historical_manifests_are_byte_identical_and_r0144_is_unique(self):
        release = _require_release(self)
        names = _git("ls-tree", "--name-only", R0143_RELEASE, "deploy/releases/").split()
        self.assertEqual(len(names), 143)
        for name in names:
            self.assertEqual(_git("rev-parse", f"{release}:{name}"), _git("rev-parse", f"{R0143_RELEASE}:{name}"), name)
        self.assertEqual(_changed(R0143_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertEqual(_introducing(), [release])

    def test_complete_chain_r0001_through_r0144_is_contiguous_and_validates(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, 145)])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0142"], commits["r0143"], commits["r0144"]),
                         (R0142_RELEASE, R0143_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        self.assertNotIn("FAIL", out.getvalue() + err.getvalue())
        self.assertIn("r0143 -> r0144", out.getvalue())


class R0144TrustedPlanTests(SimpleTestCase):
    def _assert_plan(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        plan = derive_plan(_repository(), release, installed, "r0144", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         (label, "r0144", release))
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
                         [("r0143", R0143_RELEASE), ("r0144", release)])
        self._assert_plan("r0143")

    def test_catch_up_from_r0142_stays_gunicorn_only(self):
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


class R0144ReachabilityTests(SimpleTestCase):
    def test_every_named_object_is_reachable_from_r_alone(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            bare = _isolated_repository(Path(directory), release)
            self.assertEqual(_git("for-each-ref", "--format=%(refname)", cwd=bare), "refs/heads/main")
            self.assertFalse((bare / "objects/info/alternates").exists())
            self.assertEqual(_git("rev-parse", "main", cwd=bare), release)
            for commit, tree, _origin in ENGINEERING_CHAIN:
                self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}", cwd=bare), tree)
            self.assertEqual(_git("rev-parse", f"{R0143_RELEASE}^{{tree}}", cwd=bare), R0143_TREE)
            for _label, (installed, *_rest) in PLANS.items():
                _git("merge-base", "--is-ancestor", installed, release, cwd=bare)
            for path in RELEASE_FILES | CHANGE_FILES:
                _git("cat-file", "-e", f"{release}:{path}", cwd=bare)
            isolated = derive_plan(_repository(bare), release, R0143_RELEASE, "r0144", known_units=_known_units())
            shared = derive_plan(_repository(), release, R0143_RELEASE, "r0144", known_units=_known_units())
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


class R0144PlannerParityTests(TestCase):
    def _assert_parity(self, label):
        release = _require_release(self)
        installed, releases, migrations, compatibility, restarts = PLANS[label]
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(installed)
            application = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, installed, "r0144",
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
        self._assert_parity("r0143")

    def test_catch_up_from_r0142_application_and_protected_planners_match(self):
        self._assert_parity("r0142")

    def test_catch_up_from_r0140_application_and_protected_planners_match(self):
        self._assert_parity("r0140")

    def test_catch_up_from_r0130_application_and_protected_planners_match(self):
        self._assert_parity("r0130")
