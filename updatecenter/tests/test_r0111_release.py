"""r0111: accepted iPortal live-playhead and navigation polish.

Historical assertions are bounded by the unique commit that introduces the
r0111 manifest. The later trusted migration-authorization companion is
optional at that immutable target and, once present, is validated against it.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase

from updatecenter import manifest as manifest_mod
from updatecenter import migration_authorization
from updatecenter.management.commands import updatecenter_probe as probe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "deploy" / "releases" / "r0111.json"
COMPANION = PROJECT_ROOT / "deploy" / "migration_authorizations" / "r0111.json"
R0110_TARGET = "38e0fef801728b01f51df00422767b2c5210dc8c"
PUBLISHED_MAIN = "bfd46a1bd6072d0955da4fb7b9b7f14fcc04f2c5"
ACCEPTED_FEATURE = "d2da6e9795d81957f54057b5c714dd75b287aec1"
PLAN = ["library.0090_seed_iportal_nav_item"]
MANUAL = [("library.0090_seed_iportal_nav_item", 0, "RunPython", "manual")]
RELEASE_SURFACE = {
    "production/static/production/iportal/workstation.js",
    "production/tests/test_recorder_browser.py",
    "library/migrations/0090_seed_iportal_nav_item.py",
    "library/tests/test_iportal_nav.py",
    "deploy/releases/r0111.json",
    "docs/releases/r0111.md",
    "updatecenter/tests/test_r0111_release.py",
}


def _git(*args) -> str:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _release_commit() -> str:
    """The unique immutable commit that introduced r0111.json."""
    commits = _git(
        "log", "--full-history", "--no-renames", "--diff-filter=A",
        "--format=%H", "HEAD", "--", "deploy/releases/r0111.json",
    ).split()
    if len(commits) != 1:
        raise AssertionError(
            f"r0111.json must have exactly one introducing commit, found {commits!r}"
        )
    return commits[0]


def _migration_path(ref: str) -> str:
    app, name = ref.split(".", 1)
    return f"{app}/migrations/{name}.py"


class R0111ManifestTests(SimpleTestCase):
    def setUp(self):
        self.raw = MANIFEST.read_bytes()
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(
            self.data, source_label="r0111.json"
        )

    def test_manifest_declares_the_exact_static_and_data_only_update(self):
        m = self.manifest
        self.assertEqual(
            (m.release_id, m.previous_release_id, m.minimum_updater_protocol_version),
            ("r0111", "r0110", 5),
        )
        self.assertIsNone(m.protected_runtime)
        self.assertEqual(list(m.migrations_required), PLAN)
        self.assertEqual(m.migration_compatibility, "additive")
        self.assertTrue(m.collectstatic_required)
        self.assertEqual(list(m.services_requiring_restart), [])
        for empty in (
            "apt_packages_new",
            "systemd_units_changed",
            "systemd_units_new_required",
            "systemd_units_new_optional",
            "systemd_units_removed_or_renamed",
        ):
            self.assertEqual(self.data[empty], [], empty)
        self.assertEqual(
            (
                self.data["python_requirements_changed"],
                self.data["requirements_sha256"],
                self.data["nginx_changed"],
                self.data["runtime_components_changed"],
                self.data["manual_bootstrap_required"],
            ),
            (False, None, False, False, False),
        )
        self.assertEqual(self.data["minimum_supported_release_id"], "r0081")
        self.assertNotIn("migration_authorization", self.data)
        self.assertEqual(self.raw, (json.dumps(self.data, indent=2) + "\n").encode())

    def test_0090_is_the_only_migration_introduced_since_the_r0110_target(self):
        added = _git(
            "diff", "--name-only", "--diff-filter=A", R0110_TARGET,
            _release_commit(), "--", "*/migrations/0*.py",
        ).split()
        self.assertEqual(added, ["library/migrations/0090_seed_iportal_nav_item.py"])

    def test_real_classifier_finds_exactly_one_manual_runpython(self):
        plan = probe._serialize_exact_plan(MigrationLoader(None), PLAN)
        self.assertEqual([item["ref"] for item in plan], PLAN)
        classified = [
            (
                entry["ref"],
                entry["operation_index"],
                entry["operation"],
                entry["classification"],
            )
            for entry in probe.extract_manual_operations(plan)
        ]
        self.assertEqual(classified, MANUAL)
        self.assertEqual(len(plan[0]["operations"]), 1)

    def test_release_surface_is_exact_and_contains_no_undeclared_runtime_change(self):
        release = _release_commit()
        self.assertEqual(_git("rev-parse", f"{release}^"), ACCEPTED_FEATURE)
        changed = set(_git("diff", "--name-only", PUBLISHED_MAIN, release).split())
        self.assertEqual(changed, RELEASE_SURFACE)

        # Published main already carried r0110's later metadata-only companion
        # and bounded-history test correction beyond the installed r0110
        # target. Account for both explicitly so neither can conceal a product
        # surface addition.
        inherited = set(_git("diff", "--name-only", R0110_TARGET, release).split()) - changed
        self.assertEqual(
            inherited,
            {
                "deploy/migration_authorizations/r0110.json",
                "updatecenter/tests/test_r0110_release.py",
            },
        )

        all_changed = set(_git("diff", "--name-only", R0110_TARGET, release).split())
        forbidden_prefixes = (
            "deploy/updater_runtime/",
            "deploy/updater_attestations/",
            "deploy/systemd/",
            "ogremote/",
        )
        self.assertFalse(
            [path for path in all_changed if path.startswith(forbidden_prefixes)]
        )
        self.assertFalse(
            all_changed
            & {
                "requirements.txt",
                "deploy/isadoraair.nginx",
                "library/services/engine.py",
                "production/services/validation.py",
            }
        )
        static = sorted(path for path in all_changed if "/static/" in path)
        self.assertEqual(static, ["production/static/production/iportal/workstation.js"])

    def test_native_airing_remains_unavailable_in_source_and_import(self):
        source = (PROJECT_ROOT / "active_messages" / "services.py").read_text()
        self.assertIn("\nNATIVE_AIRING_AVAILABLE = False\n", source)
        from active_messages import services
        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)


class R0111CompanionTests(SimpleTestCase):
    def test_companion_is_optional_at_release_then_exact_when_present(self):
        if not COMPANION.exists():
            history = _git("log", "--full-history", "--format=%H", "HEAD", "--", str(
                COMPANION.relative_to(PROJECT_ROOT)
            ))
            self.assertEqual(history, "")
            self.skipTest("the r0111 companion is introduced after the release commit")

        release = _release_commit()
        raw = COMPANION.read_bytes()
        record = migration_authorization.parse_companion(raw)
        self.assertEqual(
            (record["release_id"], record["target_commit"]),
            ("r0111", release),
        )
        manifest_at_release = subprocess.run(
            [
                "git", "-C", str(PROJECT_ROOT), "show",
                f"{release}:deploy/releases/r0111.json",
            ],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(
            record["manifest_sha256"],
            hashlib.sha256(manifest_at_release).hexdigest(),
        )
        plan = probe._serialize_exact_plan(MigrationLoader(None), PLAN)
        classified = {
            (
                entry["ref"],
                entry["operation_index"],
                entry["operation"],
                entry["classification"],
            )
            for entry in probe.extract_manual_operations(plan)
        }
        authorized = {
            (
                entry["ref"],
                entry["operation_index"],
                entry["operation"],
                entry["classification"],
            )
            for entry in record["authorized_manual_operations"]
        }
        self.assertEqual(classified, set(MANUAL))
        self.assertEqual(authorized, classified)
        self.assertEqual(len(record["authorized_manual_operations"]), 1)
        entry = record["authorized_manual_operations"][0]
        migration_at_release = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "show", f"{release}:{_migration_path(entry['ref'])}"],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(
            entry["migration_file_sha256"],
            hashlib.sha256(migration_at_release).hexdigest(),
        )
        validated = migration_authorization.validate_release_companion(
            PROJECT_ROOT,
            _git("rev-parse", "HEAD"),
            release_id="r0111",
            release_commit=release,
            migrations_required=PLAN,
        )
        self.assertEqual(
            {(ref, index, op, cls) for ref, _sha, index, op, cls in validated},
            classified,
        )
        self.assertEqual(raw, (json.dumps(record, indent=2) + "\n").encode())
