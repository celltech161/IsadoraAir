"""r0139 immutable release contract (Console UI and navigation refinements).

Published r0138 plus three accepted linear commits, then R:
  810714d -- seven UI refinements (UI Theme darkening / outline settings, Play and sort SVG icons, clock menu, headers);
  7fdfdd9 -- themed NEXT pill;
  17ce2ec -- color-contract (validate_css_color) and RGBAColorWidget round-trip correction;
  R       -- metadata-only child adding this contract, the manifest and two release documents.

Every object named by these tests is reachable from R. A fresh station clone does not need a feature branch,
temporary evidence directory or unpublished release-preparation ref.
"""
from __future__ import annotations

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
R0131_RELEASE = "c89ae56a15667a458b1cebb897c068b774256727"
R0136_RELEASE = "7c1df8f8d30c396fd01cfb6ded7d2240ecfa8fa3"
R0137_RELEASE = "ac22f4fd8445b22342fbaa334c5cf3f697ea644c"
R0138_RELEASE = "167a806ef97dd224d6f1182d2492ba3d908a5da5"
R0138_TREE = "c6e6f22ec17284bdd6c720de011827592891d35f"
ENGINEERING = (
    ("810714dad568ae387971b3f79518ccf7e62fad7f", "7d265d9da28f303ee6e8d62d7d2475c663865fe7"),
    ("7fdfdd9ab8e1749f59c270f8db616ffb9296971a", "f85a4d16ef5e283e0eff4f438c8210b832a09e01"),
    ("17ce2ecf91f097f50c407947681b8478404625c6", "b3cc88c09d69271021c6b434933933eaee23011a"),
)
ACCEPTED = ENGINEERING[-1][0]
ACCEPTED_TREE = ENGINEERING[-1][1]
ENGINEERING_ADDED = {
    "library/migrations/0102_uitheme_log_display_appearance.py",
    "library/tests/test_r0139_ui_refinements.py",
}
ENGINEERING_MODIFIED = {
    "library/admin.py",
    "library/models.py",
    "library/templates/library/dashboard.html",
    "library/tests/render_console_log_display_artifact.py",
    "library/tests/test_console_log_display_browser.py",
    "library/tests/test_voicetrack_production_media.py",
    "templates/base.html",
}
ENGINEERING_FILES = ENGINEERING_ADDED | ENGINEERING_MODIFIED
MANIFEST_PATH = "deploy/releases/r0139.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0139.md",
    "docs/releases/r0139_installation.md",
    "updatecenter/tests/test_r0139_release.py",
}
MIGRATION = "library.0102_uitheme_log_display_appearance"
MIGRATION_PATH = "library/migrations/0102_uitheme_log_display_appearance.py"
MIGRATION_SHA256 = "d4d65fc0787d57925ae4a24111af84e1bdeb9b3b2d10e914b7d4ae5aa2d0e265"
R0138_MIGRATION = "library.0101_uitheme_show_played_items"
R0131_MIGRATION = "library.0100_dst_fallback_replay_pass"
RESTARTS = ("isadoraair-gunicorn",)
CATCH_UP_FROM_R0137_RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine")
CATCH_UP_FROM_R0136_RESTARTS = ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring")
CATCH_UP_FROM_R0124_RESTARTS = (
    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring", "isadoraair-active-message-worker",
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
        raise AssertionError(f"r0139 manifest introduced more than once: {commits!r}")
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
    return parse_policy_dict(json.loads(_show(R0138_RELEASE, POLICY_PATH)), label="r0138 protected policy")


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


def _exact_plan() -> list[dict]:
    return probe._serialize_exact_plan(MigrationLoader(None, ignore_no_migrations=True), [MIGRATION])


class R0139ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_additive_gunicorn_only_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0139.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0139", "r0138", 5),
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
        self.assertEqual(list(data), list(json.loads(_show(R0138_RELEASE, "deploy/releases/r0138.json"))))
        self.assertEqual(list(RESTARTS), [unit for unit in planner.RESTART_ORDER if unit in RESTARTS])
        self.assertNotIn("isadoraair-engine", data["services_requiring_restart"])
        self.assertLessEqual(len(data["summary"]), 500)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0139")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0139", "r0138"))
        self.assertEqual((parsed.migrations_required, parsed.migration_compatibility), ((MIGRATION,), "additive"))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertIsNone(parsed.protected_runtime)

    def test_release_documents_preserve_feature_and_operational_boundaries(self):
        notes = " ".join(_artifact("docs/releases/r0139.md").decode().split())
        runbook = " ".join(_artifact("docs/releases/r0139_installation.md").decode().split())
        for required in (
            "`UITheme.played_item_darkening`", "`UITheme.current_item_outline_color`", "default 55%", "opaque white",
            "`deck_transport_disabled_opacity`", "reusable opacity slider", "library.models.validate_css_color",
            "exactly 3, 4, 6 or 8 digits", "`#FFFFFF80`", "channels restricted to 0–255", "alpha to 0–1",
            "does not rewrite an untouched color", "Category Kind and FX Cart", "Play indicator", "Drag-sort indicator",
            "NEXT button", "Header alignment", "keyboard-accessible button", "`aria-expanded`",
            "NEXT pill contrast", "`protected_runtime` is `null`", "collectstatic is not required",
            "`library/services/engine.py` is not changed", "The engine is **not** restarted", MIGRATION,
            "`isadoraair-gunicorn`", "Publication does not authorize installation",
            "not represented as an execution of that committed module", "inherited",
        ):
            self.assertIn(required, notes)
        for required in (
            R0138_RELEASE, *(commit for commit, _tree in ENGINEERING), ACCEPTED_TREE, MIGRATION, MIGRATION_SHA256,
            "## KOGR", "## WRJE", "separate station-specific", "Do not manually run `migrate`",
            "`dry_run_only` remains checked", "`HOUR_REFILL_INSTALL_ARMED` remains false",
            "Remote DJ", "Passive post-install acceptance", "`played_item_darkening=0.55`",
            "`current_item_outline_color=\"rgba(255, 255, 255, 1.00)\"`", "No engine restart",
            "previously stored colors", "Stop and preserve", "Do not use manual Git resets",
        ):
            self.assertIn(required, runbook)


class R0139HistoryAndScopeTests(SimpleTestCase):
    def test_accepted_engineering_is_exact_linear_and_content_identical(self):
        self.assertEqual(_git("rev-parse", f"{R0138_RELEASE}^{{tree}}"), R0138_TREE)
        self.assertEqual(_git("rev-list", "--reverse", f"{R0138_RELEASE}..{ACCEPTED}").split(),
                         [commit for commit, _tree in ENGINEERING])
        self.assertEqual(_git("rev-list", "--merges", f"{R0138_RELEASE}..{ACCEPTED}"), "")
        parent = R0138_RELEASE
        for commit, tree in ENGINEERING:
            self.assertEqual(_git("rev-list", "--parents", "-n", "1", commit).split()[1:], [parent], commit)
            self.assertEqual(_git("rev-parse", f"{commit}^{{tree}}"), tree, commit)
            parent = commit

    def test_accepted_engineering_inventory_is_exact(self):
        self.assertEqual(_changed(R0138_RELEASE, ACCEPTED), ENGINEERING_FILES)
        self.assertEqual(_changed(R0138_RELEASE, ACCEPTED, "--diff-filter=A"), ENGINEERING_ADDED)
        self.assertEqual(_changed(R0138_RELEASE, ACCEPTED, "--diff-filter=M"), ENGINEERING_MODIFIED)
        self.assertIn("9 files changed, 887 insertions(+), 39 deletions(-)",
                      _git("diff", "--shortstat", "--no-renames", R0138_RELEASE, ACCEPTED))

    def test_release_is_one_metadata_only_child(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ACCEPTED])
        self.assertEqual(_changed(ACCEPTED, release), RELEASE_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=MDR"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0138_RELEASE}..{release}"), "")
        self.assertEqual(_git("rev-list", "--reverse", f"{R0138_RELEASE}..{release}").split(),
                         [commit for commit, _tree in ENGINEERING] + [release])
        for path in ENGINEERING_FILES:
            self.assertEqual(_git("rev-parse", f"{release}:{path}"), _git("rev-parse", f"{ACCEPTED}:{path}"), path)

    def test_release_covenants_and_protected_runtime_are_unchanged(self):
        release = _release_commit() or ACCEPTED
        self.assertEqual(_changed(R0138_RELEASE, release, "--", "requirements*.txt", "*/requirements.txt"), set())
        self.assertEqual(_changed(R0138_RELEASE, release, "--", "deploy/isadoraair.nginx"), set())
        self.assertEqual(_changed(R0138_RELEASE, release, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0138_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertEqual(_changed(R0138_RELEASE, release, "--", "*/migrations/*"), {MIGRATION_PATH})
        self.assertFalse([path for path in _changed(R0138_RELEASE, release) if "/static/" in path])
        # No engine / playout change: the engine restart declared by r0138 is not repeated.
        self.assertEqual(_changed(R0138_RELEASE, release, "--", "library/services"), set())
        self.assertEqual(_changed(R0138_RELEASE, release, "--", "library/views.py", "library/urls.py"), set())
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0138_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{release}:{prefix}"), prefix)

    def test_manifest_claims_match_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0139.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0138_RELEASE,
        )
        self.assertEqual(list(findings), [])


class R0139MigrationTests(SimpleTestCase):
    def test_exact_hash_dependency_defaults_and_additive_operations(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.get_migration("library", "0102_uitheme_log_display_appearance")
        self.assertEqual(migration.dependencies, [("library", "0101_uitheme_show_played_items")])
        self.assertEqual([(op.__class__.__name__, op.model_name, op.name) for op in migration.operations],
                         [("AddField", "uitheme", "played_item_darkening"),
                          ("AddField", "uitheme", "current_item_outline_color")])
        fields = {op.name: op.field for op in migration.operations}
        self.assertEqual(fields["played_item_darkening"].default, 0.55)
        self.assertEqual(fields["current_item_outline_color"].default, "rgba(255, 255, 255, 1.00)")
        self.assertFalse(any(field.null for field in fields.values()))
        plan = _exact_plan()
        self.assertEqual([item["ref"] for item in plan], [MIGRATION])
        self.assertEqual(plan[0]["migration_file_sha256"], MIGRATION_SHA256)
        self.assertEqual(plan[0]["dependencies"], [R0138_MIGRATION])
        self.assertEqual([(op["operation"], op["classification"]) for op in plan[0]["operations"]],
                         [("AddField", "additive"), ("AddField", "additive")])
        self.assertEqual(probe.extract_manual_operations(plan), [])
        self.assertEqual(hashlib.sha256((PROJECT_ROOT / MIGRATION_PATH).read_bytes()).hexdigest(), MIGRATION_SHA256)

    def test_migration_and_model_share_the_importable_color_validator(self):
        from library.models import UITheme, validate_css_color
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.get_migration("library", "0102_uitheme_log_display_appearance")
        fields = {op.name: op.field for op in migration.operations}
        self.assertIn(validate_css_color, fields["current_item_outline_color"].validators)
        self.assertIn(validate_css_color, UITheme._meta.get_field("current_item_outline_color").validators)
        self.assertEqual(validate_css_color.__module__, "library.models")
        for accepted in ("#fff", "#fff8", "#FFFFFF", "#FFFFFF80", "rgba(255, 255, 255, 1.00)", "rgb(1, 2, 3)"):
            validate_css_color(accepted)
        for rejected in ("#12345", "#1234567", "rgb(999,999,999)", "rgba(0, 0, 0, 1.5)"):
            with self.assertRaises(Exception, msg=rejected):
                validate_css_color(rejected)

    def test_no_companion_exists_or_is_required(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0139.json").exists())
            return
        self.assertEqual(_changed(R0138_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0139", release_commit=release,
            migrations_required=[MIGRATION],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0139"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))


class R0139MigrationApplicationTests(TransactionTestCase):
    databases = {"default"}
    BEFORE = [("library", "0101_uitheme_show_played_items")]
    AFTER = [("library", "0102_uitheme_log_display_appearance")]

    def tearDown(self):
        call_command("migrate", verbosity=0)
        super().tearDown()

    def test_existing_ui_theme_rows_receive_the_r0138_appearance(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.BEFORE)
        before_apps = executor.loader.project_state(self.BEFORE).apps
        HistoricalUITheme = before_apps.get_model("library", "UITheme")
        HistoricalUITheme.objects.all().delete()
        existing = HistoricalUITheme.objects.create(deck_transport_disabled_opacity=0.333, accent="#4ade80")

        MigrationExecutor(connection).migrate(self.AFTER)
        after_apps = MigrationExecutor(connection).loader.project_state(self.AFTER).apps
        MigratedUITheme = after_apps.get_model("library", "UITheme")
        row = MigratedUITheme.objects.get(pk=existing.pk)
        self.assertEqual((row.played_item_darkening, row.current_item_outline_color),
                         (0.55, "rgba(255, 255, 255, 1.00)"))
        # Existing configuration is untouched.
        self.assertEqual((row.deck_transport_disabled_opacity, row.accent), (0.333, "#4ade80"))


class R0139ChainTests(SimpleTestCase):
    def test_historical_manifests_are_byte_identical_and_r0139_is_unique(self):
        release = _require_release(self)
        names = _git("ls-tree", "--name-only", R0138_RELEASE, "deploy/releases/").split()
        self.assertEqual(len(names), 138)
        for name in names:
            self.assertEqual(_git("rev-parse", f"{release}:{name}"), _git("rev-parse", f"{R0138_RELEASE}:{name}"), name)
        self.assertEqual(_changed(R0138_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertEqual(_introducing("deploy/releases/r0138.json"), [R0138_RELEASE])
        self.assertEqual(_introducing(), [release])

    def test_complete_chain_r0001_through_r0139_is_contiguous_and_validates(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([item.manifest.release_id for item in chain], [f"r{n:04d}" for n in range(1, 140)])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0138"], commits["r0139"]), (R0138_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        self.assertNotIn("FAIL", out.getvalue() + err.getvalue())
        self.assertIn("r0138 -> r0139", out.getvalue())


class R0139TrustedPlanTests(SimpleTestCase):
    def _assert_no_other_actions(self, plan):
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed, plan.runtime_components_changed,
                          plan.python_requirements_changed, plan.manual_bootstrap_required),
                         (False, False, False, False, False))
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed, plan.systemd_units_new_required,
                          plan.systemd_units_new_optional, plan.systemd_units_removed_or_renamed),
                         ((), (), (), (), ()))
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=_known_units()), ())

    def test_direct_plan_is_additive_gunicorn_only_and_unblocked(self):
        release = _require_release(self)
        chain = load_chain(_repository(), release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0138", R0138_RELEASE), ("r0139", release)])
        plan = derive_plan(_repository(), release, R0138_RELEASE, "r0139", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0138", "r0139", release))
        self.assertEqual(plan.releases_in_plan, ("r0139",))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((MIGRATION,), "additive"))
        self.assertEqual(plan.services_requiring_restart, RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0137_keeps_the_r0138_engine_restart_and_migration(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0137_RELEASE, "r0139", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, ("r0138", "r0139"))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility),
                         ((R0138_MIGRATION, MIGRATION), "additive"))
        self.assertEqual(plan.services_requiring_restart, CATCH_UP_FROM_R0137_RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0136_keeps_r0137_monitoring_restart(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0136_RELEASE, "r0139", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, ("r0137", "r0138", "r0139"))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility),
                         ((R0138_MIGRATION, MIGRATION), "additive"))
        self.assertEqual(plan.services_requiring_restart, CATCH_UP_FROM_R0136_RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0130_keeps_all_three_migrations(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0130_RELEASE, "r0139", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, tuple(f"r{n:04d}" for n in range(131, 140)))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility),
                         ((R0131_MIGRATION, R0138_MIGRATION, MIGRATION), "additive"))
        self.assertEqual(plan.services_requiring_restart, CATCH_UP_FROM_R0136_RESTARTS)
        self._assert_no_other_actions(plan)

    def test_catch_up_from_r0124_preserves_all_intervening_actions(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0124_RELEASE, "r0139", known_units=_known_units())
        self.assertEqual(plan.releases_in_plan, tuple(f"r{n:04d}" for n in range(125, 140)))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility),
                         ((R0131_MIGRATION, R0138_MIGRATION, MIGRATION), "additive"))
        self.assertEqual(plan.services_requiring_restart, CATCH_UP_FROM_R0124_RESTARTS)
        self._assert_no_other_actions(plan)

    def test_restart_rehearsal_touches_only_gunicorn(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
        runner = FakeSystemRunner()
        manager = SystemdManager(config, runner, enforce_root_ownership=False, signed_policy=_policy())
        self.assertEqual(manager.restart_declared(RESTARTS), list(RESTARTS))
        restarted = [call[2] for call in runner.calls if call[1] == "restart"]
        self.assertEqual(restarted, ["isadoraair-gunicorn.service"])
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


class R0139PlannerParityTests(TestCase):
    def _assert_parity(self, installed: str, releases: tuple[str, ...], migrations: tuple[str, ...], restarts):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(installed)
            application = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, installed, "r0139",
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
        self._assert_parity(R0138_RELEASE, ("r0139",), (MIGRATION,), RESTARTS)

    def test_catch_up_application_and_protected_planners_match_action_fingerprint(self):
        self._assert_parity(
            R0130_RELEASE,
            tuple(f"r{n:04d}" for n in range(131, 140)),
            (R0131_MIGRATION, R0138_MIGRATION, MIGRATION),
            CATCH_UP_FROM_R0136_RESTARTS,
        )
