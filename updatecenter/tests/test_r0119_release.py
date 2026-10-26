"""r0119: Remote DJ control consolidation and engine-authoritative PTT
generation safety: stale ON stays closed and current-session OFF survives recovery.

The manifest says exactly what the repository does: no migration (so no
manual operation and no migration-authorization companion), no static, unit,
package, nginx, dependency or protected-runtime change, and the protected
Gunicorn -> engine restart pair for the changed views/template and engine.
Every assertion is bounded by the unique commit that introduces the r0119
manifest (or by fixed historical commits), never by HEAD or the working
tree, so the test stays true after publication and after later releases
(including r0120's own migrations and chain growth).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401  (puts the protected runtime on sys.path)
from isadoraair_updater.migration_authorization import validate_release_companion
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import (
    CORE_SERVICES, TrustedRepository, derive_plan, load_chain, manual_blockers, resolve_known_managed_units,
)
from protected_bootstrap.policy import parse_policy_dict

from updatecenter import manifest as manifest_mod
from updatecenter import migration_authorization, planner

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "deploy" / "releases" / "r0119.json"
COMPANION = PROJECT_ROOT / "deploy" / "migration_authorizations" / "r0119.json"
R0118_TIP = "40c9c59c7d9a377feec1752c8a062d5d4698c676"       # published main: r0118 + its companion
R0118_RELEASE = "3cbe9ce4bc1e134040a3d9571b3ac6523cbb535f"   # the installed r0118 release commit
R0117_RELEASE = "ad52af0e97b0f7242dc4f91f0bca886cf69780be"
ACCEPTED = "d6abb86702ca9b6b30370547ab5c05dd4d5b4dd7"        # accepted r0119 engineering tip
ENGINEERING = [
    "3b4266da2e06866ffa6a4c6ba83d135de1a4821c",
    "073a3ac0f846dd2b630ba6174830ee988c5e6771",
    "d7664c1c2b092a27e238884850c7823d534074ad",
    "0a0271e6cb56fe128d218fc3aac60cd93e5c5965",
    "8a4ea2f4ac385adcf0fe9468bddbac02bc05eefd",
    ACCEPTED,
]
RESTARTS = ["isadoraair-gunicorn", "isadoraair-engine"]
RELEASE_COMMIT_FILES = {
    "deploy/releases/r0119.json", "docs/releases/r0119.md", "docs/releases/r0119_installation.md",
    "updatecenter/tests/test_r0119_release.py",
}
ENGINEERING_SURFACE = {
    "library/services/engine.py", "library/services/remote_dj_gate_identity.py", "library/views.py",
    "library/templates/library/dashboard.html",
}
PROTECTED_PREFIXES = ("deploy/updater_runtime/", "deploy/updater_bootstrap/", "deploy/updater_attestations/")


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True,
                          capture_output=True).stdout


def _release_commit() -> str:
    """The unique immutable commit that introduced r0119.json."""
    commits = _git("log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--",
                   "deploy/releases/r0119.json").split()
    if len(commits) != 1:
        raise AssertionError(f"r0119.json must have exactly one introducing commit, found {commits!r}")
    return commits[0]


def _changed(base: str, commit: str, *filters: str) -> set[str]:
    return set(_git("diff", "--name-only", "--no-renames", *filters, base, commit).split())


def _repository() -> TrustedRepository:
    return TrustedRepository(Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir")),
                             "unused", "main", CommandRunner())


class R0119ManifestTests(SimpleTestCase):
    def setUp(self):
        self.raw = _show(_release_commit(), "deploy/releases/r0119.json")
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(self.data, source_label="r0119.json")

    def test_manifest_declares_a_migration_free_gunicorn_and_engine_update(self):
        m = self.manifest
        self.assertEqual((m.release_id, m.previous_release_id, m.minimum_updater_protocol_version),
                         ("r0119", "r0118", 5))
        self.assertIsNone(m.protected_runtime)
        self.assertEqual(list(m.migrations_required), [])
        self.assertIsNone(m.migration_compatibility)
        self.assertFalse(m.collectstatic_required)
        self.assertEqual(list(m.services_requiring_restart), RESTARTS)
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

    def test_restarts_are_exactly_gunicorn_then_engine_in_protected_order(self):
        self.assertTrue(set(RESTARTS) <= CORE_SERVICES)
        self.assertEqual(RESTARTS, [name for name in planner.RESTART_ORDER if name in RESTARTS])
        changed = _changed(R0118_TIP, _release_commit())
        # Why each restart is needed -- and why nothing else is.
        self.assertIn("library/views.py", changed)
        self.assertIn("library/templates/library/dashboard.html", changed)
        self.assertIn("library/services/engine.py", changed)
        self.assertFalse([path for path in changed if path.startswith(
            ("monitoring/", "encoders/", "rbds/", "active_messages/", "spoken/", "produced_content/"))])


class R0119AncestryTests(SimpleTestCase):
    def test_release_commit_is_the_single_parent_child_of_the_accepted_tip(self):
        release = _release_commit()
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [ACCEPTED])
        self.assertEqual(_git("rev-list", "--reverse", f"{R0118_TIP}..{ACCEPTED}").split(), ENGINEERING)
        _git("merge-base", "--is-ancestor", R0118_TIP, release)
        _git("merge-base", "--is-ancestor", R0118_RELEASE, release)
        self.assertEqual(_git("rev-list", "--merges", f"{R0118_TIP}..{release}"), "")

    def test_release_commit_is_metadata_only(self):
        release = _release_commit()
        self.assertEqual(_changed(ACCEPTED, release), RELEASE_COMMIT_FILES)
        self.assertEqual(_changed(ACCEPTED, release, "--diff-filter=A"), RELEASE_COMMIT_FILES)

    def test_engineering_surface_is_exactly_the_reviewed_source(self):
        source = {path for path in _changed(R0118_TIP, ACCEPTED) if not path.startswith("library/tests/")}
        self.assertEqual(source, ENGINEERING_SURFACE)

    def test_historical_release_artifacts_are_untouched(self):
        release = _release_commit()
        history = _changed(R0118_TIP, release, "--diff-filter=MDR", "--", "deploy/releases",
                           "deploy/migration_authorizations", "docs/releases", "updatecenter/tests")
        self.assertEqual(history, set())


def _migration_blobs(commit: str) -> dict[str, str]:
    """Every repository migration module at ``commit`` -> its blob id."""
    out = {}
    for line in _git("ls-tree", "-r", commit).splitlines():
        meta, path = line.split("\t", 1)
        if "/migrations/" in path and path.endswith(".py") and not path.endswith("/__init__.py"):
            out[path] = meta.split()[2]
    return out


class R0119MigrationTests(SimpleTestCase):
    def test_no_migration_was_added_modified_or_removed_since_r0118(self):
        release = _release_commit()
        self.assertEqual(_changed(R0118_TIP, release, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0118_RELEASE, release, "--", "*/migrations/*"), set())

    def test_the_release_commit_carries_exactly_r0118s_migration_modules(self):
        at_release, at_r0118 = _migration_blobs(_release_commit()), _migration_blobs(R0118_RELEASE)
        self.assertGreater(len(at_r0118), 100)
        self.assertEqual(at_release, at_r0118)                  # same paths, byte-identical blobs

    def test_no_manual_operation_and_no_companion_requirement(self):
        release = _release_commit()
        manifest = json.loads(_show(release, "deploy/releases/r0119.json"))
        self.assertEqual(manifest["migrations_required"], [])  # nothing for any classifier to classify
        self.assertEqual(_git("log", "--full-history", "--format=%H", release, "--",
                              "deploy/migration_authorizations/r0119.json"), "")
        # Both validators treat "no companion" as a supported state (None), not an error.
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0119", release_commit=release, migrations_required=[]))
        repository = _repository()
        entry = {item.manifest.release_id: item for item in load_chain(repository, release)}["r0119"]
        self.assertIsNone(validate_release_companion(repository, release, entry))


class R0119SurfaceTests(SimpleTestCase):
    def test_no_package_unit_nginx_static_dependency_or_protected_runtime_change(self):
        changed = _changed(R0118_RELEASE, _release_commit())
        self.assertEqual({path for path in changed if path.startswith("deploy/")},
                         {"deploy/releases/r0119.json", "deploy/migration_authorizations/r0118.json"})
        self.assertFalse([path for path in changed if path.startswith(PROTECTED_PREFIXES)])
        self.assertFalse(changed & {"requirements.txt", "deploy/isadoraair.nginx"})
        self.assertFalse([path for path in changed if path.endswith((".service", ".timer"))])
        self.assertFalse([path for path in changed if "/static/" in path])
        # Inherited beyond the r0118 release commit: only its metadata-only companion.
        self.assertEqual(_changed(R0118_RELEASE, R0118_TIP), {"deploy/migration_authorizations/r0118.json"})

    def test_release_notes_close_the_recovery_off_limitation(self):
        release = _release_commit()
        notes = _show(release, "docs/releases/r0119.md").decode()
        self.assertIn("## Resolved PTT-OFF recovery race", notes)
        self.assertNotIn("## Known limitation (operator decision)", notes)


class R0119TrustedChainTests(SimpleTestCase):
    def setUp(self):
        self.repository = _repository()
        # The trusted tip at which r0119 is the newest release: its own
        # introducing commit. derive_plan only plans to the latest release
        # of the tip it is given, so HEAD would stop working at r0120.
        self.tip = _release_commit()
        self.chain = load_chain(self.repository, self.tip)
        policy = parse_policy_dict(json.loads(_show(R0118_RELEASE, "deploy/updater_runtime/protected-policy.json")),
                                   label="installed generation 15")
        self.known = resolve_known_managed_units(active_policy=policy)

    def test_chain_ends_r0117_r0118_r0119_singly_linked(self):
        tail = [(entry.manifest.release_id, entry.commit) for entry in self.chain[-3:]]
        self.assertEqual(tail, [("r0117", R0117_RELEASE), ("r0118", R0118_RELEASE), ("r0119", _release_commit())])
        ids = [entry.manifest.release_id for entry in self.chain]
        self.assertEqual(len(ids), len(set(ids)))
        entry = self.chain[-1]
        self.assertEqual(entry.manifest.previous_release_id, "r0118")
        self.assertIsNone(validate_release_companion(self.repository, self.tip, entry))
        # r0118's own companion stays valid on the r0119 candidate ancestry.
        r0118 = {item.manifest.release_id: item for item in self.chain}["r0118"]
        self.assertIsNotNone(validate_release_companion(self.repository, self.tip, r0118))

    def test_trusted_plan_from_r0118_is_exactly_gunicorn_then_engine(self):
        plan = derive_plan(self.repository, self.tip, R0118_RELEASE, "r0119", known_units=self.known)
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0118", "r0119", _release_commit()))
        self.assertEqual(plan.releases_in_plan, ("r0119",))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((), None))
        self.assertEqual(plan.services_requiring_restart, tuple(RESTARTS))
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed, plan.systemd_units_new_required,
                          plan.systemd_units_new_optional, plan.systemd_units_removed_or_renamed),
                         ((), (), (), (), ()))
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed, plan.runtime_components_changed,
                          plan.python_requirements_changed, plan.manual_bootstrap_required),
                         (False, False, False, False, False))
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=self.known), ())

    def test_trusted_plan_from_r0117_crosses_r0118_and_r0119(self):
        plan = derive_plan(self.repository, self.tip, R0117_RELEASE, "r0119", known_units=self.known)
        self.assertEqual(plan.releases_in_plan, ("r0118", "r0119"))
        self.assertEqual(plan.migrations_required, ("authz.0010_remote_dj_admin_capabilities",))
        self.assertEqual(plan.services_requiring_restart,
                         ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring"))
        self.assertIsNone(plan.protected_runtime_transition)
        self.assertEqual(manual_blockers(plan, known_units=self.known), ())
