"""The active-message render worker (iPortal Phase D2A).

One pass (``run_once``):

1. take the worker lock -- a PostgreSQL SESSION advisory lock, tried, never
   waited for: a second worker skips the pass (``busy``); a crashed worker's
   lock dies with its connection. It only avoids duplicate renders;
   correctness never depends on it (binding re-proves authority);
2. obtain the one current occurrence from D1 authority
   (``services.materialize_due_execution``: expiry first, stale airings
   suppressed, the unfinished execution coalesced, else a due one created) --
   unless the CURRENT composition already failed permanently at this
   generation (``blocked``: re-rendering identical inputs cannot succeed; an
   operator change -- clear, replacement, a limit -- makes a new generation);
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

No engine command is ever sent and nothing airs: the native-airing gate stays
closed and READY executions wait for D2B.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from django.db import connection
from django.utils import timezone

from production.services.render import client
from production.services.render.engine import RenderError

from . import rendering, services
from .models import UNFINISHED_EXECUTION_STATES, ActiveBroadcastExecution, ActiveBroadcastMessageEvent, ExecutionState

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


@dataclass(frozen=True)
class Outcome:
    result: str                 # busy | idle | blocked | backoff | ready | reused | retry | failed | superseded
    execution: str = ""
    code: str = ""


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


def _blocked() -> str:
    """The code of a permanent failure of the CURRENT composition at the
    CURRENT generation (re-rendering it cannot succeed), or ""."""
    if ActiveBroadcastExecution.objects.filter(state__in=UNFINISHED_EXECUTION_STATES).exists():
        return ""
    last = ActiveBroadcastExecution.objects.order_by("-created_at", "-id").first()
    if last is None or last.state != ExecutionState.FAILED or last.failure_code not in BLOCKING_CODES:
        return ""
    current = services.snapshot_active_composition()
    if last.policy_generation == current["generation"] and last.composition_digest == current["digest"]:
        return last.failure_code
    return ""


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
    try:
        return services.materialize_due_execution(now=now)
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
        orphan = _unreconciled()
        if orphan is not None:
            successor = _reconcile(orphan)
            if successor is not None and successor.state == ExecutionState.PENDING_RENDER:
                return process(successor, now=now, render_fn=render_fn)
        blocked = _blocked()
        if blocked:
            return Outcome("blocked", code=blocked)
        execution = _occurrence(now)
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


def run_forever(*, poll_seconds=POLL_SECONDS, stop=lambda: False, sleep=time.sleep) -> None:
    while not stop():
        outcome = run_once()
        if outcome.result in ("idle", "busy", "blocked", "backoff", "retry"):
            sleep(poll_seconds)
