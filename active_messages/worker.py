"""The active-message render worker (iPortal Phase D2A).

One pass (``run_once``):

1. take the worker lock -- a PostgreSQL SESSION advisory lock, tried, never
   waited for: a second worker skips the pass (``busy``); a crashed worker's
   lock dies with its connection. It only avoids duplicate renders;
   correctness never depends on it (binding re-proves authority);
2. obtain the one current occurrence from D1 authority
   (``services.materialize_due_execution``: expiry first, stale airings
   suppressed, the unfinished execution coalesced, else a due one created) --
   unless the due occurrence is exactly the composition that already failed
   permanently at this generation (``blocked``: re-rendering identical inputs
   cannot succeed; an operator change -- clear, replacement, a limit -- or an
   expiry makes a new generation). D3E-A: that decision is made INSIDE the
   service's transaction, after its expiry sweep, so a blocked composition
   never holds back the expiry that changes it;
3. if it is PENDING_RENDER: honour its retry backoff, then REUSE a rendition
   proven to be exactly its frozen aggregate, or RENDER one (no database lock
   is held while the render service works), then BIND it
   (``services.bind_execution_mix``: PENDING_RENDER -> READY). READY is the
   durable point; a render alone is not success.

Outcomes:

* retryable renderer infrastructure failure -> a ``render_retry`` event, the
  execution stays PENDING_RENDER, exponential backoff (operator attention
  after ATTENTION_AFTER consecutive failures);
* the FROZEN stinger pin is unusable -> that occurrence FAILS
  (``stinger_pin_unavailable``; its snapshot is never edited) and, in the
  same pass, ``services.reconcile_stinger_failure`` creates at most one
  RECONCILE successor from current authority -- independent of cadence, so a
  Replay Now is not lost -- which omits an unusable stinger (or pins a newer
  valid registration) and keeps every urgent/PA component; it is then
  processed. The stinger is never re-resolved for the old occurrence. A pass
  also first reconciles any such failure a crash left unreconciled;
* any other permanent failure (input_unavailable, provenance_mismatch,
  rendition_invalid, aggregate_too_long, render_failed) -> FAILED with that
  code and operator attention;
* authority moved on while rendering (clear, replacement, expiry, a policy
  change, another worker) -> nothing is bound; the finished rendition stays an
  immutable, unbound orphan for ordinary retention.

D2B: each pass first runs the playout step (active_messages.playout.advance)
for the execution past rendering, if any -- project the deck's own evidence
(STARTED / COMPLETED / INTERRUPTED), suppress an engine-held execution whose
composition moved on, publish a READY aggregate into UrgentPA and dispatch
its exact urgent insertion -- and a freshly READY aggregate is published and
dispatched in the same pass. A retryable publication failure keeps that SAME
READY execution (and its exact aggregate) in flight with backoff -- the pass
ends there: nothing else is materialized or rendered. Publication and
dispatch need the native-airing gate, which stays closed in production
(OGRemote remains authoritative).
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from django.db import Error as DatabaseFailure
from django.db import close_old_connections, connection
from django.utils import timezone

from production.services.render import client
from production.services.render.engine import RenderError

from . import heartbeat, playout, rendering, services
from .models import ActiveBroadcastExecution, ActiveBroadcastMessageEvent, ExecutionState

# (namespace, key) of the worker's session advisory lock. "AMSG" as an int32.
LOCK_NAMESPACE = 0x414D5347
LOCK_KEY = 1
BACKOFF_BASE_SECONDS = 15
BACKOFF_MAX_SECONDS = 600
ATTENTION_AFTER = 3
# Permanent codes that mean "these exact inputs cannot render": the same
# composition is not re-materialized at the same generation.
BLOCKING_CODES = frozenset({"input_unavailable", "provenance_mismatch", "rendition_invalid", "aggregate_too_long",
                            "render_failed"})
# How RenderError codes map to D2A outcomes (anything retryable -> retry).
_PERMANENT = {
    client.INPUT_UNAVAILABLE: "input_unavailable",
    client.STINGER_UNAVAILABLE: "stinger_pin_unavailable",
    "decoded_too_long": "aggregate_too_long",          # the service's absolute decoded-duration ceiling
    "provenance_mismatch": "provenance_mismatch",
    "rendition_authority": "provenance_mismatch",
    "rendition_invalid": "rendition_invalid",
}
POLL_SECONDS = 5.0
# Loop cadence (D3E-A) -- the ONE definition, used by run_forever and so by
# the management command. Only a pass that made progress may be followed at
# once (there may be more work now); every other result waits POLL_SECONDS:
# idle (incl. a READY aggregate behind the closed gate), busy, blocked,
# backoff, retry, playout (an execution past rendering is with publication or
# the engine -- its retries and recovery are time-based, never "as fast as
# possible") and error. Progress continues at once for at most
# MAX_PROGRESS_PASSES passes in a row; the pass after which that many have run
# back-to-back is followed by a wait like any other.
PROGRESS_RESULTS = heartbeat.PROGRESS_RESULTS
MAX_PROGRESS_PASSES = 10
PAUSE_SLICE_SECONDS = 0.5


@dataclass(frozen=True)
class Outcome:
    # busy | idle | blocked | backoff | ready | reused | retry | failed | superseded | playout | error
    result: str
    execution: str = ""
    code: str = ""


def waits_after(outcome) -> bool:
    return outcome.result not in PROGRESS_RESULTS


@contextmanager
def worker_lock():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [LOCK_NAMESPACE, LOCK_KEY])
        held = bool(cursor.fetchone()[0])
    try:
        yield held
    finally:
        if held:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_NAMESPACE, LOCK_KEY])


def _attention(code, execution_id, detail="", *, level="error"):
    from monitoring.models import emit_event
    emit_event("active_messages", f"Active messages render: {code}", level=level,
               detail={"execution": str(execution_id), "code": code,
                       "detail": services._bounded_detail(detail)},
               dedupe_key=f"active_messages|render|{code}")


def _backoff_remaining(execution, now) -> float:
    retries = list(ActiveBroadcastMessageEvent.objects.filter(execution=execution, event="render_retry")
                   .order_by("-id").values_list("created_at", flat=True)[:12])
    if not retries:
        return 0.0
    wait = min(BACKOFF_BASE_SECONDS * 2 ** (len(retries) - 1), BACKOFF_MAX_SECONDS)
    return max(0.0, (retries[0] + timedelta(seconds=wait) - now).total_seconds())


def _unreconciled():
    """The oldest stinger-failed occurrence of the CURRENT generation with no
    recorded reconciliation (a crash between failing and reconciling), or None."""
    generation = services.current_policy().generation
    return (ActiveBroadcastExecution.objects
            .filter(state=ExecutionState.FAILED, failure_code=services.STINGER_PIN_UNAVAILABLE,
                    policy_generation=generation)
            .exclude(events__event="reconciled").order_by("created_at", "id").first())


def _reconcile(failed):
    """The successor of a stinger-failed occurrence (services), or None."""
    try:
        return services.reconcile_stinger_failure(failed)
    except services.CompositionIneligible:
        return None                     # already failed closed with operator attention


def _occurrence(now):
    """The current occurrence; CompositionBlocked when it would be exactly the
    permanently failed composition (decided after the expiry sweep, under the
    policy lock)."""
    try:
        return services.materialize_due_execution(now=now, blocking_codes=BLOCKING_CODES)
    except services.CompositionIneligible:
        return None                     # already failed closed with operator attention (D1)


def _classify(exc: RenderError, execution) -> str:
    if exc.retryable or exc.code == "rendition_unvalidated":
        return services.RENDER_RETRYABLE
    if exc.code == "descriptor_mismatch":
        # The service saw bytes other than the frozen identity: name the input now.
        return _PERMANENT.get(rendering.attribute_input_failure(execution.composition_snapshot), "render_failed")
    return _PERMANENT.get(exc.code, "render_failed")


def _fail(execution, code, detail) -> Outcome:
    row = services.fail_execution(execution, code=code, detail=detail)
    if row.state != ExecutionState.FAILED:
        return Outcome("superseded", str(row.pk), row.state)
    _attention(code, row.pk, detail, level="warning" if code == "stinger_pin_unavailable" else "error")
    return Outcome("failed", str(row.pk), code)


def _bind(execution, media, result) -> Outcome:
    try:
        row = services.bind_execution_mix(execution, media)
    except services.MixRefused as exc:
        if exc.code in ("execution_not_pending", "stale_generation"):
            return Outcome("superseded", str(execution.pk), exc.code)
        if exc.code == "rendition_unavailable":
            services.record_render_retry(execution, code=exc.code)
            return Outcome("retry", str(execution.pk), exc.code)
        return _fail(execution, exc.code, str(exc))
    return Outcome(result, str(row.pk))


def process(execution, *, now=None, render_fn=rendering.render) -> Outcome:
    """Bring one PENDING_RENDER execution to READY (or a truthful failure)."""
    now = now or timezone.now()
    execution = ActiveBroadcastExecution.objects.get(pk=execution.pk)
    if execution.state != ExecutionState.PENDING_RENDER:
        return Outcome("superseded", str(execution.pk), execution.state)
    if _backoff_remaining(execution, now) > 0:
        return Outcome("backoff", str(execution.pk))
    reusable = rendering.find_reusable(execution.composition_snapshot)
    if reusable is not None:
        return _bind(execution, reusable, "reused")
    recorder = heartbeat.current()
    if recorder is not None:                               # a render may last until its own deadline
        recorder.rendering(execution.pk, rendering.timeout_seconds(execution.composition_snapshot))
    try:
        result = render_fn(execution)                       # no database lock held
    except RenderError as exc:
        code = _classify(exc, execution)
        if code == services.RENDER_RETRYABLE:
            services.record_render_retry(execution, code=exc.code)
            failures = ActiveBroadcastMessageEvent.objects.filter(execution=execution, event="render_retry").count()
            if failures >= ATTENTION_AFTER:
                _attention("render_unavailable", execution.pk, f"{failures} attempts; last {exc.code}",
                           level="warning")
            return Outcome("retry", str(execution.pk), exc.code)
        return _fail(execution, code, f"{exc.code}: {exc}")
    return _bind(execution, result.media, "ready")


def run_once(*, now=None, render_fn=rendering.render) -> Outcome:
    """One worker pass (see the module docstring)."""
    now = now or timezone.now()
    with worker_lock() as held:
        if not held:
            return Outcome("busy")
        step = playout.advance(now=now)
        if step not in _PLAYOUT_SETTLED:
            return Outcome("playout", code=step)        # one execution past rendering is still in flight
        outcome = _render_pass(now, render_fn)
        if outcome.result in ("ready", "reused"):
            step = playout.advance(now=now)             # publish + dispatch at once (gate permitting)
        if outcome.result == "idle" and step == "gated":
            # A READY aggregate waiting behind the closed native-airing gate: expected, not a failure.
            outcome = Outcome("idle", outcome.execution, "gated")
        return outcome


# Playout outcomes after which no execution past rendering remains in flight.
# "gated": with the native-airing gate closed a READY execution simply waits
# (production until Phase G), exactly as in D2A. "publication_failed" is a
# PERMANENT integrity failure (the execution FAILED). A retryable publication
# failure ("publication_retry" / "publication_backoff") is NOT settled: the
# same READY execution stays the one in flight and no other occurrence is
# materialized or rendered in that pass. The remaining codes are races in
# which the READY execution is no longer current (D1 authority settles it).
_PLAYOUT_SETTLED = frozenset({"none", "gated", "completed", "interrupted", "suppressed", "failed",
                              "publication_failed", "stale_generation", "not_ready", "native_airing_closed"})


def _render_pass(now, render_fn) -> Outcome:
    """The D2A part of a pass (worker lock held)."""
    orphan = _unreconciled()
    if orphan is not None:
        successor = _reconcile(orphan)
        if successor is not None and successor.state == ExecutionState.PENDING_RENDER:
            return process(successor, now=now, render_fn=render_fn)
    try:
        execution = _occurrence(now)
    except services.CompositionBlocked as exc:
        return Outcome("blocked", code=exc.code)
    if execution is None or execution.state != ExecutionState.PENDING_RENDER:
        return Outcome("idle", "" if execution is None else str(execution.pk))
    outcome = process(execution, now=now, render_fn=render_fn)
    if outcome.result == "failed" and outcome.code == services.STINGER_PIN_UNAVAILABLE:
        # Reconcile at once from CURRENT authority, independent of cadence (a
        # Replay Now is not lost): the successor omits an unusable stinger and
        # keeps every urgent/PA component.
        successor = _reconcile(execution)
        if successor is not None and successor.state == ExecutionState.PENDING_RENDER:
            return process(successor, now=now, render_fn=render_fn)
    return outcome


def _pause(seconds, stop, sleep) -> None:
    """Wait ``seconds`` in short slices, returning as soon as ``stop()``."""
    remaining = float(seconds)
    while remaining > 0 and not stop():
        step = min(PAUSE_SLICE_SECONDS, remaining)
        sleep(step)
        remaining -= step


def run_forever(*, poll_seconds=POLL_SECONDS, stop=lambda: False, sleep=None, run=None, recorder=None,
                report=None, refresh=None, close=None) -> None:
    """The worker loop (the management command's). Waits after every pass that
    made no progress (``waits_after``), and after MAX_PROGRESS_PASSES passes
    of progress in a row; ``stop()`` is honoured between passes and within a
    wait. A database failure closes the connection (a session advisory lock
    dies with it) and waits; any other exception ends the process (its
    supervisor restarts it -- the heartbeat counts such restarts). A clean
    stop records "stopped" and closes the connection."""
    sleep = sleep or (lambda seconds: time.sleep(seconds))
    run = run or run_once
    refresh = refresh or close_old_connections
    close = close or (lambda: connection.close())
    progressed = 0
    while not stop():
        try:
            outcome = run()
        except DatabaseFailure as exc:
            close()
            outcome = Outcome("error", code=type(exc).__name__)
        if recorder is not None:
            recorder.cycle(outcome)
        if report is not None:
            report(outcome)
        progressed = progressed + 1 if not waits_after(outcome) else 0
        if progressed and progressed < MAX_PROGRESS_PASSES:
            continue                                   # passes 1..MAX-1 of a progress run: at once
        progressed = 0
        _pause(poll_seconds, stop, sleep)
        refresh()
    if recorder is not None:
        recorder.stopped()
    close()
