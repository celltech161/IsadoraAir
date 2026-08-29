"""Runtime Foundation E7A -- disaster-recovery runtime payload contract,
builder, and validator tests.

Every scenario here uses a disposable temporary directory and an
in-memory, deepcopy-patched product manifest -- never a real
/opt/isadoraair-runtime, /var/lib/isadoraair/tts, network fetch, or the
real production Piper/Kokoro material. No test needs the internet, a
real transmitter, real SFTP, or any production path."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from isadoraair.runtime_bundle import (
    BUNDLE_FILENAME,
    BUNDLE_SCHEMA_VERSION,
    current_platform_contract,
    product_contract_digest,
)
from isadoraair.runtime_components import load_runtime_components
from isadoraair.runtime_recovery import (
    PIPER_FRESHNESS_CURRENT,
    PIPER_FRESHNESS_NOT_CHECKED,
    PIPER_FRESHNESS_STALE,
    RECOVERY_MANIFEST_FILENAME,
    RESULT_FAIL,
    RESULT_PASS,
    STATE_ABSENT,
    STATE_INVALID,
    STATE_PRESENT,
    RuntimeRecoveryBuilder,
    RuntimeRecoveryError,
    load_recovery_payload,
    piper_selection_digest,
    validate_recovery_payload,
)
from isadoraair.runtime_requirements import (
    ComponentRequirement,
    PiperModelRequirement,
    RuntimeRequirements,
)


def _write(path: Path, content: bytes, mode: int = 0o644) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)
    return hashlib.sha256(content).hexdigest()


class RecoveryFixture(SimpleTestCase):
    """Builds a disposable product manifest plus a real, valid E3 bundle
    and native-source directory the recovery payload builder can wrap.
    """

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="isadoraair-e7a-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = deepcopy(load_runtime_components())

        # ---- Kokoro assets + wheel closure -> disposable fixture files ----
        bundle_dir = self.root / "src-bundle"
        wheel_hash = _write(
            bundle_dir / "kokoro" / "wheelhouse" / "kokoro_onnx-0.4.7-py3-none-any.whl", b"FAKEWHEEL"
        )
        lock_hash = _write(
            bundle_dir / "kokoro" / "requirements.lock",
            f"kokoro-onnx==0.4.7 --hash=sha256:{wheel_hash}\n".encode(),
        )
        prov_hash = _write(bundle_dir / "kokoro" / "provenance" / "LICENSE", b"MIT license text")
        self.model_hash = _write(bundle_dir / "kokoro" / "assets" / "kokoro-v1.0.onnx", b"FAKEMODEL")
        self.voices_hash = _write(bundle_dir / "kokoro" / "assets" / "voices-v1.0.bin", b"FAKEVOICES")

        kokoro = self.manifest["components"]["kokoro"]
        kokoro["assets"]["model"].update({"filename": "kokoro-v1.0.onnx", "sha256": self.model_hash})
        kokoro["assets"]["voices"].update({"filename": "voices-v1.0.bin", "sha256": self.voices_hash})
        kokoro["runtime"]["packages"] = {"kokoro-onnx": "0.4.7"}

        # ---- Piper: one station-selected model -----------------------------
        # Staged OUTSIDE bundle_dir, on purpose: E3's strict bundle-tree
        # closure means any file actually present under bundle_dir must be
        # declared -- these are only materialized into bundle_dir by
        # write_tts_bundle(include_piper=True), never unconditionally here,
        # so a kokoro-only bundle's tree stays exactly kokoro-only.
        piper_staging = self.root / "piper-staging"
        self.piper_model_hash = _write(piper_staging / "en_us-test.onnx", b"FAKEPIPERMODEL")
        self.piper_config_hash = _write(piper_staging / "en_us-test.onnx.json", b'{"fake": "config"}')
        piper_wheel_hash = _write(
            piper_staging / "piper_tts-1.4.2-py3-none-any.whl", b"FAKEPIPERWHEEL"
        )
        piper_lock_hash = _write(
            piper_staging / "requirements.lock",
            f"piper-tts==1.4.2 --hash=sha256:{piper_wheel_hash}\n".encode(),
        )
        piper_prov_hash = _write(piper_staging / "LICENSE", b"MIT license text")
        self._piper_staging = piper_staging
        self.manifest["components"]["piper"]["runtime"]["packages"] = {"piper-tts": "1.4.2"}

        # ---- native fdkaac source archives -----------------------------
        native_dir = self.root / "src-native"
        archives = self.manifest["components"]["fdkaac"]["source_archives"]
        for name, expected in archives.items():
            content = f"FAKE ARCHIVE {name}".encode()
            h = _write(native_dir / expected["filename"], content)
            expected["bytes"] = len(content)
            expected["sha256"] = h
        self.native_source_dir = native_dir

        self._bundle_dir = bundle_dir
        self._wheel_hash = wheel_hash
        self._lock_hash = lock_hash
        self._prov_hash = prov_hash
        self._piper_wheel_hash = piper_wheel_hash
        self._piper_lock_hash = piper_lock_hash
        self._piper_prov_hash = piper_prov_hash

    def _bundle_manifest_dict(self, *, include_piper: bool = False, bundle_id: str = "test-bundle") -> dict:
        components = {
            "kokoro": {
                "lock": {"filename": "kokoro/requirements.lock", "sha256": self._lock_hash},
                "wheelhouse": "kokoro/wheelhouse",
                "wheels": [
                    {
                        "filename": "kokoro_onnx-0.4.7-py3-none-any.whl",
                        "package": "kokoro-onnx",
                        "version": "0.4.7",
                        "sha256": self._wheel_hash,
                    }
                ],
                "provenance": [{"filename": "kokoro/provenance/LICENSE", "sha256": self._prov_hash}],
                "assets": {
                    "model": {"filename": "kokoro/assets/kokoro-v1.0.onnx", "sha256": self.model_hash},
                    "voices": {"filename": "kokoro/assets/voices-v1.0.bin", "sha256": self.voices_hash},
                },
            },
        }
        if include_piper:
            components["piper"] = {
                "lock": {"filename": "piper/requirements.lock", "sha256": self._piper_lock_hash},
                "wheelhouse": "piper/wheelhouse",
                "wheels": [
                    {
                        "filename": "piper_tts-1.4.2-py3-none-any.whl",
                        "package": "piper-tts",
                        "version": "1.4.2",
                        "sha256": self._piper_wheel_hash,
                    }
                ],
                "provenance": [{"filename": "piper/provenance/LICENSE", "sha256": self._piper_prov_hash}],
                "models": [
                    {
                        "model_id": "en_us-test",
                        "model": {"filename": "piper/models/en_us-test.onnx", "sha256": self.piper_model_hash},
                        "config": {
                            "filename": "piper/models/en_us-test.onnx.json",
                            "sha256": self.piper_config_hash,
                        },
                        "language": "en-us",
                        "sample_rate_hz": 22050,
                    }
                ],
            }
        return {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "bundle_id": bundle_id,
            "platform": current_platform_contract(),
            "product_contract_sha256": product_contract_digest(self.manifest),
            "components": components,
        }

    def write_tts_bundle(self, *, include_piper: bool = False, bundle_id: str = "test-bundle") -> Path:
        if include_piper:
            _write(self._bundle_dir / "piper" / "models" / "en_us-test.onnx", (self._piper_staging / "en_us-test.onnx").read_bytes())
            _write(
                self._bundle_dir / "piper" / "models" / "en_us-test.onnx.json",
                (self._piper_staging / "en_us-test.onnx.json").read_bytes(),
            )
            _write(
                self._bundle_dir / "piper" / "wheelhouse" / "piper_tts-1.4.2-py3-none-any.whl",
                (self._piper_staging / "piper_tts-1.4.2-py3-none-any.whl").read_bytes(),
            )
            _write(
                self._bundle_dir / "piper" / "requirements.lock",
                (self._piper_staging / "requirements.lock").read_bytes(),
            )
            _write(self._bundle_dir / "piper" / "provenance" / "LICENSE", (self._piper_staging / "LICENSE").read_bytes())
        bundle_manifest = self._bundle_manifest_dict(include_piper=include_piper, bundle_id=bundle_id)
        _write(self._bundle_dir / BUNDLE_FILENAME, json.dumps(bundle_manifest).encode())
        return self._bundle_dir

    def piper_requirement(self) -> RuntimeRequirements:
        model = PiperModelRequirement(
            model_id="en_us-test",
            model_filename="en_us-test.onnx",
            config_filename="en_us-test.onnx.json",
            model_sha256=self.piper_model_hash,
            config_sha256=self.piper_config_hash,
            language="en-us",
            sample_rate_hz=22050,
        )
        return RuntimeRequirements(
            components={
                "kokoro": ComponentRequirement("kokoro"),
                "piper": ComponentRequirement("piper", True, ("test",), piper_models=(model,)),
                "fdkaac": ComponentRequirement("fdkaac"),
            }
        )

    def builder(self) -> RuntimeRecoveryBuilder:
        return RuntimeRecoveryBuilder(product_manifest=self.manifest)


class HappyPathTests(RecoveryFixture):
    def test_valid_minimal_kokoro_capable_payload(self):
        tts_bundle = self.write_tts_bundle()
        output = self.root / "payload-kokoro"
        result = self.builder().apply(tts_bundle=tts_bundle, output=output, payload_id="p-kokoro")
        self.assertEqual(result.evidence.result, RESULT_PASS)
        self.assertEqual(result.evidence.components["tts"].state, STATE_PRESENT)
        self.assertEqual(result.evidence.components["native_fdkaac"].state, STATE_ABSENT)

    def test_valid_piper_containing_payload(self):
        tts_bundle = self.write_tts_bundle(include_piper=True)
        output = self.root / "payload-piper"
        result = self.builder().apply(
            tts_bundle=tts_bundle, output=output, payload_id="p-piper", piper_selection=self.piper_requirement()
        )
        self.assertEqual(result.evidence.result, RESULT_PASS)
        stored_digest = json.loads((output / RECOVERY_MANIFEST_FILENAME).read_text())["piper_selection_sha256"]
        self.assertEqual(stored_digest, piper_selection_digest(self.piper_requirement()))

    def test_valid_native_fdkaac_only_payload(self):
        output = self.root / "payload-native"
        result = self.builder().apply(native_source_dir=self.native_source_dir, output=output, payload_id="p-native")
        self.assertEqual(result.evidence.result, RESULT_PASS)
        self.assertEqual(result.evidence.components["tts"].state, STATE_ABSENT)
        self.assertEqual(result.evidence.components["native_fdkaac"].state, STATE_PRESENT)

    def test_combined_tts_and_native_payload(self):
        tts_bundle = self.write_tts_bundle()
        output = self.root / "payload-combined"
        result = self.builder().apply(
            tts_bundle=tts_bundle, native_source_dir=self.native_source_dir, output=output, payload_id="p-combo"
        )
        self.assertEqual(result.evidence.result, RESULT_PASS)
        self.assertEqual(result.evidence.components["tts"].state, STATE_PRESENT)
        self.assertEqual(result.evidence.components["native_fdkaac"].state, STATE_PRESENT)

    def test_deterministic_repeated_validation(self):
        tts_bundle = self.write_tts_bundle()
        output = self.root / "payload-repeat"
        self.builder().apply(tts_bundle=tts_bundle, output=output, payload_id="p-repeat")
        first = validate_recovery_payload(output, product_manifest=self.manifest)
        second = validate_recovery_payload(output, product_manifest=self.manifest)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.result, RESULT_PASS)


class IntegrityTests(RecoveryFixture):
    def setUp(self):
        super().setUp()
        self.tts_bundle = self.write_tts_bundle(include_piper=True)
        self.output = self.root / "payload"
        self.builder().apply(
            tts_bundle=self.tts_bundle,
            native_source_dir=self.native_source_dir,
            output=self.output,
            payload_id="p-integrity",
            piper_selection=self.piper_requirement(),
        )

    def _mutated_copy(self) -> Path:
        mutated = self.root / "mutated"
        shutil.copytree(self.output, mutated)
        return mutated

    def test_changed_wheel_is_invalid(self):
        mutated = self._mutated_copy()
        (mutated / "tts" / "kokoro" / "wheelhouse" / "kokoro_onnx-0.4.7-py3-none-any.whl").write_bytes(b"EVIL")
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertEqual(evidence.components["tts"].state, STATE_INVALID)

    def test_changed_kokoro_model_is_invalid(self):
        mutated = self._mutated_copy()
        (mutated / "tts" / "kokoro" / "assets" / "kokoro-v1.0.onnx").write_bytes(b"EVIL")
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertEqual(evidence.components["tts"].state, STATE_INVALID)

    def test_changed_piper_model_is_invalid(self):
        mutated = self._mutated_copy()
        (mutated / "tts" / "piper" / "models" / "en_us-test.onnx").write_bytes(b"EVIL")
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertEqual(evidence.components["tts"].state, STATE_INVALID)

    def test_changed_piper_config_is_invalid(self):
        mutated = self._mutated_copy()
        (mutated / "tts" / "piper" / "models" / "en_us-test.onnx.json").write_bytes(b'{"evil": true}')
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertEqual(evidence.components["tts"].state, STATE_INVALID)

    def test_changed_fdkaac_source_archive_is_invalid(self):
        mutated = self._mutated_copy()
        archive_name = self.manifest["components"]["fdkaac"]["source_archives"]["fdk-aac"]["filename"]
        (mutated / "native" / "fdkaac" / archive_name).write_bytes(b"EVIL")
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertEqual(evidence.components["native_fdkaac"].state, STATE_INVALID)
        # unrelated component still validates independently
        self.assertEqual(evidence.components["tts"].state, STATE_PRESENT)

    def test_missing_file_is_invalid(self):
        mutated = self._mutated_copy()
        (mutated / "tts" / "kokoro" / "provenance" / "LICENSE").unlink()
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertEqual(evidence.components["tts"].state, STATE_INVALID)

    def test_extra_file_where_prohibited_is_invalid(self):
        mutated = self._mutated_copy()
        (mutated / "native" / "fdkaac" / "stray.txt").write_text("nope")
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertEqual(evidence.components["native_fdkaac"].state, STATE_INVALID)

    def test_wrong_product_digest_is_invalid(self):
        wrong_manifest = deepcopy(self.manifest)
        wrong_manifest["components"]["fdkaac"]["runtime"]["fdkaac_version"] = "9.9.9"
        evidence = validate_recovery_payload(self.output, product_manifest=wrong_manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertIsNotNone(evidence.manifest_error)

    def test_wrong_station_requirement_digest_is_stale(self):
        current_digest = piper_selection_digest(self.piper_requirement())
        evidence = validate_recovery_payload(
            self.output, product_manifest=self.manifest, current_piper_selection_digest="0" * 64
        )
        self.assertEqual(evidence.piper_freshness.state, PIPER_FRESHNESS_STALE)
        self.assertEqual(evidence.result, RESULT_FAIL)
        # and the CORRECT current digest reports current, not stale
        fresh_evidence = validate_recovery_payload(
            self.output, product_manifest=self.manifest, current_piper_selection_digest=current_digest
        )
        self.assertEqual(fresh_evidence.piper_freshness.state, PIPER_FRESHNESS_CURRENT)
        self.assertEqual(fresh_evidence.result, RESULT_PASS)

    def test_not_checked_when_no_live_digest_supplied(self):
        evidence = validate_recovery_payload(self.output, product_manifest=self.manifest)
        self.assertEqual(evidence.piper_freshness.state, PIPER_FRESHNESS_NOT_CHECKED)
        self.assertEqual(evidence.result, RESULT_PASS)

    def test_wrong_platform_abi_is_invalid(self):
        mutated = self._mutated_copy()
        bundle_manifest_path = mutated / "tts" / BUNDLE_FILENAME
        data = json.loads(bundle_manifest_path.read_text())
        data["platform"]["architecture"] = "definitely-not-a-real-arch"
        content = json.dumps(data).encode()
        bundle_manifest_path.write_bytes(content)
        # The recovery manifest recorded the OLD bundle manifest_sha256 --
        # a platform edit is itself indistinguishable from tampering at
        # the recovery layer, and must be caught either way.
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertEqual(evidence.components["tts"].state, STATE_INVALID)


class FilesystemSafetyTests(RecoveryFixture):
    def setUp(self):
        super().setUp()
        self.tts_bundle = self.write_tts_bundle()
        self.output = self.root / "payload"
        self.builder().apply(tts_bundle=self.tts_bundle, output=self.output, payload_id="p-safety")

    def _mutated_copy(self) -> Path:
        mutated = self.root / "mutated"
        shutil.copytree(self.output, mutated)
        return mutated

    def test_symlink_inside_tts_bundle_is_rejected(self):
        mutated = self._mutated_copy()
        target = mutated / "tts" / "kokoro" / "assets" / "kokoro-v1.0.onnx"
        real_bytes = target.read_bytes()
        target.unlink()
        (mutated / "evil.bin").write_bytes(real_bytes)
        target.symlink_to(mutated / "evil.bin")
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertEqual(evidence.components["tts"].state, STATE_INVALID)

    def test_hardlink_inside_tts_bundle_is_rejected(self):
        mutated = self._mutated_copy()
        target = mutated / "tts" / "kokoro" / "assets" / "kokoro-v1.0.onnx"
        hardlink = mutated / "tts" / "kokoro" / "assets" / "hardlinked-extra.onnx"
        import os

        os.link(target, hardlink)
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertEqual(evidence.components["tts"].state, STATE_INVALID)

    def test_path_traversal_in_component_path_is_rejected(self):
        mutated = self._mutated_copy()
        recovery_path = mutated / RECOVERY_MANIFEST_FILENAME
        data = json.loads(recovery_path.read_text())
        data["components"]["tts"]["path"] = "../../../etc"
        recovery_path.write_text(json.dumps(data))
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertIn("confined relative path", evidence.manifest_error)

    def test_absolute_path_in_component_path_is_rejected(self):
        mutated = self._mutated_copy()
        recovery_path = mutated / RECOVERY_MANIFEST_FILENAME
        data = json.loads(recovery_path.read_text())
        data["components"]["tts"]["path"] = "/etc"
        recovery_path.write_text(json.dumps(data))
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)

    def test_dotdot_component_in_component_path_is_rejected(self):
        mutated = self._mutated_copy()
        recovery_path = mutated / RECOVERY_MANIFEST_FILENAME
        data = json.loads(recovery_path.read_text())
        data["components"]["tts"]["path"] = "tts/../../escape"
        recovery_path.write_text(json.dumps(data))
        evidence = validate_recovery_payload(mutated, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)

    def test_non_regular_file_manifest_is_rejected(self):
        import os

        broken = self.root / "broken-manifest"
        broken.mkdir()
        os.mkfifo(broken / RECOVERY_MANIFEST_FILENAME)
        evidence = validate_recovery_payload(broken, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)

    def test_payload_root_itself_symlinked_is_rejected(self):
        real_dir = self.root / "real-payload-target"
        shutil.copytree(self.output, real_dir)
        link = self.root / "payload-symlink"
        link.symlink_to(real_dir)
        evidence = validate_recovery_payload(link, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)

    def test_destination_already_exists_refuses_apply(self):
        with self.assertRaises(RuntimeRecoveryError):
            self.builder().apply(tts_bundle=self.tts_bundle, output=self.output, payload_id="p-again")

    def test_failed_apply_leaves_no_partial_payload_at_output(self):
        broken_bundle = self.root / "broken-bundle"
        broken_bundle.mkdir()
        # a directory with no runtime-bundle.json at all -- plan already
        # blocks this, but exercise apply's own defense too.
        never_written_output = self.root / "never-written"
        with self.assertRaises(RuntimeRecoveryError):
            self.builder().apply(tts_bundle=broken_bundle, output=never_written_output)
        self.assertFalse(never_written_output.exists())
        # and no stray `.{name}.e7-building-*` sibling left behind either
        leftovers = list(self.root.glob(f".{never_written_output.name}.e7-building-*"))
        self.assertEqual(leftovers, [])


class ContractReuseTests(RecoveryFixture):
    """Proves E7 invokes/reuses E3/E4 validation authority rather than
    silently maintaining copied constants -- a future product-version/
    hash change must break stale E7 payload validation automatically."""

    def test_load_runtime_bundle_is_actually_called(self):
        from isadoraair import runtime_bundle as runtime_bundle_module

        tts_bundle = self.write_tts_bundle()
        output = self.root / "payload"
        self.builder().apply(tts_bundle=tts_bundle, output=output, payload_id="p-reuse")
        with patch(
            "isadoraair.runtime_recovery.load_runtime_bundle",
            wraps=runtime_bundle_module.load_runtime_bundle,
        ) as mocked:
            evidence = validate_recovery_payload(output, product_manifest=self.manifest)
        self.assertTrue(mocked.called)
        self.assertEqual(evidence.result, RESULT_PASS)

    def test_verify_native_sources_is_actually_called(self):
        from isadoraair import runtime_native as runtime_native_module

        output = self.root / "payload"
        self.builder().apply(native_source_dir=self.native_source_dir, output=output, payload_id="p-reuse-native")
        with patch(
            "isadoraair.runtime_recovery.verify_native_sources",
            wraps=runtime_native_module.verify_native_sources,
        ) as mocked:
            evidence = validate_recovery_payload(output, product_manifest=self.manifest)
        self.assertTrue(mocked.called)
        self.assertEqual(evidence.result, RESULT_PASS)

    def test_verify_native_sources_failure_becomes_invalid_component_evidence(self):
        from isadoraair.runtime_provisioning import RuntimeProvisioningError

        output = self.root / "payload"
        self.builder().apply(native_source_dir=self.native_source_dir, output=output, payload_id="p-reuse-fail")
        with patch(
            "isadoraair.runtime_recovery.verify_native_sources",
            side_effect=RuntimeProvisioningError("boom"),
        ):
            evidence = validate_recovery_payload(output, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        self.assertEqual(evidence.components["native_fdkaac"].state, STATE_INVALID)

    def test_stale_payload_breaks_automatically_when_product_contract_changes(self):
        """A future change to the product manifest (e.g. a Kokoro model
        hash bump) must invalidate an already-built E7 payload without
        E7 itself needing any new logic -- proves E7 never forked its
        own copy of product identity."""

        tts_bundle = self.write_tts_bundle()
        output = self.root / "payload"
        self.builder().apply(tts_bundle=tts_bundle, output=output, payload_id="p-stale")
        self.assertEqual(validate_recovery_payload(output, product_manifest=self.manifest).result, RESULT_PASS)

        bumped_manifest = deepcopy(self.manifest)
        bumped_manifest["components"]["kokoro"]["assets"]["model"]["sha256"] = "f" * 64
        evidence = validate_recovery_payload(output, product_manifest=bumped_manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)

    def test_piper_selection_digest_reuses_e1_resolver(self):
        with patch(
            "isadoraair.runtime_recovery.resolve_current_runtime_requirements",
            return_value=self.piper_requirement(),
        ) as mocked:
            digest = piper_selection_digest()
        self.assertTrue(mocked.called)
        self.assertEqual(digest, piper_selection_digest(self.piper_requirement()))

    def test_no_duplicated_kokoro_hash_constants_in_module_source(self):
        source = Path(__import__("isadoraair.runtime_recovery", fromlist=["x"]).__file__).read_text(encoding="utf-8")
        self.assertNotIn("7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5", source)
        self.assertNotIn("bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d", source)


class PlanApplyTests(RecoveryFixture):
    def test_plan_has_zero_filesystem_mutation(self):
        tts_bundle = self.write_tts_bundle()
        output = self.root / "payload"
        before = sorted(str(p) for p in self.root.rglob("*"))
        plan = self.builder().plan(tts_bundle=tts_bundle, native_source_dir=self.native_source_dir, output=output)
        after = sorted(str(p) for p in self.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertTrue(plan.ready)

    def test_apply_writes_only_below_output_root(self):
        tts_bundle = self.write_tts_bundle()
        output = self.root / "isolated-output"
        before = sorted(str(p) for p in self.root.rglob("*") if not str(p).startswith(str(output)))
        self.builder().apply(tts_bundle=tts_bundle, output=output, payload_id="p-isolated")
        after = sorted(str(p) for p in self.root.rglob("*") if not str(p).startswith(str(output)))
        self.assertEqual(before, after)

    def test_at_least_one_source_required(self):
        plan = self.builder().plan(output=self.root / "empty-output")
        self.assertFalse(plan.ready)
        self.assertIn("at least one", plan.errors[0])

    def test_no_production_canonical_path_ever_referenced_in_apply(self):
        tts_bundle = self.write_tts_bundle()
        output = self.root / "payload"
        result = self.builder().apply(tts_bundle=tts_bundle, output=output, payload_id="p-noprod")
        for forbidden in ("/opt/isadoraair-runtime", "/var/lib/isadoraair/tts", "/usr/local", "/run/isadoraair"):
            self.assertNotIn(forbidden, result.output)


class ManagementCommandTests(RecoveryFixture):
    def test_prepare_plan_and_apply_round_trip(self):
        tts_bundle = self.write_tts_bundle()
        output = self.root / "cli-payload"
        with patch("isadoraair.runtime_recovery.load_runtime_components", return_value=self.manifest):
            call_command(
                "prepare_runtime_recovery_payload", "--plan", f"--tts-bundle={tts_bundle}", f"--output={output}"
            )
            self.assertFalse(output.exists())
            call_command(
                "prepare_runtime_recovery_payload",
                "--apply",
                f"--tts-bundle={tts_bundle}",
                f"--output={output}",
                "--payload-id=cli-payload-1",
            )
        self.assertTrue((output / RECOVERY_MANIFEST_FILENAME).is_file())

    def test_validate_command_fails_closed_on_missing_payload(self):
        with self.assertRaises(CommandError):
            call_command("validate_runtime_recovery_payload", str(self.root / "does-not-exist"))
