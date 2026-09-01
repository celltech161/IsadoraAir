"""D2 corrective review, Correction 4: worker process lifecycle
ownership.

The worker is no longer its own systemd service under Phase D -- the
supervisor is now the ONLY thing that can ever have launched it, so
the supervisor is also the ONLY thing that can accidentally leave an
old one running while starting a new one (systemd itself used to
guarantee "at most one instance" for a Type=simple service; that
guarantee does not exist automatically anymore once launching is this
package's own responsibility). This module is a small, pure POLICY
tracker answering exactly one question the supervisor's future real
event loop (D3) must consult before ever calling launch.launch_worker()
again: is starting a NEW worker legal right now, given whatever this
tracker currently believes about the CURRENT one?

Not yet wired to a real event loop or to process.TrackedChild directly
in this phase (D2-S's own scope boundary: no real worker handoff yet)
-- but the STATE MACHINE and its refusal rules are real and tested now,
so D3's wiring only has to call into an already-proven policy, not
invent one under schedule pressure."""
from __future__ import annotations

import dataclasses
import enum
import time


class WorkerLifecycleState(enum.Enum):
    NONE = "none"          # no worker has ever been launched, or the
                            # previous one's exit has been fully
                            # acknowledged -- launching is legal.
    RUNNING = "running"     # a worker is currently tracked as alive --
                            # launching a second one is illegal.
    EXITED_UNACKNOWLEDGED = "exited_unacknowledged"  # the tracked
                            # worker has exited (observed via poll()),
                            # but that fact has not yet been explicitly
                            # acknowledged -- launching is still
                            # illegal until acknowledge_exit() runs, so
                            # a caller cannot accidentally race past
                            # cleanup (e.g. reading a final readiness/
                            # exit-code fact) by launching immediately.


class WorkerLifecycleError(RuntimeError):
    pass


DEFAULT_MAX_CONSECUTIVE_RESTART_ATTEMPTS = 5
DEFAULT_RESTART_ATTEMPT_WINDOW_SECONDS = 300


@dataclasses.dataclass
class WorkerLifecycle:
    """Tracks exactly one logical worker slot's lifecycle. One instance
    per "the currently active worker this supervisor process owns" --
    never a collection, since D2's own acceptance target is "no
    duplicate simultaneous active workers," not "manage a pool.\""""

    state: WorkerLifecycleState = WorkerLifecycleState.NONE
    pid: int | None = None
    max_consecutive_restart_attempts: int = DEFAULT_MAX_CONSECUTIVE_RESTART_ATTEMPTS
    restart_attempt_window_seconds: float = DEFAULT_RESTART_ATTEMPT_WINDOW_SECONDS
    _attempt_timestamps: list[float] = dataclasses.field(default_factory=list)

    def can_launch(self) -> bool:
        return self.state is WorkerLifecycleState.NONE

    def require_can_launch(self, *, now: float | None = None) -> None:
        if not self.can_launch():
            raise WorkerLifecycleError(
                f"cannot launch a new worker while lifecycle state is {self.state.value!r} "
                "-- a previously-launched worker must be observed exited AND acknowledged first"
            )
        self._prune_old_attempts(now)
        if len(self._attempt_timestamps) >= self.max_consecutive_restart_attempts:
            raise WorkerLifecycleError(
                f"refusing to launch: {len(self._attempt_timestamps)} restart attempts already "
                f"recorded within the last {self.restart_attempt_window_seconds}s "
                f"(bound: {self.max_consecutive_restart_attempts})"
            )

    def record_launch(self, pid: int, *, now: float | None = None) -> None:
        """Called immediately after a real launch.launch_worker() call
        succeeds (not yet wired here -- see this module's own
        docstring). Refuses if a launch is not currently legal, exactly
        mirroring require_can_launch() so a caller cannot bypass the
        check by simply not calling it first."""
        self.require_can_launch(now=now)
        self._attempt_timestamps.append(now if now is not None else time.monotonic())
        self.state = WorkerLifecycleState.RUNNING
        self.pid = pid

    def _prune_old_attempts(self, now: float | None) -> None:
        current = now if now is not None else time.monotonic()
        cutoff = current - self.restart_attempt_window_seconds
        self._attempt_timestamps = [t for t in self._attempt_timestamps if t >= cutoff]

    def record_exit(self) -> None:
        """Called once the supervisor has observed (via TrackedChild.
        poll() returning a non-None exit code) that the tracked worker
        has actually exited -- covers both a normal exit and a crash;
        this module does not distinguish them, since "may a new worker
        be launched now" is the same answer either way (no) until
        acknowledged."""
        if self.state is not WorkerLifecycleState.RUNNING:
            raise WorkerLifecycleError(
                f"record_exit() called while lifecycle state is {self.state.value!r}, not 'running'"
            )
        self.state = WorkerLifecycleState.EXITED_UNACKNOWLEDGED

    def acknowledge_exit(self) -> None:
        """Explicit, separate step from record_exit() -- see D2-J's own
        readiness/exit distinctions: a caller must have actually
        finished dealing with the exited worker (logged its final
        state, decided whether this was a rollback/failure/normal
        stop) before a new launch becomes legal again."""
        if self.state is not WorkerLifecycleState.EXITED_UNACKNOWLEDGED:
            raise WorkerLifecycleError(
                f"acknowledge_exit() called while lifecycle state is {self.state.value!r}, "
                "not 'exited_unacknowledged'"
            )
        self.state = WorkerLifecycleState.NONE
        self.pid = None

    def reset_restart_attempt_history(self) -> None:
        """An operator/supervisor-restart-level reset of the bounded-
        restart-attempt counter -- never called automatically by this
        module itself, so a caller cannot silently defeat the bound by
        calling it in a loop."""
        self._attempt_timestamps = []

    def adopt_running_worker(self, pid: int) -> None:
        """A successful protected-runtime candidate promotion, NOT an
        ordinary launch -- the process named by `pid` is already
        running and already independently proven (readiness +
        acceptance, both re-verified by the supervisor itself) BEFORE
        this is ever called; this method only makes THIS tracker
        recognize a process it did not itself launch via
        record_launch(). Deliberately distinct from record_launch():
        it does not consult require_can_launch() (a promotion is never
        refused by the restart-attempt bound -- that bound exists to
        stop a crash-loop of NEW launches, not to block adopting an
        already-healthy incumbent) and it does not append to
        `_attempt_timestamps` (adopting is not itself a "restart
        attempt").

        Deliberately unconditional on the CURRENT state (works from
        NONE, RUNNING, or EXITED_UNACKNOWLEDGED alike): the promoted
        process belongs to a different generation than whatever this
        tracker most recently knew about, so that prior state is
        never authoritative for it. Clears the restart-attempt history
        too -- any attempts recorded before promotion describe the
        OLD generation (or, if the supervisor's own post-commit
        bookkeeping had a bug, phantom relaunch attempts into the
        candidate's own slot); either way they must never count
        against the newly-promoted worker's own future crash-recovery
        budget."""
        self.state = WorkerLifecycleState.RUNNING
        self.pid = pid
        self._attempt_timestamps = []
