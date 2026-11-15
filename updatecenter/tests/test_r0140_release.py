"""r0140 immutable release contract (roadmap 2.17 Package B: Remote DJ automatic reconnect and navigation protection).

Published r0139 (52e764e) plus five accepted linear commits, then R:
  f83e21b -- engine session end reasons and slot state, reconnect-policy endpoint, RemoteDJConfig switch + 0103;
  774ad5f -- browser fresh-session reconnect, microphone cleanup, leave-page protection, render helper + harness;
  3321ad4 -- contract fixes (Codex-reviewed candidate);
  31a7c7f -- Codex F4: truthful switch help text in the model and migration 0103;
  0a75798 -- Codex F1-F3: setup failures, authoritative sequence deadline, Web Audio output tracks (accepted);
  R       -- metadata-only child of 0a75798 adding this contract, the r0140 manifest and the two release documents.

Every object named by these tests is reachable from R, so a fresh station clone never depends on a feature branch,
evidence directory or unpublished release-preparation ref.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import subprocess
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase, TransactionTestCase

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
from updatecenter.management.commands import updatecenter_probe as probe


PROJECT_ROOT = Path(__file__).resolve().parents[2]
R0124_RELEASE = "69d53bc4f40e96a47359dce817375aa415cd068f"
R0130_RELEASE = "58e67cb247bd75ed489213dfc7e0bd6800596d3a"
R0136_RELEASE = "7c1df8f8d30c396fd01cfb6ded7d2240ecfa8fa3"
R0137_RELEASE = "ac22f4fd8445b22342fbaa334c5cf3f697ea644c"
R0138_RELEASE = "167a806ef97dd224d6f1182d2492ba3d908a5da5"
R0139_RELEASE = "52e764ed0a8dab63a308214b0e85bc3918c8bf07"
R0139_TREE = "4405b722af511f46881bbe921f1d413698c2d5c9"
ENGINEERING = (
    ("f83e21bc61b8757e5364751d64126264cec05372", "c040481b5e8b909600fdf5956716ac24a464f7db"),
    ("774ad5f1b6547b375b663d476282bc342e16628a", "f417c6eee2d84f8160c89aefe5ef77e1c37b91dd"),
    ("3321ad412579ebc264a60b4d2e3acdbdecaa1a98", "9b23fc695c89009c7ce7072c6c1a14aca8c29bac"),
    ("31a7c7fe69a09db0b6c7f1b5a195c20cb09a212b", "4f123027281f6d0f1592e1173d97388ae9e3757d"),
    ("0a75798444b87295408f33ff68cd167be4d3441e", "8f99502c176e2639f2ce46b3305f8d925d983fde"),
)
ACCEPTED = ENGINEERING[-1][0]
ACCEPTED_TREE = ENGINEERING[-1][1]
ENGINEERING_ADDED = {
    "docs/REMOTE_DJ_AUTO_RECONNECT.md",
    "library/migrations/0103_remotedjconfig_auto_reconnect_enabled.py",
    "library/tests/browser_remote_dj_reconnect_check.py",
    "library/tests/render_remote_dj_console_artifact.py",
    "library/tests/test_remote_dj_auto_reconnect_2_17b.py",
}
ENGINEERING_MODIFIED = {
    "library/admin.py",
    "library/models.py",
    "library/services/engine.py",
    "library/services/remote_dj_connection.py",
    "library/services/remote_dj_signaling.py",
    "library/templates/library/dashboard.html",
    "library/tests/test_config_audit_admin.py",
    "library/tests/test_console_2_23_presentation.py",
    "library/tests/test_engine_remote_dj_connection_failures.py",
    "library/tests/test_remote_dj_admin_controls_2_17a.py",
    "library/tests/test_remote_dj_connection_observability.py",
    "library/tests/test_remote_dj_reconnect_recovery.py",
    "library/tests/test_remote_dj_signaling_lifecycle.py",
    "library/tests/test_voicetrack_production_media.py",
    "library/urls.py",
    "library/views.py",
}
ENGINEERING_FILES = ENGINEERING_ADDED | ENGINEERING_MODIFIED
#: Changed importable production modules (tests and the migration excluded).
PRODUCTION_MODULES = {
    "library.admin", "library.models", "library.services.engine", "library.services.remote_dj_connection",
    "library.services.remote_dj_signaling", "library.urls", "library.views",
}
#: Modules whose code (not merely a model's schema) changed and must be reloaded by a restart.
BEHAVIOR_MODULES = PRODUCTION_MODULES - {"library.models"}
#: Every non-test, non-migration module that names RemoteDJConfig or the new field.
CONFIG_READERS = {
    "library/admin.py", "library/models.py", "library/services/engine.py",
    "library/services/remote_dj_signaling.py", "library/views.py",
}
MANIFEST_PATH = "deploy/releases/r0140.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0140.md",
    "docs/releases/r0140_installation.md",
    "updatecenter/tests/test_r0140_release.py",
}
MIGRATION = "library.0103_remotedjconfig_auto_reconnect_enabled"
MIGRATION_PATH = "library/migrations/0103_remotedjconfig_auto_reconnect_enabled.py"
MIGRATION_SHA256 = "4c189e2421b6fe46a38eec46d8f1fd3d546a9506cf650f431bc4203330a84fa7"
R0139_MIGRATION = "library.0102_uitheme_log_display_appearance"
R0138_MIGRATION = "library.0101_uitheme_show_played_items"
R0131_MIGRATION = "library.0100_dst_fallback_replay_pass"
RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine")
CATCH_UP_FROM_R0136_RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring")
CATCH_UP_FROM_R0124_RESTARTS = (
    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring", "isadoraair-active-message-worker",
)
HELP_TEXT_SWITCH_SENTENCE = (
    "When disabled, prevents subsequent automatic reconnect attempts at their next policy decision. "
    "It does not necessarily cancel a connection attempt already negotiating."
)
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
        raise AssertionError(f"r0140 manifest introduced more than once: {commits!r}")
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
    return parse_policy_dict(json.loads(_show(R0139_RELEASE, POLICY_PATH)), label="r0139 protected policy")


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


def _exact_plan() -> list[dict]:
    return probe._serialize_exact_plan(MigrationLoader(None, ignore_no_migrations=True), [MIGRATION])


def _models_without_remote_dj_config(commit: str) -> str:
    tree = ast.parse(_show(commit, "library/models.py").decode("utf-8"))
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ClassDef) and node.name == "RemoteDJConfig")]
    return ast.dump(tree)


def _remote_dj_config_fields(commit: str) -> list[str]:
    tree = ast.parse(_show(commit, "library/models.py").decode("utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RemoteDJConfig")
    return [target.id for node in cls.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)]


class R0140ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_additive_gunicorn_then_engine_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0140.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0140", "r0139", 5),
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
        self.assertEqual(list(data), list(json.loads(_show(R0139_RELEASE, "deploy/releases/r0139.json"))))
        self.assertEqual(list(RESTARTS), [unit for unit in planner.RESTART_ORDER if unit in RESTARTS])
        self.assertLessEqual(len(data["summary"]), 500)
        summary = " ".join(data["summary"].split())
        for required in ("Off by default", "leave-page warning", "library.0103", "Gunicorn and the engine",
                         "interrupts program audio"):
            self.assertIn(required, summary)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0140")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0140", "r0139"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((MIGRATION,), "additive"))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertFalse(parsed.collectstatic_required)
        self.assertIsNone(parsed.protected_runtime)

    def test_release_notes_preserve_the_feature_contract_and_limits(self):
        notes = " ".join(_artifact("docs/releases/r0140.md").decode().split())
        for required in (
            "**Off by default**", "Remote DJ Configuration", "**Reconnect / recovery**",
            "`RemoteDJConfig.auto_reconnect_enabled`", "`next_remote_dj_reconnect_decision`",
            "prevents subsequent automatic reconnect attempts at their next policy decision",
            "does not necessarily cancel a connection attempt already negotiating", "same-session recovery", "`reconnect_grace_seconds`", "**definitively ended**",
            "completely **new session**", "It is not ICE restart", "/api/remote-dj/reconnect-policy/",
            "**Kick**", "**authorization revocation**", "**engine shutdown**", "**Emergency Mute latched**",
            "**quarantined**", "`cleaning`", "| Attempts per qualifying loss | 3 |", "150 s",
            "**Cancel reconnect**", "new token", "new WebSocket", "`RTCPeerConnection`", "new microphone stream",
            "**The Remote Mic is Off when every replacement session is established**",
            "**Stay on this page**", "**Leave and disconnect**", "`beforeunload`", "generic text",
            "Some mobile exits", "**Leaving still ends the connection.**", "Package C",
            "**Pre-existing cleanup defect corrected.**", "**stops the microphone source tracks**",
            "`MediaStreamDestination`", "**Failed WebRTC setup.**", "`Setup failed`",
            "**Attempt-bound cleanup and deadline enforcement.**",
            "Exact-session PTT", "**session-scoped Emergency Mute**", "r0132 bounded native teardown and quarantine",
            "Manual/Auto ownership and mix-minus", "Opus monitor settings", "program playout",
            "Same-session ICE restart, TURN and persistent cross-page WebRTC ownership are **not** included",
            "**Simulation-based browser testing.**", "no mobile browser", "**Exception-injection coverage.**",
            "Codex recommended this additional coverage as nonblocking", "474 of 474", "452 of 452",
            "274 passed, 178 failed", "is **not clean**", "451 failure/error identifiers", MIGRATION,
            "1. `isadoraair-gunicorn` 2. `isadoraair-engine`", "`protected_runtime` is `null`",
            "**The engine restart interrupts program audio and ends any live Remote DJ session.**",
            "Installing r0140 does not enable automatic reconnect", "Publication does not authorize installation",
        ):
            self.assertIn(required, notes)
        for forbidden in ("TURN support", "ICE restart is included", "persists across pages"):
            self.assertNotIn(forbidden, notes)

    def test_runbook_preserves_station_and_operational_boundaries(self):
        runbook = " ".join(_artifact("docs/releases/r0140_installation.md").decode().split())
        for required in (
            R0139_RELEASE, *(commit for commit, _tree in ENGINEERING), ACCEPTED_TREE, MIGRATION, MIGRATION_SHA256,
            "Publication does not authorize deployment", "separate station-specific",
            "**This release restarts the audio engine.**", "interrupts program audio",
            "terminates any live Remote DJ session", "Do not install during an active live broadcast",
            "separately approved operational plan", "trusted updater identity", "recovery payload",
            "action fingerprint", "`RemoteDJConfig`", "`reconnect_grace_seconds`", "Remote Mic (PTT) state",
            "Weather/AMBER", "Active Messages", "no Remote DJ quarantine", "restart-required",
            "## KOGR preflight and acceptance", "`dry_run_only` remains checked",
            "`HOUR_REFILL_INSTALL_ARMED` remains false", "must not enable Hour Refill, enable automatic Remote DJ "
            "reconnect", "## WRJE preflight and acceptance", "Do not assume it is on r0139",
            "complete trusted catch-up plan", "Success at KOGR is neither authorization nor acceptance evidence for "
            "WRJE", "`auto_reconnect_enabled=False`", "Gunicorn and then the engine restarted",
            "next top of hour", "is present and **Off**", "monitor return", "PTT only under an operator-approved "
            "on-air plan", "Remote Mic is Off when the session is established", "on both pages",
            "microphone-in-use indicator clears", "Record KOGR and WRJE evidence independently",
            "Do not perform them on a live, on-air station", "separately approved field acceptance",
            "Do not manually run `migrate`", "Stop and preserve", "Do not use manual Git resets",
            "not rollback authority",
        ):
            self.assertIn(required, runbook)


class R0140HistoryAndScopeTests(SimpleTestCase):
    def test_accepted_engineering_is_exact_linear_and_content_identical(self):
        self.assertEqual(_git("rev-parse", f"{R0139_RELEASE}^{{tree}}"), R0139_TREE)
        self.assertEqual(_git("rev-list", "--reverse", f"{R0139_RELEASE}..{ACCEPTED}").split(),
                         [commit for commit, _tree in ENGINEERING])
        self.assertEqual(_git("rev-list", "--merges", f"{R0139_RELEASE}..{ACCEPTED}"), "")
        parent = R0139_RELEASE
        for commit, tree in ENGINEERING:
            self.assertEqual(_git("rev-list", "--parents", "-n", "1", commit).split()[1:], [parent], commit)
            self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}"), tree, commit)
            parent = commit

    def test_accepted_engineering_inventory_is_exact(self):
        self.assertEqual(_changed(R0139_RELEASE, ACCEPTED), ENGINEERING_FILES)
        self.assertEqual(_changed(R0139_RELEASE, ACCEPTED, "--diff-filter=A"), ENGINEERING_ADDED)
        self.assertEqual(_changed(R0139_RELEASE, ACCEPTED, "--diff-filter=M"), ENGINEERING_MODIFIED)
        self.assertIn("21 files changed, 2678 insertions(+), 48 deletions(-)",
                      _git("diff", "--shortstat", "--no-renames", R0139_RELEASE, ACCEPTED))

    def test_release_is_one_metadata_only_child(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ACCEPTED])
        self.assertEqual(_changed(ACCEPTED, release), RELEASE_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=MDR"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0139_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0139_RELEASE}..{release}").split(),
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
        self.assertEqual(_changed(R0139_RELEASE, release, "--", "requirements*.txt", "*/requirements.txt"), set())
        self.assertEqual(_changed(R0139_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0139_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0139_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertEqual(_changed(R0139_RELEASE, release, "--", "*/migrations/*"), {MIGRATION_PATH})
        self.assertFalse([path for path in _changed(R0139_RELEASE, release) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0139_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{release}:{prefix}"), prefix)
        touched = _changed(R0139_RELEASE, release)
        for untouched in ("isadoraair_updater/", "protected_bootstrap/", "monitoring/", "weather/", "weather_ingest/",
                          "active_messages/", "webrequests/", "production/", "encoders/", "rbds/", "spoken/",
                          "authz/", "deploy/updater", "templates/"):
            self.assertFalse({path for path in touched if path.startswith(untouched)}, untouched)
        self.assertFalse({path for path in touched if "hour_refill" in path})
        self.assertLessEqual({path for path in touched if not path.startswith(("library/", "docs/"))},
                             {MANIFEST_PATH, "updatecenter/tests/test_r0140_release.py"})

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0140.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0139_RELEASE,
        )
        self.assertEqual(list(findings), [])

    def test_changed_modules_are_exactly_the_audited_set(self):
        release = _release_commit() or ACCEPTED
        changed_modules = {
            path[:-3].replace("/", ".") for path in _changed(R0139_RELEASE, release)
            if path.endswith(".py") and "/tests/" not in path and "/migrations/" not in path
        }
        self.assertEqual(changed_modules, PRODUCTION_MODULES)

    def test_model_change_is_only_the_new_remote_dj_config_field(self):
        release = _release_commit() or ACCEPTED
        self.assertEqual(_models_without_remote_dj_config(release), _models_without_remote_dj_config(R0139_RELEASE))
        before, after = _remote_dj_config_fields(R0139_RELEASE), _remote_dj_config_fields(release)
        self.assertEqual(after, before + ["auto_reconnect_enabled"])
        readers = {
            path for path in _git("grep", "-l", "-e", "RemoteDJConfig", "-e", "auto_reconnect_enabled", release,
                                  "--", "*.py").replace(f"{release}:", "").split()
            if "/tests/" not in path and "/migrations/" not in path
        }
        self.assertEqual(readers, CONFIG_READERS)

    def test_declared_restarts_are_exactly_the_long_running_services_that_execute_the_changed_code(self):
        """Gunicorn loads the changed views, URLs and admin (and, through them, the engine-facing services); the
        engine runs the changed engine, connection and signaling code. Every other long-running service reaches
        only ``library.models``, whose sole change is a new RemoteDJConfig field that none of them reads."""
        release = _release_commit() or ACCEPTED
        graph = _ImportGraph(release)
        for entry in LONG_RUNNING_ENTRIES.values():
            self.assertIsNotNone(graph.path(entry), entry)
        for module in PRODUCTION_MODULES:
            self.assertIsNotNone(graph.path(module), module)
        loading = {service for service in LONG_RUNNING_ENTRIES if graph.service_reach(service) & BEHAVIOR_MODULES}
        self.assertEqual(loading, set(RESTARTS))
        self.assertEqual(graph.service_reach("isadoraair-gunicorn") & PRODUCTION_MODULES, PRODUCTION_MODULES)
        self.assertEqual(graph.service_reach("isadoraair-engine") & PRODUCTION_MODULES,
                         {"library.models", "library.services.engine", "library.services.remote_dj_connection",
                          "library.services.remote_dj_signaling"})
        for service in set(LONG_RUNNING_ENTRIES) - set(RESTARTS):
            self.assertEqual(graph.service_reach(service) & PRODUCTION_MODULES, {"library.models"}, service)
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


class R0140MigrationTests(SimpleTestCase):
    def test_exact_hash_dependency_default_and_additive_operation(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.get_migration("library", "0103_remotedjconfig_auto_reconnect_enabled")
        self.assertEqual(migration.dependencies, [("library", "0102_uitheme_log_display_appearance")])
        self.assertEqual([(op.__class__.__name__, op.model_name, op.name) for op in migration.operations],
                         [("AddField", "remotedjconfig", "auto_reconnect_enabled")])
        field = migration.operations[0].field
        self.assertEqual((field.__class__.__name__, field.default, field.null),
                         ("BooleanField", False, False))
        self.assertEqual(field.verbose_name, "Enable automatic Remote DJ reconnect")
        self.assertIn(HELP_TEXT_SWITCH_SENTENCE, field.help_text)
        from library.models import RemoteDJConfig
        model_field = RemoteDJConfig._meta.get_field("auto_reconnect_enabled")
        self.assertEqual((model_field.default, model_field.null, model_field.verbose_name, model_field.help_text),
                         (field.default, field.null, field.verbose_name, field.help_text))
        self.assertEqual(loader.graph.leaf_nodes("library"), [("library", "0103_remotedjconfig_auto_reconnect_enabled")])
        plan = _exact_plan()
        self.assertEqual([item["ref"] for item in plan], [MIGRATION])
        self.assertEqual(plan[0]["migration_file_sha256"], MIGRATION_SHA256)
        self.assertEqual(plan[0]["dependencies"], [R0139_MIGRATION])
        self.assertEqual([(op["operation"], op["classification"]) for op in plan[0]["operations"]],
                         [("AddField", "additive")])
        self.assertEqual(probe.extract_manual_operations(plan), [])
        self.assertEqual(hashlib.sha256(_artifact(MIGRATION_PATH)).hexdigest(), MIGRATION_SHA256)
        self.assertEqual(hashlib.sha256((PROJECT_ROOT / MIGRATION_PATH).read_bytes()).hexdigest(), MIGRATION_SHA256)

    def test_no_companion_exists_or_is_required(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0140.json").exists())
            return
        self.assertEqual(_changed(R0139_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0140", release_commit=release,
            migrations_required=[MIGRATION],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0140"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))


class R0140MigrationApplicationTests(TransactionTestCase):
    databases = {"default"}
    BEFORE = [("library", "0102_uitheme_log_display_appearance")]
    AFTER = [("library", "0103_remotedjconfig_auto_reconnect_enabled")]

    def tearDown(self):
        call_command("migrate", verbosity=0)
        super().tearDown()

    def test_existing_remote_dj_config_rows_receive_the_off_switch_and_keep_their_settings(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.BEFORE)
        before_apps = executor.loader.project_state(self.BEFORE).apps
        HistoricalConfig = before_apps.get_model("library", "RemoteDJConfig")
        HistoricalConfig.objects.all().delete()
        existing = HistoricalConfig.objects.create(
            enabled=True, stun_server="stun:example.invalid:3478", ice_udp_min_port=41000, ice_udp_max_port=41020,
            reconnect_grace_seconds=17,
        )

        MigrationExecutor(connection).migrate(self.AFTER)
        after_apps = MigrationExecutor(connection).loader.project_state(self.AFTER).apps
        row = after_apps.get_model("library", "RemoteDJConfig").objects.get(pk=existing.pk)
        self.assertIs(row.auto_reconnect_enabled, False)
        self.assertEqual(
            (row.enabled, row.stun_server, row.ice_udp_min_port, row.ice_udp_max_port, row.reconnect_grace_seconds),
            (True, "stun:example.invalid:3478", 41000, 41020, 17),
        )


class R0140ChainTests(SimpleTestCase):
    def test_historical_manifests_are_byte_identical_and_r0140_is_unique(self):
        release = _require_release(self)
        names = _git("ls-tree", "--name-only", R0139_RELEASE, "deploy/releases/").split()
        self.assertEqual(len(names), 139)
        for name in names:
            self.assertEqual(_git("rev-parse", f"{release}:{name}"), _git("rev-parse", f"{R0139_RELEASE}:{name}"), name)
        self.assertEqual(_changed(R0139_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertEqual(_introducing("deploy/releases/r0139.json"), [R0139_RELEASE])
        self.assertEqual(_introducing(), [release])

    def test_complete_chain_r0001_through_r0140_is_contiguous_and_validates(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, 141)])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0139"], commits["r0140"]), (R0139_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        self.assertNotIn("FAIL", out.getvalue() + err.getvalue())
        self.assertIn("r0139 -> r0140", out.getvalue())


class R0140TrustedPlanTests(SimpleTestCase):
    def _assert_no_other_actions(self, plan):
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed, plan.runtime_components_changed,
                          plan.python_requirements_changed, plan.manual_bootstrap_required),
                         (False, False, False, False, False))
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed, plan.systemd_units_new_required,
                          plan.systemd_units_new_optional, plan.systemd_units_removed_or_renamed),
                         ((), (), (), (), ()))
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=_known_units()), ())

    def test_direct_plan_is_additive_gunicorn_then_engine_and_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0139", R0139_RELEASE), ("r0140", release)])
        plan = derive_plan(_repository(), release, R0139_RELEASE, "r0140", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0139", "r0140", release))
        self.assertEqual(plan.releases_in_plan, ("r0140",))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((MIGRATION,), "additive"))
        self.assertEqual(plan.services_requiring_restart, RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0138_keeps_the_r0139_migration(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0138_RELEASE, "r0140", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, ("r0139", "r0140"))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility),
                         ((R0139_MIGRATION, MIGRATION), "additive"))
        self.assertEqual(plan.services_requiring_restart, RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0137_keeps_the_r0138_and_r0139_migrations(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0137_RELEASE, "r0140", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, ("r0138", "r0139", "r0140"))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility),
                         ((R0138_MIGRATION, R0139_MIGRATION, MIGRATION), "additive"))
        self.assertEqual(plan.services_requiring_restart, RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0136_keeps_the_r0137_monitoring_restart(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0136_RELEASE, "r0140", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, ("r0137", "r0138", "r0139", "r0140"))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility),
                         ((R0138_MIGRATION, R0139_MIGRATION, MIGRATION), "additive"))
        self.assertEqual(plan.services_requiring_restart, CATCH_UP_FROM_R0136_RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0130_keeps_all_four_migrations(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0130_RELEASE, "r0140", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, tuple(f"r{n:04d}" for n in range(131, 141)))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility),
                         ((R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, MIGRATION), "additive"))
        self.assertEqual(plan.services_requiring_restart, CATCH_UP_FROM_R0136_RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0124_preserves_all_intervening_actions(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0124_RELEASE, "r0140", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, tuple(f"r{n:04d}" for n in range(125, 141)))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility),
                         ((R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, MIGRATION), "additive"))
        self.assertEqual(plan.services_requiring_restart, CATCH_UP_FROM_R0124_RESTARTS)
        self._assert_no_other_actions(plan)

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


class R0140PlannerParityTests(TestCase):
    def _assert_parity(self, installed: str, releases: tuple[str, ...], migrations: tuple[str, ...], restarts):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(installed)
            application = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, installed, "r0140",
                                    known_units=_known_units())
            self.assertEqual(application.safety_status, planner.SafetyStatus.READY_TO_PLAN,
                             application.safety_detail)
            self.assertEqual((application.releases_in_plan, application.target_commit), (releases, release))
            self.assertIsNotNone(application.migrations)
            self.assertEqual(application.migrations.explicitly_required, migrations)
            self.assertEqual(application.migrations.compatibility, "additive")
            self.assertEqual(application.services_requiring_restart, restarts)
            self.assertEqual(application.fingerprint, protected.fingerprint)

    def test_direct_application_and_protected_planners_match_action_fingerprint(self):
        self._assert_parity(R0139_RELEASE, ("r0140",), (MIGRATION,), RESTARTS)

    def test_catch_up_application_and_protected_planners_match_action_fingerprint(self):
        self._assert_parity(
            R0130_RELEASE,
            tuple(f"r{n:04d}" for n in range(131, 141)),
            (R0131_MIGRATION, R0138_MIGRATION, R0139_MIGRATION, MIGRATION),
            CATCH_UP_FROM_R0136_RESTARTS,
        )
