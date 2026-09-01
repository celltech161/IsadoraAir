"""Update Center Phase D, D4-A/D4-K: the REAL supervisor event loop
(SupervisorDaemon) driving REAL subprocesses -- process launch,
crash/restart, the full activation handoff sequence over the REAL
bootstrap IPC protocol (REQUEST_ACTIVATION -> REPORT_READINESS ->
CONFIRM_RUNTIME_ACCEPTANCE -> commit), forced old-worker termination,
and pre-acceptance rollback on candidate timeout/crash.

Uses lightweight SYNTHETIC fixture worker scripts (matching D2's own
established test pattern -- FIXTURE_WORKER_QUICK_EXIT et al in
test_phase_d2_worker_lifecycle.py) rather than the real production
updaterd.py/Executor pipeline, which requires root and a full trusted-
Git+Django application fixture (covered separately, in-process, by
test_phase_d3/d4_executor_handoff.py). This file's own job is proving
SupervisorDaemon's OWN real process/IPC orchestration -- the "does the
supervisor correctly launch, wait, terminate, and react to the ACTUAL
activation-phase state machine" question, independent of what a real
worker's internal update logic does."""
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
from unittest.mock import Mock

from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT, RUNTIME_ROOT  # noqa: F401

from isadoraair_updater_bootstrap.attestation import OPENSSL_BINARY, build_attestation_statement
from isadoraair_updater_bootstrap.config import BootstrapConfig
from isadoraair_updater_bootstrap.descriptor import FileEntry, compute_bundle_sha256
from isadoraair_updater_bootstrap.slots import Slot, SlotLayout
from isadoraair_updater_bootstrap.state import RuntimeState, write_runtime_state_atomically
from isadoraair_updater_bootstrap.supervisor_daemon import SupervisorDaemon
from isadoraair_updater_bootstrap.trust import parse_trust_policy_dict
from isadoraair_updater_bootstrap.worker_lifecycle import WorkerLifecycle


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


# A synthetic OLD-WORKER fixture: connects to the supervisor's real
# activation socket and requests activation for a candidate the TEST
# itself has already staged/published -- then exits immediately
# (voluntary yield) OR sleeps past the supervisor's own bounded
# timeout (forced-termination path), controlled by sys.argv.
FIXTURE_OLD_WORKER = """
import sys
sys.path.insert(0, {runtime_root!r})
from isadoraair_updater.supervisor_client import SupervisorClient
client = SupervisorClient({activation_socket!r}, timeout=10)
client.request_activation(
    transaction_id={job_uuid!r}, candidate_slot={candidate_slot!r},
    candidate_generation={candidate_generation!r}, candidate_descriptor_sha256={descriptor_sha256!r},
    release_id={release_id!r}, previous_release_id={previous_release_id!r},
)
if {should_yield!r}:
    sys.exit(0)
import time
time.sleep(60)
"""

# A synthetic CANDIDATE fixture: reports readiness, waits briefly, then
# (unless told to crash first) confirms runtime acceptance -- exactly
# the two REAL IPC calls the real Executor/daemon.py would make, using
# the SAME real supervisor_client.py.
FIXTURE_CANDIDATE_WORKER = """
import argparse, sys, time
sys.path.insert(0, {runtime_root!r})
from isadoraair_updater.supervisor_client import SupervisorClient
parser = argparse.ArgumentParser()
parser.add_argument("--config")
parser.add_argument("--expected-slot")
parser.add_argument("--expected-generation", type=int)
parser.add_argument("--expected-descriptor-sha256")
parser.add_argument("--expected-job-uuid")
args = parser.parse_args()
client = SupervisorClient({activation_socket!r}, timeout=10)
behavior = {behavior!r}
if behavior == "crash_before_readiness":
    sys.exit(1)
if behavior == "never_ready":
    # Never even connects to report readiness -- proves the
    # CANDIDATE_STARTING-phase readiness TIMEOUT specifically.
    time.sleep(60)
    sys.exit(0)
client.report_readiness(
    transaction_id=args.expected_job_uuid, candidate_slot=args.expected_slot,
    candidate_generation=args.expected_generation, candidate_descriptor_sha256=args.expected_descriptor_sha256,
    bootstrap_protocol_version=1, supported_wire_protocols=[3], config_parsed=True,
    privilege_drop_self_check_passed=True, job_store_ready=True, worker_socket_bound=True,
    trusted_repository_usable=True, resumable_job_uuid=args.expected_job_uuid,
)
if behavior == "crash_after_readiness":
    sys.exit(1)
if behavior == "ready_but_never_accepts":
    # Reports ready normally, then hangs WITHOUT ever confirming
    # acceptance -- proves the CANDIDATE_READY-phase acceptance
    # TIMEOUT specifically (a real gap this D4 pass found and fixed:
    # SupervisorDaemon previously waited forever here).
    time.sleep(60)
    sys.exit(0)
client.confirm_runtime_acceptance(
    transaction_id=args.expected_job_uuid, candidate_slot=args.expected_slot,
    candidate_generation=args.expected_generation, candidate_descriptor_sha256=args.expected_descriptor_sha256,
    resumable_job_uuid=args.expected_job_uuid,
)
time.sleep(60)
"""


class SupervisorDaemonRealHandoffTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

        self.signer_dir = self.root / "signers"
        self.signer_dir.mkdir()
        self.private_key, self.public_key = _keypair(self.signer_dir, "primary")
        self.trust_policy = parse_trust_policy_dict(
            {"schema_version": 1, "signature_algorithm": "ed25519", "threshold": 1,
             "signers": [{"id": "primary-release", "public_key_path": str(self.public_key)}]},
            signer_directory=self.signer_dir,
        )

        self.slots_root = self.root / "slots"
        self.layout = SlotLayout(self.slots_root)
        (self.layout.slot_path(Slot.A)).mkdir(parents=True)
        # Slot A's own "active worker" entrypoint -- overwritten per
        # test with whichever synthetic fixture script that test needs.
        self.state_path = self.root / "runtime-state.json"
        write_runtime_state_atomically(self.state_path, RuntimeState(
            schema_version=1, active_slot=Slot.A, active_generation=1,
            active_descriptor_sha256="a" * 64, previous_slot=None,
            previous_generation=None, previous_descriptor_sha256=None, activation=None,
        ))
        self.activation_socket = self.root / "activation.sock"
        self.bootstrap_config = BootstrapConfig(
            schema_version=1, bootstrap_protocol_version=1, slots_root=self.slots_root,
            runtime_state_path=self.state_path, activation_socket=self.activation_socket,
            worker_socket=self.root / "worker.sock", signer_root=self.signer_dir,
            trust_policy_path=self.root / "trust-policy.json",
        )
        self.worker_config_path = self.root / "worker-config.json"
        self.worker_config_path.write_text("{}")  # never actually parsed by these synthetic fixtures
        import os
        self.daemon = SupervisorDaemon(
            self.bootstrap_config, self.trust_policy, layout=self.layout,
            worker_config_path=self.worker_config_path,
            authorized_uids={os.getuid()}, poll_interval=0.05,
            old_worker_yield_timeout=2, candidate_readiness_timeout=2, candidate_acceptance_timeout=2,
        )
        self.addCleanup(self.daemon.stop)

    def _start_daemon_thread(self):
        thread = threading.Thread(target=self.daemon.start, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not self.activation_socket.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        return thread

    def test_exhausted_restart_bound_does_not_launch_an_untracked_worker(self):
        daemon = object.__new__(SupervisorDaemon)
        daemon.active_worker = None
        daemon.lifecycle = WorkerLifecycle(max_consecutive_restart_attempts=0)
        daemon._launch_active_worker = Mock()

        daemon._check_active_worker_liveness()

        daemon._launch_active_worker.assert_not_called()
        self.assertIsNone(daemon.active_worker)

    def _stage_and_publish_candidate(self, *, generation=2, candidate_slot="B"):
        """Real staging -- materialize_candidate/stage_descriptor/
        stage_attestations/publish_to_candidate_slot, exactly as the
        real Executor would, performed here directly (in-process)
        since this test's own synthetic old-worker fixture only needs
        to REQUEST activation, not perform staging itself."""
        import sys
        sys.path.insert(0, str(RUNTIME_ROOT))
        from isadoraair_updater.runtime_handoff import new_supervisor_staging_directory, publish_to_candidate_slot
        entry_content = FIXTURE_CANDIDATE_WORKER.format(
            runtime_root=str(RUNTIME_ROOT), activation_socket=str(self.activation_socket),
            behavior=self._candidate_behavior,
        ).encode()
        entries = (FileEntry("updaterd.py", hashlib.sha256(entry_content).hexdigest(), "0755", len(entry_content)),)
        descriptor = {
            "schema_version": 1, "generation": generation, "runtime_version": 5, "manifest_protocol_version": 5,
            "supported_wire_protocols": [3], "entrypoint": "updaterd.py",
            "files": [{"path": e.path, "sha256": e.sha256, "mode": e.mode, "size_bytes": e.size_bytes} for e in entries],
            "bundle_sha256": compute_bundle_sha256(entries),
        }
        descriptor_bytes = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
        descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()
        statement = build_attestation_statement(
            release_id="r0004", previous_release_id="r0003", generation=generation, descriptor_sha256=descriptor_sha256,
        )
        signature = _sign(self.private_key, statement)

        staging = new_supervisor_staging_directory(self.slots_root)
        (staging / "updaterd.py").write_bytes(entry_content)
        (staging / "updaterd.py").chmod(0o755)
        from isadoraair_updater.runtime_handoff import descriptor_staging_path, attestations_staging_directory
        descriptor_staging_path(self.slots_root, candidate_slot).parent.mkdir(parents=True, exist_ok=True)
        descriptor_staging_path(self.slots_root, candidate_slot).write_bytes(descriptor_bytes)
        attestations_dir = attestations_staging_directory(self.slots_root, candidate_slot)
        attestations_dir.mkdir(parents=True, exist_ok=True)
        (attestations_dir / "00.json").write_text(json.dumps({
            "schema_version": 1, "signer_id": "primary-release",
            "signature_base64": base64.b64encode(signature).decode(),
        }))
        publish_to_candidate_slot(self.slots_root, candidate_slot, staging, active_slot="A")
        return descriptor_sha256

    def _write_old_worker_fixture(self, *, job_uuid, candidate_slot, generation, descriptor_sha256, should_yield=True):
        content = FIXTURE_OLD_WORKER.format(
            runtime_root=str(RUNTIME_ROOT), activation_socket=str(self.activation_socket),
            job_uuid=job_uuid, candidate_slot=candidate_slot, candidate_generation=generation,
            descriptor_sha256=descriptor_sha256, release_id="r0004", previous_release_id="r0003",
            should_yield=should_yield,
        )
        entrypoint = self.layout.slot_path(Slot.A) / "updaterd.py"
        entrypoint.write_text(content)
        entrypoint.chmod(0o755)

    def test_full_real_handoff_reaches_committed_generation(self):
        self._candidate_behavior = "accept"
        job_uuid = str(uuid.uuid4())
        descriptor_sha256 = self._stage_and_publish_candidate(generation=2, candidate_slot="B")
        self._write_old_worker_fixture(
            job_uuid=job_uuid, candidate_slot="B", generation=2, descriptor_sha256=descriptor_sha256,
        )
        self._start_daemon_thread()

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.daemon.ipc_server.state
            if state.active_slot.value == "B" and state.activation is None:
                break
            time.sleep(0.05)
        else:
            self.fail(f"handoff never committed; final state: {self.daemon.ipc_server.state}")

        self.assertEqual(self.daemon.ipc_server.state.active_generation, 2)
        self.assertEqual(self.daemon.ipc_server.state.active_descriptor_sha256, descriptor_sha256)
        self.assertEqual(self.daemon.ipc_server.state.previous_slot.value, "A")
        self.assertEqual(self.daemon.ipc_server.state.previous_generation, 1)

    def test_old_worker_that_does_not_exit_is_forcibly_terminated(self):
        self._candidate_behavior = "accept"
        job_uuid = str(uuid.uuid4())
        descriptor_sha256 = self._stage_and_publish_candidate(generation=2, candidate_slot="B")
        self._write_old_worker_fixture(
            job_uuid=job_uuid, candidate_slot="B", generation=2, descriptor_sha256=descriptor_sha256,
            should_yield=False,  # hangs -- proves the supervisor's own bounded-timeout termination
        )
        self._start_daemon_thread()

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = self.daemon.ipc_server.state
            if state.active_slot.value == "B" and state.activation is None:
                break
            time.sleep(0.05)
        else:
            self.fail("handoff never completed despite the old worker hanging past its yield timeout")

    def _assert_rolls_back_and_restarts_previous_worker(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.daemon.ipc_server.state
            if state.activation is None and state.active_slot.value == "A":
                break
            time.sleep(0.05)
        else:
            self.fail(f"rollback never completed; final state: {self.daemon.ipc_server.state}")
        # The previous (never-changed) active slot's own worker must be
        # running again -- the station is never left with no worker.
        deadline = time.monotonic() + 5
        while self.daemon.active_worker is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(self.daemon.active_worker)
        self.assertIsNone(self.daemon.active_worker.poll())

    def test_candidate_readiness_timeout_rolls_back_and_restarts_previous_worker(self):
        self._candidate_behavior = "never_ready"  # never reports readiness at all
        job_uuid = str(uuid.uuid4())
        descriptor_sha256 = self._stage_and_publish_candidate(generation=2, candidate_slot="B")
        self._write_old_worker_fixture(
            job_uuid=job_uuid, candidate_slot="B", generation=2, descriptor_sha256=descriptor_sha256,
        )
        self._start_daemon_thread()
        self._assert_rolls_back_and_restarts_previous_worker()

    def test_candidate_ready_but_never_accepts_times_out_and_rolls_back(self):
        # Real gap this D4 pass found and fixed: a candidate that
        # reaches CANDIDATE_READY (readiness independently confirmed)
        # but then never calls CONFIRM_RUNTIME_ACCEPTANCE previously
        # had NO timeout at all -- SupervisorDaemon waited forever.
        self._candidate_behavior = "ready_but_never_accepts"
        job_uuid = str(uuid.uuid4())
        descriptor_sha256 = self._stage_and_publish_candidate(generation=2, candidate_slot="B")
        self._write_old_worker_fixture(
            job_uuid=job_uuid, candidate_slot="B", generation=2, descriptor_sha256=descriptor_sha256,
        )
        self._start_daemon_thread()
        self._assert_rolls_back_and_restarts_previous_worker()

    def test_candidate_crash_before_readiness_rolls_back(self):
        self._candidate_behavior = "crash_before_readiness"
        job_uuid = str(uuid.uuid4())
        descriptor_sha256 = self._stage_and_publish_candidate(generation=2, candidate_slot="B")
        self._write_old_worker_fixture(
            job_uuid=job_uuid, candidate_slot="B", generation=2, descriptor_sha256=descriptor_sha256,
        )
        self._start_daemon_thread()

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.daemon.ipc_server.state
            if state.activation is None and state.active_slot.value == "A":
                break
            time.sleep(0.05)
        else:
            self.fail(f"rollback after candidate crash never completed; final state: {self.daemon.ipc_server.state}")


class SupervisorDaemonPromotionBookkeepingTests(SupervisorDaemonRealHandoffTests):
    """Real defect regression: the first genuine production generation-1
    -> generation-2 promotion (r0027) left self.active_worker == None
    after a successful commit (the OLD worker's exit had already been
    acknowledged; nothing ever repointed the ALREADY-RUNNING, already-
    verified candidate process into self.active_worker). Every
    subsequent tick's _check_active_worker_liveness() then believed no
    worker was tracked at all and tried to launch a fresh one into the
    now-active candidate slot -- colliding with the real process
    already running there (correctly refused by JobStore's own flock),
    repeatedly, until the bounded restart-attempt budget exhausted and
    the supervisor was left permanently unable to recover a REAL future
    crash of that worker without an operator restarting the whole
    supervisor process.

    Subclasses SupervisorDaemonRealHandoffTests purely to reuse its
    setUp()/fixture-staging helpers -- these are genuinely new test
    methods, not overrides."""

    def test_promoted_candidate_is_tracked_as_active_worker_with_no_duplicate_launch(self):
        self._candidate_behavior = "accept"
        job_uuid = str(uuid.uuid4())
        descriptor_sha256 = self._stage_and_publish_candidate(generation=2, candidate_slot="B")
        self._write_old_worker_fixture(
            job_uuid=job_uuid, candidate_slot="B", generation=2, descriptor_sha256=descriptor_sha256,
        )
        self._start_daemon_thread()

        # Capture the candidate's PID before promotion, so "the exact
        # same process" can be proven, not merely "some process".
        deadline = time.monotonic() + 15
        while self.daemon.candidate_worker is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(self.daemon.candidate_worker, "candidate was never launched")
        candidate_pid = self.daemon.candidate_worker.pid

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.daemon.ipc_server.state
            if state.active_slot.value == "B" and state.activation is None:
                break
            time.sleep(0.05)
        else:
            self.fail(f"handoff never committed; final state: {self.daemon.ipc_server.state}")

        # Give the tick loop a moment to observe the commit and adopt
        # the promoted candidate.
        deadline = time.monotonic() + 5
        while self.daemon.candidate_worker is not None and time.monotonic() < deadline:
            time.sleep(0.02)

        # The promoted process must be tracked as the active worker --
        # the EXACT SAME process, never terminated/relaunched -- and
        # candidate tracking must be cleared.
        self.assertIsNotNone(self.daemon.active_worker, "promotion left no active worker tracked at all")
        self.assertEqual(self.daemon.active_worker.pid, candidate_pid)
        self.assertIsNone(self.daemon.active_worker.poll(), "promoted worker must still be the original live process")
        self.assertIsNone(self.daemon.candidate_worker)

        # The restart-budget tracker must recognize the promoted
        # process too, with a clean (unpoisoned) attempt history --
        # never populated by record_launch()'s own bookkeeping, since
        # promotion is not an ordinary launch.
        self.assertEqual(self.daemon.lifecycle.state.value, "running")
        self.assertEqual(self.daemon.lifecycle.pid, candidate_pid)
        self.assertEqual(self.daemon.lifecycle._attempt_timestamps, [])  # noqa: SLF001

        # This is exactly the window where the real defect spawned a
        # duplicate launch into slot B roughly every poll_interval,
        # forever: let many more ticks pass and prove nothing changes.
        time.sleep(self.daemon.poll_interval * 15)
        self.assertEqual(
            self.daemon.active_worker.pid, candidate_pid,
            "a duplicate worker was launched into the already-promoted slot",
        )
        self.assertIsNone(self.daemon.active_worker.poll())
        self.assertIsNone(self.daemon.candidate_worker)
        self.assertEqual(self.daemon.lifecycle._attempt_timestamps, [])  # noqa: SLF001

    def test_promoted_worker_crash_recovers_normally_afterward(self):
        """The forward-looking half of the same defect: a real future
        crash of the promoted worker must still be recoverable by
        ordinary bounded restart logic -- proving the fix does not
        merely stop the log storm but actually restores real crash
        recovery, which the pre-fix bug silently disabled until an
        operator manually restarted the whole supervisor."""
        self._candidate_behavior = "accept"
        job_uuid = str(uuid.uuid4())
        descriptor_sha256 = self._stage_and_publish_candidate(generation=2, candidate_slot="B")
        self._write_old_worker_fixture(
            job_uuid=job_uuid, candidate_slot="B", generation=2, descriptor_sha256=descriptor_sha256,
        )
        self._start_daemon_thread()

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.daemon.ipc_server.state
            if state.active_slot.value == "B" and state.activation is None:
                break
            time.sleep(0.05)
        else:
            self.fail(f"handoff never committed; final state: {self.daemon.ipc_server.state}")

        deadline = time.monotonic() + 5
        while self.daemon.candidate_worker is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(self.daemon.active_worker)
        promoted_pid = self.daemon.active_worker.pid

        # Replace slot B's entrypoint with a trivial, argv-tolerant
        # fixture before killing the promoted process -- the real
        # crash-recovery relaunch uses ActiveIdentity (never the
        # candidate's --expected-job-uuid flags), and this test cares
        # only about "did a fresh distinct worker get launched", not
        # about re-exercising the activation IPC protocol a second time.
        entrypoint = self.layout.slot_path(Slot.B) / "updaterd.py"
        entrypoint.write_text("import time\ntime.sleep(30)\n")
        entrypoint.chmod(0o755)

        self.daemon.active_worker.terminate()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            worker = self.daemon.active_worker
            if worker is not None and worker.pid != promoted_pid and worker.poll() is None:
                break
            time.sleep(0.05)
        else:
            self.fail(
                "supervisor never recovered a real crash of the promoted active worker -- "
                "the restart budget was likely still poisoned"
            )
        self.assertEqual(self.daemon.lifecycle.pid, self.daemon.active_worker.pid)

    def test_fresh_supervisor_start_with_gen2_already_committed_needs_no_promotion_logic(self):
        """Distinct from in-process promotion: a supervisor process
        that starts up fresh (e.g. after the real operator restart
        that mitigated the production incident) with runtime-state.json
        already showing the committed generation active reads that
        state directly via the ordinary start() path -- activation is
        already None, there is no candidate_worker to adopt, and
        _launch_active_worker() launches the real active slot exactly
        as it always does. This is the exact scenario production's own
        `systemctl restart updater-bootstrapd.service` exercised for
        real; asserted here so it stays covered by an in-repo test
        rather than only having been proven once, manually, in
        production."""
        # IPCServer reads runtime-state.json exactly once, at its own
        # construction time (see ipc_server.py's __init__) -- the
        # setUp()-built self.daemon already loaded the OLD (slot-A)
        # state before this test ever runs, so the desired B/gen2
        # state must be written BEFORE constructing a fresh daemon,
        # not after.
        write_runtime_state_atomically(self.state_path, RuntimeState(
            schema_version=1, active_slot=Slot.B, active_generation=2,
            active_descriptor_sha256="b" * 64, previous_slot=Slot.A,
            previous_generation=1, previous_descriptor_sha256="a" * 64, activation=None,
        ))
        (self.layout.slot_path(Slot.B)).mkdir(parents=True)
        entrypoint = self.layout.slot_path(Slot.B) / "updaterd.py"
        entrypoint.write_text("import time\ntime.sleep(30)\n")
        entrypoint.chmod(0o755)

        import os
        self.daemon = SupervisorDaemon(
            self.bootstrap_config, self.trust_policy, layout=self.layout,
            worker_config_path=self.worker_config_path,
            authorized_uids={os.getuid()}, poll_interval=0.05,
            old_worker_yield_timeout=2, candidate_readiness_timeout=2, candidate_acceptance_timeout=2,
        )
        self.addCleanup(self.daemon.stop)
        self._start_daemon_thread()

        deadline = time.monotonic() + 5
        while self.daemon.active_worker is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(self.daemon.active_worker)
        self.assertIsNone(self.daemon.active_worker.poll())
        self.assertIsNone(self.daemon.candidate_worker)
        self.assertEqual(self.daemon.lifecycle.state.value, "running")

        # No duplicate-launch storm here either -- prove stability the
        # same way as the promotion test above.
        pid = self.daemon.active_worker.pid
        time.sleep(self.daemon.poll_interval * 15)
        self.assertEqual(self.daemon.active_worker.pid, pid)
        self.assertIsNone(self.daemon.active_worker.poll())
