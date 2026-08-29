"""Runtime Foundation D archive/build/validator contract tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = PROJECT_ROOT / "deploy" / "build_fdkaac.sh"
CHECK_SCRIPT = PROJECT_ROOT / "deploy" / "check_he_aac.sh"
MANIFEST = PROJECT_ROOT / "isadoraair" / "runtime_components.json"


class FdkaacToolingTests(SimpleTestCase):
    maxDiff = None

    def setUp(self):
        super().setUp()
        self.temp_dir = Path(tempfile.mkdtemp(prefix="isadoraair-fdkaac-test."))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

    def _run_build(self, *arguments, environment=None):
        env = os.environ.copy()
        env.pop("PREFIX", None)
        env.pop("BUILD_DIR", None)
        env["TMPDIR"] = str(self.temp_dir)
        if environment:
            env.update(environment)
        return subprocess.run(
            [BUILD_SCRIPT, *map(str, arguments)],
            cwd=self.temp_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_build_script_reads_the_manifest_source_contract(self):
        result = self._run_build("--print-source-contract")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))["components"]["fdkaac"]
        expected = []
        for key, version_key in (("fdk-aac", "libfdk_aac_version"), ("fdkaac", "fdkaac_version")):
            archive = manifest["source_archives"][key]
            expected.append(
                "\t".join(
                    (
                        key,
                        manifest["runtime"][version_key],
                        archive["filename"],
                        str(archive["bytes"]),
                        archive["sha256"],
                    )
                )
            )
        self.assertEqual(result.stdout.splitlines(), expected)

    def test_missing_local_archive_fails_without_creating_install_prefix(self):
        source_dir = self.temp_dir / "sources"
        source_dir.mkdir()
        prefix = self.temp_dir / "stage"
        result = self._run_build("--source-dir", source_dir, "--prefix", prefix)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing required source archive", result.stderr)
        self.assertFalse(prefix.exists())

    def test_wrong_archive_hash_fails_before_extraction_or_install(self):
        source_dir = self.temp_dir / "sources"
        source_dir.mkdir()
        (source_dir / "fdk-aac-2.0.3.tar.gz").write_bytes(b"not the audited archive")
        (source_dir / "fdkaac-1.0.7.tar.gz").write_bytes(b"also not audited")
        prefix = self.temp_dir / "stage"
        result = self._run_build("--source-dir", source_dir, "--prefix", prefix)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256 mismatch for fdk-aac-2.0.3.tar.gz", result.stderr)
        self.assertFalse(prefix.exists())

    def test_explicit_local_mode_never_invokes_network_acquisition(self):
        source_dir = self.temp_dir / "sources"
        source_dir.mkdir()
        fake_bin = self.temp_dir / "fake-bin"
        fake_bin.mkdir()
        marker = self.temp_dir / "curl-was-called"
        fake_curl = fake_bin / "curl"
        fake_curl.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n", encoding="utf-8")
        fake_curl.chmod(0o755)
        result = self._run_build(
            "--source-dir",
            source_dir,
            "--prefix",
            self.temp_dir / "stage",
            environment={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Network acquisition is disabled", result.stdout)
        self.assertFalse(marker.exists())

    def test_production_prefix_requires_a_second_explicit_guard(self):
        source_dir = self.temp_dir / "sources"
        source_dir.mkdir()
        result = self._run_build("--source-dir", source_dir, "--prefix", "/usr/local")
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing production prefix", result.stderr)

    def test_auto_workspace_is_cleaned_after_failure(self):
        source_dir = self.temp_dir / "sources"
        source_dir.mkdir()
        workspace_root = self.temp_dir / "workspaces"
        workspace_root.mkdir()
        result = self._run_build(
            "--source-dir",
            source_dir,
            "--prefix",
            self.temp_dir / "stage",
            environment={"TMPDIR": str(workspace_root)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(workspace_root.iterdir()), [])

    def test_validator_is_one_linkage_and_functional_authority(self):
        text = CHECK_SCRIPT.read_text(encoding="utf-8")
        for required in (
            "readelf -d",
            "ldd",
            "pkg-config --modversion fdk-aac",
            'for profile in 2 5 29',
            "ffmpeg",
            "HE-AAC/SBR",
            "HE-AACv2/SBR+PS",
            "--runtime-only",
        ):
            self.assertIn(required, text)

    def test_network_acquisition_is_explicit_and_does_not_create_a_second_recipe(self):
        text = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("--download-sources", text)
        self.assertIn("--source-dir", text)
        self.assertNotIn("git clone", text)
        self.assertEqual(text.count("autoreconf -fiv"), 2)

    def test_restore_stage_can_forward_the_local_source_directory(self):
        text = (PROJECT_ROOT / "deploy" / "restore" / "50-native-deps.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("FDKAAC_SOURCE_DIR", text)
        self.assertIn('SOURCE_ARGS=(--source-dir "$SOURCE_DIR")', text)
        self.assertIn("network disabled", text)

    def test_no_archive_or_native_payload_is_checked_in(self):
        forbidden_suffixes = (".onnx", ".so", ".a", ".tar.gz", ".wav", ".aac")
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
        ).stdout.decode().split("\0")
        found = [
            path
            for name in tracked
            if name and (path := Path(name)).name.endswith(forbidden_suffixes)
        ]
        self.assertEqual(found, [])

    def test_exact_archive_prepare_integration_when_explicitly_requested(self):
        source_dir_value = os.environ.get("ISADORAAIR_FDKAAC_SOURCE_DIR")
        if not source_dir_value:
            self.skipTest("set ISADORAAIR_FDKAAC_SOURCE_DIR for the audited-archive integration")
        source_dir = Path(source_dir_value)
        build_dir = self.temp_dir / "prepared"
        result = self._run_build(
            "--source-dir",
            source_dir,
            "--build-dir",
            build_dir,
            "--prepare-only",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((build_dir / "fdk-aac" / "NOTICE").is_file())
        self.assertTrue((build_dir / "fdkaac" / "COPYING").is_file())
        self.assertFalse((self.temp_dir / "stage").exists())
