"""r0124 release contract: the reviewed reanalysis simplification replayed
linearly onto published r0123 C, with no migration companion and a
Gunicorn-only trusted plan.

Assertions resolve the unique commit introducing r0124.json, so later
releases cannot silently redefine this release's identity.
"""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401  (protected runtime on sys.path)
from isadoraair_updater.migration_authorization import validate_release_companion
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import (
    TrustedRepository,
    derive_plan,
    load_chain,
    manual_blockers,
    resolve_known_managed_units,
)
from protected_bootstrap.policy import parse_policy_dict

from updatecenter import manifest as manifest_mod
from updatecenter import migration_authorization, planner


PROJECT_ROOT = Path(__file__).resolve().parents[2]
R0123_RELEASE = "1a7c5b113a31f619ab20a7361be3eecf2f285559"
R0123_COMPANION = "e9e182d9a21b2d52d72ac2b0042cff51d2bb95bd"
ACCEPTED = "b3a24fec4dc0c3cee7e846f3a5b826c98fb51ac5"
ACCEPTED_TREE = "b52272cd3ef6cf87fb0bc40720a60b44493f2d0b"
INTEGRATION = "f2de333ffede03fc398f84584988bf5a4d3513d7"
INTEGRATION_TREE = "d736a9a1611f33dc3a42487f6ebf1cf1cf994c8e"
REPLAYED = [
    "2c131138f850fa058401fa25f4d980e00436fd3d",
    "6dc55a83589d9485747e17b4011f2af7f85fc337",
    INTEGRATION,
]
MANIFEST_PATH = "deploy/releases/r0124.json"
RELEASE_FILES = {
    MANIFEST_PATH,
    "docs/releases/r0124.md",
    "docs/releases/r0124_installation.md",
    "updatecenter/tests/test_r0124_release.py",
}
PROTECTED_PREFIXES = (
    "deploy/updater_runtime",
    "deploy/updater_bootstrap",
    "deploy/updater_attestations",
)
INHERITED_COMPANION = "deploy/migration_authorizations/r0123.json"
ANALYZER_BLOB = "0aa80d8bf25f2dfeb05c7a7be57ffb1881c1fb3d"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"],
        check=True, capture_output=True,
    ).stdout


def _introducing(path: str) -> list[str]:
    return _git(
        "log", "--full-history", "--no-renames", "--diff-filter=A",
        "--format=%H", "HEAD", "--", path,
    ).split()


def _release_commit() -> str | None:
    commits = _introducing(MANIFEST_PATH)
    if len(commits) > 1:
        raise AssertionError(f"r0124 manifest introduced more than once: {commits!r}")
    return commits[0] if commits else None


def _artifact(path: str) -> bytes:
    release = _release_commit()
    return _show(release, path) if release else (PROJECT_ROOT / path).read_bytes()


def _changed(base: str, commit: str, *filters: str) -> set[str]:
    options, paths = list(filters), []
    if "--" in options:
        cut = options.index("--")
        options, paths = options[:cut], options[cut:]
    return set(_git("diff", "--name-only", "--no-renames", *options, base, commit, *paths).split())


def _repository() -> TrustedRepository:
    return TrustedRepository(
        Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir")),
        "unused", "main", CommandRunner(),
    )


def _known_units() -> frozenset[str]:
    policy = parse_policy_dict(
        json.loads(_show(R0123_RELEASE, "deploy/updater_runtime/protected-policy.json")),
        label="r0123 protected policy",
    )
    return resolve_known_managed_units(active_policy=policy)


class R0124ManifestTests(SimpleTestCase):
    def test_manifest_is_exact_migration_free_and_gunicorn_only(self):
        raw = _artifact(MANIFEST_PATH)
        data = json.loads(raw)
        manifest = manifest_mod.validate_manifest_dict(data, source_label="r0124.json")
        self.assertEqual(
            (manifest.release_id, manifest.previous_release_id,
             manifest.minimum_updater_protocol_version),
            ("r0124", "r0123", 5),
        )
        self.assertEqual(list(manifest.migrations_required), [])
        self.assertIsNone(manifest.migration_compatibility)
        self.assertEqual(list(manifest.services_requiring_restart), ["isadoraair-gunicorn"])
        self.assertFalse(manifest.collectstatic_required)
        self.assertIsNone(manifest.protected_runtime)
        self.assertEqual(data["minimum_supported_release_id"], "r0081")
        for field in (
            "apt_packages_new", "systemd_units_changed", "systemd_units_new_required",
            "systemd_units_new_optional", "systemd_units_removed_or_renamed",
        ):
            self.assertEqual(data[field], [], field)
        self.assertEqual(
            (data["manual_bootstrap_required"], data["python_requirements_changed"],
             data["requirements_sha256"], data["nginx_changed"],
             data["runtime_components_changed"]),
            (False, False, None, False, False),
        )
        self.assertLessEqual(len(data["summary"]), 500)
        self.assertEqual(raw, (json.dumps(data, indent=2) + "\n").encode())

    def test_release_commit_is_the_metadata_only_child_of_the_integrated_tip(self):
        release = _release_commit()
        if release is None:
            self.skipTest("R has not been committed yet")
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", release).split()[1:], [INTEGRATION])
        self.assertEqual(_changed(INTEGRATION, release), RELEASE_FILES)
        self.assertEqual(_changed(INTEGRATION, release, "--diff-filter=A"), RELEASE_FILES)
        self.assertEqual(_changed(INTEGRATION, release, "--diff-filter=MDR"), set())
        self.assertEqual(_git("rev-list", "--merges", f"{R0123_COMPANION}..{release}"), "")


class R0124IntegrationTests(SimpleTestCase):
    def test_replay_is_linear_and_preserves_the_accepted_tree_plus_companion(self):
        self.assertEqual(_git("rev-list", "--reverse", f"{R0123_COMPANION}..{INTEGRATION}").split(),
                         REPLAYED)
        self.assertEqual(_git("rev-parse", f"{ACCEPTED}^{{tree}}"), ACCEPTED_TREE)
        self.assertEqual(_git("rev-parse", f"{INTEGRATION}^{{tree}}"), INTEGRATION_TREE)
        self.assertEqual(_changed(ACCEPTED, INTEGRATION), {INHERITED_COMPANION})
        self.assertEqual(_git("diff", "--name-only", ACCEPTED, INTEGRATION, "--", ".",
                              f":(exclude){INHERITED_COMPANION}"), "")

    def test_no_migration_or_release_covenant_surface_changed(self):
        self.assertEqual(_changed(R0123_RELEASE, INTEGRATION, "--", "*/migrations/*"), set())
        self.assertEqual(_changed(R0123_RELEASE, INTEGRATION, "--", "deploy/*.service", "deploy/*.timer"), set())
        self.assertEqual(_changed(R0123_RELEASE, INTEGRATION, "--", "requirements.txt"), set())
        self.assertEqual(_changed(R0123_RELEASE, INTEGRATION, "--", "deploy/isadoraair.nginx"), set())
        self.assertFalse([path for path in _changed(R0123_RELEASE, INTEGRATION) if "/static/" in path])
        for prefix in PROTECTED_PREFIXES:
            self.assertEqual(_git("rev-parse", f"{R0123_RELEASE}:{prefix}"),
                             _git("rev-parse", f"{INTEGRATION}:{prefix}"), prefix)
        self.assertEqual(_git("rev-parse", f"{INTEGRATION}:deploy/isadoraair-analyze.service"),
                         ANALYZER_BLOB)


class R0124MigrationAndHistoryTests(SimpleTestCase):
    def test_no_migration_operation_and_no_r0124_companion(self):
        release = _release_commit()
        if release is None:
            self.assertFalse((PROJECT_ROOT / "deploy/migration_authorizations/r0124.json").exists())
            return
        self.assertEqual(_changed(R0123_RELEASE, release, "--", "*/migrations/*"), set())
        self.assertEqual(_git("log", "--full-history", "--format=%H", release, "--",
                              "deploy/migration_authorizations/r0124.json"), "")
        self.assertIsNone(migration_authorization.validate_release_companion(
            PROJECT_ROOT, release, release_id="r0124", release_commit=release,
            migrations_required=[],
        ))
        entry = {item.manifest.release_id: item for item in load_chain(_repository(), release)}["r0124"]
        self.assertIsNone(validate_release_companion(_repository(), release, entry))

    def test_published_r0123_release_and_companion_remain_exact(self):
        self.assertEqual(_git("rev-list", "--parents", "-n", "1", R0123_COMPANION).split()[1:],
                         [R0123_RELEASE])
        self.assertEqual(_changed(R0123_RELEASE, R0123_COMPANION), {INHERITED_COMPANION})
        self.assertEqual(_git("rev-parse", f"{R0123_RELEASE}:deploy/releases/r0123.json"),
                         "97fb43942e6df6dcc501d839c8836ce02d593a1a")
        self.assertEqual(_git("rev-parse", f"{R0123_COMPANION}:{INHERITED_COMPANION}"),
                         "52d7be8ebb3bb83ca1b992b5297ad6998cd1b1cd")


class R0124TrustedPlanTests(SimpleTestCase):
    def test_chain_and_exact_plan_from_r0123(self):
        release = _release_commit()
        if release is None:
            self.skipTest("R has not been committed yet")
        repository = _repository()
        chain = load_chain(repository, release)
        self.assertEqual([(item.manifest.release_id, item.commit) for item in chain[-2:]],
                         [("r0123", R0123_RELEASE), ("r0124", release)])
        r0123 = {item.manifest.release_id: item for item in chain}["r0123"]
        self.assertIsNotNone(validate_release_companion(repository, release, r0123))

        plan = derive_plan(repository, release, R0123_RELEASE, "r0124", known_units=_known_units())
        self.assertEqual((plan.installed_release_id, plan.target_release_id, plan.target_commit),
                         ("r0123", "r0124", release))
        self.assertEqual(plan.releases_in_plan, ("r0124",))
        self.assertEqual((plan.migrations_required, plan.migration_compatibility), ((), None))
        self.assertEqual(plan.services_requiring_restart, ("isadoraair-gunicorn",))
        self.assertEqual((plan.apt_packages_new, plan.systemd_units_changed,
                          plan.systemd_units_new_required, plan.systemd_units_new_optional,
                          plan.systemd_units_removed_or_renamed), ((), (), (), (), ()))
        self.assertEqual((plan.collectstatic_required, plan.nginx_changed,
                          plan.runtime_components_changed, plan.python_requirements_changed,
                          plan.manual_bootstrap_required), (False, False, False, False, False))
        self.assertIsNone(plan.protected_runtime_transition)
        blockers = manual_blockers(plan, known_units=_known_units())
        self.assertEqual(blockers, ())
        for forbidden in (
            "UNKNOWN_MANAGED_UNIT", "MIGRATION_OPERATION_MANUAL", "MISSING_COMPANION",
            "PROTECTED_RUNTIME_MISMATCH", "MANIFEST_DIFF_MISMATCH",
        ):
            self.assertNotIn(forbidden, blockers)
        self.assertEqual([unit for unit in planner.RESTART_ORDER if unit in plan.services_requiring_restart],
                         ["isadoraair-gunicorn"])


class R0124ManifestValidatorTests(SimpleTestCase):
    def test_complete_historical_manifest_chain_validates(self):
        out, err = io.StringIO(), io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=err)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn("FAIL", combined)
        self.assertIn("r0123 -> r0124", out.getvalue())
