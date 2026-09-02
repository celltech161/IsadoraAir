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
import os
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
    PAYLOADS_SUBDIR,
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
    activate_recovery_payload,
    evaluate_recovery_policy,
    load_recovery_payload,
    piper_selection_digest,
    parse_recovery_policy_components,
    resolve_current_recovery_payload_root,
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

    def test_piper_bundle_must_match_selected_station_identity(self):
        tts_bundle = self.write_tts_bundle(include_piper=True)
        unrelated = RuntimeRequirements(
            components={"piper": ComponentRequirement("piper")}
        )
        with self.assertRaisesRegex(RuntimeRecoveryError, "Piper model/config identity"):
            self.builder().apply(
                tts_bundle=tts_bundle,
                output=self.root / "payload-unrelated-piper",
                payload_id="p-unrelated-piper",
                piper_selection=unrelated,
            )

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
        self.assertEqual(evidence.result, RESULT_FAIL)

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


class RecoveryPolicyTests(RecoveryFixture):
    """Runtime Foundation E7B -- the required-component policy layer.
    Deliberately independent of E1's `required` flag (see this fixture's
    piper_requirement, kokoro is NEVER inferred that way)."""

    def test_no_policy_is_trivially_satisfied(self):
        tts_bundle = self.write_tts_bundle()
        output = self.root / "payload"
        self.builder().apply(tts_bundle=tts_bundle, output=output, payload_id="p1")
        evidence = validate_recovery_payload(output, product_manifest=self.manifest)
        policy = evaluate_recovery_policy(evidence, None)
        self.assertTrue(policy.satisfied)
        self.assertEqual(policy.missing, frozenset())

    def test_kokoro_required_and_present_is_satisfied(self):
        tts_bundle = self.write_tts_bundle()
        output = self.root / "payload"
        self.builder().apply(tts_bundle=tts_bundle, output=output, payload_id="p1")
        evidence = validate_recovery_payload(output, product_manifest=self.manifest)
        policy = evaluate_recovery_policy(evidence, {"kokoro"})
        self.assertTrue(policy.satisfied)

    def test_kokoro_required_but_absent_is_not_satisfied(self):
        """The exact E7B scenario: a payload built without Kokoro must
        never silently pass a policy that requires it -- station E1
        `required=False` is never consulted here at all."""

        output = self.root / "payload"
        self.builder().apply(native_source_dir=self.native_source_dir, output=output, payload_id="p1")
        evidence = validate_recovery_payload(output, product_manifest=self.manifest)
        policy = evaluate_recovery_policy(evidence, {"kokoro"})
        self.assertFalse(policy.satisfied)
        self.assertEqual(policy.missing, frozenset({"kokoro"}))

    def test_native_fdkaac_required_but_absent_is_not_satisfied(self):
        tts_bundle = self.write_tts_bundle()
        output = self.root / "payload"
        self.builder().apply(tts_bundle=tts_bundle, output=output, payload_id="p1")
        evidence = validate_recovery_payload(output, product_manifest=self.manifest)
        policy = evaluate_recovery_policy(evidence, {"native_fdkaac"})
        self.assertFalse(policy.satisfied)
        self.assertEqual(policy.missing, frozenset({"native_fdkaac"}))

    def test_piper_required_present_but_not_checked_is_not_satisfied(self):
        """not_checked is not success (task's own explicit rule): a
        policy-required Piper must be POSITIVELY confirmed current, an
        indeterminate DB-dependent check must fail closed."""

        tts_bundle = self.write_tts_bundle(include_piper=True)
        output = self.root / "payload"
        self.builder().apply(
            tts_bundle=tts_bundle, output=output, payload_id="p1", piper_selection=self.piper_requirement()
        )
        evidence = validate_recovery_payload(output, product_manifest=self.manifest)  # no live digest given
        self.assertEqual(evidence.piper_freshness.state, PIPER_FRESHNESS_NOT_CHECKED)
        policy = evaluate_recovery_policy(evidence, {"piper"})
        self.assertFalse(policy.satisfied)

    def test_piper_required_and_confirmed_current_is_satisfied(self):
        tts_bundle = self.write_tts_bundle(include_piper=True)
        output = self.root / "payload"
        self.builder().apply(
            tts_bundle=tts_bundle, output=output, payload_id="p1", piper_selection=self.piper_requirement()
        )
        digest = piper_selection_digest(self.piper_requirement())
        evidence = validate_recovery_payload(output, product_manifest=self.manifest, current_piper_selection_digest=digest)
        self.assertEqual(evidence.piper_freshness.state, PIPER_FRESHNESS_CURRENT)
        policy = evaluate_recovery_policy(evidence, {"piper"})
        self.assertTrue(policy.satisfied)

    def test_piper_required_but_stale_is_not_satisfied(self):
        tts_bundle = self.write_tts_bundle(include_piper=True)
        output = self.root / "payload"
        self.builder().apply(
            tts_bundle=tts_bundle, output=output, payload_id="p1", piper_selection=self.piper_requirement()
        )
        evidence = validate_recovery_payload(
            output, product_manifest=self.manifest, current_piper_selection_digest="0" * 64
        )
        self.assertEqual(evidence.piper_freshness.state, PIPER_FRESHNESS_STALE)
        policy = evaluate_recovery_policy(evidence, {"piper"})
        self.assertFalse(policy.satisfied)

    def test_multiple_required_components_all_must_be_satisfied(self):
        tts_bundle = self.write_tts_bundle()  # kokoro only, no native
        output = self.root / "payload"
        self.builder().apply(tts_bundle=tts_bundle, output=output, payload_id="p1")
        evidence = validate_recovery_payload(output, product_manifest=self.manifest)
        policy = evaluate_recovery_policy(evidence, {"kokoro", "native_fdkaac"})
        self.assertFalse(policy.satisfied)
        self.assertEqual(policy.missing, frozenset({"native_fdkaac"}))

    def test_unknown_policy_component_name_is_rejected(self):
        tts_bundle = self.write_tts_bundle()
        output = self.root / "payload"
        self.builder().apply(tts_bundle=tts_bundle, output=output, payload_id="p1")
        evidence = validate_recovery_payload(output, product_manifest=self.manifest)
        with self.assertRaises(RuntimeRecoveryError):
            evaluate_recovery_policy(evidence, {"not-a-real-component"})

    def test_strict_policy_parser_rejects_empty_whitespace_unknown_and_duplicates(self):
        for malformed in (
            "kokoro,",
            ",kokoro",
            "kokoro,,piper",
            "kokoro, piper",
            "kokoro,kokoro",
            "not-real",
        ):
            with self.subTest(malformed=malformed), self.assertRaises(RuntimeRecoveryError):
                parse_recovery_policy_components(malformed)
        self.assertEqual(
            parse_recovery_policy_components("kokoro,piper,native_fdkaac"),
            frozenset({"kokoro", "piper", "native_fdkaac"}),
        )
        self.assertEqual(parse_recovery_policy_components(""), frozenset())

    def test_invalid_payload_never_satisfies_any_policy(self):
        broken_output = self.root / "does-not-exist"
        evidence = validate_recovery_payload(broken_output, product_manifest=self.manifest)
        self.assertEqual(evidence.result, RESULT_FAIL)
        policy = evaluate_recovery_policy(evidence, {"kokoro"})
        self.assertFalse(policy.satisfied)


class PersistentLocationTests(RecoveryFixture):
    """Runtime Foundation E7B -- the durable base_root/payloads/<id> +
    base_root/current convention. Never established on any production
    path by these tests -- every base_root here is a disposable temp
    directory."""

    def _base_root(self) -> Path:
        base = self.root / "persistent"
        (base / PAYLOADS_SUBDIR).mkdir(parents=True)
        base.chmod(0o755)
        (base / PAYLOADS_SUBDIR).chmod(0o755)
        return base

    def _activate(self, base: Path, payload_id: str) -> Path:
        return activate_recovery_payload(
            base,
            payload_id,
            product_manifest=self.manifest,
            expected_owner_uid=os.getuid(),
        )

    def _resolve(self, base: Path) -> Path:
        return resolve_current_recovery_payload_root(base, expected_owner_uid=os.getuid())

    def test_resolve_with_no_pointer_fails_closed(self):
        base = self._base_root()
        with self.assertRaises(RuntimeRecoveryError):
            self._resolve(base)

    def test_activate_then_resolve_round_trip(self):
        base = self._base_root()
        tts_bundle = self.write_tts_bundle()
        target = base / PAYLOADS_SUBDIR / "p1"
        self.builder().apply(tts_bundle=tts_bundle, output=target, payload_id="p1")

        activated = self._activate(base, "p1")
        self.assertEqual(activated, target)

        resolved = self._resolve(base)
        self.assertEqual(resolved, target)

    def test_activate_refuses_an_invalid_payload(self):
        base = self._base_root()
        broken = base / PAYLOADS_SUBDIR / "broken"
        broken.mkdir()  # no runtime-recovery.json at all
        with self.assertRaises(RuntimeRecoveryError):
            self._activate(base, "broken")
        # and no pointer was created as a side effect of the failed attempt
        self.assertFalse((base / "current").exists())

    def test_activate_never_overwrites_a_payload_directory(self):
        """activate_recovery_payload only ever repoints the `current`
        symlink -- it must never write into payloads/<id>/ itself."""

        base = self._base_root()
        tts_bundle = self.write_tts_bundle()
        target = base / PAYLOADS_SUBDIR / "p1"
        self.builder().apply(tts_bundle=tts_bundle, output=target, payload_id="p1")
        before = {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()}

        self._activate(base, "p1")

        after = {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_activation_is_atomic_pointer_swap_not_directory_scan(self):
        """Two payloads exist; activating the OLDER one after the newer
        one was already built must select exactly the one asked for --
        proving this is never a "pick newest" scan."""

        base = self._base_root()
        tts_bundle_a = self.write_tts_bundle(bundle_id="bundle-a")
        target_a = base / PAYLOADS_SUBDIR / "p-a"
        self.builder().apply(tts_bundle=tts_bundle_a, output=target_a, payload_id="p-a")

        # A second, newer payload -- native-only, so it's trivially
        # distinguishable from p-a's tts-only shape.
        target_b = base / PAYLOADS_SUBDIR / "p-b"
        self.builder().apply(native_source_dir=self.native_source_dir, output=target_b, payload_id="p-b")

        self._activate(base, "p-a")
        resolved = self._resolve(base)
        self.assertEqual(resolved, target_a)
        evidence = validate_recovery_payload(resolved, product_manifest=self.manifest)
        self.assertEqual(evidence.components["tts"].state, STATE_PRESENT)
        self.assertEqual(evidence.components["native_fdkaac"].state, STATE_ABSENT)

    def test_reactivating_a_different_payload_swaps_cleanly(self):
        base = self._base_root()
        tts_bundle = self.write_tts_bundle()
        target_a = base / PAYLOADS_SUBDIR / "p-a"
        self.builder().apply(tts_bundle=tts_bundle, output=target_a, payload_id="p-a")
        target_b = base / PAYLOADS_SUBDIR / "p-b"
        self.builder().apply(native_source_dir=self.native_source_dir, output=target_b, payload_id="p-b")

        self._activate(base, "p-a")
        self.assertEqual(self._resolve(base), target_a)
        self._activate(base, "p-b")
        self.assertEqual(self._resolve(base), target_b)
        # p-a itself must remain untouched by the re-activation
        self.assertTrue(target_a.is_dir())

    def test_pointer_target_outside_payloads_root_is_rejected(self):
        base = self._base_root()
        escape_target = self.root / "outside-payloads"
        escape_target.mkdir()
        (base / "current").symlink_to(escape_target)
        with self.assertRaises(RuntimeRecoveryError):
            self._resolve(base)

    def test_current_pointer_must_itself_be_a_symlink(self):
        base = self._base_root()
        (base / "current").mkdir()  # a real directory, not a symlink
        with self.assertRaises(RuntimeRecoveryError):
            self._resolve(base)

    def test_symlinked_payload_id_directory_is_rejected(self):
        base = self._base_root()
        tts_bundle = self.write_tts_bundle()
        real_target = base / PAYLOADS_SUBDIR / "p-real"
        self.builder().apply(tts_bundle=tts_bundle, output=real_target, payload_id="p-real")
        fake_link = base / PAYLOADS_SUBDIR / "p-fake"
        fake_link.symlink_to(real_target)
        (base / "current").symlink_to(Path(PAYLOADS_SUBDIR) / "p-fake")
        with self.assertRaises(RuntimeRecoveryError):
            self._resolve(base)

    def test_activate_rejects_traversal_in_payload_id(self):
        base = self._base_root()
        with self.assertRaises(RuntimeRecoveryError):
            self._activate(base, "../../etc")

    def test_base_root_itself_symlinked_is_rejected(self):
        real_base = self.root / "real-base"
        (real_base / PAYLOADS_SUBDIR).mkdir(parents=True)
        link = self.root / "base-link"
        link.symlink_to(real_base)
        with self.assertRaises(RuntimeRecoveryError):
            self._resolve(link)

    def test_group_writable_payload_file_is_rejected(self):
        base = self._base_root()
        tts_bundle = self.write_tts_bundle()
        target = base / PAYLOADS_SUBDIR / "p1"
        self.builder().apply(tts_bundle=tts_bundle, output=target, payload_id="p1")
        self._activate(base, "p1")
        (target / RECOVERY_MANIFEST_FILENAME).chmod(0o664)
        with self.assertRaises(RuntimeRecoveryError):
            self._resolve(base)


class ManagementCommandE7BTests(RecoveryFixture):
    def _base_root(self) -> Path:
        base = self.root / "persistent"
        (base / PAYLOADS_SUBDIR).mkdir(parents=True)
        base.chmod(0o755)
        (base / PAYLOADS_SUBDIR).chmod(0o755)
        return base

    def test_activate_and_validate_current_round_trip_via_cli(self):
        base = self._base_root()
        tts_bundle = self.write_tts_bundle()
        target = base / PAYLOADS_SUBDIR / "p1"
        with patch("isadoraair.runtime_recovery.load_runtime_components", return_value=self.manifest):
            call_command(
                "prepare_runtime_recovery_payload",
                "--apply",
                f"--tts-bundle={tts_bundle}",
                f"--output={target}",
                "--payload-id=p1",
            )
            call_command(
                "prepare_runtime_recovery_payload", "--activate", f"--base-root={base}", "--payload-id=p1",
                f"--trusted-owner-uid={os.getuid()}",
            )
            call_command(
                "validate_runtime_recovery_payload", f"--base-root={base}", "--current", "--require=kokoro",
                f"--trusted-owner-uid={os.getuid()}",
            )
            with self.assertRaises(CommandError):
                call_command(
                    "validate_runtime_recovery_payload", f"--base-root={base}", "--current", "--require=piper",
                    f"--trusted-owner-uid={os.getuid()}",
                )

    def test_validate_current_json_includes_resolved_path_and_policy(self):
        import io
        import json

        base = self._base_root()
        tts_bundle = self.write_tts_bundle()
        target = base / PAYLOADS_SUBDIR / "p1"
        with patch("isadoraair.runtime_recovery.load_runtime_components", return_value=self.manifest):
            call_command(
                "prepare_runtime_recovery_payload",
                "--apply",
                f"--tts-bundle={tts_bundle}",
                f"--output={target}",
                "--payload-id=p1",
            )
            call_command(
                "prepare_runtime_recovery_payload", "--activate", f"--base-root={base}", "--payload-id=p1",
                f"--trusted-owner-uid={os.getuid()}",
            )
            out = io.StringIO()
            call_command(
                "validate_runtime_recovery_payload",
                f"--base-root={base}",
                "--current",
                "--require=kokoro",
                "--json",
                f"--trusted-owner-uid={os.getuid()}",
                stdout=out,
            )
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["resolved_path"], str(target))
        self.assertTrue(payload["policy"]["satisfied"])
        self.assertEqual(payload["policy"]["required"], ["kokoro"])

    def test_validate_requires_exactly_one_resolution_mode(self):
        with self.assertRaises(CommandError):
            call_command("validate_runtime_recovery_payload")
        with self.assertRaises(CommandError):
            call_command(
                "validate_runtime_recovery_payload", str(self.root), f"--base-root={self.root}", "--current"
            )

    def test_validate_rejects_malformed_csv_policy_before_inspection(self):
        with self.assertRaisesRegex(CommandError, "without empty"):
            call_command(
                "validate_runtime_recovery_payload",
                str(self.root / "not-even-inspected"),
                "--require-components=kokoro,",
            )

    def test_validate_current_not_configured_exits_2_distinct_from_broken(self):
        """Exit code 2 (not CommandError's usual 1) specifically for a
        never-set-up base root -- distinct from a configured-but-broken
        one, which must still raise the ordinary CommandError/exit 1."""

        never_configured = self.root / "never-configured"
        with self.assertRaises(SystemExit) as cm:
            call_command("validate_runtime_recovery_payload", f"--base-root={never_configured}", "--current")
        self.assertEqual(cm.exception.code, 2)

        broken_base = self.root / "broken-base"
        (broken_base / PAYLOADS_SUBDIR).mkdir(parents=True)
        (broken_base / "current").symlink_to(Path("/etc"))  # escapes payloads/
        with self.assertRaises(CommandError):
            call_command("validate_runtime_recovery_payload", f"--base-root={broken_base}", "--current")

    def test_prepare_activate_requires_base_root_and_payload_id(self):
        with self.assertRaises(CommandError):
            call_command("prepare_runtime_recovery_payload", "--activate")


class ManagementCommandPhaseDTests(RecoveryFixture):
    """r0031: --phase-d's own argument-validation/dispatch logic at the
    manage.py command layer -- the full derived capture/attach/activate
    workflow itself is exercised end to end (real fixtures, no mocking)
    in isadoraair.tests.test_phase_d_recovery.InstalledPhaseDPublicationTests;
    this class only proves the command's own dispatch/argument
    contract, matching ManagementCommandE7BTests' own established
    call_command-based pattern above."""

    def _base_root(self) -> Path:
        base = self.root / "phase-d-persistent"
        (base / PAYLOADS_SUBDIR).mkdir(parents=True)
        base.chmod(0o755)
        (base / PAYLOADS_SUBDIR).chmod(0o755)
        return base

    def test_phase_d_rejects_tts_bundle(self):
        with self.assertRaisesRegex(CommandError, "do not apply"):
            call_command(
                "prepare_runtime_recovery_payload", "--plan", "--phase-d",
                f"--base-root={self._base_root()}", f"--tts-bundle={self.root}",
            )

    def test_phase_d_rejects_native_source_dir(self):
        with self.assertRaisesRegex(CommandError, "do not apply"):
            call_command(
                "prepare_runtime_recovery_payload", "--plan", "--phase-d",
                f"--base-root={self._base_root()}", f"--native-source-dir={self.root}",
            )

    def test_phase_d_rejects_output(self):
        with self.assertRaisesRegex(CommandError, "do not pass --output"):
            call_command(
                "prepare_runtime_recovery_payload", "--plan", "--phase-d",
                f"--base-root={self._base_root()}", f"--output={self.root / 'unused'}",
            )

    def test_phase_d_requires_base_root(self):
        with self.assertRaisesRegex(CommandError, "requires --base-root"):
            call_command("prepare_runtime_recovery_payload", "--plan", "--phase-d")

    def test_phase_d_plan_against_unreadable_installed_config_fails_closed_not_silently(self):
        """Run genuinely unprivileged (this sandbox's real permission
        state -- /etc/isadoraair/station.json is real, root-owned, and
        unreadable here) against a fresh --base-root with no `current`
        selected yet: both real sources of failure are absent/
        unreadable, and the command must report BOTH clearly rather
        than silently guessing or crashing uninformatively."""
        base = self._base_root()
        with self.assertRaises(CommandError):
            call_command("prepare_runtime_recovery_payload", "--plan", "--phase-d", f"--base-root={base}")

    def test_phase_d_apply_also_requires_base_root(self):
        with self.assertRaisesRegex(CommandError, "requires --base-root"):
            call_command("prepare_runtime_recovery_payload", "--apply", "--phase-d")
