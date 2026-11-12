"""r0135 immutable release contract (playout continuity after the KOGR 2026-11-11 dead-air incident).

Published r0134 (fc58ba6) plus three accepted linear commits, then R:
  f5d8ad4 -- legacy live fill survives deleted-Track history; single-owner exhausted-log retry;
  622033f -- first review: bounded unplayable successor; the retry respects Manual;
  43b521b -- second review: pending engine insertions survive restart/reload; non-reporting poison probe;
  R       -- metadata-only child of 43b521b adding this contract, the r0135 manifest and the two release documents.
Tests refer solely to commits and objects reachable from R, so a fresh station clone never depends on an
unpublished feature or release-preparation ref.
"""
from __future__ import annotations

import ast
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
R0124_RELEASE = "69d53bc4f40e96a47359dce817375aa415cd068f"
R0130_RELEASE = "58e67cb247bd75ed489213dfc7e0bd6800596d3a"
R0131_RELEASE = "c89ae56a15667a458b1cebb897c068b774256727"
R0132_RELEASE = "653c5bfd09972858fc529d00a28deee7890607a3"
R0133_RELEASE = "55c69a33393f0c5b00c3d10ad60c5e3a0ce1ae83"
R0134_RELEASE = "fc58ba6c96e5a62914f94ff34ec0dda9d8d75a4b"
R0134_TREE = "4cbf604edbd503b7fbb8d6dcbdf46721e0cfb2f0"
ENGINEERING = (
    ("f5d8ad471bbae0a45c498e21b99a7221ebe1c48b", "a155568512ae7e5f3bb7509f151edc83e63edd4d"),
    ("622033f96d9f37c6cc9be27f1fc36e030eb33403", "e0d415b3bf566b0290b48ed33d2aaf6e4d772ac3"),
    ("43b521bc9dba16301a34850851f41d8e157954ca", "ce4909aede84c6a29ba16e7a3f2c8ee86fc27e67"),
)
ACCEPTED = ENGINEERING[-1][0]
ACCEPTED_TREE = ENGINEERING[-1][1]
ENGINEERING_ADDED = {"library/tests/test_r0135_playout_continuity.py"}
ENGINEERING_MODIFIED = {
    "library/services/engine.py",
    "library/services/log_builder.py",
    "library/tests/test_engine_deck_lifecycle.py",
}
ENGINEERING_FILES = ENGINEERING_ADDED | ENGINEERING_MODIFIED
PRODUCTION_MODULES = {"library.services.engine", "library.services.log_builder"}
MANIFEST_PATH = "deploy/releases/r0135.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0135.md",
    "docs/releases/r0135_installation.md",
    "updatecenter/tests/test_r0135_release.py",
}
RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine")
CATCH_UP_FROM_R0133_RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring")
CATCH_UP_FROM_R0124_RESTARTS = (
    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring", "isadoraair-active-message-worker",
)
R0131_MIGRATION = "library.0100_dst_fallback_replay_pass"
POLICY_PATH = "deploy/updater_runtime/protected-policy.json"
PROTECTED_PREFIXES = (
    "deploy/updater_runtime",
    "deploy/updater_bootstrap",
    "deploy/updater_attestations",
)
#: Every long-running Django service (systemd Type=simple or default) and the module that starts it.
LONG_RUNNING_ENTRIES = {
    "isadoraair-gunicorn": "isadoraair.wsgi",
    "isadoraair-engine": "library.management.commands.run_engine",
    "isadoraair-monitoring": "monitoring.management.commands.run_monitoring",
    "isadoraair-rbds": "rbds.management.commands.run_rbds",
    "isadoraair-encoders": "encoders.management.commands.run_encoders",
    "isadoraair-active-message-worker": "active_messages.management.commands.active_message_worker",
    "isadoraair-spoken-render-worker": "spoken.management.commands.spoken_render_worker",
    "isadoraair-render": "production.management.commands.production_render_service",
    "isadoraair-validation": "production.management.commands.production_validation_service",
}
PROJECT_APPS = (
    "isadoraair.tts", "authz", "hardware", "library", "encoders", "monitoring", "rbds", "road_conditions", "weather",
    "ogremote", "webrequests", "aircheck", "updatecenter", "production", "spoken", "produced_content",
    "active_messages",
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
        raise AssertionError(f"r0135 manifest introduced more than once: {commits!r}")
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
    return parse_policy_dict(json.loads(_show(R0134_RELEASE, POLICY_PATH)), label="r0134 protected policy")


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


class _ImportGraph:
    """Project-local import reachability at one commit, read with ``ast`` (nothing is imported or executed).

    Deliberately an over-approximation: every ``import`` anywhere in a module (including function-level, lazy
    imports) counts, and ``from a import b`` counts ``a.b`` when it is a module. A service that cannot reach a module
    even in this graph cannot load it."""

    def __init__(self, commit: str):
        self.files = {
            path for path in _git("ls-tree", "-r", "--name-only", commit).split() if path.endswith(".py")
        }
        self.commit = commit
        self._edges: dict[str, set[str]] = {}

    def path(self, module: str) -> str | None:
        base = module.replace(".", "/")
        for candidate in (f"{base}.py", f"{base}/__init__.py"):
            if candidate in self.files:
                return candidate
        return None

    def _imports(self, module: str) -> set[str]:
        if module not in self._edges:
            path = self.path(module)
            tree = ast.parse(_show(self.commit, path).decode("utf-8"))
            package = module if path.endswith("__init__.py") else module.rpartition(".")[0]
            found = set()
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        parts = package.split(".")
                        parts = parts[: len(parts) - node.level + 1]
                        base = ".".join(parts + ([node.module] if node.module else []))
                    else:
                        base = node.module
                    names = [base] + [f"{base}.{alias.name}" for alias in node.names]
                for name in names:
                    pieces = name.split(".")
                    found.update(".".join(pieces[:i]) for i in range(1, len(pieces) + 1))
            self._edges[module] = {name for name in found if self.path(name)}
        return self._edges[module]

    def reach(self, starts) -> set[str]:
        seen, todo = set(), [start for start in starts if self.path(start)]
        while todo:
            module = todo.pop()
            if module not in seen:
                seen.add(module)
                todo.extend(self._imports(module) - seen)
        return seen

    def service_reach(self, service: str) -> set[str]:
        # django.setup() imports every installed app's AppConfig and models; Gunicorn also loads every URLconf,
        # admin and view module.
        starts = {LONG_RUNNING_ENTRIES[service]}
        starts |= {f"{app}.{name}" for app in PROJECT_APPS for name in ("apps", "models", "signals")}
        if service == "isadoraair-gunicorn":
            starts |= {"isadoraair.urls"} | {f"{app}.{name}" for app in PROJECT_APPS for name in ("urls", "admin", "views")}
        return self.reach(starts)


class R0135ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_gunicorn_then_engine_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0135.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0135", "r0134", 5),
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
        self.assertEqual(list(data), list(json.loads(_show(R0134_RELEASE, "deploy/releases/r0134.json"))))
        self.assertEqual(list(RESTARTS), [unit for unit in planner.RESTART_ORDER if unit in RESTARTS])
        self.assertLessEqual(len(data["summary"]), 500)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0135")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0135", "r0134"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((), None))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertIsNone(parsed.protected_runtime)

    def test_release_notes_preserve_the_operator_facing_contract(self):
        notes = _artifact("docs/releases/r0135.md").decode()
        for required in (
            "2026-11-11", "`LogFillConfig`", "fixed PSA\n  fallback Category", "**One retry owner.**",
            "**No stale starts.**", "**Logs with nothing playable wait.**", "**Manual mode is respected.**",
            "never started again", "native Active Messages", "dedication intros", "only looks at the poison-guard list",
            "**Hour Refill (Roadmap 2.27)** is independent of this fix and unchanged",
            "**No end-to-end acceptance with real GStreamer playout has taken place.**",
            "**The full Library test suite is not green.**", "`DURATION_FIT_MARGIN`", "a 3-second gap was observed",
            "**Unavailable items can be processed repeatedly.**", "**Inserted items can come back late.**",
            "validated-media environment", "`protected_runtime` is `null`",
            "`isadoraair-gunicorn`, then `isadoraair-engine`",
            "**The playout engine is restarted by this release**", "ends any active Remote DJ session",
        ):
            self.assertIn(required, notes)
        self.assertNotIn("test suite is green", notes)

    def test_runbook_preserves_operational_boundaries(self):
        runbook = _artifact("docs/releases/r0135_installation.md").decode()
        for required in (
            R0134_RELEASE, *(commit for commit, _tree in ENGINEERING), ACCEPTED_TREE, "## KOGR", "## WRJE",
            "**Unlike r0134, r0135 restarts the playout engine.**", "Publishing r0135\ndoes not authorize",
            "installation record", "intervening release", R0131_MIGRATION, "Log fill config", "`remote_dj_connected: false`",
            "teardown fingerprint", "`dry_run_only` checked", "`HOUR_REFILL_INSTALL_ARMED` false",
            "**The engine restart is a process interruption of the Hour Refill runner, not a reset of the observation period.**",
            "Do not arm 2.27", "Never replace source directly", "## Post-install acceptance (read-only)",
            "Do not create dead air, delete production Tracks", "generate test alerts", "## Stop and escalation conditions",
            "no automatic source rollback is authorized", "A further\nengine restart is not harmless",
            "Success on KOGR is neither authorization nor evidence for WRJE",
        ):
            self.assertIn(required, runbook)


class R0135HistoryAndScopeTests(SimpleTestCase):
    def test_accepted_engineering_is_exact_linear_and_content_identical(self):
        self.assertEqual(_git("rev-parse", f"{R0134_RELEASE}^{{tree}}"), R0134_TREE)
        self.assertEqual(_git("rev-list", "--reverse", f"{R0134_RELEASE}..{ACCEPTED}").split(),
                         [commit for commit, _tree in ENGINEERING])
        self.assertEqual(_git("rev-list", "--merges", f"{R0134_RELEASE}..{ACCEPTED}"), "")
        parent = R0134_RELEASE
        for commit, tree in ENGINEERING:
            self.assertEqual(_git("rev-list", "--parents", "-n", "1", commit).split()[1:], [parent], commit)
            self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}"), tree, commit)
            parent = commit

    def test_accepted_engineering_inventory_is_exactly_4_files(self):
        self.assertEqual(_changed(R0134_RELEASE, ACCEPTED), ENGINEERING_FILES)
        self.assertEqual(_changed(R0134_RELEASE, ACCEPTED, "--diff-filter=A"), ENGINEERING_ADDED)
        self.assertEqual(_changed(R0134_RELEASE, ACCEPTED, "--diff-filter=M"), ENGINEERING_MODIFIED)
        stat = _git("diff", "--shortstat", "--no-renames", R0134_RELEASE, ACCEPTED)
        self.assertIn("4 files changed, 1352 insertions(+), 22 deletions(-)", stat)

    def test_release_is_one_metadata_only_child_of_the_accepted_engineering(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ACCEPTED])
        self.assertEqual(_changed(ACCEPTED, release), RELEASE_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=MDR"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0134_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0134_RELEASE}..{release}").split(),
                         [commit for commit, _tree in ENGINEERING] + [release])
        self.assertEqual(_changed(R0134_RELEASE, release), ENGINEERING_FILES | RELEASE_FILES)
        for path in ENGINEERING_FILES:
            self.assertEqual(_git("rev-parse", f"{release}:{path}"), _git("rev-parse", f"{ACCEPTED}:{path}"), path)

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        release = _release_commit() or ACCEPTED
        self.assertEqual(_changed(R0134_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0134_RELEASE, release, "--", "requirements*.txt", "*/requirements.txt"), set())
        self.assertEqual(_changed(R0134_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0134_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0134_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertFalse([path for path in _changed(R0134_RELEASE, release) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0134_RELEASE}:{prefix}"), _git("rev-parse", f"{release}:{prefix}"), prefix)
        # No updater, protected bootstrap, monitoring, Hour Refill, Web Requests or production code is touched.
        touched = _changed(R0134_RELEASE, release)
        for untouched in ("isadoraair_updater/", "monitoring/", "webrequests/", "production/", "active_messages/",
                          "deploy/updater", "library/migrations/"):
            self.assertFalse({path for path in touched if path.startswith(untouched)}, untouched)
        self.assertFalse({path for path in touched if "hour_refill" in path})
        self.assertLessEqual({path for path in touched if not path.startswith(("library/", "docs/", "deploy/releases/"))},
                             {"updatecenter/tests/test_r0135_release.py"})

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0135.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0134_RELEASE,
        )
        self.assertEqual(list(findings), [])

    def test_declared_restarts_are_exactly_the_long_running_services_that_load_the_changed_code(self):
        """The two changed production modules are loaded by the engine and Gunicorn and by no other long-running
        service, even under an import graph that over-approximates (lazy imports included)."""
        release = _release_commit() or ACCEPTED
        changed_modules = {
            path[:-3].replace("/", ".") for path in _changed(R0134_RELEASE, release)
            if path.endswith(".py") and "/tests/" not in path
        }
        self.assertEqual(changed_modules, PRODUCTION_MODULES)
        graph = _ImportGraph(release)
        for entry in LONG_RUNNING_ENTRIES.values():
            self.assertIsNotNone(graph.path(entry), entry)
        loading = {service for service in LONG_RUNNING_ENTRIES if graph.service_reach(service) & PRODUCTION_MODULES}
        self.assertEqual(loading, set(RESTARTS))
        self.assertEqual(graph.service_reach("isadoraair-engine") & PRODUCTION_MODULES, PRODUCTION_MODULES)
        # The long-running set is every Type=simple/default isadoraair unit that runs project code.
        units = _git("ls-tree", "--name-only", release, "deploy/").split()
        long_running = set()
        for unit in units:
            name = Path(unit).name
            if not (name.startswith("isadoraair-") and name.endswith(".service")):
                continue
            text = _show(release, unit).decode()
            if "Type=oneshot" in text or "manage.py" not in text and "gunicorn" not in text:
                continue
            long_running.add(name[: -len(".service")])
        self.assertEqual(long_running, set(LONG_RUNNING_ENTRIES))


class R0135ChainAndCompanionTests(SimpleTestCase):
    def test_no_migration_companion_or_historical_manifest_change(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0135.json").exists())
            return
        self.assertEqual(_changed(R0134_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0134_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertEqual(_changed(R0134_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0135", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0135"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))

    def test_every_historical_manifest_is_byte_identical(self):
        release = _require_release(self)
        names = _git("ls-tree", "--name-only", R0134_RELEASE, "deploy/releases/").split()
        self.assertEqual(len(names), 134)
        for name in names:
            self.assertEqual(_git("rev-parse", f"{release}:{name}"), _git("rev-parse", f"{R0134_RELEASE}:{name}"), name)

    def test_chain_resolves_unique_r0133_to_r0135_identities(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain[-3:]], ["r0133", "r0134", "r0135"])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0133"], commits["r0134"], commits["r0135"]), (R0133_RELEASE, R0134_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))

    def test_complete_chain_r0001_through_r0135_is_contiguous_and_validates(self):
        release = _require_release(self)
        chain = release_chain.build_chain(release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases"))
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, 136)])
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn("FAIL", combined)
        self.assertIn("r0134 -> r0135", out.getvalue())

    def test_r0134_release_identity_and_manifest_are_preserved(self):
        release = _require_release(self)
        self.assertEqual(_introducing("deploy/releases/r0134.json"), [R0134_RELEASE])
        self.assertEqual(_show(release, "deploy/releases/r0134.json"), _show(R0134_RELEASE, "deploy/releases/r0134.json"))
        _git("merge-base", "--is-ancestor", R0134_RELEASE, release)  # raises unless r0134 is an ancestor of R


class R0135TrustedPlanTests(SimpleTestCase):
    def _assert_no_other_actions(self, plan):
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed, plan.runtime_components_changed,
                          plan.python_requirements_changed, plan.manual_bootstrap_required),
                         (False, False, False, False, False))
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed, plan.systemd_units_new_required,
                          plan.systemd_units_new_optional, plan.systemd_units_removed_or_renamed),
                         ((), (), (), (), ()))
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=_known_units()), ())

    def test_exact_plan_is_migration_free_gunicorn_then_engine_and_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0134", R0134_RELEASE), ("r0135", release)])
        plan = derive_plan(_repository(), release, R0134_RELEASE, "r0135", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0134", "r0135", release))
        self.assertEqual(plan.releases_in_plan, ("r0135",))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((), None))
        self.assertEqual(plan.services_requiring_restart, RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0133_adds_r0134_monitoring_after_the_engine(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0133_RELEASE, "r0135", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, ("r0134", "r0135"))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((), None))
        self.assertEqual(plan.services_requiring_restart, CATCH_UP_FROM_R0133_RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0132_aggregates_in_order(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0132_RELEASE, "r0135", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, ("r0133", "r0134", "r0135"))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((), None))
        self.assertEqual(plan.services_requiring_restart, CATCH_UP_FROM_R0133_RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0130_keeps_the_r0131_migration(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0130_RELEASE, "r0135", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, ("r0131", "r0132", "r0133", "r0134", "r0135"))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((R0131_MIGRATION,), "additive"))
        self.assertEqual(plan.services_requiring_restart, CATCH_UP_FROM_R0133_RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0124_keeps_every_intervening_restart(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0124_RELEASE, "r0135", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, tuple(f"r{n:04d}" for n in range(125, 136)))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((R0131_MIGRATION,), "additive"))
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
            "isadoraair-monitoring", "isadoraair-encoders", "isadoraair-rbds",
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


class R0135PlannerParityTests(TestCase):
    def test_application_and_protected_planners_match_action_fingerprint(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(R0134_RELEASE)
            built = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, R0134_RELEASE, "r0135",
                                    known_units=_known_units())
            self.assertEqual(built.safety_status, planner.SafetyStatus.READY_TO_PLAN, built.safety_detail)
            self.assertEqual((built.releases_in_plan, built.target_commit), (("r0135",), release))
            self.assertEqual(built.services_requiring_restart, RESTARTS)
            self.assertIsNone(built.migrations)
            self.assertEqual(built.fingerprint, protected.fingerprint)

    def test_catch_up_from_r0132_application_and_protected_planners_match(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(R0132_RELEASE)
            built = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, R0132_RELEASE, "r0135",
                                    known_units=_known_units())
            self.assertEqual(built.safety_status, planner.SafetyStatus.READY_TO_PLAN, built.safety_detail)
            self.assertEqual(built.releases_in_plan, ("r0133", "r0134", "r0135"))
            self.assertEqual(built.services_requiring_restart, CATCH_UP_FROM_R0133_RESTARTS)
            self.assertEqual(built.fingerprint, protected.fingerprint)
