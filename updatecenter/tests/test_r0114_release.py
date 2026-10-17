"""r0114: accepted iPortal recorder UI refinements (calibrated meter,
Play/Pause/Stop preview transport, waveform wheel and touch navigation).

The manifest says exactly what the repository does: no migration (so no
migration-authorization companion), collectstatic for the three changed
iPortal scripts, a Gunicorn restart for the workstation view and template,
and no protected-runtime, unit, package or nginx change. Historical
assertions are bounded by the unique commit that introduces the r0114
manifest, never by HEAD.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401  (puts the protected runtime on sys.path)
from isadoraair_updater.release import CORE_SERVICES

from updatecenter import manifest as manifest_mod
from updatecenter import planner

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "deploy" / "releases" / "r0114.json"
COMPANION = PROJECT_ROOT / "deploy" / "migration_authorizations" / "r0114.json"
R0113_TARGET = "8926fe0068d2008bdf857c73fd7c4920ac1c98b0"       # the installed r0113 release commit
PUBLISHED_MAIN = "82288057cab2b381fa68425fa0da8fac8d020e48"     # + r0113's metadata-only companion
ACCEPTED_FEATURE = "54dff05dcdb19ff7c20c1a5d0f84be6c56218461"
STATIC = [
    "production/static/production/iportal/audio_core.js",
    "production/static/production/iportal/recorder.js",
    "production/static/production/iportal/workstation.js",
]
RELEASE_SURFACE = {
    *STATIC,
    "production/templates/production/iportal/workstation.html",
    "production/recorder/views.py",
    "production/tests/test_recorder_browser.py",
    "produced_content/tests/test_browser.py",
    "library/tests/test_voicetrack_browser.py",
    "docs/IPORTAL.md",
    "deploy/releases/r0114.json",
    "docs/releases/r0114.md",
    "updatecenter/tests/test_r0114_release.py",
}


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _release_commit() -> str:
    """The unique immutable commit that introduced r0114.json."""
    commits = _git("log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--",
                   "deploy/releases/r0114.json").split()
    if len(commits) != 1:
        raise AssertionError(f"r0114.json must have exactly one introducing commit, found {commits!r}")
    return commits[0]


class R0114ManifestTests(SimpleTestCase):
    def setUp(self):
        self.raw = subprocess.run(["git", "-C", str(PROJECT_ROOT), "show",
                                   f"{_release_commit()}:deploy/releases/r0114.json"],
                                  check=True, capture_output=True).stdout
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(self.data, source_label="r0114.json")

    def test_manifest_declares_a_static_and_gunicorn_only_update(self):
        m = self.manifest
        self.assertEqual((m.release_id, m.previous_release_id, m.minimum_updater_protocol_version),
                         ("r0114", "r0113", 5))
        self.assertIsNone(m.protected_runtime)
        self.assertEqual(list(m.migrations_required), [])
        self.assertIsNone(m.migration_compatibility)
        self.assertTrue(m.collectstatic_required)
        self.assertEqual(list(m.services_requiring_restart), ["isadoraair-gunicorn"])
        for empty in ("apt_packages_new", "systemd_units_changed", "systemd_units_new_required",
                      "systemd_units_new_optional", "systemd_units_removed_or_renamed"):
            self.assertEqual(self.data[empty], [], empty)
        self.assertEqual((self.data["python_requirements_changed"], self.data["requirements_sha256"],
                          self.data["nginx_changed"], self.data["runtime_components_changed"],
                          self.data["manual_bootstrap_required"]), (False, None, False, False, False))
        self.assertEqual(self.data["minimum_supported_release_id"], "r0081")
        self.assertEqual(self.raw, (json.dumps(self.data, indent=2) + "\n").encode())
        self.assertEqual(MANIFEST.read_bytes(), self.raw)                       # never edited afterwards
        self.assertTrue(set(m.services_requiring_restart) <= CORE_SERVICES)
        self.assertIn("isadoraair-gunicorn", planner.RESTART_ORDER)

    def test_no_migration_was_introduced_and_no_companion_exists(self):
        added = _git("diff", "--name-only", "--diff-filter=A", R0113_TARGET, _release_commit(), "--",
                     "*/migrations/*.py").split()
        self.assertEqual(added, [])
        self.assertFalse(COMPANION.exists())
        self.assertEqual(_git("log", "--full-history", "--format=%H", "HEAD", "--",
                              "deploy/migration_authorizations/r0114.json"), "")

    def test_release_surface_is_exact_and_contains_no_undeclared_runtime_change(self):
        release = _release_commit()
        self.assertEqual(_git("rev-parse", f"{release}^"), ACCEPTED_FEATURE)
        self.assertEqual(_git("rev-parse", f"{ACCEPTED_FEATURE}^"), PUBLISHED_MAIN)
        changed = set(_git("diff", "--name-only", PUBLISHED_MAIN, release).split())
        self.assertEqual(changed, RELEASE_SURFACE)
        # Published main already carried r0113's metadata-only companion beyond
        # the installed r0113 target; nothing else is inherited.
        inherited = set(_git("diff", "--name-only", R0113_TARGET, release).split()) - changed
        self.assertEqual(inherited, {"deploy/migration_authorizations/r0113.json"})
        self.assertEqual(sorted(path for path in changed if "/static/" in path), STATIC)   # hence collectstatic
        self.assertFalse([path for path in changed if path.startswith(
            ("deploy/updater_runtime/", "deploy/updater_attestations/", "deploy/updater_bootstrap/", "ogremote/"))])
        self.assertFalse([path for path in changed if path.endswith((".service", ".timer"))])
        self.assertFalse(changed & {"requirements.txt", "deploy/isadoraair.nginx", "library/services/engine.py",
                                    "active_messages/services.py", "production/models.py"})

    def test_native_airing_remains_unavailable(self):
        source = (PROJECT_ROOT / "active_messages" / "services.py").read_text()
        self.assertIn("\nNATIVE_AIRING_AVAILABLE = False\n", source)
        from active_messages import services
        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)
