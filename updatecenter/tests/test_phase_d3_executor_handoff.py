"""Update Center Phase D, D3: the OLD worker's own real end-to-end
handoff pipeline (Executor.execute() -> runtime_handoff branch ->
real supervisor IPC), and the durable job-store LOCK ownership proof
D3-E explicitly requires (not merely asserted -- two real JobStore
instances, sequentially)."""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import config_dict, git  # noqa: F401

from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import Executor
from isadoraair_updater.jobs import JobError, JobStore
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import TrustedRepository
from isadoraair_updater.runtime_handoff import (
    MUTATION_GATE_MILESTONE, MutationGateError, require_mutation_allowed,
)

from isadoraair_updater_bootstrap.attestation import OPENSSL_BINARY, build_attestation_statement
from isadoraair_updater_bootstrap.descriptor import FileEntry, compute_bundle_sha256
from isadoraair_updater_bootstrap.ipc_server import IPCServer
from isadoraair_updater_bootstrap.slots import Slot, SlotLayout
from isadoraair_updater_bootstrap.state import RuntimeState, write_runtime_state_atomically
from isadoraair_updater_bootstrap.trust import parse_trust_policy_dict


def _keypair(directory: Path, name: str):
    private_path = directory / f"{name}.key"
    public_path = directory / f"{name}.pem"
    subprocess.run([OPENSSL_BINARY, "genpkey", "-algorithm", "ed25519", "-out", str(private_path)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    subprocess.run([OPENSSL_BINARY, "pkey", "-in", str(private_path), "-pubout", "-out", str(public_path)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return private_path, public_path


def _sign(private_key_path: Path, statement: bytes) -> bytes:
    with tempfile.NamedTemporaryFile() as statement_file:
        statement_file.write(statement)
        statement_file.flush()
        result = subprocess.run(
            [OPENSSL_BINARY, "pkeyutl", "-sign", "-inkey", str(private_key_path), "-rawin", "-in", statement_file.name],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return result.stdout


class ExecutorRuntimeHandoffEndToEndTests(SimpleTestCase):
    """Builds a REAL trusted Git repository (an r0002 baseline plus an
    r0003 release declaring protected_runtime, its descriptor, and its
    bundle files all genuinely committed under deploy/updater_runtime/)
    and a REAL running supervisor IPCServer, then drives Executor.
    execute() exactly as daemon.py's own _run_worker would."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

        # -- trusted Git repository: r0002 baseline, r0003 protected_runtime --
        self.author = self.root / "author"
        self.author.mkdir()
        git(self.author, "init", "-b", "main")
        releases = self.author / "deploy" / "releases"
        releases.mkdir(parents=True)

        def manifest(release_id, previous, **changes):
            data = {
                "schema_version": 1, "release_id": release_id, "previous_release_id": previous,
                "minimum_updater_protocol_version": 1, "summary": "t",
                "migrations_required": [], "migration_compatibility": None,
                "python_requirements_changed": False, "requirements_sha256": None,
                "apt_packages_new": [], "systemd_units_changed": [], "systemd_units_new_required": [],
                "systemd_units_new_optional": [], "systemd_units_removed_or_renamed": [],
                "collectstatic_required": False, "services_requiring_restart": [],
                "nginx_changed": False, "runtime_components_changed": False,
                "minimum_supported_release_id": None,
            }
            data.update(changes)
            return data

        (self.author / "README").write_text("baseline\n")
        git(self.author, "add", "README")
        git(self.author, "commit", "-m", "baseline")
        bootstrap = git(self.author, "rev-parse", "HEAD")
        (releases / "r0001.json").write_text(json.dumps(manifest("r0001", None, bootstrap_commit=bootstrap)))
        git(self.author, "add", "deploy/releases/r0001.json")
        git(self.author, "commit", "-m", "r0001")
        (releases / "r0002.json").write_text(json.dumps(manifest("r0002", "r0001", minimum_supported_release_id="r0001")))
        git(self.author, "add", "deploy/releases/r0002.json")
        git(self.author, "commit", "-m", "r0002")
        self.r0002_commit = git(self.author, "rev-parse", "HEAD")

        self.signer_dir = self.root / "signers"
        self.signer_dir.mkdir()
        self.private_key, self.public_key = _keypair(self.signer_dir, "primary")

        def _descriptor_release(release_id, previous_release_id, generation):
            entry_content = f"import sys\nsys.exit(0)  # generation {generation}\n".encode()
            entries = (FileEntry("updaterd.py", hashlib.sha256(entry_content).hexdigest(), "0755", len(entry_content)),)
            descriptor = {
                "schema_version": 1, "generation": generation, "runtime_version": 5,
                "manifest_protocol_version": 5, "supported_wire_protocols": [3], "entrypoint": "updaterd.py",
                "files": [{"path": e.path, "sha256": e.sha256, "mode": e.mode, "size_bytes": e.size_bytes} for e in entries],
                "bundle_sha256": compute_bundle_sha256(entries),
            }
            descriptor_bytes = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode("utf-8")
            descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()
            statement = build_attestation_statement(
                release_id=release_id, previous_release_id=previous_release_id,
                generation=generation, descriptor_sha256=descriptor_sha256,
            )
            signature = _sign(self.private_key, statement)
            attestation_wrapper = json.dumps({
                "schema_version": 1, "signer_id": "primary-release",
                "signature_base64": base64.b64encode(signature).decode("ascii"),
            }).encode("utf-8")

            runtime_dir = self.author / "deploy" / "updater_runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / f"updater-descriptor-{release_id}.json").write_bytes(descriptor_bytes)
            (runtime_dir / "updaterd.py").write_bytes(entry_content)
            attestation_dir = self.author / "deploy" / "updater_attestations"
            attestation_dir.mkdir(parents=True, exist_ok=True)
            (attestation_dir / f"{release_id}.json").write_bytes(attestation_wrapper)
            return {
                "generation": generation, "descriptor_path": f"deploy/updater_runtime/updater-descriptor-{release_id}.json",
                "descriptor_sha256": descriptor_sha256, "minimum_bootstrap_protocol_version": 1,
                "runtime_version": 5, "manifest_protocol_version": 5, "supported_wire_protocols": [3],
                "attestations": [f"deploy/updater_attestations/{release_id}.json"],
            }

        # r0003: generation=1 -- the chain-relative FIRST-EVER
        # protected_runtime declaration (D1's own rule: previous_
        # generation is None here, so generation must be exactly 1).
        # This is the STATION'S ALREADY-INSTALLED baseline in this
        # test -- it stands in for whatever a real D0 bootstrap
        # manually installed (RuntimeState.active_generation=1 below
        # matches it EXACTLY), never something driven through
        # execute()'s own handoff branch here.
        protected_runtime_r0003 = _descriptor_release("r0003", "r0002", generation=1)
        (releases / "r0003.json").write_text(json.dumps(manifest(
            "r0003", "r0002", minimum_supported_release_id="r0002",
            minimum_updater_protocol_version=5, protected_runtime=protected_runtime_r0003,
        )))
        git(self.author, "add", ".")
        git(self.author, "commit", "-m", "r0003 protected runtime generation 1 (D0-equivalent baseline)")
        self.r0003_commit = git(self.author, "rev-parse", "HEAD")

        # r0004: generation=2 -- the REAL, FIRST-EVER AUTOMATED
        # handoff this test actually drives Executor.execute() through
        # (installed=r0003 -> target=r0004). Strictly exceeds BOTH the
        # chain-relative previous generation (1, from r0003) and the
        # supervisor's own active_generation (1) -- consistent on both
        # axes, unlike naively reusing generation=1 for the target too.
        protected_runtime_r0004 = _descriptor_release("r0004", "r0003", generation=2)
        (releases / "r0004.json").write_text(json.dumps(manifest(
            "r0004", "r0003", minimum_supported_release_id="r0003",
            minimum_updater_protocol_version=5, protected_runtime=protected_runtime_r0004,
        )))
        git(self.author, "add", ".")
        git(self.author, "commit", "-m", "r0004 protected runtime generation 2")
        self.r0004_commit = git(self.author, "rev-parse", "HEAD")

        self.upstream = self.root / "upstream.git"
        subprocess.run(["git", "init", "--bare", str(self.upstream)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git(self.author, "remote", "add", "origin", str(self.upstream))
        git(self.author, "push", "-u", "origin", "main")

        # -- Executor/StationConfig, with the r0002 baseline "installed" --
        data = config_dict(self.root, str(self.upstream))
        self.slots_root = self.root / "slots"
        data["phase_d_supervisor_slots_root"] = str(self.slots_root)
        self.activation_socket = self.root / "activation.sock"
        data["phase_d_supervisor_activation_socket"] = str(self.activation_socket)
        # D4-G/D4-P: this worker's OWN read access to the same trust
        # material the supervisor uses -- a REAL file on disk (unlike
        # the in-memory TrustPolicy object built further below for the
        # IPCServer, Executor._load_phase_d_trust_policy() reads this
        # path directly).
        self.trust_policy_path = self.root / "trust-policy.json"
        self.trust_policy_path.write_text(json.dumps({
            "schema_version": 1, "signature_algorithm": "ed25519", "threshold": 1,
            "signers": [{"id": "primary-release", "public_key_path": str(self.public_key)}],
        }))
        data["phase_d_trust_policy_path"] = str(self.trust_policy_path)
        data["phase_d_signer_root"] = str(self.signer_dir)
        self.config = validate_config_dict(data, allow_local_repository=True)
        app = Path(data["application_root"])
        git(app, "init", "-b", "main")
        # config_dict() already wrote app/.env (untracked) above --
        # exclude it LOCALLY (.git/info/exclude, never committed) so
        # it survives the git reset --hard below unchanged: a
        # committed .gitignore would itself be reset away the moment
        # the working tree moves to a commit from the UPSTREAM
        # history that never had one, right when it's needed most.
        (app / ".git" / "info" / "exclude").write_text(".env\n")
        (app / "README").write_text("baseline\n")
        git(app, "add", "README")
        git(app, "commit", "-m", "baseline")
        git(app, "remote", "add", "origin", str(self.upstream))
        git(app, "fetch", "origin", "main")
        git(app, "reset", "--hard", self.r0003_commit)

        self.jobs_root = self.config.jobs_root
        self.logs_root = self.config.logs_root
        self.old_store = JobStore(self.jobs_root, self.logs_root, acquire_daemon_lock=True)
        self.executor = Executor(self.config, self.old_store, CommandRunner())

        # -- supervisor: A/B slot state, IPCServer --
        self.layout = SlotLayout(self.slots_root)
        self.state_path = self.root / "runtime-state.json"
        write_runtime_state_atomically(self.state_path, RuntimeState(
            schema_version=1, active_slot=Slot.A, active_generation=1,
            active_descriptor_sha256="a" * 64, previous_slot=None,
            previous_generation=None, previous_descriptor_sha256=None, activation=None,
        ))
        from isadoraair_updater_bootstrap.config import BootstrapConfig
        self.trust_policy = parse_trust_policy_dict(
            {"schema_version": 1, "signature_algorithm": "ed25519", "threshold": 1,
             "signers": [{"id": "primary-release", "public_key_path": str(self.public_key)}]},
            signer_directory=self.signer_dir,
        )
        self.bootstrap_config = BootstrapConfig(
            schema_version=1, bootstrap_protocol_version=1, slots_root=self.slots_root,
            runtime_state_path=self.state_path, activation_socket=self.activation_socket,
            worker_socket=self.root / "worker.sock", signer_root=self.signer_dir,
            trust_policy_path=self.root / "trust-policy.json",
        )
        import os
        self.server = IPCServer(self.bootstrap_config, self.trust_policy, layout=self.layout, authorized_uids={os.getuid()})
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        deadline = time.monotonic() + 5
        while not self.activation_socket.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.stop()
        self.server_thread.join(timeout=5)

    def test_old_worker_yields_job_after_requesting_activation(self):
        job_id = str(uuid.uuid4())
        from isadoraair_updater.release import derive_plan
        plan = derive_plan(self.executor.repository, self.executor.repository.fetch(), self.r0003_commit, "r0004")
        self.old_store.accept(job_id, "r0004", plan.fingerprint)

        result = self.executor.execute(job_id)

        self.assertEqual(result["state"], "running", result.get("failure_detail"))  # never succeeded/failed -- yielded, open
        self.assertIn("runtime_activation_requested", result["milestones"])
        self.assertNotIn("systemd_reconciled", result["milestones"])  # never reached the Phase-B pipeline
        self.assertIsNotNone(result["protected_runtime_candidate"])
        self.assertEqual(result["protected_runtime_candidate"]["generation"], 2)

    def test_lock_ownership_transfers_to_a_second_jobstore_after_yield(self):
        """D3-E's own explicit 'prove lock ownership' requirement --
        not asserted, PROVEN: a SECOND, independent JobStore instance
        (standing in for the candidate worker's own separate process)
        can acquire the SAME jobs_root's exclusive daemon lock only
        AFTER the old worker's own execute() call has yielded."""
        # Before yielding: a second acquisition attempt must fail --
        # this IS the flock actually doing its job.
        with self.assertRaises(JobError):
            JobStore(self.jobs_root, self.logs_root, acquire_daemon_lock=True)

        job_id = str(uuid.uuid4())
        from isadoraair_updater.release import derive_plan
        plan = derive_plan(self.executor.repository, self.executor.repository.fetch(), self.r0003_commit, "r0004")
        self.old_store.accept(job_id, "r0004", plan.fingerprint)
        self.executor.execute(job_id)  # yields -- calls self.old_store.close() internally

        # After yielding: acquisition succeeds -- REAL fcntl.flock
        # release/reacquire across two distinct JobStore instances,
        # not a mock.
        new_store = JobStore(self.jobs_root, self.logs_root, acquire_daemon_lock=True)
        try:
            resumed = new_store.load(job_id)
            self.assertEqual(resumed["state"], "running")
            self.assertIn("runtime_activation_requested", resumed["milestones"])
        finally:
            new_store.close()

    def test_no_duplicate_active_worker_old_process_reentry_is_idempotent(self):
        """Even if daemon.py's own _ensure_worker() somehow re-launched
        a SECOND worker thread for the SAME job_id on the OLD process
        (e.g. a Django submit_job() retry racing the first thread) --
        never two DIFFERENT jobs, never a second, conflicting
        transaction; the supervisor's own idempotent-replay handling
        (test_phase_d3_supervisor_ipc.py) absorbs the repeat."""
        job_id = str(uuid.uuid4())
        from isadoraair_updater.release import derive_plan
        plan = derive_plan(self.executor.repository, self.executor.repository.fetch(), self.r0003_commit, "r0004")
        self.old_store.accept(job_id, "r0004", plan.fingerprint)
        first = self.executor.execute(job_id)
        second = self.executor.execute(job_id)
        self.assertEqual(first["milestones"], second["milestones"])
        self.assertEqual(
            first["protected_runtime_candidate"]["descriptor_sha256"],
            second["protected_runtime_candidate"]["descriptor_sha256"],
        )


class MutationGateWiringTests(SimpleTestCase):
    """Confirms Executor._require_mutation_allowed actually converts
    MutationGateError into the expected ExecutionError classification
    -- the piece test_phase_d3_runtime_handoff.py's own MutationGate
    tests (against the pure function directly) cannot see, since they
    never touch Executor."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        data = config_dict(self.root, str(self.root / "upstream.git"))
        self.config = validate_config_dict(data, allow_local_repository=True)
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.addCleanup(self.store.close)
        self.executor = Executor(self.config, self.store, CommandRunner())

    def test_refuses_without_acceptance_milestone(self):
        from protected_bootstrap.manifest_field import ProtectedRuntimeField
        from isadoraair_updater.executor import ExecutionError

        field = ProtectedRuntimeField(
            generation=1, descriptor_path="deploy/updater_runtime/d.json", descriptor_sha256="a" * 64,
            minimum_bootstrap_protocol_version=1, runtime_version=5, manifest_protocol_version=5,
            supported_wire_protocols=(3,), attestations=("deploy/updater_attestations/a.json",),
        )
        from isadoraair_updater.release import TrustedPlan
        plan = TrustedPlan(
            installed_release_id="r0002", installed_commit="a" * 40, target_release_id="r0003",
            target_commit="b" * 40, releases_in_plan=("r0003",), migrations_required=(),
            migration_compatibility=None, python_requirements_changed=False, apt_packages_new=(),
            systemd_units_changed=(), systemd_units_new_required=(), systemd_units_new_optional=(),
            systemd_units_removed_or_renamed=(), collectstatic_required=False,
            services_requiring_restart=(), nginx_changed=False, runtime_components_changed=False,
            minimum_updater_protocol_version=5, manual_bootstrap_required=False, fingerprint="f" * 64,
            protected_runtime=field,
        )
        with self.assertRaises(ExecutionError) as ctx:
            self.executor._require_mutation_allowed(plan, {"runtime_activation_requested"})
        self.assertEqual(ctx.exception.classification, "RUNTIME_ACTIVATION_NOT_ACCEPTED")
        self.assertTrue(ctx.exception.manual)
