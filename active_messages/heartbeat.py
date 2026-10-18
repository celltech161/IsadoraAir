"""The active-message worker's heartbeat and readiness contract (iPortal D3E-A).

The established IsadoraAir pattern (aircheck's buffer state, Liquidsoap's
state file, Monitoring's own self-health): the WORKER PROCESS writes one
small, fixed-shape JSON document -- atomically and best-effort (a write
failure never affects the worker) -- and readers only read it. No database
row, no migration: the state is per process and ephemeral, and a database
heartbeat would put a write into every cycle of the database the engine's
admission path locks.

Document (``SCHEMA`` 2; every key required, no other key allowed; at most
MAX_DOCUMENT_BYTES):

| key | type and rule |
| --- | --- |
| schema | int, exactly 2 |
| pid | int 1..2**31-1 |
| started_at, checked_at | finite epoch seconds in [MIN_TIMESTAMP, MAX_TIMESTAMP]; started_at <= checked_at |
| recent_starts | list of 1..MAX_STARTS such timestamps, nondecreasing, the last == started_at, none older than started_at - CRASH_WINDOW_SECONDS: this process's start and the UNCLEAN starts before it |
| previous | how the previous process ended: "none" (no history), "clean" (a recorded stop), "unclean" (it never recorded a stop), "malformed" (its document was unreadable or invalid -- history unverified) |
| phase | "starting", "polling", "rendering" or "stopped" |
| cycle | int 0..MAX_COUNTER: completed loop cycles |
| result, code, execution | the last cycle's worker outcome: result one of RESULTS ("" only before the first cycle), code [A-Za-z0-9_]{0,64}, execution "" or a UUID |
| repeats | int: consecutive cycles with that result (0 before the first cycle, else 1..cycle) |
| consecutive_errors | int: == repeats when the result is "error", else 0 |
| last_progress_at | null or a timestamp in [started_at, checked_at] |
| render_deadline | a timestamp exactly when phase is "rendering" (after checked_at, at most RENDER_DEADLINE_MAX_SECONDS later), else null |

Relations (the writer's own state rules): unless ``previous`` is "unclean",
``recent_starts`` is exactly [started_at]; "polling" needs cycle >= 1;
"starting" has checked_at == started_at; before the first completed cycle
(cycle 0) there is no result, code, counter or last_progress_at, and an
execution only while "rendering" (a render during the first pass).

"starting" means cycle 0 with no result yet. A document that breaks ANY rule
is "malformed" (critical) -- it is never partially interpreted -- and a
missing or unreadable one is "no_heartbeat" (unknown). Neither can ever be
"ok".

Written (``Recorder``) at start-up, just before each render (with its
deadline: a render may legitimately last up to its own bounded timeout), after
every loop cycle, and on a clean stop. File safety: a random temporary name
created exclusively by ``tempfile.mkstemp`` (O_CREAT|O_EXCL: a pre-planted
file or symlink at any name is never opened, let alone followed), mode 0600,
verified regular by ``fstat``, written and fsync'ed through that descriptor,
then ``os.replace``'d over the heartbeat path in the same directory (which
replaces a symlink there, never its target); the temporary file is removed on
any failure. Read (``read_state``) with O_NOFOLLOW|O_NONBLOCK, ``fstat``
(regular files only) and a size cap before parsing; NaN/Infinity are refused.
A concurrent replacement is seen as either the old or the new complete file.

The directory: on a station the existing tmpfiles.d entry
``d /run/isadoraair 0755 <station user> <station user>`` (deploy/isadoraair-tmpfiles.conf),
shared by several services; the worker and Monitoring both run as the station
user, so a 0600 file is readable by Monitoring and nobody else. D3E-B's unit
must use that directory as it is -- not a unit-owned RuntimeDirectory, which
would conflict with the shared one -- so the document survives a worker
restart (crash-loop history) and is cleared only at boot.

Crash-loop accounting: a start appends to ``recent_starts`` only after an
UNCLEAN end (the previous document exists, is valid and is not "stopped");
a recorded clean stop, a missing history and a malformed history all start a
fresh sequence (``previous`` says which). A start keeps only previous starts
within CRASH_WINDOW_SECONDS of itself, at most MAX_STARTS. ``assess`` counts
the starts within CRASH_WINDOW_SECONDS of the OBSERVATION time (a true sliding
window, inclusive at exactly the window): CRASH_LOOP_STARTS or more is
"crash_looping".

Read (``assess`` -- pure; ``readiness`` -- reads the file and the gate with
SELECTs only; observing the worker never renders, expires or creates
anything):

| condition | status | meaning |
| --- | --- | --- |
| idle | ok | polling; nothing is due |
| ready_waiting_for_gate | ok | a READY aggregate waits behind the CLOSED native-airing gate -- expected until Phase G |
| working | ok | the last pass made progress (rendered, reused, failed, superseded) |
| rendering | ok | a render is in progress and within its deadline |
| retry_backoff | ok | a retryable render failure is backing off |
| playout_in_flight | ok | an execution past rendering is with publication / the engine |
| busy | ok | another holder of the worker lock skipped this pass |
| history_unverified | warning | the previous document was malformed: restart history is unknown (for CRASH_WINDOW_SECONDS after the start) |
| lock_held_elsewhere | warning | BUSY_WARNING_REPEATS consecutive busy passes |
| blocked | warning | the current composition failed permanently; an operator change is needed |
| database_errors | warning | a pass failed on the database; the loop closed its connection and waits |
| malfunction | critical | ERROR_CRITICAL_REPEATS consecutive database failures |
| render_overdue | critical | a render ran past its deadline |
| stale | critical | no record for STALE_AFTER_SECONDS while not rendering (wedged or dead) |
| crash_looping | critical | CRASH_LOOP_STARTS unclean starts within the window |
| stopped | critical | the process stopped cleanly (a managed unit should be running) |
| malformed | critical | the document breaks the contract (detail.reason says how) |
| no_heartbeat | unknown | no document, or it cannot be read |

``detail["native_airing"]`` is "closed" or "open" -- informational: a closed
gate never degrades the status.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

SCHEMA = 2
DEFAULT_STATE_PATH = "/run/isadoraair/active_message_worker.json"
MAX_DOCUMENT_BYTES = 4096
MIN_TIMESTAMP = 946684800.0          # 2000-01-01
MAX_TIMESTAMP = 4102444800.0         # 2100-01-01
MAX_FUTURE_SKEW_SECONDS = 30
MAX_COUNTER = 10 ** 12
MAX_STARTS = 8
# A non-rendering pass takes seconds and the loop records every cycle (its
# waits are at most MAX_IDLE_SECONDS): a minute without a record is wedged.
STALE_AFTER_SECONDS = 60
# Beyond the render's own bound (its timeout plus the render client's grace).
RENDER_GRACE_SECONDS = 120
RENDER_TIMEOUT_MAX_SECONDS = 3600 + 30            # protocol.MAX_TIMEOUT_SECONDS + client.RESPONSE_GRACE_SECONDS
RENDER_DEADLINE_MAX_SECONDS = RENDER_TIMEOUT_MAX_SECONDS + RENDER_GRACE_SECONDS
CRASH_WINDOW_SECONDS = 900
CRASH_LOOP_STARTS = 3
BUSY_WARNING_REPEATS = 12
ERROR_CRITICAL_REPEATS = 3
PROGRESS_RESULTS = frozenset({"ready", "reused", "failed", "superseded"})
RESULTS = frozenset({"busy", "idle", "blocked", "backoff", "retry", "playout", "error"}) | PROGRESS_RESULTS
PHASES = frozenset({"starting", "polling", "rendering", "stopped"})
PREVIOUS = frozenset({"none", "clean", "unclean", "malformed"})
KEYS = frozenset({"schema", "pid", "started_at", "checked_at", "recent_starts", "previous", "phase", "cycle",
                  "result", "code", "execution", "repeats", "consecutive_errors", "last_progress_at",
                  "render_deadline"})
_CODE = re.compile(r"[A-Za-z0-9_]{0,64}")

_current = None


class Malformed(ValueError):
    """The document breaks the contract; ``args[0]`` is a stable reason."""


@dataclass(frozen=True)
class Heartbeat:
    pid: int
    started_at: float
    checked_at: float
    recent_starts: tuple
    previous: str
    phase: str
    cycle: int
    result: str
    code: str
    execution: str
    repeats: int
    consecutive_errors: int
    last_progress_at: float | None
    render_deadline: float | None


# -- the strict decoder ------------------------------------------------------------------

def _integer(doc, key, low, high) -> int:
    value = doc[key]
    if type(value) is not int or not low <= value <= high:
        raise Malformed(f"bad_{key}")
    return value


def _timestamp(value, key) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not MIN_TIMESTAMP <= value <= MAX_TIMESTAMP:
        raise Malformed(f"bad_{key}")
    return float(value)


def _text(doc, key) -> str:
    value = doc[key]
    if type(value) is not str:
        raise Malformed(f"bad_{key}")
    return value


def validate(doc) -> Heartbeat:
    """The fully validated heartbeat, or Malformed -- never a partial reading."""
    if type(doc) is not dict:
        raise Malformed("not_an_object")
    if set(doc) != KEYS:
        raise Malformed("missing_key" if KEYS - set(doc) else "unexpected_key")
    if type(doc["schema"]) is not int or doc["schema"] != SCHEMA:
        raise Malformed("unsupported_schema")
    pid = _integer(doc, "pid", 1, 2 ** 31 - 1)
    started_at = _timestamp(doc["started_at"], "started_at")
    checked_at = _timestamp(doc["checked_at"], "checked_at")
    if checked_at < started_at:
        raise Malformed("checked_before_start")
    starts = doc["recent_starts"]
    if type(starts) is not list or not 1 <= len(starts) <= MAX_STARTS:
        raise Malformed("bad_recent_starts")
    starts = tuple(_timestamp(value, "recent_starts") for value in starts)
    if (list(starts) != sorted(starts) or starts[-1] != started_at
            or starts[0] < started_at - CRASH_WINDOW_SECONDS):
        raise Malformed("bad_recent_starts")
    previous, phase = _text(doc, "previous"), _text(doc, "phase")
    if previous not in PREVIOUS:
        raise Malformed("bad_previous")
    if phase not in PHASES:
        raise Malformed("bad_phase")
    cycle = _integer(doc, "cycle", 0, MAX_COUNTER)
    result, code, execution = _text(doc, "result"), _text(doc, "code"), _text(doc, "execution")
    repeats = _integer(doc, "repeats", 0, MAX_COUNTER)
    errors = _integer(doc, "consecutive_errors", 0, MAX_COUNTER)
    if not _CODE.fullmatch(code):
        raise Malformed("bad_code")
    if execution:
        try:
            if str(uuid.UUID(execution)) != execution:
                raise ValueError
        except ValueError:
            raise Malformed("bad_execution") from None
    if cycle == 0:
        if result or code or repeats or errors:
            raise Malformed("result_before_first_cycle")
    else:
        if result not in RESULTS:
            raise Malformed("bad_result")
        if not 1 <= repeats <= cycle:
            raise Malformed("bad_repeats")
        if errors != (repeats if result == "error" else 0):
            raise Malformed("bad_consecutive_errors")
    if phase == "starting" and cycle != 0:
        raise Malformed("starting_after_a_cycle")
    progress = doc["last_progress_at"]
    if progress is not None:
        progress = _timestamp(progress, "last_progress_at")
        if not started_at <= progress <= checked_at:
            raise Malformed("bad_last_progress_at")
    deadline = doc["render_deadline"]
    if phase == "rendering":
        if deadline is None:
            raise Malformed("rendering_without_deadline")
        deadline = _timestamp(deadline, "render_deadline")
        if not checked_at < deadline <= checked_at + RENDER_DEADLINE_MAX_SECONDS or not execution:
            raise Malformed("bad_render_deadline")
    elif deadline is not None:
        raise Malformed("deadline_outside_rendering")
    # Relations the writer guarantees (Recorder): only an unclean end carries
    # earlier starts; a document records "polling" only after completing a
    # cycle; "starting" is written once, at the start; before the first cycle
    # completes there is no progress, and an execution only while rendering.
    if previous != "unclean" and len(starts) != 1:
        raise Malformed("history_without_unclean_restart")
    if phase == "polling" and cycle == 0:
        raise Malformed("polling_before_first_cycle")
    if phase == "starting" and checked_at != started_at:
        raise Malformed("starting_after_its_start")
    if cycle == 0:
        if progress is not None:
            raise Malformed("progress_before_first_cycle")
        if execution and phase != "rendering":
            raise Malformed("execution_before_first_cycle")
    return Heartbeat(pid=pid, started_at=started_at, checked_at=checked_at, recent_starts=starts,
                     previous=previous, phase=phase, cycle=cycle, result=result, code=code, execution=execution,
                     repeats=repeats, consecutive_errors=errors, last_progress_at=progress, render_deadline=deadline)


# -- file access ---------------------------------------------------------------------------

def state_path() -> Path:
    return Path(getattr(settings, "ACTIVE_MESSAGE_WORKER_STATE_PATH", DEFAULT_STATE_PATH))


def _refuse_constant(name):
    raise ValueError(f"non-finite number {name}")


def read_state(path):
    """(document or None, reason): reason is "" (a JSON object, NOT yet
    validated), "missing", "unreadable", "not_a_regular_file", "oversized"
    or "invalid_json". Never follows a symlink, never blocks on a FIFO,
    never raises."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        import errno
        return None, "not_a_regular_file" if exc.errno == errno.ELOOP else "unreadable"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "not_a_regular_file"
        if info.st_size > MAX_DOCUMENT_BYTES:
            return None, "oversized"
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, MAX_DOCUMENT_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_DOCUMENT_BYTES:
                return None, "oversized"
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)
    try:
        document = json.loads(b"".join(chunks).decode("utf-8"), parse_constant=_refuse_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "invalid_json"
    return document, ""


def _write(path, data) -> bool:
    """Atomic, exclusive, best-effort (see the module docstring); never raises."""
    path = Path(path)
    try:
        payload = json.dumps(data, sort_keys=True, allow_nan=False).encode()
    except ValueError:
        return False
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    except OSError:
        return False
    try:
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("the temporary file is not a regular file")
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        return True
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False


def current():
    """The running worker loop's recorder, or None (single passes, tests)."""
    return _current


# -- the writer ------------------------------------------------------------------------------

def _code(value) -> str:
    text = str(value or "")
    return text[:64] if _CODE.fullmatch(text[:64]) else "unrecognized"


def _execution(value) -> str:
    try:
        return str(uuid.UUID(str(value))) if value else ""
    except ValueError:
        return ""


class Recorder:
    def __init__(self, path=None, *, clock=time.time, pid=None):
        global _current
        self.path = Path(path) if path is not None else state_path()
        self.clock = clock
        now = float(clock())
        document, reason = read_state(self.path)
        starts = [now]
        if document is None:
            previous = "none" if reason == "missing" else "malformed"
        else:
            try:
                prior = validate(document)
            except Malformed:
                previous = "malformed"
            else:
                if prior.phase == "stopped":
                    previous = "clean"                    # a recorded clean stop ends the sequence
                else:
                    previous = "unclean"
                    kept = [start for start in prior.recent_starts if 0 <= now - start <= CRASH_WINDOW_SECONDS]
                    starts = (kept + [now])[-MAX_STARTS:]
        self.state = {
            "schema": SCHEMA, "pid": pid if pid is not None else os.getpid(), "started_at": now,
            "checked_at": now, "recent_starts": starts, "previous": previous, "phase": "starting", "cycle": 0,
            "result": "", "code": "", "execution": "", "repeats": 0, "consecutive_errors": 0,
            "last_progress_at": None, "render_deadline": None,
        }
        self._save()
        _current = self

    def _save(self) -> bool:
        try:
            validate(self.state)                          # never publish a document readers would reject
        except Malformed:
            return False
        return _write(self.path, self.state)

    def cycle(self, outcome):
        """After every loop cycle (``outcome``: a worker.Outcome)."""
        now = max(float(self.clock()), self.state["checked_at"])
        result = outcome.result if outcome.result in RESULTS else "error"
        repeats = self.state["repeats"] + 1 if result == self.state["result"] else 1
        self.state.update(
            phase="polling", checked_at=now, cycle=min(self.state["cycle"] + 1, MAX_COUNTER), result=result,
            code=_code(outcome.code), execution=_execution(outcome.execution), repeats=min(repeats, MAX_COUNTER),
            consecutive_errors=min(repeats, MAX_COUNTER) if result == "error" else 0, render_deadline=None)
        if result in PROGRESS_RESULTS:
            self.state["last_progress_at"] = now
        self._save()

    def rendering(self, execution_id, timeout_seconds):
        """Just before a render: it may legitimately last until its deadline."""
        now = max(float(self.clock()), self.state["checked_at"])
        timeout = min(max(float(timeout_seconds), 0.0), float(RENDER_TIMEOUT_MAX_SECONDS))
        self.state.update(phase="rendering", checked_at=now, execution=_execution(execution_id),
                          render_deadline=now + timeout + RENDER_GRACE_SECONDS)
        self._save()

    def stopped(self):
        global _current
        self.state.update(phase="stopped", checked_at=max(float(self.clock()), self.state["checked_at"]),
                          render_deadline=None)
        self._save()
        if _current is self:
            _current = None


# -- the reader ------------------------------------------------------------------------------

def _verdict(status, condition, gate_open, heartbeat=None, now=None, **extra):
    detail = {"native_airing": "open" if gate_open else "closed"}
    if heartbeat is not None:
        detail.update(pid=heartbeat.pid, phase=heartbeat.phase, result=heartbeat.result, code=heartbeat.code,
                      execution=heartbeat.execution, repeats=heartbeat.repeats, previous=heartbeat.previous,
                      consecutive_errors=heartbeat.consecutive_errors,
                      recent_starts=len(heartbeat.recent_starts), age_seconds=round(now - heartbeat.checked_at, 1))
    detail.update(extra)
    return {"status": status, "condition": condition, "detail": detail}


def assess(state, *, now, gate_open, unreadable=None) -> dict:
    """The readiness verdict for one document (see the module table). Never
    raises; a document that is not fully valid is never "ok"."""
    if type(now) not in (int, float) or not math.isfinite(now):
        return _verdict("unknown", "no_heartbeat", gate_open, reason="no_clock")
    if state is None:
        if unreadable in (None, "missing", "unreadable"):
            return _verdict("unknown", "no_heartbeat", gate_open, reason=unreadable or "missing")
        return _verdict("critical", "malformed", gate_open, reason=unreadable)
    try:
        beat = validate(state)
    except Malformed as exc:
        return _verdict("critical", "malformed", gate_open, reason=str(exc))
    except Exception:                                    # noqa: BLE001 -- a contract, not a crash
        return _verdict("critical", "malformed", gate_open, reason="unvalidatable")
    if beat.checked_at > now + MAX_FUTURE_SKEW_SECONDS:
        return _verdict("critical", "malformed", gate_open, reason="future_timestamp")

    def verdict(status, condition, **extra):
        return _verdict(status, condition, gate_open, beat, now, **extra)

    if beat.phase == "stopped":
        return verdict("critical", "stopped")
    if sum(1 for start in beat.recent_starts if 0 <= now - start <= CRASH_WINDOW_SECONDS) >= CRASH_LOOP_STARTS:
        return verdict("critical", "crash_looping")
    if beat.phase == "rendering":
        if now > beat.render_deadline:
            return verdict("critical", "render_overdue")
        found = verdict("ok", "rendering")
    elif now - beat.checked_at > STALE_AFTER_SECONDS:
        return verdict("critical", "stale")
    elif beat.consecutive_errors >= ERROR_CRITICAL_REPEATS:
        return verdict("critical", "malfunction")
    elif beat.consecutive_errors:
        return verdict("warning", "database_errors")
    elif beat.result == "blocked":
        return verdict("warning", "blocked")
    elif beat.result == "busy":
        found = verdict("warning", "lock_held_elsewhere") if beat.repeats >= BUSY_WARNING_REPEATS \
            else verdict("ok", "busy")
    elif beat.result in ("backoff", "retry"):
        found = verdict("ok", "retry_backoff")
    elif beat.result == "playout":
        found = verdict("ok", "playout_in_flight")
    elif beat.result in PROGRESS_RESULTS:
        found = verdict("ok", "working")
    elif beat.result == "idle" and beat.code == "gated":
        found = verdict("ok", "ready_waiting_for_gate")
    else:
        found = verdict("ok", "idle")                    # idle, or "starting": the first pass has not finished
    if (found["status"] == "ok" and beat.previous == "malformed"
            and 0 <= now - beat.started_at <= CRASH_WINDOW_SECONDS):
        return verdict("warning", "history_unverified")
    return found


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
    expires or creates anything, and never raises on a bad file."""
    document, reason = read_state(Path(path) if path is not None else state_path())
    return assess(document, now=time.time() if now is None else now, gate_open=gate_open_read_only(),
                  unreadable=reason or None)
