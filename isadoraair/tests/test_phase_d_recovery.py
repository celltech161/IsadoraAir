from __future__ import annotations

import base64
import hashlib
import inspect
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
    load_installed_phase_d_state,
    publish_phase_d_component,
    resolve_installed_phase_d_capture_kwargs,
    restore_phase_d_component,
    validate_phase_d_component,
)
from isadoraair.runtime_components import load_runtime_components
from isadoraair.runtime_recovery import (
    RuntimeRecoveryBuilder,
    RuntimeRecoveryError,
    activate_recovery_payload,
    attach_phase_d_recovery_component,
    build_and_attach_installed_phase_d_payload,
    plan_installed_phase_d_publication,
    restore_protected_updater_component,
    validate_current_recovery_payload,
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
        self.station.write_text(json.dumps({
            "schema_version": 1,
            "trusted_repository_url": "https://example.invalid/isadoraair.git",
            "trusted_branch": "main",
            "application_root": "/opt/isadoraair",
            "application_user": "nobody",
            "application_group": "nobody",
            "application_environment_file": "/opt/isadoraair/.env",
            "trusted_repository": "/var/lib/isadoraair-updater/repository.git",
            "jobs_root": "/var/lib/isadoraair-updater/jobs",
            "logs_root": "/var/log/isadoraair-updater",
            "staging_root": "/var/lib/isadoraair-updater/staging",
            "checkpoint_root": "/var/backups/isadoraair/update-checkpoints",
            "socket_path": "/run/isadoraair-updater/updater.sock",
            "systemd_unit_root": "/etc/systemd/system",
            "render_values": {
                "isa_user": "nobody",
                "isa_root": "/opt/isadoraair",
                "isa_home": "/var/lib/isadoraair",
                "syndicated_root": "/var/lib/isadoraair/syndicated",
                "weather_root": "/var/lib/isadoraair/weather",
                "ogremote_root": "/var/lib/isadoraair/ogremote",
            },
            "database": {
                "name": "isadoraair", "user": "isadoraair",
                "host": "127.0.0.1", "port": 5432, "pgpass_file": None,
            },
            "gunicorn_health_url": "http://127.0.0.1:8000/health/",
            "update_execution_enabled": False,
            "operator_restart_units": [],
            "phase_d_supervisor_activation_socket": "/run/isadoraair-updater-bootstrap/activation.sock",
            "phase_d_supervisor_slots_root": "/var/lib/isadoraair-updater-bootstrap/runtime-slots",
            "phase_d_trust_policy_path": "/etc/isadoraair/updater-trust.json",
            "phase_d_signer_root": "/etc/isadoraair/updater-signers",
        }))
        self.station.chmod(0o600)
        self.bootstrap_config = self.root / "updater-bootstrap.json"
        self.bootstrap_config.write_text(json.dumps({
            "schema_version": 1, "bootstrap_protocol_version": 1,
            "slots_root": "/var/lib/isadoraair-updater-bootstrap/runtime-slots",
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
        runtime_root = fake / "var/lib/isadoraair-updater-bootstrap"
        self.assertTrue((runtime_root / "runtime-state.json").is_file())
        self.assertTrue((runtime_root / "runtime-slots/A/updaterd.py").is_file())
        self.assertTrue((runtime_root / "runtime-slots/B/updaterd.py").is_file())
        self.assertTrue((runtime_root / "runtime-slots/.staging/descriptor-A.json").is_file())
        self.assertTrue((runtime_root / "runtime-slots/.staging/descriptor-B.json").is_file())
        self.assertFalse((fake / "var/lib/isadoraair/updater-runtime-slots").exists())

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

    def _build_schema_two_payload(self, output_name: str = "schema-two") -> Path:
        """A real, fully-valid schema-2 recovery payload with
        protected_updater attached -- the exact fixture shape r0030's
        stage-75 integration consumes. Shares self._capture()'s real
        cryptographic Phase-D component with
        test_schema_two_attachment_preserves_protected_component_modes
        above; the only difference is explicit 0o644 permissions on the
        native-source fixture files below (Path.write_bytes here
        otherwise inherits a group/world-writable mode under this
        sandbox's umask, which RuntimeRecoveryBuilder's own native-
        source contract correctly rejects -- see
        isadoraair/tests/test_phase_d_recovery.py's own pre-existing,
        environment-dependent failure on the SAME check when this isn't
        done; fixing it here rather than there keeps this fixture
        reliable without touching that unrelated, already-tracked
        issue)."""
        self._capture()
        self.product_manifest = deepcopy(load_runtime_components())
        native_source = self.root / f"native-source-{output_name}"
        native_source.mkdir()
        for name, declaration in self.product_manifest["components"]["fdkaac"]["source_archives"].items():
            content = f"fixture-{name}".encode("ascii")
            path = native_source / declaration["filename"]
            path.write_bytes(content)
            path.chmod(0o644)
            declaration["bytes"] = len(content)
            declaration["sha256"] = hashlib.sha256(content).hexdigest()

        base = self.root / f"base-schema-one-{output_name}"
        RuntimeRecoveryBuilder(product_manifest=self.product_manifest).apply(
            native_source_dir=native_source,
            output=base,
            payload_id="phase-d-stage-integration",
        )
        combined = self.root / output_name
        attach_phase_d_recovery_component(
            existing_payload=base,
            protected_updater_component=self.component,
            output=combined,
            product_manifest=self.product_manifest,
        )
        return combined

    def test_restore_protected_updater_component_from_schema_two_payload(self):
        """The r0030 restore-side entry point stage 75 calls: locates
        protected_updater inside a real schema-2 payload, verifies the
        restore-manifest digest, and delegates to
        restore_phase_d_component -- proving the exact result shape and
        content stage 75/the receipt logic depend on."""
        payload = self._build_schema_two_payload()
        fake_root = self.root / "restored-fake-root"
        evidence = restore_protected_updater_component(
            payload, fake_root=fake_root, product_manifest=self.product_manifest,
        )

        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["result"], "pass")
        self.assertEqual(evidence["active_generation"], 2)
        self.assertFalse(evidence["worker_started"])
        self.assertEqual(evidence["readiness"], "not-run")
        self.assertTrue(
            (fake_root / "usr/local/libexec/isadoraair-updater-bootstrap/updater_bootstrapd.py").is_file()
        )

    def test_restore_protected_updater_component_returns_none_when_archive_lacks_it(self):
        """A schema-1-only payload (no protected_updater component at
        all) is a clean, no-op-for-this-component signal -- never an
        error -- matching every other component's ABSENT state. Stage
        75's own bash dispatch checks this via
        validate_runtime_recovery_payload before ever calling the
        restore command; this proves the underlying function itself
        also degrades cleanly if called directly."""
        self._capture()
        product_manifest = deepcopy(load_runtime_components())
        native_source = self.root / "native-source-schema-one-only"
        native_source.mkdir()
        for name, declaration in product_manifest["components"]["fdkaac"]["source_archives"].items():
            content = f"fixture-{name}".encode("ascii")
            path = native_source / declaration["filename"]
            path.write_bytes(content)
            path.chmod(0o644)
            declaration["bytes"] = len(content)
            declaration["sha256"] = hashlib.sha256(content).hexdigest()
        schema_one = self.root / "schema-one-only"
        RuntimeRecoveryBuilder(product_manifest=product_manifest).apply(
            native_source_dir=native_source, output=schema_one, payload_id="schema-one-only",
        )

        evidence = restore_protected_updater_component(
            schema_one, fake_root=self.root / "unused-fake-root", product_manifest=product_manifest,
        )
        self.assertIsNone(evidence)

    def test_restore_protected_updater_component_rejects_tampered_restore_manifest_digest(self):
        """The outer recovery-payload manifest pins protected_updater's
        own restore-manifest.json by sha256 (restore_manifest_sha256).
        A restore-manifest modified after payload assembly -- even one
        that would otherwise still validate on its own -- must be
        rejected before any restore is attempted, not silently
        restored from tampered material."""
        payload = self._build_schema_two_payload(output_name="schema-two-tamper")
        manifest_path = payload / "protected-updater" / "restore-manifest.json"
        content = manifest_path.read_bytes()
        manifest_path.write_bytes(content + b" ")  # any byte change invalidates the pinned digest

        with self.assertRaises(RuntimeRecoveryError):
            restore_protected_updater_component(
                payload, fake_root=self.root / "should-not-exist", product_manifest=self.product_manifest,
            )
        self.assertFalse((self.root / "should-not-exist").exists())

    def test_publish_phase_d_component_materializes_files_at_target_root(self):
        """After restore_protected_updater_component builds the fake
        root, publish_phase_d_component must make its content genuinely
        present at the real/staging restore target -- not merely leave
        a throwaway proof tree -- so a receipt recorded afterward means
        the same thing it means for kokoro/native_fdkaac."""
        payload = self._build_schema_two_payload(output_name="schema-two-publish")
        fake_root = self.root / "fake-root-for-publish"
        restore_protected_updater_component(
            payload, fake_root=fake_root, product_manifest=self.product_manifest,
        )

        target_root = self.root / "staging-target" / "opt-analog"
        publish_phase_d_component(fake_root=fake_root, target_root=target_root)

        launcher = target_root / "usr/local/libexec/isadoraair-updater-bootstrap/updater_bootstrapd.py"
        self.assertTrue(launcher.is_file())
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
        unit = target_root / "etc/systemd/system/updater-bootstrapd.service"
        self.assertTrue(unit.is_file())
        state = target_root / "var/lib/isadoraair-updater-bootstrap/runtime-state.json"
        self.assertTrue(state.is_file())
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        # The fake root itself is untouched/still present -- publish copies, never moves.
        self.assertTrue(fake_root.exists())

    def test_publish_phase_d_component_refuses_preexisting_destination_file(self):
        """A stale/partial prior restore (or any unrelated file) already
        occupying one of protected_updater's real destination paths
        must stop the publish cold, never be silently overwritten --
        the same 'never guess what's safe to clobber' discipline every
        other guard in this restore tooling already enforces."""
        payload = self._build_schema_two_payload(output_name="schema-two-conflict")
        fake_root = self.root / "fake-root-for-conflict"
        restore_protected_updater_component(
            payload, fake_root=fake_root, product_manifest=self.product_manifest,
        )

        target_root = self.root / "staging-target-conflict"
        conflicting = target_root / "usr/local/libexec/isadoraair-updater-bootstrap/updater_bootstrapd.py"
        conflicting.parent.mkdir(parents=True)
        conflicting.write_text("pre-existing, unrelated content -- must not be touched")
        conflicting.chmod(0o644)

        with self.assertRaises(PhaseDRecoveryError):
            publish_phase_d_component(fake_root=fake_root, target_root=target_root)

        self.assertEqual(
            conflicting.read_text(), "pre-existing, unrelated content -- must not be touched",
            "publish must never overwrite pre-existing destination content",
        )

    def test_restore_then_publish_is_the_only_path_that_can_precede_a_receipt(self):
        """End-to-end proof of the exact sequence stage 75 performs
        before it may ever call restore_record_recovery_components:
        restore into a fake root, publish to the target, only then is
        there genuine evidence at the target root a receipt could
        honestly describe as 'recovered'."""
        payload = self._build_schema_two_payload(output_name="schema-two-e2e")
        fake_root = self.root / "e2e-fake-root"
        evidence = restore_protected_updater_component(
            payload, fake_root=fake_root, product_manifest=self.product_manifest,
        )
        self.assertEqual(evidence["result"], "pass")

        target_root = self.root / "e2e-target"
        self.assertFalse(
            (target_root / "usr/local/libexec/isadoraair-updater-bootstrap").exists(),
            "sanity: nothing at the target root before publish",
        )
        publish_phase_d_component(fake_root=fake_root, target_root=target_root)
        self.assertTrue(
            (target_root / "usr/local/libexec/isadoraair-updater-bootstrap/updater_bootstrapd.py").is_file(),
            "only after publish does the target root have genuine evidence a receipt may describe",
        )


class InstalledPhaseDPublicationTests(SimpleTestCase):
    """r0031: the operator-facing publish/activate workflow, exercised
    against a REALISTIC on-disk 'installed host' layout -- station.json
    + updater-bootstrap.json at caller-overridable paths, slots_root/
    {A,B} runtime slot trees, slots_root/.staging/{descriptor-<slot>.json,
    attestations-<slot>/} (the REAL supervisor-maintained convention --
    slots.py/runtime_handoff.py/ipc_server.py/updaterd.py all agree on
    it -- distinct from PhaseDRecoveryComponentTests' own fixture above,
    which is pre-organized by ROLE to feed capture_phase_d_component
    directly), runtime-state.json, trust-policy.json, and a persistent
    recovery base_root with a real schema-1 Foundation-E payload as
    `current` (mirrors production's own e7c-real-acceptance-1 exactly:
    directory basename == manifest payload_id). Proves
    load_installed_phase_d_state, resolve_installed_phase_d_capture_kwargs,
    plan_installed_phase_d_publication, build_and_attach_installed_phase_d_payload,
    and activate_recovery_payload's r0031 identity invariant all work
    together end to end, with no operator-typed protected path anywhere
    in the chain. enforce_root_ownership=False throughout -- the real
    production ownership/mode contract is proven separately: directly
    against this host in the final report, and by security.py's own
    assert_root_protected*'s "inactive unless truly running as root"
    design (unchanged, not modified by r0031)."""

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
        self.bootstrap_root = self.root / "installed-bootstrap-source"
        (self.bootstrap_root / "isadoraair_updater_bootstrap").mkdir(parents=True)
        shutil.copy2(reviewed_bootstrap / "updater_bootstrapd.py", self.bootstrap_root / "updater_bootstrapd.py")
        for source in (reviewed_bootstrap / "isadoraair_updater_bootstrap").glob("*.py"):
            shutil.copy2(source, self.bootstrap_root / "isadoraair_updater_bootstrap" / source.name)
        for path in self.bootstrap_root.rglob("*.py"):
            path.chmod(0o755 if path.name == "updater_bootstrapd.py" else 0o644)
        self.supervisor_service = self.root / "updater-bootstrapd.service"
        shutil.copy2(self.repository / "deploy" / "updater-bootstrapd.service", self.supervisor_service)
        self.supervisor_service.chmod(0o644)

        self.private = self.root / "release-private.key"
        self.signers = self.root / "signer-public-keys"
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

        self.slots_root = self.root / "runtime-slots"
        self.staging = self.slots_root / ".staging"
        self.staging.mkdir(parents=True)
        self.identities = {}
        self._generation(slot="B", generation=1, release_id="r0026", previous_release_id="r0025", previous_generation=None)
        self._generation(slot="A", generation=2, release_id="r0027", previous_release_id="r0026", previous_generation=1)

        self.runtime_state_path = self.root / "runtime-state.json"
        self._write_runtime_state(
            active_slot="A", active_generation=2, active_descriptor=self.identities["A"],
            previous_slot="B", previous_generation=1, previous_descriptor=self.identities["B"],
            activation=None,
        )

        self.trust_policy_path = self.root / "trust-policy.json"
        self.trust_policy_path.write_text(json.dumps({
            "schema_version": 1, "signature_algorithm": "ed25519", "threshold": 1,
            "signers": [{"id": "primary-release", "public_key_path": str(self.public)}],
        }))
        self.trust_policy_path.chmod(0o644)

        self.bootstrap_config_path = self.root / "updater-bootstrap.json"
        self.bootstrap_config_path.write_text(json.dumps({
            "schema_version": 1, "bootstrap_protocol_version": 1,
            "slots_root": str(self.slots_root),
            "runtime_state_path": str(self.runtime_state_path),
            "activation_socket": str(self.root / "activation.sock"),
            "worker_socket": str(self.root / "worker.sock"),
            "signer_root": str(self.signers),
            "trust_policy_path": str(self.trust_policy_path),
        }))
        self.bootstrap_config_path.chmod(0o644)

        self.station_config_path = self.root / "station.json"
        self.station_config_path.write_text(json.dumps({
            "schema_version": 1,
            "trusted_repository_url": "https://example.invalid/isadoraair.git",
            "trusted_branch": "main",
            "application_root": str(self.root / "app"),
            "application_user": "nobody",
            "application_group": "nobody",
            "application_environment_file": str(self.root / "app" / ".env"),
            "trusted_repository": str(self.root / "trusted-repo.git"),
            "jobs_root": str(self.root / "jobs"),
            "logs_root": str(self.root / "logs"),
            "staging_root": str(self.root / "staging"),
            "checkpoint_root": str(self.root / "checkpoints"),
            "socket_path": str(self.root / "run" / "updater.sock"),
            "systemd_unit_root": str(self.root / "systemd"),
            "render_values": {
                "isa_user": "nobody", "isa_root": str(self.root / "app"),
                "isa_home": str(self.root / "home"),
                "syndicated_root": str(self.root / "syndicated"),
                "weather_root": str(self.root / "weather"),
                "ogremote_root": str(self.root / "ogremote"),
            },
            "database": {
                "name": "isadoraair", "user": "isadoraair",
                "host": "127.0.0.1", "port": 5432, "pgpass_file": None,
            },
            "gunicorn_health_url": "http://127.0.0.1:8000/health/",
        }))
        self.station_config_path.chmod(0o644)

        self.base_root = self.root / "runtime-recovery"
        (self.base_root / "payloads").mkdir(parents=True)
        self.base_root.chmod(0o755)
        (self.base_root / "payloads").chmod(0o755)
        self.product_manifest = deepcopy(load_runtime_components())
        native_source = self.root / "native-source"
        native_source.mkdir()
        for name, declaration in self.product_manifest["components"]["fdkaac"]["source_archives"].items():
            content = f"fixture-{name}".encode("ascii")
            path = native_source / declaration["filename"]
            path.write_bytes(content)
            path.chmod(0o644)
            declaration["bytes"] = len(content)
            declaration["sha256"] = hashlib.sha256(content).hexdigest()
        RuntimeRecoveryBuilder(product_manifest=self.product_manifest).apply(
            native_source_dir=native_source,
            output=self.base_root / "payloads" / "e7c-real-acceptance-1",
            payload_id="e7c-real-acceptance-1",
        )
        (self.base_root / "current").symlink_to("payloads/e7c-real-acceptance-1")

    def _generation(self, *, slot, generation, release_id, previous_release_id, previous_generation):
        descriptor = build_descriptor(
            runtime_root=self.runtime_source, generation=generation,
            runtime_version=5, manifest_protocol_version=5, supported_wire_protocols=(3,),
        )
        descriptor_path = self.staging / f"descriptor-{slot}.json"
        descriptor_path.write_bytes(descriptor)
        descriptor_path.chmod(0o644)
        digest = hashlib.sha256(descriptor).hexdigest()
        self.identities[slot] = digest
        slot_root = self.slots_root / slot
        shutil.copytree(self.runtime_source, slot_root)
        for path in slot_root.rglob("*"):
            if path.is_file():
                path.chmod(0o755 if path.relative_to(slot_root).as_posix() == "updaterd.py" else 0o644)
        statement = build_statement(
            descriptor_bytes=descriptor, release_id=release_id,
            previous_release_id=previous_release_id, generation=generation,
        )
        signature = sign_statement(statement=statement, private_key_path=self.private, public_key_path=self.public)
        evidence_root = self.staging / f"attestations-{slot}"
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

    def _write_runtime_state(self, *, active_slot, active_generation, active_descriptor,
                              previous_slot, previous_generation, previous_descriptor, activation):
        self.runtime_state_path.write_text(json.dumps({
            "schema_version": 1,
            "active_slot": active_slot, "active_generation": active_generation,
            "active_descriptor_sha256": active_descriptor,
            "previous_slot": previous_slot, "previous_generation": previous_generation,
            "previous_descriptor_sha256": previous_descriptor,
            "activation": activation,
        }, sort_keys=True, separators=(",", ":")))
        self.runtime_state_path.chmod(0o600)

    def _load_installed(self):
        return load_installed_phase_d_state(
            station_config_path=self.station_config_path,
            bootstrap_config_path=self.bootstrap_config_path,
            enforce_root_ownership=False,
        )

    def _plan(self, **overrides):
        kwargs = dict(
            base_root=self.base_root, expected_owner_uid=os.geteuid(), enforce_root_ownership=False,
            station_config_path=self.station_config_path, bootstrap_config_path=self.bootstrap_config_path,
            product_manifest=self.product_manifest,
        )
        kwargs.update(overrides)
        return plan_installed_phase_d_publication(**kwargs)

    def _build(self, **overrides):
        kwargs = dict(
            base_root=self.base_root, expected_owner_uid=os.geteuid(), enforce_root_ownership=False,
            station_config_path=self.station_config_path, bootstrap_config_path=self.bootstrap_config_path,
            bootstrap_root=self.bootstrap_root, supervisor_service=self.supervisor_service,
            product_manifest=self.product_manifest,
        )
        kwargs.update(overrides)
        return build_and_attach_installed_phase_d_payload(**kwargs)

    def _activate(self, payload_id):
        return activate_recovery_payload(
            self.base_root, payload_id, expected_owner_uid=os.geteuid(), product_manifest=self.product_manifest,
        )

    # ---- load_installed_phase_d_state / resolve_installed_phase_d_capture_kwargs ----

    def test_load_installed_phase_d_state_derives_from_real_config_files(self):
        installed = self._load_installed()
        self.assertEqual(installed["bootstrap_config"].slots_root, self.slots_root)
        self.assertEqual(installed["bootstrap_config"].runtime_state_path, self.runtime_state_path)
        self.assertEqual(installed["bootstrap_config"].signer_root, self.signers)
        self.assertEqual(installed["bootstrap_config"].trust_policy_path, self.trust_policy_path)
        self.assertEqual(installed["runtime_state"].active_slot.value, "A")
        self.assertEqual(installed["runtime_state"].active_generation, 2)
        self.assertEqual(installed["runtime_state"].previous_slot.value, "B")
        self.assertIsNone(installed["runtime_state"].activation)
        self.assertEqual(installed["trust_policy"].threshold, 1)
        self.assertEqual({s.id for s in installed["trust_policy"].signers}, {"primary-release"})

    def test_resolve_installed_phase_d_capture_kwargs_remaps_attestations_by_role(self):
        installed = self._load_installed()
        scratch = self.root / "attestations-scratch"
        kwargs = resolve_installed_phase_d_capture_kwargs(
            installed=installed, scratch_dir=scratch,
            station_config_path=self.station_config_path,
            bootstrap_config_path=self.bootstrap_config_path,
        )
        from isadoraair.phase_d_recovery import INSTALLED_BOOTSTRAP_SOURCE_ROOT, INSTALLED_SUPERVISOR_SERVICE
        self.assertEqual(kwargs["bootstrap_root"], INSTALLED_BOOTSTRAP_SOURCE_ROOT)
        self.assertEqual(kwargs["supervisor_service"], INSTALLED_SUPERVISOR_SERVICE)
        self.assertEqual(kwargs["slots_root"], self.slots_root)
        self.assertEqual(kwargs["descriptors_root"], self.staging)
        self.assertTrue((kwargs["attestations_root"] / "active" / "signature.json").is_file())
        self.assertTrue((kwargs["attestations_root"] / "previous" / "signature.json").is_file())
        # The real .staging/ directory itself is never modified.
        self.assertTrue((self.staging / f"attestations-A").is_dir())
        self.assertTrue((self.staging / f"attestations-B").is_dir())

    def test_capture_via_derived_kwargs_produces_a_valid_component(self):
        """End-to-end proof that the derived kwargs are genuinely usable
        by capture_phase_d_component -- not just shaped correctly."""
        installed = self._load_installed()
        kwargs = resolve_installed_phase_d_capture_kwargs(
            installed=installed, scratch_dir=self.root / "attestations-scratch-2",
            station_config_path=self.station_config_path,
            bootstrap_config_path=self.bootstrap_config_path,
            bootstrap_root=self.bootstrap_root,
            supervisor_service=self.supervisor_service,
        )
        evidence = capture_phase_d_component(output=self.root / "captured", **kwargs)
        self.assertEqual(evidence["active_generation"], 2)
        self.assertEqual(evidence["previous_generation"], 1)
        self.assertFalse(any(p.suffix == ".key" for p in (self.root / "captured").rglob("*")))

    # ---- plan (read-only) ----

    def test_plan_is_entirely_read_only(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        plan = self._plan()
        after = sorted(str(p) for p in self.root.rglob("*"))
        self.assertEqual(before, after, "plan must never write anything")
        self.assertTrue(plan.ready, plan.errors)
        self.assertEqual(plan.source_payload_id, "e7c-real-acceptance-1")
        self.assertEqual(plan.source_schema_version, 1)
        self.assertFalse(plan.source_requires_refresh_derivation)
        self.assertEqual(plan.active_slot, "A")
        self.assertEqual(plan.active_generation, 2)
        self.assertEqual(plan.previous_slot, "B")
        self.assertFalse(plan.activation_in_progress)
        self.assertEqual(plan.trust_threshold, 1)
        self.assertIn("primary-release", plan.public_signer_ids)
        self.assertFalse(plan.to_dict()["private_key_material_included"])

    def test_plan_reports_activation_in_progress_and_blocks(self):
        self._write_runtime_state(
            active_slot="A", active_generation=2, active_descriptor=self.identities["A"],
            previous_slot="B", previous_generation=1, previous_descriptor=self.identities["B"],
            activation={
                "transaction_id": "11111111-1111-1111-1111-111111111111",
                "candidate_slot": "B", "candidate_generation": 3,
                "candidate_descriptor_sha256": "0" * 64, "phase": "staged",
            },
        )
        plan = self._plan()
        self.assertFalse(plan.ready)
        self.assertTrue(plan.activation_in_progress)
        self.assertTrue(any("activation" in e for e in plan.errors))

    # ---- apply: schema-1 current -> schema-2 ----

    def test_apply_from_schema_one_current_produces_valid_schema_two_with_matching_identity(self):
        evidence = self._build(new_payload_id="phase-d-test-1")
        self.assertEqual(evidence.result, "pass")
        self.assertEqual(evidence.schema_version, 2)
        self.assertEqual(evidence.payload_id, "phase-d-test-1")
        self.assertEqual(evidence.components["protected_updater"].state, "present")
        output = self.base_root / "payloads" / "phase-d-test-1"
        self.assertTrue(output.is_dir())
        # Source payload untouched.
        source_evidence = validate_current_recovery_payload(
            self.base_root / "payloads" / "e7c-real-acceptance-1", product_manifest=self.product_manifest,
        )
        self.assertEqual(source_evidence.schema_version, 1)
        self.assertEqual(source_evidence.components["protected_updater"].state, "absent")

    def test_apply_refuses_when_activation_in_progress(self):
        self._write_runtime_state(
            active_slot="A", active_generation=2, active_descriptor=self.identities["A"],
            previous_slot="B", previous_generation=1, previous_descriptor=self.identities["B"],
            activation={
                "transaction_id": "11111111-1111-1111-1111-111111111111",
                "candidate_slot": "B", "candidate_generation": 3,
                "candidate_descriptor_sha256": "0" * 64, "phase": "staged",
            },
        )
        with self.assertRaises(PhaseDRecoveryError):
            self._build(new_payload_id="phase-d-test-blocked")
        self.assertFalse((self.base_root / "payloads" / "phase-d-test-blocked").exists())

    def test_apply_refuses_on_invalid_runtime_state(self):
        """Active descriptor missing/inconsistent -- fails closed rather
        than capturing from an ambiguous protected-runtime state."""
        self._write_runtime_state(
            active_slot="A", active_generation=2, active_descriptor="f" * 64,  # wrong digest
            previous_slot="B", previous_generation=1, previous_descriptor=self.identities["B"],
            activation=None,
        )
        with self.assertRaises(PhaseDRecoveryError):
            self._build(new_payload_id="phase-d-test-invalid")

    def test_apply_default_payload_id_is_self_describing_and_bounded(self):
        evidence = self._build()
        self.assertTrue(evidence.payload_id.startswith("phase-d-"))
        self.assertLessEqual(len(evidence.payload_id), 128)

    # ---- apply: schema-2 current (refresh) ----

    def test_apply_from_schema_two_current_rederives_fresh_base_and_never_touches_it(self):
        first = self._build(new_payload_id="phase-d-first")
        self._activate("phase-d-first")
        before_mtime = (self.base_root / "payloads" / "phase-d-first" / "runtime-recovery.json").stat().st_mtime
        before_files = sorted(str(p) for p in (self.base_root / "payloads" / "phase-d-first").rglob("*"))

        second = self._build(new_payload_id="phase-d-second")
        self.assertEqual(second.result, "pass")
        self.assertEqual(second.payload_id, "phase-d-second")
        self.assertNotEqual(first.payload_id, second.payload_id)

        after_mtime = (self.base_root / "payloads" / "phase-d-first" / "runtime-recovery.json").stat().st_mtime
        after_files = sorted(str(p) for p in (self.base_root / "payloads" / "phase-d-first").rglob("*"))
        self.assertEqual(before_mtime, after_mtime, "refresh must never touch the prior schema-2 payload's tree")
        self.assertEqual(before_files, after_files)

    # ---- activation identity invariant ----

    def test_activate_matching_directory_and_manifest_id_succeeds(self):
        self._build(new_payload_id="phase-d-match")
        target = self._activate("phase-d-match")
        self.assertEqual(target, self.base_root / "payloads" / "phase-d-match")
        self.assertEqual(os.readlink(self.base_root / "current"), "payloads/phase-d-match")

    def test_activate_refuses_mismatched_directory_and_manifest_id_leaves_current_untouched(self):
        self._build(new_payload_id="phase-d-real-id")
        # Rename the directory without touching its manifest -- the
        # exact ambiguity the r0031 hardening review flagged.
        (self.base_root / "payloads" / "phase-d-real-id").rename(self.base_root / "payloads" / "renamed-copy")
        with self.assertRaises(RuntimeRecoveryError):
            self._activate("renamed-copy")
        self.assertEqual(os.readlink(self.base_root / "current"), "payloads/e7c-real-acceptance-1")

    def test_activate_accepts_current_production_style_schema_one_payload(self):
        # production's own e7c-real-acceptance-1 already obeys the
        # identity contract (directory basename == manifest payload_id)
        # -- re-activating it must still succeed under the new invariant.
        target = self._activate("e7c-real-acceptance-1")
        self.assertEqual(target, self.base_root / "payloads" / "e7c-real-acceptance-1")

    # ---- private keys / network ----

    def test_no_private_key_material_anywhere_in_the_built_payload(self):
        evidence = self._build(new_payload_id="phase-d-no-keys")
        output = self.base_root / "payloads" / evidence.payload_id
        self.assertFalse(any(p.suffix == ".key" for p in output.rglob("*")))
        self.assertNotIn(self.private.read_bytes(), b"".join(
            p.read_bytes() for p in output.rglob("*") if p.is_file() and p.stat().st_size < 10_000_000
        ))

    def test_publication_workflow_makes_no_network_calls(self):
        """Offline requirement, proven directly against the new source:
        no socket/urllib/requests/subprocess-git usage anywhere in the
        r0031 additions."""
        import isadoraair.phase_d_recovery as phase_d_module
        import isadoraair.runtime_recovery as recovery_module
        for module in (phase_d_module, recovery_module):
            source = inspect.getsource(module)
            for forbidden in ("import socket", "import urllib", "import requests", "git clone", "git fetch"):
                self.assertNotIn(forbidden, source, f"{module.__name__} must never reference {forbidden!r}")

    def test_no_protected_runtime_generation_changes_during_publication(self):
        """Publication is observational -- the live runtime-state.json
        (generation/slot identity) must be byte-identical before and
        after, whether or not activation of the RESULT ever happens."""
        before = self.runtime_state_path.read_bytes()
        self._build(new_payload_id="phase-d-no-gen-change")
        after = self.runtime_state_path.read_bytes()
        self.assertEqual(before, after)

    # ---- backup-v3 integration: required-component policy after activation ----

    def test_activated_schema_two_payload_satisfies_full_required_component_policy(self):
        """This fixture's base Foundation-E payload is native_fdkaac-only
        (no TTS bundle -- matching PhaseDRecoveryComponentTests' own
        established convention above, to avoid duplicating
        test_runtime_recovery.py's own separate, elaborate TTS-bundle
        fixture here); kokoro-required policy satisfaction from a real
        embedded TTS bundle is already covered there. What this proves
        that nothing else does: once protected_updater is genuinely
        ACTIVATED (not merely built), evaluate_recovery_policy -- the
        exact function backup-v3's write_metadata delegates its
        satisfied/required reporting to -- sees it as satisfied
        alongside an existing component, through the real activation
        path, not just a hand-built evidence object."""
        from isadoraair.runtime_recovery import evaluate_recovery_policy

        self._build(new_payload_id="phase-d-policy")
        self._activate("phase-d-policy")
        current = validate_current_recovery_payload(
            self.base_root / "payloads" / "phase-d-policy", product_manifest=self.product_manifest,
        )
        self.assertEqual(current.result, "pass")
        self.assertEqual(current.schema_version, 2)
        policy = evaluate_recovery_policy(current, {"native_fdkaac", "protected_updater"})
        self.assertTrue(policy.satisfied, policy.missing)
