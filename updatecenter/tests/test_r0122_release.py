"""r0122: roadmap 1.21 test isolation (kernel sandbox, disposable PostgreSQL,
fail-closed write guard, fixture corrections, read-only waveform diagnostic).

The manifest says exactly what the repository does: test, tooling and docs
only. No migration (so no manual operation and no companion), no static,
unit, package, nginx, dependency or protected-runtime change, and no
service restart: the only runtime-loaded change is settings.py's appended
TEST_RUNNER and a branch taken only by ``manage.py test`` or the test
runner's own services. Every assertion is bounded by the unique commit that
introduces the r0122 manifest (or by fixed historical commits), never by
HEAD or the working tree, so the test stays true after later releases.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401  (puts the protected runtime on sys.path)
from isadoraair_updater.migration_authorization import validate_release_companion
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import TrustedRepository, derive_plan, load_chain, manual_blockers, \
    resolve_known_managed_units
from protected_bootstrap.policy import parse_policy_dict

from updatecenter import manifest as manifest_mod
from updatecenter import migration_authorization

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "deploy" / "releases" / "r0122.json"
R0121_TIP = "9a3aadce350281dce325b7b8adcb615c59ef6db3"       # published main: r0121 + its companion
R0121_RELEASE = "5eeefa6707b607692ac70a9752a536022ab26378"   # the installed r0121 release commit
R0120_RELEASE = "07fd80bf44406b9219dc91d144f8a336c50e1796"
ACCEPTED = "a45dc9934523518941a72855b2970dfdbaaef349"        # qualified test-isolation tip
ENGINEERING = [
    "78119754d6fbbd48687b9f52e76788e67cc2821a",
    "89fed0f1917bb994933dd69876adb139ee266d1b",
    "bb3c50b71eb72a8be8afe4f23934a4203c8c2440",
    "007f42eab01f6e4248742668c3b824b7114756a6",
    "774b07a287187f42457ce5dc1cd0c7df07ba2445",
    "00e103dbbe7a00c2858b3f5a21562956bc5df194",
    "4c1e5bd82f0b326d848bbb3c83be69cecd2bc2b6",
    "91b7fb27573631de6c6cb314d5c0602e4595acba",
    "54993cb780b098850c36069d760d037da925b9fe",
    "3f0783f6dcb07ce2bd13f8470bf68d6694f9c387",
    "0b92f9504a8f9f11acb1d0eab2d901d5d65381e6",
    "685b2295b8a5c5a2836ff82484288f633195ca0a",
    "10ae1bc5e5ab98dfd9e618d97d560f766f5900e2",
    "928cc3782ec1910e8d9abbd6f81f10b9e9b810b6",
    "3c92d200fa6b7d478e71fde35ade76e13560b9dc",
    ACCEPTED,
]
RELEASE_COMMIT_FILES = {
    "deploy/releases/r0122.json", "docs/releases/r0122.md", "docs/releases/r0122_installation.md",
    "updatecenter/tests/test_r0122_release.py",
}
#: Everything outside tests and docs that the engineering commits touch.
ENGINEERING_SURFACE = {
    "isadoraair/settings.py", "isadoraair/test_isolation.py", "isadoraair/test_runner.py",
    "isadoraair/test_safety.py", "library/management/commands/check_waveform_integrity.py",
    "production/management/commands/test_isolation_preflight.py",
}
PROTECTED_PREFIXES = ("deploy/updater_runtime/", "deploy/updater_bootstrap/", "deploy/updater_attestations/")
HISTORY_PATHS = ("deploy/releases", "deploy/migration_authorizations", "docs/releases")


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True,
                          capture_output=True).stdout


def _release_commit() -> str:
    """The unique immutable commit that introduced r0122.json."""
    commits = _git("log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--",
                   "deploy/releases/r0122.json").split()
    if len(commits) != 1:
        raise AssertionError(f"r0122.json must have exactly one introducing commit, found {commits!r}")
    return commits[0]


def _diff_args(base: str, commit: str, *filters: str) -> list[str]:
    """``git diff --name-only`` arguments; any ``--`` pathspec goes after both revisions."""
    options = list(filters)
    paths = options[options.index("--"):] if "--" in options else []
    return ["diff", "--name-only", "--no-renames", *options[:len(options) - len(paths)], base, commit, *paths]


def _changed(base: str, commit: str, *filters: str) -> set[str]:
    return set(_git(*_diff_args(base, commit, *filters)).split())


def _is_test_or_doc(path: str) -> bool:
    return path.startswith("docs/") or "/tests/" in path or path.endswith("/tests.py")


def _repository() -> TrustedRepository:
    return TrustedRepository(Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir")),
                             "unused", "main", CommandRunner())


def _migration_blobs(commit: str) -> dict[str, str]:
    out = {}
    for line in _git("ls-tree", "-r", commit).splitlines():
        meta, path = line.split("\t", 1)
        if "/migrations/" in path and path.endswith(".py") and not path.endswith("/__init__.py"):
            out[path] = meta.split()[2]
    return out


class R0122ManifestTests(SimpleTestCase):
    def setUp(self):
        self.raw = _show(_release_commit(), "deploy/releases/r0122.json")
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(self.data, source_label="r0122.json")

    def test_manifest_declares_a_restart_free_migration_free_update(self):
        m = self.manifest
        self.assertEqual((m.release_id, m.previous_release_id, m.minimum_updater_protocol_version),
                         ("r0122", "r0121", 5))
        self.assertIsNone(m.protected_runtime)
        self.assertEqual(list(m.migrations_required), [])
        self.assertIsNone(m.migration_compatibility)
        self.assertFalse(m.collectstatic_required)
        self.assertEqual(list(m.services_requiring_restart), [])
        for empty in ("apt_packages_new", "systemd_units_changed", "systemd_units_new_required",
                      "systemd_units_new_optional", "systemd_units_removed_or_renamed"):
            self.assertEqual(self.data[empty], [], empty)
        self.assertEqual((self.data["python_requirements_changed"], self.data["requirements_sha256"],
                          self.data["nginx_changed"], self.data["runtime_components_changed"],
                          self.data["manual_bootstrap_required"]), (False, None, False, False, False))
        self.assertEqual(self.data["minimum_supported_release_id"], "r0081")
        self.assertLessEqual(len(self.data["summary"]), 500)
        self.assertEqual(self.raw, (json.dumps(self.data, indent=2) + "\n").encode())
        self.assertEqual(MANIFEST.read_bytes(), self.raw)                       # never edited afterwards
        self.assertNotIn(_release_commit(), self.raw.decode())                  # no self-reference


class R0122AncestryTests(SimpleTestCase):
    def test_release_commit_is_the_single_parent_child_of_the_accepted_tip(self):
        release = _release_commit()
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ACCEPTED])
        self.assertEqual(_git("rev-list", "--reverse", f"{R0121_TIP}..{ACCEPTED}").split(), ENGINEERING)
        _git("merge-base", "--is-ancestor", R0121_TIP, release)
        _git("merge-base", "--is-ancestor", R0121_RELEASE, release)
        self.assertEqual(_git("rev-list", "--merges", f"{R0121_TIP}..{release}"), "")

    def test_release_commit_is_metadata_only(self):
        release = _release_commit()
        self.assertEqual(_changed(ACCEPTED, release), RELEASE_COMMIT_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=A"), RELEASE_COMMIT_FILES)

    def test_engineering_surface_outside_tests_and_docs_is_exactly_the_reviewed_set(self):
        source = {path for path in _changed(R0121_TIP, ACCEPTED) if not _is_test_or_doc(path)}
        self.assertEqual(source, ENGINEERING_SURFACE)
        added = _changed(R0121_TIP, ACCEPTED, "--diff-filter=A")
        # Only settings.py and test_safety.py existed before; the rest are new modules.
        self.assertEqual(ENGINEERING_SURFACE - added, {"isadoraair/settings.py", "isadoraair/test_safety.py"})

    def test_settings_is_only_appended_to(self):
        diff = _git("diff", "--unified=0", R0121_TIP, ACCEPTED, "--", "isadoraair/settings.py").splitlines()
        removed = [line for line in diff if line.startswith("-") and not line.startswith("---")]
        self.assertEqual(removed, [])
        added = "\n".join(line[1:] for line in diff if line.startswith("+") and not line.startswith("+++"))
        self.assertIn("TEST_RUNNER = 'isadoraair.test_runner.IsolatedDiscoverRunner'", added)
        self.assertIn("if _os.environ.get('ISADORAAIR_TEST_DATA_ROOT') or _sys.argv[1:2] == ['test']:", added)
        before = _show(R0121_TIP, "isadoraair/settings.py")
        self.assertTrue(_show(ACCEPTED, "isadoraair/settings.py").startswith(before))

    def test_no_station_unit_engages_test_isolation(self):
        release = _release_commit()
        for path in _git("ls-tree", "-r", "--name-only", release, "--", "deploy").split():
            if path.endswith((".service", ".timer")):
                text = _show(release, path).decode()
                for marker in ("ISADORAAIR_TEST_DATA_ROOT", "ISADORAAIR_TEST_SANDBOX", "test_isolation_preflight",
                               "check_waveform_integrity", "manage.py test"):
                    self.assertNotIn(marker, text, path)

    def test_historical_release_artifacts_are_untouched(self):
        release = _release_commit()
        self.assertEqual(_changed(R0121_TIP, release, "--diff-filter=MDR", "--", *HISTORY_PATHS), set())
        self.assertEqual({path for path in _changed(R0121_TIP, release, "--diff-filter=MDR")
                          if Path(path).name.startswith("test_r0") and path.endswith("_release.py")}, set())


class R0122MigrationTests(SimpleTestCase):
    def test_no_migration_was_added_modified_or_removed_since_r0121(self):
        release = _release_commit()
        self.assertEqual(_changed(R0121_TIP, release, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0121_RELEASE, release, "--", "*/migrations/*"), set())

    def test_the_release_commit_carries_exactly_r0121s_migration_modules(self):
        at_release, at_r0121 = _migration_blobs(_release_commit()), _migration_blobs(R0121_RELEASE)
        self.assertGreater(len(at_r0121), 100)
        self.assertEqual(at_release, at_r0121)

    def test_no_manual_operation_and_no_companion_requirement(self):
        release = _release_commit()
        self.assertEqual(json.loads(_show(release, "deploy/releases/r0122.json"))["migrations_required"], [])
        self.assertEqual(_git("log", "--full-history", "--format=%H", release, "--",
                              "deploy/migration_authorizations/r0122.json"), "")
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0122", release_commit=release, migrations_required=[]))
        repository = _repository()
        entry = {item.manifest.release_id: item for item in load_chain(repository, release)}["r0122"]
        self.assertIsNone(validate_release_companion(repository, release, entry))


class R0122SurfaceTests(SimpleTestCase):
    def test_no_package_unit_nginx_static_dependency_template_or_protected_runtime_change(self):
        changed = _changed(R0121_RELEASE, _release_commit())
        self.assertEqual({path for path in changed if path.startswith("deploy/")},
                         {"deploy/releases/r0122.json", "deploy/migration_authorizations/r0121.json"})
        self.assertFalse([path for path in changed if path.startswith(PROTECTED_PREFIXES)])
        self.assertFalse(changed & {"requirements.txt", "deploy/isadoraair.nginx"})
        self.assertFalse([path for path in changed if path.endswith((".service", ".timer"))])
        self.assertFalse([path for path in changed if "/static/" in path or "/templates/" in path])
        # Inherited beyond the r0121 release commit: only its metadata-only companion.
        self.assertEqual(_changed(R0121_RELEASE, R0121_TIP), {"deploy/migration_authorizations/r0121.json"})

    def test_pathspec_filtered_diffs_see_real_history(self):
        self.assertEqual(_diff_args("a", "b", "--diff-filter=MDR", "--", "x"),
                         ["diff", "--name-only", "--no-renames", "--diff-filter=MDR", "a", "b", "--", "x"])
        self.assertEqual(_changed(R0121_RELEASE, R0121_TIP, "--", *HISTORY_PATHS),
                         {"deploy/migration_authorizations/r0121.json"})


class R0122TrustedChainTests(SimpleTestCase):
    def setUp(self):
        self.repository = _repository()
        self.tip = _release_commit()          # derive_plan only plans to the latest release of the tip it is given
        self.chain = load_chain(self.repository, self.tip)
        policy = parse_policy_dict(json.loads(_show(R0121_RELEASE, "deploy/updater_runtime/protected-policy.json")),
                                   label="installed generation 15")
        self.known = resolve_known_managed_units(active_policy=policy)

    def test_chain_ends_r0120_r0121_r0122_singly_linked(self):
        tail = [(entry.manifest.release_id, entry.commit) for entry in self.chain[-3:]]
        self.assertEqual(tail, [("r0120", R0120_RELEASE), ("r0121", R0121_RELEASE), ("r0122", _release_commit())])
        ids = [entry.manifest.release_id for entry in self.chain]
        self.assertEqual(len(ids), len(set(ids)))
        entry = self.chain[-1]
        self.assertEqual(entry.manifest.previous_release_id, "r0121")
        self.assertIsNone(validate_release_companion(self.repository, self.tip, entry))
        r0121 = {item.manifest.release_id: item for item in self.chain}["r0121"]
        self.assertIsNotNone(validate_release_companion(self.repository, self.tip, r0121))

    def test_protected_runtime_is_byte_identical_to_r0121(self):
        release = _release_commit()
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0121_RELEASE}:{prefix.rstrip('/')}"),
                             _git("rev-parse", f"{release}:{prefix.rstrip('/')}"), prefix)

    def test_trusted_plan_from_r0121_is_checkout_only(self):
        plan = derive_plan(self.repository, self.tip, R0121_RELEASE, "r0122", known_units=self.known)
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0121", "r0122", _release_commit()))
        self.assertEqual(plan.releases_in_plan, ("r0122",))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((), None))
        self.assertEqual(plan.services_requiring_restart, ())
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed, plan.systemd_units_new_required,
                          plan.systemd_units_new_optional, plan.systemd_units_removed_or_renamed),
                         ((), (), (), (), ()))
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed, plan.runtime_components_changed,
                          plan.python_requirements_changed, plan.manual_bootstrap_required),
                         (False, False, False, False, False))
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=self.known), ())
