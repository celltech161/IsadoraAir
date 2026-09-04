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


class CheckHeAacLibDirRegressionTests(SimpleTestCase):
    """A clean host with no system-wide libfdk-aac must still validate a
    freshly staged prefix: the initial version probe has to resolve
    libfdk-aac.so.2 through the explicitly selected --lib-dir, exactly like
    the later ldd/linkage and encode checks already do.
    """

    maxDiff = None

    def setUp(self):
        super().setUp()
        self.temp_dir = Path(tempfile.mkdtemp(prefix="isadoraair-checkheaac-test."))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

    def _compile(self, *args):
        subprocess.run(list(args), check=True, capture_output=True, text=True)

    def _build_stub(self, name: str, *, version: str = "1.0.7"):
        """Build a real ELF fdkaac binary that DT_NEEDs libfdk-aac.so.2 and
        a real shared library carrying that SONAME, laid out exactly like a
        canonical prefix (<name>/bin/fdkaac, <name>/lib/libfdk-aac.so.2.0.3
        with the libfdk-aac.so.2 SONAME symlink). No rpath is embedded, so
        the binary only loads when the caller supplies the library on
        LD_LIBRARY_PATH -- reproducing a clean DR host with nothing
        pre-installed globally.
        """
        root = self.temp_dir / name
        lib_dir = root / "lib"
        bin_dir = root / "bin"
        lib_dir.mkdir(parents=True)
        bin_dir.mkdir(parents=True)

        lib_source = root.parent / f"{name.replace('/', '_')}_lib.c"
        lib_source.parent.mkdir(parents=True, exist_ok=True)
        lib_source.write_text("int fdk_aac_stub_marker(void) { return 42; }\n", encoding="utf-8")
        real_library = lib_dir / "libfdk-aac.so.2.0.3"
        self._compile(
            "cc", "-shared", "-fPIC", "-Wl,-soname,libfdk-aac.so.2",
            "-o", str(real_library), str(lib_source),
        )
        soname_link = lib_dir / "libfdk-aac.so.2"
        soname_link.symlink_to(real_library.name)

        bin_source = root.parent / f"{name.replace('/', '_')}_bin.c"
        bin_source.write_text(
            "#include <stdio.h>\n"
            "int fdk_aac_stub_marker(void);\n"
            "int main(void) {\n"
            "    if (fdk_aac_stub_marker() != 42) { return 1; }\n"
            f'    printf("fdkaac {version}\\n");\n'
            '    printf("Usage: fdkaac [options] input_file\\n");\n'
            "    return 0;\n"
            "}\n",
            encoding="utf-8",
        )
        binary = bin_dir / "fdkaac"
        self._compile(
            "cc", "-o", str(binary), str(bin_source),
            "-L", str(lib_dir), "-l:libfdk-aac.so.2", "-Wl,--no-as-needed",
        )
        return binary, lib_dir, real_library

    def _run_check(self, *args):
        env = os.environ.copy()
        env.pop("LD_LIBRARY_PATH", None)
        return subprocess.run(
            [str(CHECK_SCRIPT), *map(str, args)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    def test_version_probe_uses_the_explicit_lib_dir(self):
        text = CHECK_SCRIPT.read_text(encoding="utf-8")
        version_probe_line = next(
            line for line in text.splitlines() if "VERSION_OUTPUT=" in line
        )
        self.assertIn('LD_LIBRARY_PATH="$LIB_DIR', version_probe_line)

    def test_freshly_staged_fdkaac_passes_version_probe_and_linkage_with_only_lib_dir(self):
        binary, lib_dir, _ = self._build_stub("staged")
        result = self._run_check(
            "--fdkaac", binary,
            "--lib-dir", lib_dir,
            "--expected-fdkaac-version", "1.0.7",
            "--expected-libfdk-version", "2.0.3",
            "--runtime-only",
        )
        combined = result.stdout + result.stderr
        self.assertIn("PASS fdkaac version: 1.0.7", result.stdout)
        self.assertIn("PASS library linkage:", result.stdout)
        self.assertNotIn("FAIL: fdkaac version mismatch", combined)
        self.assertNotIn("FAIL: fdkaac resolved the wrong libfdk-aac", combined)
        self.assertNotIn("error while loading shared libraries", combined)

    def test_absent_supplied_library_fails_closed(self):
        binary, lib_dir, real_library = self._build_stub("staged")
        real_library.unlink()
        (lib_dir / "libfdk-aac.so.2").unlink()
        result = self._run_check(
            "--fdkaac", binary,
            "--lib-dir", lib_dir,
            "--expected-fdkaac-version", "1.0.7",
            "--expected-libfdk-version", "2.0.3",
            "--runtime-only",
        )
        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL: fdkaac version mismatch", combined)

    def test_wrong_version_library_fails_closed(self):
        binary, lib_dir, _ = self._build_stub("staged", version="9.9.9")
        result = self._run_check(
            "--fdkaac", binary,
            "--lib-dir", lib_dir,
            "--expected-fdkaac-version", "1.0.7",
            "--expected-libfdk-version", "2.0.3",
            "--runtime-only",
        )
        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL: fdkaac version mismatch", combined)
        self.assertIn("expected: 1.0.7", combined)
        self.assertIn("actual:   9.9.9", combined)

    def test_exact_linkage_verification_still_rejects_a_wrongly_resolved_library(self):
        binary, lib_dir, _ = self._build_stub("staged")
        decoy_source = self.temp_dir / "decoy_lib.c"
        decoy_source.write_text("int fdk_aac_stub_marker(void) { return 42; }\n", encoding="utf-8")
        decoy_library = lib_dir / "libfdk-aac-decoy.so"
        self._compile(
            "cc", "-shared", "-fPIC", "-Wl,-soname,libfdk-aac.so.2",
            "-o", str(decoy_library), str(decoy_source),
        )
        soname_link = lib_dir / "libfdk-aac.so.2"
        soname_link.unlink()
        soname_link.symlink_to(decoy_library.name)

        result = self._run_check(
            "--fdkaac", binary,
            "--lib-dir", lib_dir,
            "--expected-fdkaac-version", "1.0.7",
            "--expected-libfdk-version", "2.0.3",
            "--runtime-only",
        )
        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL: fdkaac resolved the wrong libfdk-aac", combined)

    def test_prefix_flag_derivation_passes_using_only_the_prefix_lib_dir(self):
        """Mirrors the exact invocation isadoraair.runtime_native._run_validator
        uses in production (script --prefix PREFIX): no per-file overrides,
        just the staged prefix, with nothing installed globally.
        """
        binary, _lib_dir, _real_library = self._build_stub("stage")
        prefix = binary.parent.parent
        result = self._run_check(
            "--prefix", prefix,
            "--expected-fdkaac-version", "1.0.7",
            "--expected-libfdk-version", "2.0.3",
            "--runtime-only",
        )
        combined = result.stdout + result.stderr
        self.assertIn("PASS fdkaac version: 1.0.7", result.stdout)
        self.assertIn("PASS library linkage:", result.stdout)
        self.assertNotIn("FAIL: fdkaac version mismatch", combined)
        self.assertNotIn("error while loading shared libraries", combined)
