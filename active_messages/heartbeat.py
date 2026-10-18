"""The active-message worker's heartbeat and readiness contract (iPortal D3E-A).

The established IsadoraAir pattern (aircheck's buffer state, Liquidsoap's
state file, Monitoring's own self-health): the WORKER PROCESS writes one
small, fixed-shape JSON state file -- atomically (write, fsync, rename) and
best-effort (a write failure never affects the worker) -- and readers only
read it. No database row, no migration: the state is per process and
ephemeral, and a database heartbeat would put a write into every 5-second
cycle of the very database the engine's admission path locks.

Written (``Recorder``):

* at start-up (``phase: "starting"``; ``recent_starts`` counts restarts within
  CRASH_WINDOW_SECONDS, read from the previous file, which survives a process
  restart under /run);
* just before each render (``phase: "rendering"`` with ``render_deadline``:
  a render may legitimately take up to its own bounded timeout, so "time since
  the last record" alone is NOT the heartbeat while one runs);
* after every loop cycle (``phase: "polling"``, the pass's result, code and
  execution, how many consecutive passes had that result, the last progress
  and consecutive database errors);
* on a clean stop (``phase: "stopped"``).

Read (``assess`` -- pure, given the state, the clock and the gate; and
``readiness`` -- reads the file and the gate WITHOUT any write; observing the
worker never renders, expires or creates anything). Conditions:

| condition | status | meaning |
| --- | --- | --- |
| idle | ok | polling; nothing is due |
| ready_waiting_for_gate | ok | a READY aggregate waits behind the CLOSED native-airing gate -- expected until Phase G, not a failure |
| working | ok | the last pass made progress (rendered, reused, failed, superseded) |
| rendering | ok | a render is in progress and within its own deadline |
| retry_backoff | ok | a retryable render failure is backing off |
| playout_in_flight | ok | an execution past rendering is with publication / the engine |
| busy | ok | another holder of the worker lock skipped this pass |
| lock_held_elsewhere | warning | ... for BUSY_WARNING_REPEATS consecutive passes |
| blocked | warning | the current composition failed permanently; an operator change is needed (its SystemEvent says which) |
| database_errors | warning | a pass failed on the database; the loop closed its connection and waits |
| malfunction | critical | ERROR_CRITICAL_REPEATS consecutive database failures |
| render_overdue | critical | a render ran past its deadline |
| stale | critical | no record for STALE_AFTER_SECONDS while polling (wedged or dead) |
| crash_looping | critical | CRASH_LOOP_STARTS starts within CRASH_WINDOW_SECONDS |
| stopped | critical | the process stopped cleanly (a managed unit should be running) |
| no_heartbeat | unknown | no readable state file |

``detail["native_airing"]`` is "closed" or "open" -- informational: a closed
gate never degrades the status. D3E-B wires this into the managed unit and a
Monitoring probe (with systemd's own ActiveState / NRestarts, as
monitoring/services/self_health.py does for the Monitoring service).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from django.conf import settings

SCHEMA = 1
DEFAULT_STATE_PATH = "/run/isadoraair/active_message_worker.json"
# A non-rendering pass takes seconds; the loop records every cycle (at most
# POLL_SECONDS apart while waiting): a minute without a record is wedged.
STALE_AFTER_SECONDS = 60
# Beyond the render's own bound (its timeout plus the render client's grace).
RENDER_GRACE_SECONDS = 120
CRASH_WINDOW_SECONDS = 900
CRASH_LOOP_STARTS = 3
BUSY_WARNING_REPEATS = 12
ERROR_CRITICAL_REPEATS = 3
PROGRESS_RESULTS = frozenset({"ready", "reused", "failed", "superseded"})

_current = None


def state_path() -> Path:
    return Path(getattr(settings, "ACTIVE_MESSAGE_WORKER_STATE_PATH", DEFAULT_STATE_PATH))


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path, data) -> bool:
    """Atomic, best-effort: a reader never sees a partial file, and a failure
    (e.g. no /run/isadoraair) never reaches the worker."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def current():
    """The running worker loop's recorder, or None (single passes, tests)."""
    return _current


class Recorder:
    def __init__(self, path=None, *, clock=time.time, pid=None):
        global _current
        self.path = Path(path) if path is not None else state_path()
        self.clock = clock
        now = clock()
        prior = _read(self.path) or {}
        previous_start = prior.get("started_at")
        recent = 1
        if isinstance(previous_start, (int, float)) and now - previous_start <= CRASH_WINDOW_SECONDS:
            recent = int(prior.get("recent_starts") or 0) + 1
        self.state = {
            "schema": SCHEMA, "pid": pid if pid is not None else os.getpid(), "started_at": now,
            "recent_starts": recent, "phase": "starting", "checked_at": now, "cycle": 0,
            "result": "", "code": "", "execution": "", "repeats": 0, "last_progress_at": None,
            "consecutive_errors": 0, "render_deadline": None,
        }
        self._save()
        _current = self

    def _save(self):
        return _write(self.path, self.state)

    def cycle(self, outcome):
        """After every loop cycle (``outcome``: a worker.Outcome)."""
        now = self.clock()
        same = outcome.result == self.state["result"]
        self.state.update(
            phase="polling", checked_at=now, cycle=self.state["cycle"] + 1, result=outcome.result,
            code=outcome.code, execution=outcome.execution, repeats=self.state["repeats"] + 1 if same else 1,
            render_deadline=None,
            consecutive_errors=self.state["consecutive_errors"] + 1 if outcome.result == "error" else 0)
        if outcome.result in PROGRESS_RESULTS:
            self.state["last_progress_at"] = now
        self._save()

    def rendering(self, execution_id, timeout_seconds):
        """Just before a render: it may legitimately last until its deadline."""
        now = self.clock()
        self.state.update(phase="rendering", checked_at=now, execution=str(execution_id),
                          render_deadline=now + float(timeout_seconds) + RENDER_GRACE_SECONDS)
        self._save()

    def stopped(self):
        global _current
        self.state.update(phase="stopped", checked_at=self.clock(), render_deadline=None)
        self._save()
        if _current is self:
            _current = None


def assess(state, *, now, gate_open) -> dict:
    """The readiness verdict for one state document (see the module table)."""
    def verdict(status, condition, **extra):
        detail = {"native_airing": "open" if gate_open else "closed"}
        if state:
            detail.update({key: state.get(key) for key in ("pid", "phase", "result", "code", "execution",
                                                           "repeats", "recent_starts", "consecutive_errors")})
            if isinstance(state.get("checked_at"), (int, float)):
                detail["age_seconds"] = round(now - state["checked_at"], 1)
        detail.update(extra)
        return {"status": status, "condition": condition, "detail": detail}

    if not state or state.get("schema") != SCHEMA or not isinstance(state.get("checked_at"), (int, float)):
        return verdict("unknown", "no_heartbeat")
    if state.get("phase") == "stopped":
        return verdict("critical", "stopped")
    if int(state.get("recent_starts") or 0) >= CRASH_LOOP_STARTS:
        return verdict("critical", "crash_looping")
    if state.get("phase") == "rendering":
        deadline = state.get("render_deadline")
        if not isinstance(deadline, (int, float)) or now > deadline:
            return verdict("critical", "render_overdue")
        return verdict("ok", "rendering")
    if now - state["checked_at"] > STALE_AFTER_SECONDS:
        return verdict("critical", "stale")
    errors = int(state.get("consecutive_errors") or 0)
    if errors >= ERROR_CRITICAL_REPEATS:
        return verdict("critical", "malfunction")
    if errors:
        return verdict("warning", "database_errors")
    result, code = state.get("result"), state.get("code")
    if result == "blocked":
        return verdict("warning", "blocked")
    if result == "busy":
        if int(state.get("repeats") or 0) >= BUSY_WARNING_REPEATS:
            return verdict("warning", "lock_held_elsewhere")
        return verdict("ok", "busy")
    if result in ("backoff", "retry"):
        return verdict("ok", "retry_backoff")
    if result == "playout":
        return verdict("ok", "playout_in_flight")
    if result in PROGRESS_RESULTS:
        return verdict("ok", "working")
    if result == "idle":
        return verdict("ok", "ready_waiting_for_gate" if code == "gated" else "idle")
    return verdict("ok", "idle")                     # "starting": the first pass has not finished yet


def gate_open_read_only() -> bool:
    """The native-airing gate, read WITHOUT creating the policy singleton
    (services.current_policy would insert it if missing)."""
    from .models import POLICY_ID, ActiveBroadcastPolicy
    from .services import NATIVE_AIRING_AVAILABLE
    if not NATIVE_AIRING_AVAILABLE:
        return False
    return bool(ActiveBroadcastPolicy.objects.filter(pk=POLICY_ID)
                .values_list("native_airing_enabled", flat=True).first())


def readiness(*, path=None, now=None) -> dict:
    """Read-only: the state file and the gate, assessed. Never renders,
    expires or creates anything."""
    return assess(_read(Path(path) if path is not None else state_path()),
                  now=time.time() if now is None else now, gate_open=gate_open_read_only())
