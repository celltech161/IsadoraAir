"""Runtime Foundation E4 native preparation and publication tests."""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import multiprocessing
import os
import stat
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from isadoraair.runtime_components import load_runtime_components
from isadoraair.runtime_native import (
    NativeProvisioningSeams,
    NativeRuntimeProvisioner,
    RuntimeProvisioningError,
    _ensure_noncanonical_publication_directories,
    _run_bounded,
    _run_build,
    _run_ldconfig,
    _safe_message,
    verify_native_sources,
)
from isadoraair.runtime_provisioning import ProvisioningLayout, runtime_provision_lock
from isadoraair.runtime_recovery import RuntimeRecoveryBuilder, load_recovery_payload
from isadoraair.runtime_requirements import ComponentRequirement, RuntimeRequirements
from isadoraair.runtime_validation import (
    ComponentEvidence,
    RuntimeEvidence,
    STATUS_FAIL,
    STATUS_OPTIONAL_ABSENT,
    STATUS_PASS,
)
from monitoring.management.commands.provision_runtime_components import (
    RECOVERY_PAYLOAD_REASON,
    _requirements_for_recovery_native,
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def with_uid(metadata: os.stat_result, uid: int) -> os.stat_result:
    values = list(metadata)
    values[4] = uid
    return os.stat_result(values)


def requirements(*, required=True, errors=()) -> RuntimeRequirements:
    return RuntimeRequirements(
        components={
            "fdkaac": ComponentRequirement(
                "fdkaac", required, ("enabled HE-AAC output",) if required else ()
            ),
            "kokoro": ComponentRequirement("kokoro"),
            "piper": ComponentRequirement("piper"),
        },
        errors=tuple(errors),
    )


def evidence(
    resolved: RuntimeRequirements,
    *,
    fdkaac_status: str,
    tts_status: str = STATUS_OPTIONAL_ABSENT,
    marker: str | None = None,
) -> RuntimeEvidence:
    return RuntimeEvidence(
        runtime_contract_sha256="a" * 64,
        runtime_manifest_schema_version=1,
        components={
            "fdkaac": ComponentEvidence(
                required=resolved.components["fdkaac"].required,
                status=fdkaac_status,
                observed={"marker": marker} if marker else {},
            ),
            "kokoro": ComponentEvidence(required=False, status=tts_status),
            "piper": ComponentEvidence(required=False, status=STATUS_OPTIONAL_ABSENT),
        },
        requirement_errors=resolved.errors,
    )


def _lock_worker(target, product, entered, release=None):
    layout = ProvisioningLayout.from_manifest(product, target_root=target)
    with runtime_provision_lock(layout):
        entered.set()
        if release is not None and not release.wait(10):
            raise RuntimeError("lock test release timed out")


class NativeFixture(SimpleTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="isadoraair-e4-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "target"
        self.target.mkdir()
        self.product = deepcopy(load_runtime_components())
        self.source = self.root / "source"
        self.source.mkdir()
        self._small_source_contract()
        self.prepared = self.root / "prepared"
        self.calls: list[object] = []

    def _small_source_contract(self):
        archives = self.product["components"]["fdkaac"]["source_archives"]
        for index, name in enumerate(sorted(archives)):
            data = f"audited-{index}".encode()
            path = self.source / archives[name]["filename"]
            path.write_bytes(data)
            path.chmod(0o600)
            archives[name]["bytes"] = len(data)
            archives[name]["sha256"] = digest(data)

    def fake_build(self, script: Path, source: Path, prefix: Path):
        self.calls.append(("build", script, source, prefix))
        (prefix / "bin").mkdir(parents=True)
        (prefix / "lib" / "pkgconfig").mkdir(parents=True)
        for directory in (
            prefix,
            prefix / "bin",
            prefix / "lib",
            prefix / "lib" / "pkgconfig",
        ):
            directory.chmod(0o755)
        binary = prefix / "bin" / "fdkaac"
        binary.write_bytes(b"new-fdkaac")
        binary.chmod(0o755)
        version = self.product["components"]["fdkaac"]["runtime"]["libfdk_aac_version"]
        library = prefix / "lib" / f"libfdk-aac.so.{version}"
        library.write_bytes(b"new-libfdk")
        library.chmod(0o644)
        soname = prefix / "lib" / f"libfdk-aac.so.{version.split('.', 1)[0]}"
        soname.symlink_to(library.name)
        pc = prefix / "lib" / "pkgconfig" / "fdk-aac.pc"
        pc.write_text(f"Version: {version}\n", encoding="utf-8")
        pc.chmod(0o644)

    def fake_prefix_validation(self, script: Path, prefix: Path):
        self.calls.append(("validate_prefix", script, prefix))
        self.assertTrue((prefix / "bin" / "fdkaac").is_file())

    def fake_ldconfig(self, target_root: Path):
        self.calls.append(("ldconfig", target_root))
        lib = self.target / "usr" / "local" / "lib"
        version = self.product["components"]["fdkaac"]["runtime"]["libfdk_aac_version"]
        versioned = lib / f"libfdk-aac.so.{version}"
        soname = lib / f"libfdk-aac.so.{version.split('.', 1)[0]}"
        if soname.is_symlink() or soname.exists():
            soname.unlink()
        if versioned.exists():
            soname.symlink_to(versioned.name)

    def filesystem_validation(self, manifest, resolved):
        binary = Path(manifest["components"]["fdkaac"]["runtime"]["binary"])
        libroot = Path(manifest["components"]["fdkaac"]["runtime"]["library_root"])
        version = self.product["components"]["fdkaac"]["runtime"]["libfdk_aac_version"]
        valid = binary.is_file() and (libroot / f"libfdk-aac.so.{version}").is_file()
        marker = None
        if binary.is_file():
            marker = digest(binary.read_bytes())
        return evidence(
            resolved,
            fdkaac_status=STATUS_PASS if valid else STATUS_FAIL,
            tts_status=STATUS_FAIL,
            marker=marker,
        )

    def seams(self, *, validate=None, checkpoint=None, ldconfig=None):
        return NativeProvisioningSeams(
            build=self.fake_build,
            validate_prefix=self.fake_prefix_validation,
            ldconfig=ldconfig or self.fake_ldconfig,
            validate_runtime=validate or self.filesystem_validation,
            checkpoint=checkpoint or (lambda name: self.calls.append(("checkpoint", name))),
        )

    def provisioner(self, *, required=True, bootstrap=False, seams=None):
        return NativeRuntimeProvisioner(
            requirements=requirements(required=required),
            product_manifest=self.product,
            target_root=self.target,
            project_root=Path(__file__).parents[2],
            bootstrap=bootstrap,
            seams=seams or self.seams(),
        )

    def prepare(self, provisioner=None):
        active = provisioner or self.provisioner()
        return active.prepare(source_dir=self.source, prepared_root=self.prepared)


class SafeMessageTruncationTests(SimpleTestCase):
    """The most useful part of a failed build/validator transcript is at
    the end, not the start. _safe_message must collapse whitespace and
    stay bounded, but keep the tail when text exceeds its budget."""

    def test_short_text_is_returned_unchanged_aside_from_whitespace_collapse(self):
        self.assertEqual(_safe_message("  hello   world  \n"), "hello world")

    def test_empty_or_whitespace_only_falls_back_to_default_message(self):
        self.assertEqual(_safe_message(""), "native provisioning failed")
        self.assertEqual(_safe_message("   \n\t  "), "native provisioning failed")

    def test_long_text_keeps_the_tail_not_the_head(self):
        early_marker = "EARLY-IRRELEVANT-BUILD-OUTPUT"
        filler = "x" * 2000
        tail = "the real compiler error is right here"
        text = early_marker + filler + tail
        result = _safe_message(text)
        self.assertLessEqual(len(result), 512)
        self.assertIn(tail, result)
        self.assertNotIn(early_marker, result)

    def test_max_length_override_is_honored(self):
        text = "x" * 40 + "END-MARKER"
        result = _safe_message(text, max_length=15)
        self.assertEqual(len(result), 15)
        self.assertTrue(result.endswith("END-MARKER"))


class RunBoundedDiagnosticTailTests(SimpleTestCase):
    """End-to-end coverage for the E8 diagnostic-quality defect: a failed
    child process with a long transcript must surface the END of that
    transcript in the raised exception, not get flattened down to the
    first ~512 characters of an already-bounded 32 KiB tail."""

    def test_failed_child_process_diagnostic_preserves_the_end_of_a_long_transcript(self):
        marker = "REAL-FAILURE-REASON-AT-THE-END"
        script = (
            "import sys\n"
            "sys.stdout.write('noise-' * 20000)\n"
            f"sys.stdout.write({marker!r})\n"
            "sys.exit(1)\n"
        )
        with tempfile.TemporaryDirectory() as workdir:
            with self.assertRaises(RuntimeProvisioningError) as captured:
                _run_bounded(
                    ["python3", "-c", script],
                    cwd=Path(workdir),
                    timeout=10.0,
                    label="test child process",
                )
        message = str(captured.exception)
        self.assertIn(marker, message)
        self.assertIn("test child process exited with status 1", message)
        self.assertLessEqual(len(message), 512)

    def test_short_child_failure_message_is_unaffected(self):
        with tempfile.TemporaryDirectory() as workdir:
            with self.assertRaises(RuntimeProvisioningError) as captured:
                _run_bounded(
                    ["python3", "-c", "import sys; print('boom'); sys.exit(3)"],
                    cwd=Path(workdir),
                    timeout=10.0,
                    label="test child process",
                )
        message = str(captured.exception)
        self.assertEqual(message, "test child process exited with status 3: boom")


class NativeSourceSecurityTests(NativeFixture):
    def test_exact_manifest_archives_are_verified(self):
        result = verify_native_sources(self.source, self.product)
        self.assertEqual([item.name for item in result.archives], ["fdk-aac", "fdkaac"])

    def test_missing_wrong_size_and_wrong_hash_fail_closed(self):
        archive = next(self.source.iterdir())
        for mutation in (b"", b"wrong-same"):
            with self.subTest(mutation=mutation):
                original = archive.read_bytes()
                archive.write_bytes(mutation)
                with self.assertRaisesRegex(RuntimeProvisioningError, "byte count|SHA-256"):
                    verify_native_sources(self.source, self.product)
                archive.write_bytes(original)

    def test_symlink_and_hardlink_archives_are_rejected(self):
        archive = next(self.source.iterdir())
        original = archive.read_bytes()
        archive.unlink()
        outside = self.root / "outside"
        outside.write_bytes(original)
        archive.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeProvisioningError, "regular file"):
            verify_native_sources(self.source, self.product)
        archive.unlink()
        os.link(outside, archive)
        with self.assertRaisesRegex(RuntimeProvisioningError, "hard-linked"):
            verify_native_sources(self.source, self.product)

    def test_symlinked_source_directory_is_rejected(self):
        link = self.root / "source-link"
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeProvisioningError, "symlink"):
            verify_native_sources(link, self.product)


class NativePlanningTests(NativeFixture):
    def test_optional_fdkaac_is_not_selected(self):
        plan = self.provisioner(required=False).plan()
        self.assertEqual(plan.action, "not_selected")
        self.assertFalse(plan.needs_work)

    def test_explicit_bootstrap_selects_optional_fdkaac(self):
        plan = self.provisioner(required=False, bootstrap=True).plan(source_dir=self.source)
        self.assertEqual(plan.action, "prepare")
        self.assertIn("explicit fdkaac bootstrap", plan.reasons)

    def test_required_healthy_is_no_op_without_source_inspection(self):
        validate = lambda manifest, resolved: evidence(resolved, fdkaac_status=STATUS_PASS)
        provisioner = self.provisioner(seams=self.seams(validate=validate))
        plan = provisioner.plan(source_dir=self.root / "does-not-exist")
        self.assertEqual(plan.action, "no_op")
        self.assertEqual(plan.source_status, "not_checked")

    def test_required_broken_reports_source_available_or_unavailable(self):
        available = self.provisioner().plan(source_dir=self.source)
        self.assertEqual((available.action, available.source_status), ("prepare", "verified"))
        missing = self.provisioner().plan()
        self.assertEqual(missing.action, "blocked")
        self.assertIn("explicit source", " ".join(missing.errors))

    def test_requirement_errors_block_native_action(self):
        provisioner = NativeRuntimeProvisioner(
            requirements=requirements(errors=("station invalid",)),
            product_manifest=self.product,
            target_root=self.target,
            seams=self.seams(),
        )
        plan = provisioner.plan(source_dir=self.source)
        self.assertFalse(plan.ready)
        self.assertEqual(plan.action, "blocked")


class NativePreparationTests(NativeFixture):
    def test_prepare_invokes_existing_authorities_and_writes_receipt(self):
        result = self.prepare()
        self.assertFalse(result.no_op)
        build = next(call for call in self.calls if call[0] == "build")
        self.assertEqual(build[2:], (self.prepared / "sources", self.prepared / "prefix"))
        self.assertEqual(
            [call[0] for call in self.calls].count("validate_prefix"), 1
        )
        receipt = json.loads((self.prepared / "prepared-native.json").read_text())
        self.assertEqual(receipt["component"], "fdkaac")
        self.assertEqual({item["name"] for item in receipt["artifacts"]}, {
            "fdkaac", "libfdk-aac", "fdk-aac-pkgconfig"
        })
        self.assertEqual(receipt["preparer_uid"], os.geteuid())
        self.assertEqual((self.prepared / "prepared-native.json").stat().st_uid, os.geteuid())

    def test_default_build_argv_is_local_source_only(self):
        observed = {}

        def capture(command, **kwargs):
            observed["command"] = command

        script = self.root / "deploy" / "build_fdkaac.sh"
        with patch("isadoraair.runtime_native._run_bounded", side_effect=capture):
            _run_build(script, self.source, self.prepared)
        self.assertEqual(
            observed["command"],
            [str(script), "--source-dir", str(self.source), "--prefix", str(self.prepared)],
        )
        self.assertNotIn("--download-sources", observed["command"])
        self.assertNotIn("--allow-production-prefix", observed["command"])

    def test_build_failure_preserves_build_heaac_package_diagnostic(self):
        script = self.root / "build-fails"
        script.write_text(
            "#!/bin/sh\necho 'Ubuntu package authority: BUILD_HEAAC' >&2\nexit 1\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
        with self.assertRaisesRegex(RuntimeProvisioningError, "BUILD_HEAAC"):
            _run_build(script, self.source, self.prepared)

    def test_noncanonical_ldconfig_is_confined_to_mapped_library_directory(self):
        observed = {}

        def capture(command, **kwargs):
            observed["command"] = command

        with patch("isadoraair.runtime_native._run_bounded", side_effect=capture):
            _run_ldconfig(self.target)
        self.assertEqual(
            observed["command"][1:],
            ["-n", str(self.target / "usr" / "local" / "lib")],
        )

    def test_failed_build_or_staged_validation_removes_prepared_root(self):
        for failure_point in ("build", "validate"):
            with self.subTest(failure_point=failure_point):
                prepared = self.root / f"prepared-{failure_point}"

                def build(script, source, prefix):
                    if failure_point == "build":
                        raise RuntimeProvisioningError("build failed")
                    self.fake_build(script, source, prefix)

                def validate(script, prefix):
                    if failure_point == "validate":
                        raise RuntimeProvisioningError("validation failed")

                seams = NativeProvisioningSeams(
                    build=build,
                    validate_prefix=validate,
                    ldconfig=self.fake_ldconfig,
                    validate_runtime=self.filesystem_validation,
                )
                provisioner = self.provisioner(seams=seams)
                with self.assertRaises(RuntimeProvisioningError):
                    provisioner.prepare(source_dir=self.source, prepared_root=prepared)
                self.assertFalse(prepared.exists())

    def test_preparation_requires_new_caller_owned_root_and_never_sudo(self):
        self.prepared.mkdir()
        with self.assertRaisesRegex(RuntimeProvisioningError, "must not already exist"):
            self.prepare()
        self.assertNotIn("sudo", Path(__file__).parents[1].joinpath("runtime_native.py").read_text())


class NativePublicationTests(NativeFixture):
    def setUp(self):
        super().setUp()
        self.prepare()
        self.calls.clear()
        (self.target / "usr" / "local" / "bin").mkdir(parents=True)
        (self.target / "usr" / "local" / "lib").mkdir()

    @property
    def binary(self):
        return self.target / "usr" / "local" / "bin" / "fdkaac"

    @property
    def library(self):
        version = self.product["components"]["fdkaac"]["runtime"]["libfdk_aac_version"]
        return self.target / "usr" / "local" / "lib" / f"libfdk-aac.so.{version}"

    def test_protected_publication_is_minimal_ordered_and_scoped(self):
        unrelated = self.target / "usr" / "local" / "share" / "keep"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("keep", encoding="utf-8")
        result = self.provisioner().publish(prepared_root=self.prepared)
        self.assertEqual(result.changed_components, ("fdkaac",))
        self.assertEqual(result.evidence.components["fdkaac"].status, STATUS_PASS)
        self.assertEqual(result.evidence.components["kokoro"].status, STATUS_FAIL)
        self.assertEqual(self.binary.read_bytes(), b"new-fdkaac")
        self.assertEqual(self.library.read_bytes(), b"new-libfdk")
        self.assertEqual(stat.S_IMODE(self.binary.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.library.stat().st_mode), 0o644)
        self.assertEqual(unrelated.read_text(), "keep")
        protected_validation = next(
            call for call in self.calls if call[0] == "validate_prefix"
        )
        self.assertIn("/usr/local/.isadoraair-fdkaac-e4-", str(protected_validation[2]))
        checkpoints = [call[1] for call in self.calls if call[0] == "checkpoint"]
        self.assertLess(
            checkpoints.index("after_library_publication"),
            checkpoints.index("after_binary_publication"),
        )
        self.assertLess(
            checkpoints.index("after_binary_publication"), checkpoints.index("after_ldconfig")
        )
        self.assertFalse(any(path.name.startswith(".isadoraair-fdkaac-e4-") for path in (
            self.target / "usr" / "local"
        ).iterdir()))

    def test_explicit_correct_preparer_owner_is_accepted(self):
        result = self.provisioner().publish(
            prepared_root=self.prepared,
            expected_preparer_uid=os.geteuid(),
        )
        self.assertFalse(result.no_op)
        self.assertEqual(result.evidence.components["fdkaac"].status, STATUS_PASS)

    def test_wrong_prepared_root_owner_is_rejected_before_protected_copy(self):
        with self.assertRaisesRegex(RuntimeProvisioningError, "unexpected owner"):
            self.provisioner().publish(
                prepared_root=self.prepared,
                expected_preparer_uid=os.geteuid() + 1,
            )
        self.assertFalse(self.binary.exists())
        self.assertFalse(any(call[0] == "validate_prefix" for call in self.calls))

    def test_wrong_receipt_owner_is_rejected_before_protected_copy(self):
        receipt = self.prepared / "prepared-native.json"
        original_fstat = os.fstat

        def fstat_with_wrong_receipt_owner(descriptor):
            metadata = original_fstat(descriptor)
            try:
                opened_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            except OSError:
                return metadata
            if opened_path == receipt:
                return with_uid(metadata, os.geteuid() + 1)
            return metadata

        with patch("isadoraair.runtime_native.os.fstat", side_effect=fstat_with_wrong_receipt_owner):
            with self.assertRaisesRegex(RuntimeProvisioningError, "receipt.*owner"):
                self.provisioner().publish(
                    prepared_root=self.prepared,
                    expected_preparer_uid=os.geteuid(),
                )
        self.assertFalse(self.binary.exists())
        self.assertFalse(any(call[0] == "validate_prefix" for call in self.calls))

    def test_wrong_artifact_owner_is_rejected_before_protected_copy(self):
        artifact = self.prepared / "prefix" / "bin" / "fdkaac"
        original_lstat = Path.lstat

        def lstat_with_wrong_artifact_owner(path):
            metadata = original_lstat(path)
            if path == artifact:
                return with_uid(metadata, os.geteuid() + 1)
            return metadata

        with patch.object(
            Path, "lstat", autospec=True, side_effect=lstat_with_wrong_artifact_owner
        ):
            with self.assertRaisesRegex(RuntimeProvisioningError, "unexpected owner"):
                self.provisioner().publish(
                    prepared_root=self.prepared,
                    expected_preparer_uid=os.geteuid(),
                )
        self.assertFalse(self.binary.exists())
        self.assertFalse(any(call[0] == "validate_prefix" for call in self.calls))

    def test_shared_writable_prepared_root_is_rejected(self):
        self.prepared.chmod(0o770)
        with self.assertRaisesRegex(RuntimeProvisioningError, "group/world writable"):
            self.provisioner().publish(
                prepared_root=self.prepared,
                expected_preparer_uid=os.geteuid(),
            )
        self.assertFalse(self.binary.exists())

    def test_shared_writable_receipt_and_artifact_are_rejected(self):
        candidates = (
            self.prepared / "prepared-native.json",
            self.prepared / "prefix" / "bin" / "fdkaac",
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate.name):
                original_mode = stat.S_IMODE(candidate.stat().st_mode)
                candidate.chmod(original_mode | 0o020)
                try:
                    with self.assertRaisesRegex(
                        RuntimeProvisioningError, "group/world.writable"
                    ):
                        self.provisioner().publish(
                            prepared_root=self.prepared,
                            expected_preparer_uid=os.geteuid(),
                        )
                finally:
                    candidate.chmod(original_mode)
                self.assertFalse(self.binary.exists())

    def test_forged_receipt_uid_cannot_select_the_trusted_identity(self):
        receipt = self.prepared / "prepared-native.json"
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["preparer_uid"] = os.geteuid() + 1
        receipt.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        receipt.chmod(0o600)
        with self.assertRaisesRegex(RuntimeProvisioningError, "disagrees with its receipt"):
            self.provisioner().publish(
                prepared_root=self.prepared,
                expected_preparer_uid=os.geteuid(),
            )
        self.assertFalse(self.binary.exists())

    def test_mutated_prepared_artifact_is_rejected_before_canonical_mutation(self):
        (self.prepared / "prefix" / "bin" / "fdkaac").write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeProvisioningError, "receipt"):
            self.provisioner().publish(prepared_root=self.prepared)
        self.assertFalse(self.binary.exists())

    def test_symlinked_prepared_artifact_parent_is_rejected(self):
        binary = self.prepared / "prefix" / "bin" / "fdkaac"
        outside = self.root / "prepared-bin"
        binary.parent.rename(outside)
        binary.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeProvisioningError, "symlink"):
            self.provisioner().publish(prepared_root=self.prepared)
        self.assertFalse(self.binary.exists())

    def test_symlinked_canonical_target_and_parent_fail_closed(self):
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        self.binary.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeProvisioningError, "symlink"):
            self.provisioner().publish(prepared_root=self.prepared)
        self.binary.unlink()
        shutil_target = self.binary.parent
        shutil_target.rmdir()
        shutil_target.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeProvisioningError, "symlink"):
            self.provisioner().publish(prepared_root=self.prepared)

    def test_canonical_publish_requires_root_but_custom_root_does_not(self):
        canonical = NativeRuntimeProvisioner(
            requirements=requirements(),
            product_manifest=self.product,
            target_root="/",
            seams=self.seams(),
        )
        with patch("isadoraair.runtime_native.os.geteuid", return_value=12345):
            with self.assertRaisesRegex(RuntimeProvisioningError, "root privileges"):
                canonical.publish(
                    prepared_root=self.prepared,
                    expected_preparer_uid=12345,
                )

    def test_authoritative_no_op_under_lock_does_not_inspect_sources_or_publish(self):
        validate = lambda manifest, resolved: evidence(resolved, fdkaac_status=STATUS_PASS)
        provisioner = self.provisioner(seams=self.seams(validate=validate))
        canonical_local = self.target / "usr" / "local"
        (canonical_local / "lib").rmdir()
        (canonical_local / "bin").rmdir()
        canonical_local.rmdir()
        (self.target / "usr").rmdir()
        result = provisioner.publish(prepared_root=self.root / "missing")
        self.assertTrue(result.no_op)
        self.assertEqual(result.changed_components, ())
        self.assertFalse(canonical_local.exists())
        self.assertFalse(any(call[0] in {"build", "validate_prefix", "ldconfig"} for call in self.calls))

    def test_existing_canonical_state_is_restored_at_each_failure_boundary(self):
        for failure in (
            "after_library_publication",
            "after_binary_publication",
            "after_ldconfig",
            "after_native_final_acceptance",
        ):
            with self.subTest(failure=failure):
                self.binary.write_bytes(b"old-fdkaac")
                self.binary.chmod(0o755)
                self.library.write_bytes(b"old-libfdk")
                self.library.chmod(0o644)
                old_marker = digest(b"old-fdkaac")

                def validate(manifest, resolved):
                    binary = Path(manifest["components"]["fdkaac"]["runtime"]["binary"])
                    if binary.is_file() and binary.read_bytes() == b"new-fdkaac":
                        status = STATUS_PASS
                        marker = digest(b"new-fdkaac")
                    else:
                        status = STATUS_FAIL
                        marker = old_marker
                    return evidence(resolved, fdkaac_status=status, marker=marker)

                def checkpoint(name):
                    if name == failure:
                        raise RuntimeProvisioningError(f"fail {failure}")

                seams = self.seams(validate=validate, checkpoint=checkpoint)
                with self.assertRaisesRegex(RuntimeProvisioningError, f"fail {failure}"):
                    self.provisioner(seams=seams).publish(prepared_root=self.prepared)
                self.assertEqual(self.binary.read_bytes(), b"old-fdkaac")
                self.assertEqual(self.library.read_bytes(), b"old-libfdk")

    def test_absent_canonical_state_is_restored_after_failure(self):
        def checkpoint(name):
            if name == "after_binary_publication":
                raise RuntimeProvisioningError("publish failed")

        with self.assertRaisesRegex(RuntimeProvisioningError, "publish failed"):
            self.provisioner(seams=self.seams(checkpoint=checkpoint)).publish(
                prepared_root=self.prepared
            )
        self.assertFalse(self.binary.exists())
        self.assertFalse(self.library.exists())

    def test_rollback_failure_preserves_original_as_cause(self):
        self.binary.write_bytes(b"old-fdkaac")
        self.binary.chmod(0o755)
        self.library.write_bytes(b"old-libfdk")
        self.library.chmod(0o644)

        def checkpoint(name):
            if name == "after_binary_publication":
                raise RuntimeProvisioningError("original failure")

        ldconfig_calls = 0

        def fail_rollback_ldconfig(root):
            nonlocal ldconfig_calls
            ldconfig_calls += 1
            if ldconfig_calls > 0:
                raise RuntimeProvisioningError("rollback ldconfig failed")

        def validate(manifest, resolved):
            binary = Path(manifest["components"]["fdkaac"]["runtime"]["binary"])
            status = (
                STATUS_PASS
                if binary.is_file() and binary.read_bytes() == b"new-fdkaac"
                else STATUS_FAIL
            )
            return evidence(resolved, fdkaac_status=status)

        seams = self.seams(
            checkpoint=checkpoint,
            ldconfig=fail_rollback_ldconfig,
            validate=validate,
        )
        with self.assertRaisesRegex(RuntimeProvisioningError, "rollback failed") as caught:
            self.provisioner(seams=seams).publish(prepared_root=self.prepared)
        self.assertIn("original failure", str(caught.exception.__cause__))


class EnsureNoncanonicalPublicationDirectoriesUnitTests(SimpleTestCase):
    """Direct unit coverage for _ensure_noncanonical_publication_directories
    itself -- the E7C fix for the real E8 failure (`CommandError: directory
    is unavailable: /tmp/kkb/usr/local`): a noncanonical --target-root's
    /usr/local skeleton is nothing upstream creates, and _preflight_publish
    previously required it to already exist for BOTH canonical and
    noncanonical roots alike."""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="isadoraair-e7c-skeleton-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _targets(self, root=None):
        root = root or self.root
        local_root = root / "usr" / "local"
        return local_root, local_root / "bin", local_root / "lib"

    def test_refuses_the_canonical_root_outright(self):
        with self.assertRaisesRegex(RuntimeProvisioningError, "canonical root"):
            _ensure_noncanonical_publication_directories(Path("/"), Path("/usr/local"))

    def test_creates_the_full_skeleton_at_fixed_mode_from_empty(self):
        local_root, bin_dir, lib_dir = self._targets()
        _ensure_noncanonical_publication_directories(self.root, local_root, bin_dir, lib_dir)
        for directory in (self.root / "usr", local_root, bin_dir, lib_dir):
            self.assertTrue(directory.is_dir())
            self.assertFalse(directory.is_symlink())
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
            self.assertEqual(directory.stat().st_uid, os.geteuid())

    def test_is_idempotent_on_an_already_established_skeleton(self):
        local_root, bin_dir, lib_dir = self._targets()
        _ensure_noncanonical_publication_directories(self.root, local_root, bin_dir, lib_dir)
        _ensure_noncanonical_publication_directories(self.root, local_root, bin_dir, lib_dir)  # no raise
        self.assertTrue(bin_dir.is_dir())

    def test_preexisting_directory_mode_is_validated_not_mutated(self):
        (self.root / "usr").mkdir(mode=0o750)
        os.chmod(self.root / "usr", 0o750)  # mkdir's mode is subject to umask
        local_root, bin_dir, lib_dir = self._targets()
        _ensure_noncanonical_publication_directories(self.root, local_root, bin_dir, lib_dir)
        self.assertEqual(stat.S_IMODE((self.root / "usr").stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(bin_dir.stat().st_mode), 0o755)  # newly created gets the fixed mode

    def test_rejects_a_symlink_at_every_ancestor_and_leaf(self):
        for relative in ("usr", "usr/local", "usr/local/bin", "usr/local/lib"):
            with self.subTest(component=relative):
                trial_root = self.root / f"trial-{relative.replace('/', '-')}"
                trial_root.mkdir()
                real_target = trial_root / "real-target"
                real_target.mkdir()
                symlink_path = trial_root
                for part in relative.split("/")[:-1]:
                    symlink_path = symlink_path / part
                    symlink_path.mkdir(exist_ok=True)
                symlink_path = symlink_path / relative.split("/")[-1]
                symlink_path.symlink_to(real_target, target_is_directory=True)
                local_root, bin_dir, lib_dir = self._targets(trial_root)
                with self.assertRaisesRegex(RuntimeProvisioningError, "symlink"):
                    _ensure_noncanonical_publication_directories(trial_root, local_root, bin_dir, lib_dir)

    def test_rejects_a_non_directory_collision(self):
        (self.root / "usr").mkdir()
        (self.root / "usr" / "local").write_text("not a directory", encoding="utf-8")
        local_root, bin_dir, lib_dir = self._targets()
        with self.assertRaisesRegex(RuntimeProvisioningError, "not a directory"):
            _ensure_noncanonical_publication_directories(self.root, local_root, bin_dir, lib_dir)

    def test_rejects_a_target_outside_root(self):
        """Direct proof of host isolation: a target that escapes `root`
        (e.g. the REAL /usr/local) is refused before anything is touched
        -- never silently redirected, never operated on for real."""
        with self.assertRaisesRegex(RuntimeProvisioningError, "escapes"):
            _ensure_noncanonical_publication_directories(self.root, Path("/usr/local"))
        self.assertFalse((self.root / "usr").exists())


class NativePublicationNoncanonicalSkeletonTests(NativeFixture):
    """End-to-end coverage through the real publish() path -- deliberately
    WITHOUT NativePublicationTests' own setUp, which always pre-creates
    usr/local/{bin,lib} and so never exercised the real E8 failure."""

    def setUp(self):
        super().setUp()
        self.prepare()
        self.calls.clear()

    @property
    def binary(self):
        return self.target / "usr" / "local" / "bin" / "fdkaac"

    @property
    def library(self):
        version = self.product["components"]["fdkaac"]["runtime"]["libfdk_aac_version"]
        return self.target / "usr" / "local" / "lib" / f"libfdk-aac.so.{version}"

    def test_publish_against_a_completely_empty_target_root_succeeds(self):
        self.assertFalse((self.target / "usr").exists())
        result = self.provisioner().publish(prepared_root=self.prepared)
        self.assertEqual(result.changed_components, ("fdkaac",))
        self.assertEqual(result.evidence.components["fdkaac"].status, STATUS_PASS)
        for directory in (
            self.target / "usr",
            self.target / "usr" / "local",
            self.target / "usr" / "local" / "bin",
            self.target / "usr" / "local" / "lib",
        ):
            self.assertTrue(directory.is_dir())
            self.assertFalse(directory.is_symlink())
        self.assertEqual(self.binary.read_bytes(), b"new-fdkaac")
        self.assertEqual(self.library.read_bytes(), b"new-libfdk")

    def test_partially_existing_skeleton_is_completed(self):
        (self.target / "usr").mkdir()  # only the first component pre-exists
        result = self.provisioner().publish(prepared_root=self.prepared)
        self.assertFalse(result.no_op)
        self.assertTrue((self.target / "usr" / "local" / "bin").is_dir())
        self.assertTrue((self.target / "usr" / "local" / "lib").is_dir())

    def test_preexisting_unrelated_sibling_directory_is_untouched(self):
        (self.target / "usr").mkdir(mode=0o755)
        share = self.target / "usr" / "share"
        share.mkdir()
        share_marker = share / "keep"
        share_marker.write_text("keep", encoding="utf-8")
        self.provisioner().publish(prepared_root=self.prepared)
        self.assertEqual(share_marker.read_text(), "keep")

    def test_symlinked_usr_fails_closed_and_publishes_nothing(self):
        outside = self.root / "outside-usr"
        outside.mkdir()
        (self.target / "usr").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeProvisioningError, "symlink"):
            self.provisioner().publish(prepared_root=self.prepared)
        self.assertFalse(self.binary.exists())
        self.assertFalse(any(call[0] in {"build", "validate_prefix", "ldconfig"} for call in self.calls))

    def test_symlinked_usr_local_fails_closed(self):
        (self.target / "usr").mkdir()
        outside = self.root / "outside-local"
        outside.mkdir()
        (self.target / "usr" / "local").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeProvisioningError, "symlink"):
            self.provisioner().publish(prepared_root=self.prepared)
        self.assertFalse(self.binary.exists())

    def test_symlinked_usr_local_bin_fails_closed(self):
        (self.target / "usr" / "local").mkdir(parents=True)
        outside = self.root / "outside-bin"
        outside.mkdir()
        (self.target / "usr" / "local" / "bin").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeProvisioningError, "symlink"):
            self.provisioner().publish(prepared_root=self.prepared)
        self.assertFalse(self.binary.exists())

    def test_symlinked_usr_local_lib_fails_closed(self):
        (self.target / "usr" / "local" / "bin").mkdir(parents=True)
        outside = self.root / "outside-lib"
        outside.mkdir()
        (self.target / "usr" / "local" / "lib").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeProvisioningError, "symlink"):
            self.provisioner().publish(prepared_root=self.prepared)
        self.assertFalse(self.library.exists())

    def test_non_directory_collision_fails_closed(self):
        (self.target / "usr").mkdir()
        (self.target / "usr" / "local").write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeProvisioningError, "not a directory"):
            self.provisioner().publish(prepared_root=self.prepared)
        self.assertFalse(self.binary.exists())

    def test_target_root_not_owned_by_caller_fails_closed_before_any_creation(self):
        with patch("isadoraair.runtime_native.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(RuntimeProvisioningError, "owned by the caller"):
                self.provisioner().publish(prepared_root=self.prepared)
        self.assertFalse((self.target / "usr").exists())

    def test_target_root_not_writable_fails_closed_before_any_creation(self):
        os.chmod(self.target, 0o500)
        self.addCleanup(os.chmod, self.target, 0o700)
        with self.assertRaisesRegex(RuntimeProvisioningError, "not writable"):
            self.provisioner().publish(prepared_root=self.prepared)
        self.assertFalse((self.target / "usr").exists())

    def test_skeleton_created_before_a_later_failure_is_not_rolled_back(self):
        """Documented policy: the confined directory skeleton is not part
        of the two-file (binary + library) publication transaction --
        empty 0755 directories under a disposable/noncanonical target
        root are harmless, contain nothing sensitive, and are left in
        place exactly like every other _mkdir_controlled call elsewhere
        in this codebase is never rolled back on a later failure."""
        def checkpoint(name):
            if name == "after_binary_publication":
                raise RuntimeProvisioningError("publish failed after directory creation")

        with self.assertRaisesRegex(RuntimeProvisioningError, "publish failed"):
            self.provisioner(seams=self.seams(checkpoint=checkpoint)).publish(
                prepared_root=self.prepared
            )
        # The two-file transaction WAS rolled back (pre-existing E4 contract)...
        self.assertFalse(self.binary.exists())
        # ...but the skeleton directories this fix created remain.
        for directory in (
            self.target / "usr",
            self.target / "usr" / "local",
            self.target / "usr" / "local" / "bin",
            self.target / "usr" / "local" / "lib",
        ):
            self.assertTrue(directory.is_dir())

    def test_a_preexisting_skeleton_directory_is_never_removed_on_failure(self):
        (self.target / "usr" / "local" / "bin").mkdir(parents=True)
        (self.target / "usr" / "local" / "lib").mkdir()

        def checkpoint(name):
            if name == "after_binary_publication":
                raise RuntimeProvisioningError("publish failed")

        with self.assertRaisesRegex(RuntimeProvisioningError, "publish failed"):
            self.provisioner(seams=self.seams(checkpoint=checkpoint)).publish(
                prepared_root=self.prepared
            )
        self.assertTrue((self.target / "usr" / "local" / "bin").is_dir())
        self.assertTrue((self.target / "usr" / "local" / "lib").is_dir())


class CanonicalPublicationNeverAutoCreatesTests(NativeFixture):
    """Canonical "/" must keep requiring the trusted system hierarchy to
    already exist -- native publication must never manufacture trusted
    system ancestors on the real host. Real "/" cannot safely be
    exercised end-to-end in a test process, so this is proven two ways:
    a static guard on _preflight_publish's own source, and a functional
    check that the canonical branch fails before root privileges even
    let it reach directory validation."""

    def test_preflight_publish_only_calls_the_creation_helper_for_noncanonical_roots(self):
        source = inspect.getsource(NativeRuntimeProvisioner._preflight_publish)
        guard_index = source.index('if root != Path("/"):')
        call_index = source.index("_ensure_noncanonical_publication_directories(")
        self.assertGreater(
            call_index, guard_index,
            "_ensure_noncanonical_publication_directories must be called only inside "
            'the "if root != Path(\\"/\\")" branch',
        )
        # And nowhere else in the method, unconditionally.
        self.assertEqual(source.count("_ensure_noncanonical_publication_directories("), 1)

    def test_canonical_root_still_requires_root_privileges_before_anything_else(self):
        canonical = NativeRuntimeProvisioner(
            requirements=requirements(),
            product_manifest=self.product,
            target_root="/",
            seams=self.seams(),
        )
        with patch("isadoraair.runtime_native.os.geteuid", return_value=12345):
            with self.assertRaisesRegex(RuntimeProvisioningError, "root privileges"):
                canonical.publish(prepared_root=self.prepared, expected_preparer_uid=12345)

    def test_canonical_root_with_missing_usr_local_fails_closed_not_auto_created(self):
        """Simulates canonical "/" whose /usr/local does not exist, purely
        via mocking (never touching the real host filesystem, and calling
        _preflight_publish directly -- the full publish() transaction
        also acquires a real cross-process lock that itself writes under
        the real canonical runtime root, which a non-root test process
        cannot safely simulate its way past). Proves the canonical branch
        still hits _assert_existing_directory's fail-closed path rather
        than silently creating anything."""
        canonical = NativeRuntimeProvisioner(
            requirements=requirements(),
            product_manifest=self.product,
            target_root="/",
            seams=self.seams(),
        )
        real_lstat = Path.lstat

        def fake_root_lstat(path):
            if path == Path("/"):
                return real_lstat(Path("/"))
            if str(path) in ("/usr/local", "/usr"):
                raise FileNotFoundError(path)
            return real_lstat(path)

        with patch("isadoraair.runtime_native.os.geteuid", return_value=0), patch(
            "isadoraair.runtime_native.os.access", return_value=True
        ):
            with patch.object(Path, "lstat", autospec=True, side_effect=fake_root_lstat):
                with self.assertRaisesRegex(RuntimeProvisioningError, "directory is unavailable"):
                    canonical._preflight_publish()


class NativeLockingTests(NativeFixture):
    def test_native_uses_real_common_cross_process_provision_lock(self):
        context = multiprocessing.get_context("fork")
        first_entered = context.Event()
        second_entered = context.Event()
        release = context.Event()
        first = context.Process(
            target=_lock_worker,
            args=(self.target, self.product, first_entered, release),
        )
        second = context.Process(
            target=_lock_worker,
            args=(self.target, self.product, second_entered),
        )
        first.start()
        self.addCleanup(lambda: first.is_alive() and first.terminate())
        self.assertTrue(first_entered.wait(5))
        second.start()
        self.addCleanup(lambda: second.is_alive() and second.terminate())
        self.assertFalse(second_entered.wait(0.2))
        release.set()
        self.assertTrue(second_entered.wait(5))
        first.join(5)
        second.join(5)
        self.assertEqual((first.exitcode, second.exitcode), (0, 0))


class NativeCommandTests(TestCase):
    def test_native_plan_is_explicit_and_json_safe(self):
        stdout = io.StringIO()
        with patch(
            "monitoring.management.commands.provision_runtime_components.NativeRuntimeProvisioner"
        ) as provisioner:
            plan = provisioner.return_value.plan.return_value
            plan.to_json.return_value = json.dumps({"component": "fdkaac", "ready": True})
            plan.ready = True
            call_command(
                "provision_runtime_components",
                "--fdkaac",
                "--plan",
                "--json",
                stdout=stdout,
            )
        self.assertEqual(json.loads(stdout.getvalue())["component"], "fdkaac")

    def test_native_mutation_requires_explicit_phase_and_paths(self):
        with self.assertRaises(CommandError):
            call_command("provision_runtime_components", "--fdkaac", "--apply")
        with self.assertRaises(CommandError):
            call_command("provision_runtime_components", "--prepare-fdkaac")

    def test_canonical_publish_requires_explicit_trusted_preparer_uid(self):
        with patch(
            "monitoring.management.commands.provision_runtime_components.NativeRuntimeProvisioner"
        ) as provisioner:
            with self.assertRaisesRegex(CommandError, "trusted-preparer-uid"):
                call_command(
                    "provision_runtime_components",
                    "--publish-fdkaac",
                    "--prepared-native-root",
                    self.id(),
                )
        provisioner.return_value.publish.assert_not_called()

    def test_trusted_preparer_uid_rejects_non_numeric_and_negative_values(self):
        for value in ("not-a-uid", "-1"):
            with self.subTest(value=value), self.assertRaises(CommandError):
                call_command(
                    "provision_runtime_components",
                    "--publish-fdkaac",
                    "--prepared-native-root",
                    self.id(),
                    "--trusted-preparer-uid",
                    value,
                )


class RecoveryPayloadNativeRequirementsTests(NativeFixture):
    """Runtime Foundation E7B: --recovery-payload's fdkaac requiredness
    is never re-derived from live station configuration (Runtime
    Foundation E1) -- the payload's own native_fdkaac component already
    passed E7's fail-closed load, which only happens because an
    operator's recovery-component policy justified including it. See
    monitoring/management/commands/provision_runtime_components.py's
    _requirements_for_recovery_native."""

    def _payload(self) -> Path:
        payload_root = self.root / "recovery-payload"
        RuntimeRecoveryBuilder(product_manifest=self.product).apply(
            native_source_dir=self.source, output=payload_root, payload_id="p-native"
        )
        return payload_root

    def test_requirements_marks_only_fdkaac_required(self):
        requirements = _requirements_for_recovery_native()
        self.assertTrue(requirements.components["fdkaac"].required)
        self.assertEqual(requirements.components["fdkaac"].reasons, (RECOVERY_PAYLOAD_REASON,))
        self.assertFalse(requirements.components["kokoro"].required)
        self.assertFalse(requirements.components["piper"].required)

    def test_synthesized_requirements_drive_a_real_prepare(self):
        payload_root = self._payload()
        payload = load_recovery_payload(payload_root, product_manifest=self.product)
        provisioner = NativeRuntimeProvisioner(
            requirements=_requirements_for_recovery_native(),
            product_manifest=self.product,
            target_root=self.target,
            project_root=Path(__file__).parents[2],
            seams=self.seams(),
        )
        result = provisioner.prepare(
            source_dir=payload.native_source.source_dir, prepared_root=self.prepared
        )
        self.assertFalse(result.no_op)
        self.assertIsNotNone(result.prepared_root)
        self.assertTrue((Path(result.prepared_root) / "prefix" / "bin" / "fdkaac").is_file())

    def test_recovery_payload_cli_flag_bypasses_e1_and_wires_the_source_dir(self):
        payload_root = self._payload()
        with patch(
            "monitoring.management.commands.provision_runtime_components.NativeRuntimeProvisioner"
        ) as provisioner_cls, patch(
            "monitoring.management.commands.provision_runtime_components.resolve_current_runtime_requirements"
        ) as resolver, patch(
            "monitoring.management.commands.provision_runtime_components.load_runtime_components",
            return_value=self.product,
        ):
            plan = provisioner_cls.return_value.plan.return_value
            plan.ready = True
            plan.to_json.return_value = json.dumps({"ready": True})
            call_command(
                "provision_runtime_components",
                "--fdkaac",
                "--recovery-payload",
                str(payload_root),
                "--plan",
                "--json",
                stdout=io.StringIO(),
            )
        resolver.assert_not_called()
        _, plan_kwargs = provisioner_cls.return_value.plan.call_args
        self.assertEqual(Path(plan_kwargs["source_dir"]), payload_root / "native" / "fdkaac")
        _, ctor_kwargs = provisioner_cls.call_args
        self.assertTrue(ctor_kwargs["requirements"].components["fdkaac"].required)

    def test_recovery_payload_and_native_source_dir_together_is_rejected(self):
        payload_root = self._payload()
        with patch(
            "monitoring.management.commands.provision_runtime_components.load_runtime_components",
            return_value=self.product,
        ), self.assertRaisesRegex(CommandError, "do not also pass --native-source-dir"):
            call_command(
                "provision_runtime_components",
                "--fdkaac",
                "--recovery-payload",
                str(payload_root),
                "--native-source-dir",
                str(self.source),
                "--plan",
            )

    def test_recovery_payload_without_native_component_is_rejected(self):
        # This fixture's own payload is native_fdkaac-only -- asking the
        # CLI for TTS provisioning against it (no --fdkaac) must fail
        # closed rather than silently treating "not in the payload" as
        # "not needed".
        payload_root = self._payload()
        with patch(
            "monitoring.management.commands.provision_runtime_components.load_runtime_components",
            return_value=self.product,
        ), self.assertRaisesRegex(CommandError, "no tts component"):
            call_command(
                "provision_runtime_components",
                "--recovery-payload",
                str(payload_root),
                "--target-root",
                str(self.target),
                "--plan",
            )
