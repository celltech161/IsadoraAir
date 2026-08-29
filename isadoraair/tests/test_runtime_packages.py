"""Runtime Foundation E6 -- package-authority bridge tests."""

from __future__ import annotations

import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from isadoraair.runtime_components import load_runtime_components
from isadoraair.runtime_packages import (
    PACKAGES_MANIFEST_PATH,
    DPKG_EXECUTABLE_CANDIDATES,
    DPKG_INSTALLED,
    DPKG_NOT_INSTALLED,
    DPKG_UNRESOLVED,
    STATUS_FAIL,
    STATUS_NOT_APPLICABLE,
    STATUS_OPTIONAL_ABSENT,
    STATUS_PASS,
    STATUS_UNRESOLVED,
    RuntimePackageAuthorityError,
    component_package_group,
    evaluate_package_prerequisite,
    package_group_members,
    parse_package_groups,
    _dpkg_status,
)


class PackageAuthorityParserTests(SimpleTestCase):
    """The parser only ever needs to recognize the ONE real file's shape
    -- exercised directly against it, plus small synthetic fixtures for
    edge cases the real file doesn't happen to contain."""

    def test_real_authority_file_parses_expected_groups(self):
        groups = parse_package_groups(PACKAGES_MANIFEST_PATH)
        self.assertIn("OPTIONAL_KOKORO_TTS", groups)
        self.assertEqual(groups["OPTIONAL_KOKORO_TTS"], ("espeak-ng",))
        self.assertIn("BUILD_HEAAC", groups)
        self.assertIn("pkg-config", groups["BUILD_HEAAC"])
        self.assertIn("autoconf", groups["BUILD_HEAAC"])
        self.assertIn("CORE", groups)
        self.assertIn("python3", groups["CORE"])

    def test_never_duplicates_package_membership_in_python(self):
        """This module must never hardcode a package list -- the real
        authority file is the only source parse_package_groups reads."""

        import isadoraair.runtime_packages as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("espeak-ng", source)
        self.assertNotIn("autoconf", source)

    def test_synthetic_file_with_comments_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.txt"
            path.write_text(
                "# header comment\n"
                "\n"
                "GROUP_ONE=(\n"
                "  # a comment inside the group\n"
                "  pkg-a\n"
                "\n"
                "  pkg-b\n"
                ")\n"
                "\n"
                "GROUP_TWO=(\n"
                "  pkg-c\n"
                ")\n",
                encoding="utf-8",
            )
            groups = parse_package_groups(path)
            self.assertEqual(groups, {"GROUP_ONE": ("pkg-a", "pkg-b"), "GROUP_TWO": ("pkg-c",)})

    def test_unclosed_group_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.txt"
            path.write_text("BROKEN=(\n  pkg-a\n", encoding="utf-8")
            with self.assertRaises(RuntimePackageAuthorityError):
                parse_package_groups(path)

    def test_missing_file_fails_clearly(self):
        with self.assertRaises(RuntimePackageAuthorityError):
            parse_package_groups(Path("/definitely/does/not/exist/packages.txt"))

    def test_unknown_group_reference_fails_clearly(self):
        with self.assertRaises(RuntimePackageAuthorityError):
            package_group_members("NOT_A_REAL_GROUP", path=PACKAGES_MANIFEST_PATH)

    def _assert_rejected(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.txt"
            path.write_text(body, encoding="utf-8")
            with self.assertRaises(RuntimePackageAuthorityError):
                parse_package_groups(path)

    def test_command_substitution_after_member_is_rejected(self):
        self._assert_rejected("GROUP=(\n  package-name $(command)\n)\n")

    def test_shell_statement_after_member_is_rejected(self):
        self._assert_rejected("GROUP=(\n  package-name ; command\n)\n")

    def test_unsupported_outside_array_statement_is_rejected(self):
        self._assert_rejected("echo unsupported\n")

    def test_duplicate_member_is_rejected(self):
        self._assert_rejected("GROUP=(\n  package-name\n  package-name\n)\n")


class ComponentGroupResolutionTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manifest = load_runtime_components()

    def test_kokoro_runtime_group_is_optional_kokoro_tts(self):
        self.assertEqual(
            component_package_group(self.manifest, "kokoro", kind="runtime"), "OPTIONAL_KOKORO_TTS"
        )

    def test_piper_has_no_invented_runtime_group(self):
        """Piper is self-contained (docs/PIPER_PROVENANCE.md) -- it must
        not gain an Ubuntu runtime package requirement no one declared."""

        self.assertIsNone(component_package_group(self.manifest, "piper", kind="runtime"))

    def test_fdkaac_build_group_remains_build_heaac(self):
        self.assertEqual(component_package_group(self.manifest, "fdkaac", kind="build"), "BUILD_HEAAC")

    def test_fdkaac_has_no_runtime_group_distinct_from_build(self):
        """The runtime-vs-build distinction must be real: fdkaac's own
        already-built canonical binary needs nothing at RUNTIME from
        apt -- only BUILD_HEAAC, and only while actually building."""

        self.assertIsNone(component_package_group(self.manifest, "fdkaac", kind="runtime"))

    def test_kokoro_has_no_build_group(self):
        self.assertIsNone(component_package_group(self.manifest, "kokoro", kind="build"))

    def test_invalid_kind_rejected(self):
        with self.assertRaises(ValueError):
            component_package_group(self.manifest, "kokoro", kind="bogus")


class PackagePrerequisiteEvidenceTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manifest = load_runtime_components()

    def test_required_and_missing_is_fail(self):
        evidence = evaluate_package_prerequisite(
            self.manifest, "kokoro", kind="runtime", required=True, dpkg_probe=lambda pkg: False
        )
        self.assertEqual(evidence.status, STATUS_FAIL)
        self.assertEqual(evidence.missing, ("espeak-ng",))

    def test_required_and_present_is_pass(self):
        evidence = evaluate_package_prerequisite(
            self.manifest, "kokoro", kind="runtime", required=True, dpkg_probe=lambda pkg: True
        )
        self.assertEqual(evidence.status, STATUS_PASS)
        self.assertEqual(evidence.missing, ())

    def test_not_required_and_missing_is_optional_absent_not_a_failure(self):
        """Kokoro unused, espeak-ng absent => not a station failure."""

        evidence = evaluate_package_prerequisite(
            self.manifest, "kokoro", kind="runtime", required=False, dpkg_probe=lambda pkg: False
        )
        self.assertEqual(evidence.status, STATUS_OPTIONAL_ABSENT)

    def test_unresolved_required_is_unresolved_not_pass(self):
        """Station DB unavailable -> cannot resolve whether Kokoro is
        required => UNRESOLVED, never a guessed PASS."""

        evidence = evaluate_package_prerequisite(
            self.manifest, "kokoro", kind="runtime", required=None, dpkg_probe=lambda pkg: True
        )
        self.assertEqual(evidence.status, STATUS_UNRESOLVED)
        evidence_missing = evaluate_package_prerequisite(
            self.manifest, "kokoro", kind="runtime", required=None, dpkg_probe=lambda pkg: False
        )
        self.assertEqual(evidence_missing.status, STATUS_UNRESOLVED)

    def test_no_group_defined_is_not_applicable(self):
        evidence = evaluate_package_prerequisite(
            self.manifest, "piper", kind="runtime", required=True, dpkg_probe=lambda pkg: False
        )
        self.assertEqual(evidence.status, STATUS_NOT_APPLICABLE)
        self.assertIsNone(evidence.group)

    def test_malformed_group_reference_fails_as_evidence_not_a_crash(self):
        """A manifest referencing an unknown package group must produce
        FAIL evidence with a diagnostic -- never crash the whole
        evidence-gathering pass, matching every other Foundation E
        validator's fail-closed-but-never-crash design."""

        from copy import deepcopy

        broken = deepcopy(self.manifest)
        broken["components"]["kokoro"]["runtime"]["ubuntu_packages_group"] = "NOT_A_REAL_GROUP"
        evidence = evaluate_package_prerequisite(
            broken, "kokoro", kind="runtime", required=True, dpkg_probe=lambda pkg: True
        )
        self.assertEqual(evidence.status, STATUS_FAIL)
        self.assertTrue(evidence.diagnostics)

    def test_never_installs_anything(self):
        """No package installation occurs in read-only validation --
        the probe seam is the only thing that ever gets called, and it
        never receives an install instruction."""

        calls = []

        def probe(pkg):
            calls.append(pkg)
            return True

        evaluate_package_prerequisite(self.manifest, "kokoro", kind="runtime", required=True, dpkg_probe=probe)
        self.assertEqual(calls, ["espeak-ng"])


class TrustedDpkgProbeTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.executable = DPKG_EXECUTABLE_CANDIDATES[0]
        self.manifest = load_runtime_components()

    def test_installed_result(self):
        with patch(
            "isadoraair.runtime_packages._trusted_dpkg_executable",
            return_value=self.executable,
        ), patch(
            "isadoraair.runtime_packages.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            self.assertEqual(_dpkg_status("espeak-ng"), DPKG_INSTALLED)
        self.assertEqual(run.call_args.args[0], [str(self.executable), "-s", "espeak-ng"])

    def test_normal_not_installed_result(self):
        with patch(
            "isadoraair.runtime_packages._trusted_dpkg_executable",
            return_value=self.executable,
        ), patch(
            "isadoraair.runtime_packages.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1),
        ):
            self.assertEqual(_dpkg_status("espeak-ng"), DPKG_NOT_INSTALLED)

    def test_missing_executable_is_unresolved(self):
        with patch(
            "isadoraair.runtime_packages._trusted_dpkg_executable", return_value=None
        ):
            self.assertEqual(_dpkg_status("espeak-ng"), DPKG_UNRESOLVED)

    def test_timeout_is_unresolved(self):
        with patch(
            "isadoraair.runtime_packages._trusted_dpkg_executable",
            return_value=self.executable,
        ), patch(
            "isadoraair.runtime_packages.subprocess.run",
            side_effect=subprocess.TimeoutExpired("dpkg", 10),
        ):
            self.assertEqual(_dpkg_status("espeak-ng"), DPKG_UNRESOLVED)

    def test_unresolved_probe_is_not_misreported_as_missing_package(self):
        evidence = evaluate_package_prerequisite(
            self.manifest,
            "kokoro",
            kind="runtime",
            required=True,
            dpkg_probe=lambda package: DPKG_UNRESOLVED,
        )
        self.assertEqual(evidence.status, STATUS_UNRESOLVED)
        self.assertEqual(evidence.missing, ())
        self.assertIn("trusted dpkg probe failed", evidence.diagnostics[0])
