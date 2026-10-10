"""The leased spoken render worker (Phase C2A).

    claim a validated item (or one whose lease EXPIRED) -> start_processing
    -> render through isadoraair-render (render.render: executor + the sole
       rendition intake, validated by isadoraair-validation)
    -> complete_processing(exact RenderEvidence)   or   fail_processing

There is no job table: the SpokenItem lease (spoken.transitions) is the only
authority. Two workers can never process the same item -- start_processing
locks the row and refuses a held lease; a crashed worker's item is reclaimed
only after its lease expires (``processing_attempts`` counts every claim).

Retry policy (bounded everywhere):

* transient failures (service busy/unavailable, sandbox unavailable, a timeout,
  a rendition the validation service could not judge yet) are retried within
  the same lease, TRANSIENT_RETRIES times with a short backoff; then the item
  is failed with that code (failed -> validated is a manager's retry);
* ``render_outdated`` at completion (the configuration or a bed changed while
  rendering) re-renders against the CURRENT configuration once; the outdated
  rendition stays an immutable, unbindable orphan for ordinary retention;
* deterministic failures (unusable source, invalid configuration, a missing
  reference, an invalid rendition, silence, an evidence mismatch) fail at once;
* an item claimed more than MAX_ATTEMPTS times is failed ``attempts_exhausted``;
* a lease lost to expiry is left alone -- its new holder owns the item. An
  EXPIRED lease authorizes nothing even before it is reclaimed
  (transitions._require_lease), so every render gets a deadline that ends
  LEASE_MARGIN_SECONDS before the lease does, a retry that would no longer
  fit is abandoned, and a completion or failure refused for an expired lease
  leaves the item to the next claim. There is no lease renewal.

SystemEvents are only operator attention (the service is unavailable, the
sandbox is broken, an item keeps failing); SpokenItemEvent stays the history.
Nothing here runs in the web request path.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from production.services import render

from . import transitions
from .models import ItemState, SpokenContentType, SpokenItem

MAX_ATTEMPTS = 3
TRANSIENT_RETRIES = 2
OUTDATED_RERENDERS = 1
BACKOFF_SECONDS = (2.0, 10.0)
LEASE_SECONDS = transitions.LEASE_MAX_SECONDS
# A render must end this long before the lease does (rendition intake and its
# validation, then completion, still run under the lease).
LEASE_MARGIN_SECONDS = 600.0
MIN_RENDER_SECONDS = 60.0
LOST_LEASE_CODES = frozenset({"stale_lease", "lease_expired"})
# Infrastructure conditions an operator should hear about.
ATTENTION_CODES = frozenset({"service_unavailable", "sandbox_unavailable", "render_unavailable",
                             "rendition_unvalidated", "attempts_exhausted", "busy"})


@dataclass(frozen=True)
class Outcome:
    item_id: str
    result: str                 # processed | failed | lost_lease
    code: str = ""


def render_timeout_seconds(duration, lease_expires_at=None, now=None) -> float:
    """The render's own deadline, inside what remains of the lease (leaving
    LEASE_MARGIN_SECONDS to ingest, validate and complete)."""
    seconds = float(Decimal(str(duration or 0)))
    budget = float(LEASE_SECONDS)
    if lease_expires_at is not None:
        budget = (lease_expires_at - (now or timezone.now())).total_seconds()
    return min(budget - LEASE_MARGIN_SECONDS, 120.0 + 6.0 * seconds)


def candidates(limit=10) -> list:
    now = timezone.now()
    return list(SpokenItem.objects.filter(Q(state=ItemState.VALIDATED)
                                          | Q(state=ItemState.PROCESSING, lease_expires_at__lte=now))
                .order_by("validated_at", "created_at").values_list("pk", flat=True)[:limit])


def claim():
    """(item, lease token) for the first claimable item, or None."""
    for pk in candidates():
        try:
            return transitions.start_processing(pk, lease_seconds=LEASE_SECONDS)
        except (transitions.InvalidTransition, transitions.NotPermitted):
            continue                                  # taken meanwhile, or no longer eligible
    return None


def _attention(code, item_id, detail=""):
    if code in ATTENTION_CODES:
        from monitoring.models import emit_event
        emit_event("spoken_render", f"Spoken render: {code}", level="warning",
                   detail={"item": str(item_id), "code": code, "detail": str(detail)[:300]},
                   dedupe_key=f"spoken_render|{code}")


def _fail(item, token, code, detail) -> Outcome:
    try:
        transitions.fail_processing(item.pk, lease_token=token, code=code, detail=str(detail)[:500])
    except transitions.EvidenceRefused as exc:
        if exc.code not in LOST_LEASE_CODES:
            raise
        return Outcome(str(item.pk), "lost_lease", exc.code)
    _attention(code, item.pk, detail)
    return Outcome(str(item.pk), "failed", code)


def process_one(*, render_fn=render.render, sleep=time.sleep):
    """Claim and process at most one item. Returns an Outcome, or None when
    nothing was claimable."""
    claimed = claim()
    if claimed is None:
        return None
    item, token = claimed
    if item.processing_attempts > MAX_ATTEMPTS:
        return _fail(item, token, "attempts_exhausted", f"claimed {item.processing_attempts} times")
    transient = outdated = 0
    while True:
        ctype = SpokenContentType.objects.get(pk=item.content_type_id)       # the CURRENT configuration
        timeout = render_timeout_seconds(item.submission_media.decoded_duration_seconds, item.lease_expires_at)
        if timeout < MIN_RENDER_SECONDS:
            return Outcome(str(item.pk), "lost_lease", "lease_expiring")      # the next claim renders it
        try:
            result = render_fn(item.submission_media_id, ctype.recipe_key, ctype.recipe_params, item=item.pk,
                               lease=token, timeout_seconds=timeout)
        except render.RenderError as exc:
            if exc.retryable and transient < TRANSIENT_RETRIES:
                sleep(BACKOFF_SECONDS[min(transient, len(BACKOFF_SECONDS) - 1)])
                transient += 1
                continue
            return _fail(item, token, exc.code, exc)
        try:
            transitions.complete_processing(item.pk, lease_token=token, evidence=result.evidence)
            return Outcome(str(item.pk), "processed")
        except transitions.EvidenceRefused as exc:
            if exc.code in LOST_LEASE_CODES:
                return Outcome(str(item.pk), "lost_lease", exc.code)
            if exc.code == "render_outdated" and outdated < OUTDATED_RERENDERS:
                outdated += 1
                continue
            return _fail(item, token, exc.code, exc)
        except transitions.InvalidTransition as exc:
            return Outcome(str(item.pk), "lost_lease", exc.code)


def advance_ready(*, limit=20) -> list:
    """The projection pass (C2B): every ``processed`` item goes on through
    the authoritative path -- review when its type requires it, else
    delivery by the projection service (spoken.delivery) -- and the bounded
    expiry sweep runs. A delivery that fails leaves its item ``processed``
    for the next pass (its operator-attention is deduplicated)."""
    from . import delivery
    outcomes = []
    for pk in (SpokenItem.objects.filter(state=ItemState.PROCESSED).order_by("processed_at", "pk")
               .values_list("pk", flat=True)[:limit]):
        try:
            result = delivery.advance_processed(pk)
            outcomes.append(Outcome(str(pk), "delivered" if result is not None else "awaiting_review"))
        except transitions.SpokenServiceError as exc:
            outcomes.append(Outcome(str(pk), "delivery_failed", exc.code))
            delivery._attention(f"delivery_{exc.code}", None, f"item {pk}: {exc}")
    for item in delivery.expire_due():
        outcomes.append(Outcome(str(item.pk), "expired"))
    return outcomes
