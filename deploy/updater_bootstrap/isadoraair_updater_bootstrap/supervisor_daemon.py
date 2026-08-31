"""Update Center Phase D, D4-A: the real, runnable supervisor event
loop -- what updater_bootstrapd.py actually drives.

Explicit audit boundary (restated from supervisor.py's own docstring,
binding on THIS module too): SupervisorDaemon never performs release-
chain planning, migrations, application Git manipulation, systemd unit
policy, collectstatic, pg_dump, or arbitrary command execution. Its own
job, and only this: load/validate root-owned config and runtime state,
recover any interrupted activation conservatively, own the ONE active
worker process and the private IPC server, react to activation-
transaction phase changes by stopping the old worker at the approved
boundary and launching the candidate, validate candidate readiness
(delegated to ipc_server.py's own independent re-checks), roll the
candidate back pre-acceptance when it fails, and keep running with
whichever worker is now active."""
from __future__ import annotations

import logging
import threading
import time

from .activation import ActivationPhase
from .config import BootstrapConfig
from .ipc_server import IPCServer
from .launch import CandidateIdentity, launch_worker
from .process import TrackedChild
from .slots import SlotLayout
from .supervisor import (
    RecoveryAction, SupervisorError, apply_recovery, fail_transaction,
    finish_rollback, recovery_action_for, request_rollback,
)
from .trust import TrustPolicy
from .worker_lifecycle import WorkerLifecycle, WorkerLifecycleError

LOGGER = logging.getLogger("isadoraair_updater_bootstrap.supervisor_daemon")

DEFAULT_POLL_INTERVAL_SECONDS = 0.2
DEFAULT_OLD_WORKER_YIELD_TIMEOUT_SECONDS = 15
DEFAULT_CANDIDATE_READINESS_TIMEOUT_SECONDS = 30
DEFAULT_CANDIDATE_ACCEPTANCE_TIMEOUT_SECONDS = 60
ENTRYPOINT_NAME = "updaterd.py"


class SupervisorDaemonError(RuntimeError):
    pass


class SupervisorDaemon:
    def __init__(
        self, config: BootstrapConfig, trust_policy: TrustPolicy, *,
        layout: SlotLayout | None = None,
        ipc_server: IPCServer | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        old_worker_yield_timeout: float = DEFAULT_OLD_WORKER_YIELD_TIMEOUT_SECONDS,
        candidate_readiness_timeout: float = DEFAULT_CANDIDATE_READINESS_TIMEOUT_SECONDS,
        candidate_acceptance_timeout: float = DEFAULT_CANDIDATE_ACCEPTANCE_TIMEOUT_SECONDS,
        worker_config_path=None,
        authorized_uids: set[int] | None = None,
        worker_extra_env: dict[str, str] | None = None,
    ):
        self.config = config
        self.layout = layout or SlotLayout(config.slots_root)
        self.ipc_server = ipc_server or IPCServer(config, trust_policy, layout=self.layout, authorized_uids=authorized_uids)
        self.poll_interval = poll_interval
        self.old_worker_yield_timeout = old_worker_yield_timeout
        self.candidate_readiness_timeout = candidate_readiness_timeout
        # D4-K: a candidate that reaches CANDIDATE_READY (readiness
        # independently confirmed) but then simply never calls
        # CONFIRM_RUNTIME_ACCEPTANCE -- hung, wedged, or genuinely
        # unable to reacquire/resume its job -- must not be waited on
        # forever; see _check_candidate_still_alive_while_ready()'s own
        # bounded-timeout rollback, real-bug-discovered-and-fixed
        # during this D4 pass (a candidate that stays alive but never
        # progresses had NO timeout at all before this).
        self.candidate_acceptance_timeout = candidate_acceptance_timeout
        self._candidate_ready_at: float | None = None
        # The station's own worker config path -- a root-owned station.
        # json this daemon never parses itself (D4-A's own "must NOT
        # gain Django/application concerns" boundary); it only passes
        # the PATH through to launch_worker(), exactly like every
        # worker launch already did before Phase D existed.
        self.worker_config_path = worker_config_path
        # Test-only escape hatch: launch_worker() already supports
        # extra_env (used for real by nothing today); exposed here only
        # so an integration harness can hand a worker process a side-
        # channel (e.g. where to record an otherwise-unobservable
        # synthetic mutation) without this daemon interpreting it.
        self.worker_extra_env = worker_extra_env
        self.lifecycle = WorkerLifecycle()
        self.active_worker: TrackedChild | None = None
        self.candidate_worker: TrackedChild | None = None
        self._candidate_deadline: float | None = None
        self._old_worker_stop_requested_at: float | None = None
        self._ipc_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- startup --------------------------------------------------------

    def _recover_initial_state(self) -> None:
        """D4-A: recover any interrupted activation conservatively --
        the ONE decision D2's own recovery_action_for()/apply_recovery()
        already define, applied here at real process startup rather
        than only in a test fixture. Never touches active_slot/
        active_generation; only ever discards an abandoned candidate
        transaction."""
        action = recovery_action_for(self.ipc_server.state)
        if action is not RecoveryAction.NO_ACTION:
            recovered = apply_recovery(self.ipc_server.state, action)
            self.ipc_server._persist(recovered)  # noqa: SLF001 -- same process, same object; this IS the supervisor's own state
            LOGGER.info("recovered interrupted activation transaction: %s", action)

    def _launch_active_worker(self) -> None:
        slot = self.ipc_server.state.active_slot
        self.active_worker = launch_worker(
            self.layout.slot_path(slot), ENTRYPOINT_NAME, config_path=self.worker_config_path,
            extra_env=self.worker_extra_env,
        )
        self.lifecycle.record_launch(pid=self.active_worker.pid)
        LOGGER.info("launched active worker: slot=%s pid=%s", slot.value, self.active_worker.pid)

    def start(self) -> None:
        self._recover_initial_state()
        self._launch_active_worker()
        self._ipc_thread = threading.Thread(target=self.ipc_server.serve_forever, daemon=True, name="supervisor-ipc")
        self._ipc_thread.start()
        self._run_loop()

    def stop(self) -> None:
        self._stop.set()
        self.ipc_server.stop()
        if self._ipc_thread is not None:
            self._ipc_thread.join(timeout=5)
        for child in (self.active_worker, self.candidate_worker):
            if child is not None:
                child.terminate()

    # -- main loop --------------------------------------------------------

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                LOGGER.exception("supervisor event loop tick failed")
            if self._stop.wait(self.poll_interval):
                break

    def _tick(self) -> None:
        with self._lock:
            activation = self.ipc_server.state.activation
            if activation is None:
                self._check_active_worker_liveness()
                return
            phase = activation.phase
            if phase is ActivationPhase.ACTIVATION_REQUESTED:
                self._begin_candidate_launch()
            elif phase is ActivationPhase.CANDIDATE_STARTING:
                self._check_candidate_readiness_timeout()
            elif phase is ActivationPhase.CANDIDATE_READY:
                self._check_candidate_still_alive_while_ready()
            elif phase is ActivationPhase.ROLLBACK_REQUESTED:
                self._finish_rollback()
            # COMMITTED is never observed here: commit_transaction()
            # (called only from ipc_server._handle_confirm_runtime_
            # acceptance, in response to the candidate's own
            # CONFIRM_RUNTIME_ACCEPTANCE) clears activation back to
            # None in THE SAME state transition -- see activation.py's
            # own "COMMITTED is never itself durably persisted as a
            # distinct on-disk phase" docstring. This tick loop
            # observes the RESULT (active_slot already advanced,
            # activation already None) on its very next iteration,
            # exactly like any other post-transaction idle state.

    # -- old worker yield / candidate launch --------------------------------------------------------

    def _begin_candidate_launch(self) -> None:
        if self.candidate_worker is not None:
            return  # already launched for this transaction
        if not self._old_worker_has_yielded():
            return  # keep waiting/escalating -- see _old_worker_has_yielded's own bounded-timeout logic
        self._candidate_ready_at = None
        activation = self.ipc_server.state.activation
        try:
            self.ipc_server.begin_candidate_launch()
        except SupervisorError:
            LOGGER.exception("could not advance to CANDIDATE_STARTING")
            return
        candidate_slot = activation.candidate_slot
        identity = CandidateIdentity(
            slot=candidate_slot.value, generation=activation.candidate_generation,
            descriptor_sha256=activation.candidate_descriptor_sha256,
            # By convention (supervisor_client.py's own docstring) the
            # wire transaction_id the old worker used IS the durable
            # job UUID -- the internal ActivationTransaction.
            # transaction_id is a DIFFERENT, supervisor-minted value
            # (see begin_transaction()); the job UUID the candidate
            # must resume is recovered from the correlation dict the
            # SAME REQUEST_ACTIVATION call already populated.
            job_uuid=self._resumable_job_uuid_for(activation.transaction_id),
        )
        self.candidate_worker = launch_worker(
            self.layout.slot_path(candidate_slot), ENTRYPOINT_NAME,
            config_path=self.worker_config_path, candidate_identity=identity,
            extra_env=self.worker_extra_env,
        )
        self._candidate_deadline = time.monotonic() + self.candidate_readiness_timeout
        LOGGER.info("launched candidate worker: slot=%s pid=%s", candidate_slot.value, self.candidate_worker.pid)

    def _resumable_job_uuid_for(self, internal_transaction_id: str) -> str:
        for wire_id, internal_id in self.ipc_server._client_transactions.items():  # noqa: SLF001
            if internal_id == internal_transaction_id:
                return wire_id
        raise SupervisorDaemonError("no correlated job UUID found for the in-flight activation transaction")

    def _old_worker_has_yielded(self) -> bool:
        """D3-F/D4-A: waits for the old worker to exit VOLUNTARILY
        (daemon.py's own _shutdown_if_old_worker_just_yielded) within
        old_worker_yield_timeout of first observing ACTIVATION_
        REQUESTED; force-terminates it if it has not. Returns True
        once the old worker's process is confirmed gone -- only then
        is it safe to launch a candidate into a DIFFERENT slot (the
        old worker's own slot is never touched; only its PROCESS)."""
        if self.active_worker is None:
            return True
        if self.active_worker.poll() is not None:
            self.lifecycle.record_exit()
            self.lifecycle.acknowledge_exit()
            self.active_worker = None
            self._old_worker_stop_requested_at = None
            return True
        if self._old_worker_stop_requested_at is None:
            self._old_worker_stop_requested_at = time.monotonic()
            return False
        if time.monotonic() - self._old_worker_stop_requested_at >= self.old_worker_yield_timeout:
            LOGGER.warning("old worker did not exit voluntarily within the bound; terminating")
            self.active_worker.terminate()
            self.lifecycle.record_exit()
            self.lifecycle.acknowledge_exit()
            self.active_worker = None
            self._old_worker_stop_requested_at = None
            return True
        return False

    # -- candidate supervision --------------------------------------------------------

    def _check_candidate_readiness_timeout(self) -> None:
        if self.candidate_worker is not None and self.candidate_worker.poll() is not None:
            self._rollback("candidate process exited before reporting readiness")
            return
        if self._candidate_deadline is not None and time.monotonic() >= self._candidate_deadline:
            self._rollback("candidate did not report readiness within the bound")

    def _check_candidate_still_alive_while_ready(self) -> None:
        if self.candidate_worker is not None and self.candidate_worker.poll() is not None:
            self._rollback("candidate process exited after reporting ready but before runtime acceptance")
            return
        if self._candidate_ready_at is None:
            self._candidate_ready_at = time.monotonic()
            return
        if time.monotonic() - self._candidate_ready_at >= self.candidate_acceptance_timeout:
            self._rollback("candidate reported ready but never confirmed runtime acceptance within the bound")

    def _rollback(self, reason: str) -> None:
        """D2-K/D4-K: pre-acceptance rollback -- legal only from
        CANDIDATE_STARTING/CANDIDATE_READY (activation.py's own
        PHASES_WITH_A_LIVE_CANDIDATE_PROCESS), both of which are the
        only phases this method is ever called from. Never automatic
        after runtime_activation_accepted -- see ipc_server.py's own
        commit_transaction() call site, which is the ONLY path past
        this phase, and D3-N/D4-K's own "never automatic runtime
        downgrade after runtime_activation_accepted" invariant."""
        LOGGER.warning("rolling back candidate activation: %s", reason)
        if self.candidate_worker is not None:
            self.candidate_worker.terminate()
            self.candidate_worker = None
        self._candidate_deadline = None
        self._candidate_ready_at = None
        try:
            self.ipc_server._persist(request_rollback(self.ipc_server.state, reason=reason))  # noqa: SLF001
        except SupervisorError:
            self.ipc_server._persist(fail_transaction(self.ipc_server.state))  # noqa: SLF001
            self._restart_active_worker_if_needed()
            return
        self._finish_rollback()

    def _finish_rollback(self) -> None:
        self.ipc_server._persist(finish_rollback(self.ipc_server.state))  # noqa: SLF001
        self._restart_active_worker_if_needed()

    def _restart_active_worker_if_needed(self) -> None:
        """The previously-active slot's own worker may have already
        exited (the OLD worker yields voluntarily as soon as
        activation is REQUESTED, well before the supervisor knows
        whether the candidate will ever become ready) -- if so, a
        fresh process must be started from that SAME still-intact LKG
        slot so the station is never left with no worker running at
        all. D2-M's own retention rule (the previous LKG slot's
        content is never deleted) is exactly what makes this always
        possible."""
        if self.active_worker is not None and self.active_worker.poll() is None:
            return
        try:
            self.lifecycle.require_can_launch()
        except WorkerLifecycleError:
            LOGGER.error("bounded restart attempts exhausted; not relaunching the active worker")
            return
        self._launch_active_worker()

    def _check_active_worker_liveness(self) -> None:
        if self.active_worker is None:
            self._launch_active_worker()
            return
        if self.active_worker.poll() is None:
            return
        LOGGER.warning("active worker exited unexpectedly (no activation in flight); restarting")
        self.lifecycle.record_exit()
        self.lifecycle.acknowledge_exit()
        self.active_worker = None
        self._restart_active_worker_if_needed()
