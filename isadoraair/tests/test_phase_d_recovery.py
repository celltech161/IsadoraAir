from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from copy import deepcopy

from django.test import SimpleTestCase

from deploy.updater_bootstrap.tools.protected_runtime_release import (
    OPENSSL_BINARY,
    build_descriptor,
    build_statement,
    sign_statement,
)
from isadoraair.phase_d_recovery import (
    PhaseDRecoveryError,
    capture_phase_d_component,
    restore_phase_d_component,
    validate_phase_d_component,
)
from isadoraair.runtime_components import load_runtime_components
from isadoraair.runtime_recovery import (
    RuntimeRecoveryBuilder,
    attach_phase_d_recovery_component,
)


class PhaseDRecoveryComponentTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = Path(__file__).resolve().parents[2]
        reviewed_runtime = self.repository / "deploy" / "updater_runtime"
        self.runtime_source = self.root / "reviewed-runtime"
        self.runtime_source.mkdir()
        for name in ("README.md", "updaterctl.py", "updaterd.py", "protected-policy.json"):
            shutil.copy2(reviewed_runtime / name, self.runtime_source / name)
        for package in ("isadoraair_updater", "protected_bootstrap"):
            (self.runtime_source / package).mkdir()
            for source in (reviewed_runtime / package).glob("*.py"):
                shutil.copy2(source, self.runtime_source / package / source.name)
        reviewed_bootstrap = self.repository / "deploy" / "updater_bootstrap"
        self.bootstrap_source = self.root / "reviewed-bootstrap"
        (self.bootstrap_source / "isadoraair_updater_bootstrap").mkdir(parents=True)
        shutil.copy2(reviewed_bootstrap / "updater_bootstrapd.py", self.bootstrap_source / "updater_bootstrapd.py")
        for source in (reviewed_bootstrap / "isadoraair_updater_bootstrap").glob("*.py"):
            shutil.copy2(source, self.bootstrap_source / "isadoraair_updater_bootstrap" / source.name)
        for path in self.bootstrap_source.rglob("*.py"):
            path.chmod(0o755 if path.name == "updater_bootstrapd.py" else 0o644)
        self.supervisor_service = self.root / "updater-bootstrapd.service"
        shutil.copy2(self.repository / "deploy" / "updater-bootstrapd.service", self.supervisor_service)
        self.supervisor_service.chmod(0o644)

        self.private = self.root / "release-private.key"
        self.signers = self.root / "signers"
        self.signers.mkdir()
        self.public = self.signers / "primary.pem"
        subprocess.run(
            [OPENSSL_BINARY, "genpkey", "-algorithm", "ed25519", "-out", str(self.private)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.private.chmod(0o600)
        subprocess.run(
            [OPENSSL_BINARY, "pkey", "-in", str(self.private), "-pubout", "-out", str(self.public)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.public.chmod(0o644)

        self.slots = self.root / "slots"
        self.slots.mkdir()
        self.descriptors = self.root / "descriptors"
        self.descriptors.mkdir()
        self.attestations = self.root / "attestations"
        self.attestations.mkdir()
        self.identities = {}
        self._generation(
            label="previous", slot="B", generation=1,
            release_id="r0026", previous_release_id="r0025", previous_generation=None,
        )
        self._generation(
            label="active", slot="A", generation=2,
            release_id="r0027", previous_release_id="r0026", previous_generation=1,
        )

        self.runtime_state = self.root / "runtime-state.json"
        self.runtime_state.write_text(json.dumps({
            "schema_version": 1,
            "active_slot": "A", "active_generation": 2,
            "active_descriptor_sha256": self.identities["active"],
            "previous_slot": "B", "previous_generation": 1,
            "previous_descriptor_sha256": self.identities["previous"],
            "activation": None,
        }, sort_keys=True, separators=(",", ":")))
        self.runtime_state.chmod(0o600)
        self.station = self.root / "station.json"
        self.station.write_text('{"schema_version":1,"station":"fixture"}\n')
        self.station.chmod(0o600)
        self.bootstrap_config = self.root / "updater-bootstrap.json"
        self.bootstrap_config.write_text(json.dumps({
            "schema_version": 1, "bootstrap_protocol_version": 1,
            "slots_root": "/var/lib/isadoraair-updater-bootstrap/slots",
            "runtime_state_path": "/var/lib/isadoraair-updater-bootstrap/runtime-state.json",
            "activation_socket": "/run/isadoraair-updater-bootstrap/activation.sock",
            "worker_socket": "/run/isadoraair-updater/updater.sock",
            "signer_root": "/etc/isadoraair/updater-signers",
            "trust_policy_path": "/etc/isadoraair/updater-trust.json",
        }))
        self.bootstrap_config.chmod(0o600)
        self.trust_policy = self.root / "trust-policy.json"
        self.trust_policy.write_text(json.dumps({
            "schema_version": 1, "signature_algorithm": "ed25519", "threshold": 1,
            "signers": [{"id": "primary-release", "public_key_path": "/etc/isadoraair/updater-signers/primary.pem"}],
        }))
        self.trust_policy.chmod(0o644)
        self.component = self.root / "protected-updater"

    def _generation(
        self, *, label: str, slot: str, generation: int,
        release_id: str, previous_release_id: str, previous_generation: int | None,
    ) -> None:
        descriptor = build_descriptor(
            runtime_root=self.runtime_source, generation=generation,
            runtime_version=5, manifest_protocol_version=5,
            supported_wire_protocols=(3,),
        )
        descriptor_path = self.descriptors / f"generation-{generation}.json"
        descriptor_path.write_bytes(descriptor)
        descriptor_path.chmod(0o644)
        digest = hashlib.sha256(descriptor).hexdigest()
        self.identities[label] = digest
        slot_root = self.slots / slot
        shutil.copytree(self.runtime_source, slot_root)
        for path in slot_root.rglob("*"):
            if path.is_file():
                path.chmod(0o755 if path.relative_to(slot_root).as_posix() == "updaterd.py" else 0o644)
        statement = build_statement(
            descriptor_bytes=descriptor, release_id=release_id,
            previous_release_id=previous_release_id, generation=generation,
        )
        signature = sign_statement(
            statement=statement, private_key_path=self.private, public_key_path=self.public,
        )
        evidence_root = self.attestations / label
        evidence_root.mkdir()
        (evidence_root / "signature.json").write_text(json.dumps({
            "schema_version": 1, "signer_id": "primary-release",
            "signature_base64": base64.b64encode(signature).decode("ascii"),
        }))
        (evidence_root / "signature.json").chmod(0o644)
        (evidence_root / "binding.json").write_text(json.dumps({
            "release_id": release_id, "previous_release_id": previous_release_id,
            "previous_generation": previous_generation,
        }))
        (evidence_root / "binding.json").chmod(0o644)

    def _capture(self):
        return capture_phase_d_component(
            output=self.component, bootstrap_root=self.bootstrap_source,
            supervisor_service=self.supervisor_service,
            slots_root=self.slots, runtime_state=self.runtime_state,
            station_config=self.station, bootstrap_config=self.bootstrap_config,
            trust_policy=self.trust_policy, signer_root=self.signers,
            descriptors_root=self.descriptors, attestations_root=self.attestations,
        )

    def test_capture_validate_and_offline_fake_root_restore(self):
        evidence = self._capture()
        self.assertEqual(evidence["active_generation"], 2)
        self.assertEqual(evidence["previous_generation"], 1)
        self.assertEqual(evidence["trust_threshold"], 1)
        self.assertFalse(any(path.suffix == ".key" for path in self.component.rglob("*")))
        restored = restore_phase_d_component(
            component_root=self.component, fake_root=self.root / "fake-root",
        )
        self.assertEqual(restored["result"], "pass")
        self.assertFalse(restored["network_used"])
        self.assertFalse(restored["worker_started"])
        self.assertEqual(restored["readiness"], "not-run")
        fake = self.root / "fake-root"
        self.assertTrue((fake / "usr/local/libexec/isadoraair-updater-bootstrap/updater_bootstrapd.py").is_file())
        self.assertTrue((fake / "var/lib/isadoraair/updater-runtime-state.json").is_file())

    def test_schema_two_attachment_preserves_protected_component_modes(self):
        self._capture()
        product_manifest = deepcopy(load_runtime_components())
        native_source = self.root / "native-source"
        native_source.mkdir()
        for name, declaration in product_manifest["components"]["fdkaac"]["source_archives"].items():
            content = f"fixture-{name}".encode("ascii")
            (native_source / declaration["filename"]).write_bytes(content)
            declaration["bytes"] = len(content)
            declaration["sha256"] = hashlib.sha256(content).hexdigest()

        base = self.root / "base-schema-one"
        RuntimeRecoveryBuilder(product_manifest=product_manifest).apply(
            native_source_dir=native_source,
            output=base,
            payload_id="phase-d-mode-regression",
        )
        combined = self.root / "schema-two"
        evidence = attach_phase_d_recovery_component(
            existing_payload=base,
            protected_updater_component=self.component,
            output=combined,
            product_manifest=product_manifest,
        )

        self.assertEqual(evidence.result, "pass")
        protected = combined / "protected-updater"
        self.assertEqual((protected / "station.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((protected / "runtime-state.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            (protected / "runtime-slots" / "active" / "updaterd.py").stat().st_mode & 0o777,
            0o755,
        )

    def test_claimed_phase_d_backup_fails_closed_on_each_critical_gap(self):
        self._capture()
        cases = (
            "bootstrap/source/updater_bootstrapd.py",
            "runtime-slots/active/updaterd.py",
            "runtime-descriptors/generation-2.json",
            "runtime-attestations/active/signature.json",
            "signer-public-keys/primary.pem",
            "trust-policy.json",
            "runtime-state.json",
        )
        for index, relative in enumerate(cases):
            with self.subTest(relative=relative):
                copy = self.root / f"tampered-{index}"
                shutil.copytree(self.component, copy)
                (copy / relative).unlink()
                with self.assertRaises(PhaseDRecoveryError):
                    validate_phase_d_component(copy)

    def test_descriptor_and_previous_lkg_inconsistency_are_refused(self):
        self._capture()
        tampered = self.root / "tampered-state"
        shutil.copytree(self.component, tampered)
        state_path = tampered / "runtime-state.json"
        state = json.loads(state_path.read_text())
        state["previous_descriptor_sha256"] = "f" * 64
        state_path.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")))
        # Even if an attacker also rewrites the outer manifest inventory, the
        # semantic state/descriptor relationship remains fail-closed.
        manifest_path = tampered / "restore-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        content = state_path.read_bytes()
        manifest["runtime_state_sha256"] = hashlib.sha256(content).hexdigest()
        for entry in manifest["files"]:
            if entry["path"] == "runtime-state.json":
                entry["sha256"] = hashlib.sha256(content).hexdigest()
                entry["size_bytes"] = len(content)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        with self.assertRaises(PhaseDRecoveryError):
            validate_phase_d_component(tampered)
