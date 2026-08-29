"""Runtime Foundation E5 -- real execution tests for the installed,
canonical TTS launcher (deploy/isadoraair-tts-canonical, published by
isadoraair.runtime_surfaces to <target-root>/usr/local/bin/isadoraair-tts).

This is deliberately separate from deploy/isadoraair-tts's own repo-local
launcher tests (see test_tts_cli.py's StableCliTests) -- that file keeps
testing the unrelated, unmodified checkout-relative development launcher.
Nothing here touches it.

Every execution test here runs against a real, disposable, mapped target
root and a small fake canonical application/venv this file constructs
itself -- never the real host paths.

This file is, deliberately, the one legitimate consumer of the explicit
`embed_mapped_application_root=True` opt-in seam on
RuntimeSystemSurfaceManager: to genuinely execute the installed launcher
here it must reference this test's own disposable, mapped application
root rather than the canonical /opt/isadoraair the product default
embeds (see isadoraair.runtime_surfaces and
docs/RUNTIME_SYSTEM_SURFACES.md). That product default -- and the
distinct proof that a launcher written beneath a target_root still
embeds the canonical path -- is covered separately in
test_runtime_surfaces.py; this file never exercises or asserts that
default behavior."""

from __future__ import annotations

import stat
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair.runtime_components import load_runtime_components
from isadoraair.runtime_provisioning import ProvisioningLayout
from isadoraair.runtime_surfaces import RuntimeSystemSurfaceManager


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class InstalledLauncherExecutionTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="isadoraair-e5-launcher-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "target"
        self.target.mkdir()
        self.product = deepcopy(load_runtime_components())
        self.layout = ProvisioningLayout.from_manifest(self.product, target_root=self.target)

        # Explicit disposable-execution seam: this file needs the installed
        # launcher's own content to reference THIS test's mapped,
        # disposable application root so it can genuinely execute against
        # the fake app below -- never the canonical /opt/isadoraair the
        # product default embeds. See the module docstring above.
        manager = RuntimeSystemSurfaceManager(
            target_root=self.target,
            product_manifest=self.product,
            project_root=PROJECT_ROOT,
            embed_mapped_application_root=True,
        )
        manager.apply()
        self.launcher = self.layout.tts_cli
        self.application_root = self.layout.application_root

    def _install_fake_canonical_app(self):
        """A minimal, self-contained fake application root + venv -- never
        the real Django app, never a real Kokoro/Piper runtime."""
        venv_python = self.application_root / "venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.symlink_to(sys.executable)

        package_root = self.application_root / "isadoraair"
        package_root.mkdir(parents=True)
        (package_root / "__init__.py").write_text("", encoding="utf-8")
        (package_root / "tts.py").write_text(
            "import os, sys\n"
            "sys.stdout.write('argv=' + repr(sys.argv[1:]) + '\\n')\n"
            "sys.stdout.write('cwd=' + os.getcwd() + '\\n')\n"
            "sys.stdout.write('pythonpath_present=' + str('PYTHONPATH' in os.environ) + '\\n')\n",
            encoding="utf-8",
        )

    def _run(self, *args, cwd=None, extra_env=None):
        environment = {
            name: value
            for name, value in __import__("os").environ.items()
            if name in ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TZ")
        }
        environment["PYTHONPATH"] = "/should/be/stripped"
        if extra_env:
            environment.update(extra_env)
        return subprocess.run(
            [str(self.launcher), *args],
            cwd=cwd or str(self.root),
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_installed_at_mapped_destination_executable_and_exact_content(self):
        self.assertTrue(self.launcher.is_file())
        mode = stat.S_IMODE(self.launcher.stat().st_mode)
        self.assertEqual(mode, 0o755)
        self.assertEqual(
            self.launcher.read_bytes(),
            RuntimeSystemSurfaceManager(
                target_root=self.target,
                product_manifest=self.product,
                project_root=PROJECT_ROOT,
                embed_mapped_application_root=True,
            )._rendered_launcher(),
        )

    def test_missing_canonical_python_fails_clearly_without_fallback(self):
        # Deliberately do NOT install the fake app -- application_root
        # exists conceptually but its venv does not.
        result = self._run("--help")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("canonical application Python is missing", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_canonical_python_does_not_fall_back_to_path_python(self):
        # A real python IS on PATH (the one running this test); prove the
        # launcher still refuses rather than silently using it.
        result = self._run("--help")
        self.assertNotEqual(result.returncode, 0)
        # If it had fallen back to a PATH python and tried `-m isadoraair.tts`
        # from a real checkout it would behave completely differently
        # (either succeed against the real app or fail with an import
        # error) -- the exact, deterministic missing-interpreter message is
        # the actual proof of no fallback.
        self.assertIn(str(self.application_root / "venv" / "bin" / "python"), result.stderr)

    def test_works_from_arbitrary_cwd_selects_mapped_python_strips_pythonpath_passes_argv(self):
        self._install_fake_canonical_app()
        unrelated_cwd = tempfile.mkdtemp(prefix="isadoraair-e5-unrelated-cwd-")
        self.addCleanup(lambda: __import__("shutil").rmtree(unrelated_cwd, ignore_errors=True))

        result = self._run("--voice", "test-voice", "--speed", "1.5", cwd=unrelated_cwd)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("argv=['--voice', 'test-voice', '--speed', '1.5']", result.stdout)
        self.assertIn(f"cwd={self.application_root}", result.stdout)
        self.assertIn("pythonpath_present=False", result.stdout)

    def test_no_home_jreed_dependency_in_content_or_behavior(self):
        self.assertNotIn("/home/jreed", self.launcher.read_text(encoding="utf-8"))

    def test_no_provider_path_embedded_in_launcher(self):
        text = self.launcher.read_text(encoding="utf-8")
        self.assertNotIn("isadoraair-runtime/kokoro", text)
        self.assertNotIn("isadoraair-runtime/piper", text)
        self.assertNotIn("provider_cli", text)
