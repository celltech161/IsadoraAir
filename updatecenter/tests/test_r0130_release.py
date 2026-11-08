"""r0130 release contract for category drift remediation (roadmap 3.11).

The Codex-accepted implementation is six linear commits on published r0129 (four feature commits and two
corrective commits), followed by one authorized documentation-only refinement of the last-link boundary. R is a
metadata-only child that introduces this test, the manifest and the two release documents. The application and
protected planners must independently derive the same migration-free, Gunicorn-only action plan.
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
R0129_RELEASE = "032ba615140c7f20e1b380a606487162ab03fb8f"
ACCEPTED = "73ff8a5189dfcdb6900fdd69a3e1be27a658a89e"
ACCEPTED_TREE = "f37c5d55ca6a0bafbc8919f0416846b007765e4c"
DOCS = "72a1756209297b721defab400488c78d596e57c1"
DOCS_TREE = "2814677245b1aa33b00adf248ce131d7a27f4b2f"
DEVELOPMENT = [
    "70e45750c9ca4d226379b685ae7e1b18d4e097bf",
    "f0ef2ef67e6e71ad9526d62f9e030493e5ccc045",
    "3fdaf3cf925d5dc77db1e658d404922466f8d288",
    "48213c5d56351326fcfa9b43211422cdc64bd743",
    "a1a3024d51ba0a73bce78cde9fb9fddecb7b203c",
    ACCEPTED,
    DOCS,
]
DEVELOPMENT_FILES = {
    "docs/LIBRARY_RECONCILIATION.md",
    "library/services/library_category_drift.py",
    "library/services/library_reconciliation.py",
    "library/services/media_relocation.py",
    "library/services/source_paths.py",
    "library/services/track_bulk_edit.py",
    "library/templates/library/library.html",
    "library/templates/library/reconciliation.html",
    "library/tests/test_library_bulk_relocation.py",
    "library/tests/test_library_category_drift.py",
    "library/tests/test_library_reconciliation_b.py",
    "library/tests/test_media_relocation.py",
    "library/tests/test_media_relocation_identity.py",
    "library/tests/test_relocation_concurrency.py",
    "library/tests/test_relocation_identity_probes.py",
    "library/urls.py",
    "library/views.py",
}
DOCS_FILES = {"docs/LIBRARY_RECONCILIATION.md", "library/services/media_relocation.py"}
MANIFEST_PATH = "deploy/releases/r0130.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0130.md",
    "docs/releases/r0130_installation.md",
    "updatecenter/tests/test_r0130_release.py",
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
        raise AssertionError(f"r0130 manifest introduced more than once: {commits!r}")
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


def _policy(commit: str = R0129_RELEASE):
    return parse_policy_dict(json.loads(_show(commit, POLICY_PATH)), label=f"protected policy at {commit[:12]}")


def _known_units() -> frozenset[str]:
    return resolve_known_managed_units(active_policy=_policy())


def _code_without_docstrings(source: bytes) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and body \
                and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:]
    return ast.dump(tree)


class R0130ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_ordinary_application_release(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0130.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id, manifest.minimum_updater_protocol_version),
            ("r0130", "r0129", 5),
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
        self.assertEqual(list(data), list(json.loads(_show(R0129_RELEASE, "deploy/releases/r0129.json"))))
        for forbidden in manifest_mod.FORBIDDEN_FIELDS:
            self.assertNotIn(forbidden, data)

    def test_protected_parser_derives_the_same_declaration(self):
        parsed = parse_manifest(json.loads(_artifact(MANIFEST_PATH)), label="r0130")
        self.assertEqual((parsed.release_id, parsed.previous_release_id), ("r0130", "r0129"))
        self.assertEqual(parsed.services_requiring_restart, RESTARTS)
        self.assertEqual((parsed.migrations_required, parsed.protected_runtime), ((), None))

    def test_release_docs_state_options_protection_quarantine_and_production_boundaries(self):
        notes = _artifact("docs/releases/r0130.md").decode()
        install = _artifact("docs/releases/r0130_installation.md").decode()
        for required in (
            "Option A — Move file", "Option B — Change Category", "explicitly selects",
            "no automatic fallback\n  to database-only editing", "25 file moves", "Per-Track partial-success",
            "metadata-only API behaviour remains available", "`confirm_move: true`",
            "its waveform cache may\nbecome unverified", "Reanalyze Track", "does not reanalyze relocated Tracks",
            "`.relocation-quarantine/`", "`RETAINED.json`", "`committed_cleanup_required`", "`uncertain`",
            "Never automatically retry an uncertain relocation", "Do not manually empty the quarantine",
            "`renameat2(RENAME_NOREPLACE)`", "**fails closed**", "refuse to move a file across filesystems",
            "copy fallback", "No atomic guarantee against that is claimed", "fresh reconciliation scan",
            "Gunicorn", "`protected_runtime` is `null`",
        ):
            self.assertIn(required, notes)
        for required in (
            "Publishing r0130 does not authorize", "not authorize\nany step on KOGR or WRJE", "source rollback",
            "Reverting application code cannot\nundo already committed file moves or Category changes",
            "721\ncategory-drift findings", "Stop procedure", "Do not resolve all 721 findings",
            "A\n   drift finding alone does not prove", "renameat2", "RENAME_NOREPLACE",
        ):
            self.assertIn(required, notes + install)
        self.assertIn("does not complete all of roadmap 3.11", notes)


class R0130ScopeAndIdentityTests(SimpleTestCase):
    def test_accepted_history_is_exact_linear_and_scoped(self):
        self.assertEqual(_git("rev-list", "--reverse", f"{R0129_RELEASE}..{DOCS}").split(), DEVELOPMENT)
        self.assertEqual(_git("rev-list", "--merges", f"{R0129_RELEASE}..{DOCS}"), "")
        self.assertEqual(_git("rev-parse", f"{ACCEPTED}^{{tree}}"), ACCEPTED_TREE)
        self.assertEqual(_git("rev-parse", f"{DOCS}^{{tree}}"), DOCS_TREE)
        self.assertEqual(_changed(R0129_RELEASE, DOCS), DEVELOPMENT_FILES)

    def test_the_documentation_refinement_changes_no_code(self):
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", DOCS).split()[1:], [ACCEPTED])
        self.assertEqual(_changed(ACCEPTED, DOCS), DOCS_FILES)
        path = "library/services/media_relocation.py"
        self.assertEqual(_code_without_docstrings(_show(ACCEPTED, path)), _code_without_docstrings(_show(DOCS, path)))
        refined = _show(DOCS, "docs/LIBRARY_RECONCILIATION.md").decode()
        self.assertIn("No atomic guarantee against that external action is claimed", refined)
        self.assertNotIn("never when it is the recording's last remaining name", refined)

    def test_accepted_implementation_changes_no_excluded_surface(self):
        self.assertEqual(_changed(R0129_RELEASE, DOCS, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0129_RELEASE, DOCS, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0129_RELEASE, DOCS, "--", "requirements*.txt"), set())
        self.assertEqual(_changed(R0129_RELEASE, DOCS, "--", "*/static/*"), set())
        self.assertEqual(_changed(R0129_RELEASE, DOCS, "--", "deploy/releases", "deploy/migration_authorizations"), set())
        for path in ("library/services/engine.py", "library/services/waveform_cache.py",
                     "library/services/cue_markers.py", "library/services/library_duplicate_resolution.py",
                     "library/management/commands/find_category_drift.py"):
            self.assertEqual(_git("rev-parse", f"{R0129_RELEASE}:{path}"), _git("rev-parse", f"{DOCS}:{path}"), path)
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0129_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{DOCS}:{prefix}"), prefix)

    def test_accepted_relocation_contract_is_present(self):
        relocation = _show(DOCS, "library/services/media_relocation.py").decode()
        drift = _show(DOCS, "library/services/library_category_drift.py").decode()
        bulk = _show(DOCS, "library/services/track_bulk_edit.py").decode()
        views = _show(DOCS, "library/views.py").decode()
        page = _show(DOCS, "library/templates/library/reconciliation.html").decode()
        for required in (
            "RENAME_NOREPLACE = 1", 'QUARANTINE_DIRNAME = ".relocation-quarantine"', "def remove_entry",
            "keep_last_link", "def verify(self)", "def playout_conflicts", "UNCERTAIN = \"uncertain\"",
            "COMMITTED_CLEANUP_REQUIRED = \"committed_cleanup_required\"", "durable=True",
            "allow_copy=False", "RETAINED.json",
        ):
            self.assertIn(required, relocation)
        for required in ("MAX_SELECTIONS = 25", "def _session_binding", "def _claim", "MOVE_FILE, SET_CATEGORY"):
            self.assertIn(required, drift)
        for required in ("MOVE_LIMIT = 25", "confirm_move is not True", "halted = halted or result.attention",
                         'METADATA_ONLY, MOVE_FILES = "metadata_only", "move"'):
            self.assertIn(required, bulk)
        self.assertIn("media_relocation.execute(", views)
        self.assertNotIn("def _stage_media_relocation_no_replace", views)
        self.assertIn("function driftStops(item)", page)

    def test_release_is_one_metadata_only_child_of_the_documented_tip(self):
        release = _require_release(self)
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [DOCS])
        self.assertEqual(_changed(DOCS, release), RELEASE_FILES)
        self.assertEqual(_changed(DOCS, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(DOCS, release, "--diff-filter=MDR"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0129_RELEASE}..{release}"), "")

    def test_manifest_claims_match_the_release_diff(self):
        release = _require_release(self)
        manifest = manifest_mod.validate_manifest_dict(
            json.loads(_show(release, MANIFEST_PATH)), source_label="r0130.json",
        )
        findings = cross_check.cross_check_release(
            manifest, release, PROJECT_ROOT, planner.app_label_paths_for_checkout(PROJECT_ROOT), R0129_RELEASE,
        )
        self.assertEqual(list(findings), [])


class R0130ChainAndMigrationTests(SimpleTestCase):
    def test_no_migration_companion_or_historical_manifest_change(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0130.json").exists())
            return
        self.assertEqual(_changed(R0129_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0129_RELEASE, release, "--", "deploy/migration_authorizations"), set())
        self.assertEqual(_changed(R0129_RELEASE, release, "--", "deploy/releases"), {MANIFEST_PATH})
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0130", release_commit=release, migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0130"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))

    def test_chain_resolves_unique_r0129_to_r0130_identities(self):
        release = _require_release(self)
        manifests = release_chain.load_manifest_files_at_ref(PROJECT_ROOT, release, "deploy/releases")
        chain = release_chain.build_chain(manifests)
        self.assertEqual([c.manifest.release_id for c in chain[-3:]], ["r0128", "r0129", "r0130"])
        commits = release_chain.resolve_unique_release_commits(chain, PROJECT_ROOT, release, "deploy/releases")
        self.assertEqual((commits["r0129"], commits["r0130"]), (R0129_RELEASE, release))
        self.assertEqual(len(set(commits.values())), len(commits))


class R0130ProtectedPlanTests(SimpleTestCase):
    def test_exact_plan_is_migration_free_gunicorn_only_and_unblocked(self):
        release = _require_release(self)
        plan = derive_plan(_repository(), release, R0129_RELEASE, "r0130", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0129", "r0130", release))
        self.assertEqual(plan.releases_in_plan, ("r0130",))
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
        for untouched in (
            "isadoraair-engine", "isadoraair-encoders", "isadoraair-rbds", "isadoraair-monitoring",
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


class R0130PlannerParityTests(TestCase):
    def test_application_and_protected_planners_match_action_fingerprint(self):
        release = _require_release(self)
        with tempfile.TemporaryDirectory() as directory:
            clone = _Clone(Path(directory), release)
            clone.at(R0129_RELEASE)
            built = planner.build_plan(clone.work, "deploy/releases")
            protected = derive_plan(_repository(clone.origin), release, R0129_RELEASE, "r0130",
                                    known_units=_known_units())
            self.assertEqual(built.safety_status, planner.SafetyStatus.READY_TO_PLAN, built.safety_detail)
            self.assertEqual((built.releases_in_plan, built.target_commit), (("r0130",), release))
            self.assertEqual(built.services_requiring_restart, RESTARTS)
            self.assertIsNone(built.migrations)
            self.assertEqual(built.fingerprint, protected.fingerprint)


class R0130ManifestValidatorTests(SimpleTestCase):
    def test_complete_manifest_chain_validates(self):
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn("FAIL", combined)
        self.assertIn("r0129 -> r0130", out.getvalue())
