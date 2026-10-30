"""Roadmap 2.27 Phase A -- hour-refill causal evidence (evidence only).

Pure helpers plus the one guarded write used by the web-request swap.
Nothing here selects, installs or plays anything, and nothing consumes the
records yet; see docs/roadmap/2_27_PHASE_A_CAUSALITY_CONTRACT.md.

- request substitution: build_request_substitution_evidence() decides,
  from plain values, whether a real swap is a cause;
  record_request_substitution_cause() persists it inside the caller's
  transaction, in a savepoint, and never lets a failure escape.
- operator Eject: eject_evidence_from_engine() snapshots a completed Eject
  from in-memory engine state only and classify_eject_observation() decides
  eligibility; the engine hands the result to the evidence worker
  (hour_refill_evidence), which alone writes it.
- engine-path request swaps: request_substitution_from_memory() builds the
  same evidence from objects the swap already holds, for an on-commit
  handoff to the worker. No query, savepoint or lock.
- completed Start Next cut (r0121): start_next_evidence_from_engine()
  snapshots a fade_and_start transition at its exact-generation completion
  claim and classify_start_next_observation() decides eligibility; the
  engine offers it to the same worker only once the transition settles
  "completed". See docs/roadmap/2_27_START_NEXT_CAUSALITY_EXTENSION.md.
- automatic skip / media failure (r0121): automatic_skip_evidence_from_engine()
  at a committed cursor skip of missing media, media_failure_evidence_from_engine()
  at a deck's claim_completion("error"); the pure classify_* functions
  decide. See docs/roadmap/2_27_AUTOMATIC_FAILURE_CAUSALITY_EXTENSION.md.
- trace: build_causality_trace() orders, deduplicates and summarises
  causes. It reports observed shortening, never a net hour deficit.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from django.db import transaction

from library.services.log_builder import _effective_airtime, effective_airtime_seconds

CAUSE_REQUEST_SUBSTITUTION = "request_substitution"
CAUSE_OPERATOR_EJECT = "operator_eject"
CAUSE_START_NEXT = "start_next"
CAUSE_AUTOMATIC_SKIP = "automatic_skip"
CAUSE_MEDIA_FAILURE = "media_failure"
CAUSE_TYPES = (CAUSE_REQUEST_SUBSTITUTION, CAUSE_OPERATOR_EJECT, CAUSE_START_NEXT,
               CAUSE_AUTOMATIC_SKIP, CAUSE_MEDIA_FAILURE)

# Causes that each describe audio cut from ONE occurrence before its end. A
# completion claim is once-only per deck generation, so two cuts of one
# occurrence should never exist; if they did, only the first is summed.
EARLY_CUT_CAUSES = frozenset({CAUSE_OPERATOR_EJECT, CAUSE_START_NEXT})
# r0121: every cause that ends ONE occurrence's airtime (a cut, a skip, a
# media failure) is one family: at most one is counted per occurrence.
TERMINATION_CAUSES = EARLY_CUT_CAUSES | {CAUSE_AUTOMATIC_SKIP, CAUSE_MEDIA_FAILURE}

PHASE_PRE_AIR = "pre_air"
PHASE_POST_AIR = "post_air"

# Phase B rule for a web-request-triggered MUSIC refill: strictly MORE than
# 60 seconds of NET deficit. Never applied at cause-capture time.
REQUEST_MUSIC_TRIGGER_THRESHOLD_SECONDS = 60


def request_music_threshold_exceeded(net_deficit_seconds):
    """True only for a net deficit strictly greater than 60 seconds."""
    return net_deficit_seconds is not None and net_deficit_seconds > REQUEST_MUSIC_TRIGGER_THRESHOLD_SECONDS


def _ms(value):
    return round(float(value), 3)


_MISSING = object()


def _loaded(instance, name):
    """A concrete field's value only if already loaded on `instance` -- never
    a deferred-field query. _MISSING otherwise."""
    if instance is None:
        return _MISSING
    return getattr(instance, "__dict__", {}).get(name, _MISSING)


def _cached(instance, name):
    """A forward relation's object only if already cached on `instance` --
    never a lazy query. None otherwise (including for non-model stand-ins)."""
    state = getattr(instance, "_state", None)
    cache = getattr(state, "fields_cache", None)
    if not isinstance(cache, dict):
        return None
    return cache.get(name)


def _loaded_airtime(track):
    """effective_airtime from already-loaded Track fields, else None."""
    values = [_loaded(track, n) for n in ("next_start_seconds", "duration_seconds", "cue_in_seconds")]
    if any(v is _MISSING for v in values):
        return None
    return _effective_airtime(*values)


# ---------------------------------------------------------------------------
# Request substitution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RequestSubstitutionEvidence:
    source_key: str
    occurred_at: datetime
    playlist_log_id: int
    log_date: date
    log_hour: int
    log_item_id: int
    log_item_position: int
    scheduled_category_id: int | None
    scheduled_category_code: str
    scheduled_category_kind_code: str
    original_track_id: int
    original_track_title: str
    original_track_artist: str
    original_effective_airtime_seconds: float
    replacement_track_id: int
    replacement_effective_airtime_seconds: float
    observed_shortening_seconds: float
    song_request_id: int

    def cause_fields(self):
        """HourRefillCause column values."""
        return dict(
            source_key=self.source_key,
            cause_type=CAUSE_REQUEST_SUBSTITUTION,
            occurred_at=self.occurred_at,
            playlist_log_id_snapshot=self.playlist_log_id,
            log_date=self.log_date,
            log_hour=self.log_hour,
            log_item_id_snapshot=self.log_item_id,
            log_item_position=self.log_item_position,
            scheduled_category_id_snapshot=self.scheduled_category_id,
            scheduled_category_code=self.scheduled_category_code,
            scheduled_category_kind_code=self.scheduled_category_kind_code,
            original_track_id_snapshot=self.original_track_id,
            original_track_title=self.original_track_title,
            original_track_artist=self.original_track_artist,
            original_effective_airtime_seconds=self.original_effective_airtime_seconds,
            replacement_track_id_snapshot=self.replacement_track_id,
            replacement_effective_airtime_seconds=self.replacement_effective_airtime_seconds,
            observed_shortening_seconds=self.observed_shortening_seconds,
            song_request_id_snapshot=self.song_request_id,
        )


def request_substitution_source_key(log_item_id, song_request_id, original_track_id, replacement_track_id):
    return (
        f"{CAUSE_REQUEST_SUBSTITUTION}:li{log_item_id}:req{song_request_id}"
        f":t{original_track_id}-{replacement_track_id}"
    )


def build_request_substitution_evidence(
    *, occurred_at, playlist_log_id, log_date, log_hour, log_item_id, log_item_position,
    scheduled_category_id, scheduled_category_code, scheduled_category_kind_code,
    original_track_id, original_track_title, original_track_artist,
    original_effective_airtime_seconds, replacement_track_id,
    replacement_effective_airtime_seconds, song_request_id,
):
    """Return (evidence, None) for a genuine shorter different-Track
    substitution of a scheduled music slot, else (None, reason)."""
    if original_track_id is None or replacement_track_id is None:
        return None, "missing_track"
    if original_track_id == replacement_track_id:
        return None, "same_track"
    if scheduled_category_kind_code != "music":
        return None, "not_music_slot"
    if song_request_id is None:
        return None, "missing_request"
    old = _ms(original_effective_airtime_seconds)
    new = _ms(replacement_effective_airtime_seconds)
    if new >= old:
        return None, "not_shorter"
    return RequestSubstitutionEvidence(
        source_key=request_substitution_source_key(
            log_item_id, song_request_id, original_track_id, replacement_track_id,
        ),
        occurred_at=occurred_at,
        playlist_log_id=playlist_log_id,
        log_date=log_date,
        log_hour=log_hour,
        log_item_id=log_item_id,
        log_item_position=log_item_position,
        scheduled_category_id=scheduled_category_id,
        scheduled_category_code=scheduled_category_code or "",
        scheduled_category_kind_code=scheduled_category_kind_code,
        original_track_id=original_track_id,
        original_track_title=original_track_title or "",
        original_track_artist=original_track_artist or "",
        original_effective_airtime_seconds=old,
        replacement_track_id=replacement_track_id,
        replacement_effective_airtime_seconds=new,
        observed_shortening_seconds=_ms(old - new),
        song_request_id=song_request_id,
    ), None


def record_request_substitution_cause(*, locked_log, locked_item, replacement_track, song_request, occurred_at):
    """Called by webrequests.services.maybe_schedule_song_request inside its
    locked transaction, immediately BEFORE locked_item.track is overwritten.

    Returns a zero-argument callable that commits the cause (call it after
    the swap's own writes, still inside the transaction), or None when
    capture is Off or this is not a cause. The callable never raises: any
    failure rolls back only its savepoint, and a warning (internal ids
    only) is emitted after the outer transaction commits.

    With the policy Off this costs one primary-key read (in a savepoint)
    and nothing else. Like the write, preparation never raises: a failed
    read (e.g. code running before its migration) rolls back only its own
    savepoint, so the caller's transaction stays usable and the swap
    proceeds as r0119."""
    try:
        with transaction.atomic():
            return _prepare_request_substitution(
                locked_log, locked_item, replacement_track, song_request, occurred_at,
            )
    except Exception as exc:  # noqa: BLE001 -- evidence must never break scheduling
        print(f"  Hour-refill cause preparation failed (non-fatal, swap kept): "
              f"log_item={locked_item.id} {type(exc).__name__}")
        detail = {"log_item_id": locked_item.id, "song_request_id": song_request.id,
                  "error": type(exc).__name__}
        transaction.on_commit(lambda: _emit_capture_failure(detail))
        return None


def _prepare_request_substitution(locked_log, locked_item, replacement_track, song_request, occurred_at):
    from library.models import HourRefillPolicy

    capture_requests, _capture_eject = HourRefillPolicy.capture_flags()
    if not capture_requests:
        return None
    if replacement_track is None or replacement_track.id == locked_item.track_id:
        return None
    original_track = locked_item.track
    category = locked_item.category
    evidence, _reason = build_request_substitution_evidence(
        occurred_at=occurred_at,
        playlist_log_id=locked_log.id,
        log_date=locked_log.date,
        log_hour=locked_log.hour,
        log_item_id=locked_item.id,
        log_item_position=locked_item.position,
        scheduled_category_id=locked_item.category_id,
        scheduled_category_code=getattr(category, "code", ""),
        scheduled_category_kind_code=getattr(getattr(category, "kind", None), "code", ""),
        original_track_id=getattr(original_track, "id", None),
        original_track_title=locked_item.track_title or getattr(original_track, "title", ""),
        original_track_artist=locked_item.track_artist,
        original_effective_airtime_seconds=effective_airtime_seconds(original_track),
        replacement_track_id=replacement_track.id,
        replacement_effective_airtime_seconds=effective_airtime_seconds(replacement_track),
        song_request_id=song_request.id,
    )
    if evidence is None:
        return None
    return lambda: persist_request_substitution_evidence(evidence)


def persist_request_substitution_evidence(evidence):
    """Insert one cause in a savepoint. Never raises. Returns True only if
    a row was inserted."""
    from library.models import HourRefillCause

    try:
        with transaction.atomic():
            HourRefillCause.objects.create(**evidence.cause_fields())
        return True
    except Exception as exc:  # noqa: BLE001 -- evidence must never break scheduling
        error = type(exc).__name__
        print(f"  Hour-refill cause capture failed (non-fatal, swap kept): "
              f"log_item={evidence.log_item_id} request={evidence.song_request_id} {error}")
        detail = {
            "source_key": evidence.source_key,
            "log_item_id": evidence.log_item_id,
            "song_request_id": evidence.song_request_id,
            "error": error,
        }
        transaction.on_commit(lambda: _emit_capture_failure(detail))
        return False


def _emit_capture_failure(detail):
    from monitoring.models import emit_event

    emit_event(
        category="engine", level="warning",
        title="Hour-refill cause capture failed",
        detail=detail,
        dedupe_key="engine|hour-refill-cause-capture-failed",
    )


def request_substitution_from_memory(*, caller_item, locked_log, locked_item, replacement_track,
                                     song_request, occurred_at):
    """Evidence for an engine-path swap from objects already in memory, else
    None. Never queries: the displaced Track's airtime comes from the
    engine's own queue object (`caller_item`, loaded with its Track) and only
    when it is provably the same Track the locked row still holds; the slot
    category and kind come from the locked row's select_related cache.
    Anything not already loaded fails closed. Never raises."""
    try:
        original_track = _cached(caller_item, "track")
        if (
            original_track is None
            or _loaded(caller_item, "track_id") != locked_item.track_id
            or _loaded(original_track, "id") != locked_item.track_id
        ):
            return None
        original_airtime = _loaded_airtime(original_track)
        replacement_airtime = _loaded_airtime(replacement_track)
        category = _cached(locked_item, "category")
        kind = _cached(category, "kind")
        if original_airtime is None or replacement_airtime is None or kind is None:
            return None
        title = _loaded(original_track, "title")
        evidence, _reason = build_request_substitution_evidence(
            occurred_at=occurred_at,
            playlist_log_id=locked_log.id,
            log_date=locked_log.date,
            log_hour=locked_log.hour,
            log_item_id=locked_item.id,
            log_item_position=locked_item.position,
            scheduled_category_id=locked_item.category_id,
            scheduled_category_code=_loaded(category, "code") if _loaded(category, "code") is not _MISSING else "",
            scheduled_category_kind_code=_loaded(kind, "code") if _loaded(kind, "code") is not _MISSING else "",
            original_track_id=locked_item.track_id,
            original_track_title=locked_item.track_title or ("" if title is _MISSING else title),
            original_track_artist=locked_item.track_artist,
            original_effective_airtime_seconds=original_airtime,
            replacement_track_id=_loaded(replacement_track, "id"),
            replacement_effective_airtime_seconds=replacement_airtime,
            song_request_id=song_request.id,
        )
        return evidence
    except Exception:  # noqa: BLE001 -- evidence must never break scheduling
        return None


# ---------------------------------------------------------------------------
# Operator Eject
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EjectObservation:
    """What PlaybackEngine._eject_deck knows at the completion boundary
    (after claim_completion("eject"), before retirement). Plain values so
    the classification is testable without GStreamer."""
    engine_instance: str
    slot: str
    expected_log_item_id: int | None
    deck_present: bool
    deck_log_item_id: int | None = None
    deck_generation: int | None = None
    completion_claimed: bool = False
    # "scheduled" only for LogItem.origin == generated_schedule on a fresh
    # (non-continuation) deck; otherwise the origin, "continuation" or "unknown".
    occurrence_source: str = "unknown"
    on_air: bool = False                   # first real buffer recorded
    paused: bool = False
    other_live_deck: bool = False
    vt_phase: str = "idle"
    manual_mode: bool = False
    remote_dj_owner: bool = False
    mic_live: bool = False
    start_next_active: bool = False        # a Start Next transition existed at the Eject
    transition_pending: bool = False       # something else owned "what starts next"
    scheduled_category_kind_code: str = ""
    position_seconds: float | None = None
    cue_in_seconds: float | None = 0.0
    next_start_seconds: float | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class EjectCauseEnvelope:
    source_key: str
    engine_instance: str
    slot: str
    deck_generation: int
    log_item_id: int
    eject_position_seconds: float
    remaining_effective_airtime_seconds: float


def operator_eject_source_key(engine_instance, slot, deck_generation, log_item_id):
    return f"{CAUSE_OPERATOR_EJECT}:{engine_instance}:{slot}:g{deck_generation}:li{log_item_id}"


def remaining_effective_airtime(position_seconds, cue_in_seconds, next_start_seconds, duration_seconds):
    """Listener-facing airtime still ahead of `position` (source-file
    seconds): (next_start or duration) - max(position, cue_in), floored at
    0. Never the whole file length."""
    if next_start_seconds is not None:
        end_point = next_start_seconds
    elif duration_seconds is not None:
        end_point = duration_seconds
    else:
        return 0.0
    start = max(position_seconds or 0.0, cue_in_seconds or 0.0)
    return max(0.0, end_point - start)


def classify_eject_observation(obs):
    """Return (EjectCauseEnvelope, None) for a completed Eject that is a
    cause under the v1 contract, else (None, reason). Conservative: any
    ambiguity is a non-cause."""
    if not obs.deck_present:
        return None, "empty_slot"
    if obs.expected_log_item_id is not None and obs.expected_log_item_id != obs.deck_log_item_id:
        return None, "stale_binding"
    if not obs.completion_claimed:
        return None, "already_completing"
    if obs.deck_log_item_id is None or obs.deck_generation is None or not obs.engine_instance:
        return None, "identity_unknown"
    if obs.manual_mode:
        return None, "manual_hold"
    if obs.remote_dj_owner:
        return None, "remote_dj_owner"
    if obs.mic_live:
        return None, "mic_live"
    if obs.start_next_active:
        return None, "start_next_transition"
    if obs.vt_phase != "idle":
        return None, "vt_transition"
    if obs.other_live_deck:
        return None, "overlap"
    if obs.transition_pending:
        return None, "transition_pending"
    if obs.occurrence_source != "scheduled":
        return None, "not_scheduled_occurrence"
    if obs.scheduled_category_kind_code != "music":
        return None, "not_music"
    if not obs.on_air:
        return None, "not_on_air"
    if obs.paused:
        return None, "paused"
    if obs.position_seconds is None:
        return None, "position_unknown"
    remaining = _ms(remaining_effective_airtime(
        obs.position_seconds, obs.cue_in_seconds, obs.next_start_seconds, obs.duration_seconds,
    ))
    if remaining <= 0:
        return None, "no_remaining_airtime"
    return EjectCauseEnvelope(
        source_key=operator_eject_source_key(
            obs.engine_instance, obs.slot, obs.deck_generation, obs.deck_log_item_id,
        ),
        engine_instance=obs.engine_instance,
        slot=obs.slot,
        deck_generation=obs.deck_generation,
        log_item_id=obs.deck_log_item_id,
        eject_position_seconds=_ms(obs.position_seconds),
        remaining_effective_airtime_seconds=remaining,
    ), None


@dataclass(frozen=True)
class EjectCauseEvidence:
    """Primitive-only snapshot of a completed, eligible Eject, handed to the
    evidence worker. Built on the engine thread without any query."""
    envelope: EjectCauseEnvelope
    occurred_at: datetime
    playlist_log_id: int
    log_date: date
    log_hour: int
    log_item_position: int
    scheduled_category_id: int | None
    scheduled_category_code: str
    scheduled_category_kind_code: str
    original_track_id: int
    original_track_title: str
    original_track_artist: str
    original_effective_airtime_seconds: float

    @property
    def source_key(self):
        return self.envelope.source_key

    def cause_fields(self):
        """HourRefillCause column values."""
        env = self.envelope
        return dict(
            source_key=env.source_key,
            cause_type=CAUSE_OPERATOR_EJECT,
            occurred_at=self.occurred_at,
            playlist_log_id_snapshot=self.playlist_log_id,
            log_date=self.log_date,
            log_hour=self.log_hour,
            log_item_id_snapshot=env.log_item_id,
            log_item_position=self.log_item_position,
            scheduled_category_id_snapshot=self.scheduled_category_id,
            scheduled_category_code=self.scheduled_category_code,
            scheduled_category_kind_code=self.scheduled_category_kind_code,
            original_track_id_snapshot=self.original_track_id,
            original_track_title=self.original_track_title,
            original_track_artist=self.original_track_artist,
            original_effective_airtime_seconds=self.original_effective_airtime_seconds,
            observed_shortening_seconds=env.remaining_effective_airtime_seconds,
            engine_instance=env.engine_instance,
            deck_slot=env.slot,
            deck_generation=env.deck_generation,
            eject_position_seconds=env.eject_position_seconds,
        )


def _occurrence_source(deck, log_item):
    from library.models import LogItem

    if getattr(deck, "continuation_reason", None):
        return "continuation"
    origin = _loaded(log_item, "origin")
    if origin is _MISSING or not origin:
        return "unknown"
    return "scheduled" if origin == LogItem.ORIGIN_GENERATED_SCHEDULE else origin


def eject_evidence_from_engine(engine, deck, *, slot, expected_log_item_id, start_next_active,
                               transition_pending, occurred_at):
    """Called by PlaybackEngine._eject_deck on the engine thread, only after
    claim_completion("eject") succeeded and before retirement. Reads
    in-memory engine/deck state, the deck's current position (a GStreamer
    query, no database) and already-loaded LogItem/Track/Category fields.
    Returns (EjectCauseEvidence, None) or (None, reason). Never queries."""
    log_item = deck.log_item
    track = deck.track
    category = _cached(log_item, "category")
    kind = _cached(category, "kind")
    kind_code = _loaded(kind, "code")
    obs = EjectObservation(
        engine_instance=getattr(engine, "_engine_session_id", "") or "",
        slot=slot,
        expected_log_item_id=expected_log_item_id,
        deck_present=True,
        deck_log_item_id=_loaded(log_item, "id") if _loaded(log_item, "id") is not _MISSING else None,
        deck_generation=getattr(deck, "generation", None),
        completion_claimed=getattr(deck, "completion_claimed", False)
        and getattr(deck, "completion_reason", None) == "eject",
        occurrence_source=_occurrence_source(deck, log_item),
        on_air=bool(getattr(deck, "air_start_recorded", False)),
        paused=bool(getattr(deck, "paused", False)),
        other_live_deck=any(other is not deck for other in engine._live_decks()),
        vt_phase=(getattr(engine, "_vt", None) or {}).get("phase", "idle"),
        manual_mode=bool(getattr(engine, "manual_mode", False)),
        remote_dj_owner=getattr(engine, "remote_dj_session", None) is not None,
        mic_live=bool(engine._any_mic_live()),
        start_next_active=start_next_active,
        transition_pending=transition_pending,
        scheduled_category_kind_code="" if kind_code is _MISSING else kind_code,
        cue_in_seconds=None,
        next_start_seconds=None,
        duration_seconds=None,
    )
    # Cheap exclusions first (placeholder position/end point that cannot
    # themselves exclude); the position query only for a possible cause.
    envelope, reason = classify_eject_observation(replace(obs, position_seconds=0.0, next_start_seconds=1.0))
    if envelope is None:
        return None, reason
    values = {n: _loaded(track, n) for n in ("next_start_seconds", "duration_seconds", "cue_in_seconds")}
    playlist_log = _cached(log_item, "playlist_log")
    identity = {
        "log_date": _loaded(playlist_log, "date"),
        "log_hour": _loaded(playlist_log, "hour"),
        "position": _loaded(log_item, "position"),
        "track_id": _loaded(track, "id"),
        "log_id": _loaded(log_item, "playlist_log_id"),
        "category_id": _loaded(log_item, "category_id"),
    }
    if any(v is _MISSING for v in values.values()) or any(v is _MISSING for v in identity.values()):
        return None, "identity_unknown"
    obs = replace(obs, position_seconds=engine._get_deck_position(deck), **values)
    envelope, reason = classify_eject_observation(obs)
    if envelope is None:
        return None, reason
    title = _loaded(log_item, "track_title")
    artist = _loaded(log_item, "track_artist")
    code = _loaded(category, "code")
    return EjectCauseEvidence(
        envelope=envelope,
        occurred_at=occurred_at,
        playlist_log_id=identity["log_id"],
        log_date=identity["log_date"],
        log_hour=identity["log_hour"],
        log_item_position=identity["position"],
        scheduled_category_id=identity["category_id"],
        scheduled_category_code="" if code is _MISSING else code,
        scheduled_category_kind_code=kind_code,
        original_track_id=identity["track_id"],
        original_track_title="" if title is _MISSING else title,
        original_track_artist="" if artist is _MISSING else artist,
        original_effective_airtime_seconds=_ms(_effective_airtime(
            values["next_start_seconds"], values["duration_seconds"], values["cue_in_seconds"],
        )),
    ), None


# ---------------------------------------------------------------------------
# Completed Start Next cut (r0121)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StartNextObservation:
    """What PlaybackEngine._start_next_completion_due knows right after the
    outgoing deck's claim_completion("start_next") succeeded, before
    retirement. Plain values so the classification is testable without
    GStreamer. Ownership "at accept" comes from the transition record."""
    engine_instance: str
    request_id: str
    mode: str
    phase: str
    slot: str
    deck_log_item_id: int | None
    deck_generation: int | None
    outgoing: dict | None                 # active["outgoing"] at acceptance
    deck_is_outgoing: bool = False        # the very deck object accepted
    deck_mapped: bool = False             # still engine.decks[slot]
    completion_claimed: bool = False      # claimed, and for "start_next"
    incoming_started: bool = False        # an incoming deck was created
    foreign_live_deck: bool = False       # live, neither this deck nor the incoming slot
    ownership_at_accept: dict | None = None
    manual_mode: bool = False
    remote_dj_owner: bool = False
    mic_live: bool = False
    vt_phase: str = "idle"
    seek_in_progress: bool = False
    occurrence_source: str = "unknown"
    on_air: bool = False
    paused: bool = False
    scheduled_category_kind_code: str = ""
    position_seconds: float | None = None
    cue_in_seconds: float | None = 0.0
    next_start_seconds: float | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class StartNextCauseEnvelope:
    source_key: str
    engine_instance: str
    request_id: str
    slot: str
    deck_generation: int
    log_item_id: int
    cut_position_seconds: float
    remaining_effective_airtime_seconds: float


def start_next_source_key(engine_instance, slot, deck_generation, log_item_id, request_id):
    """Bounded (< 100 chars) whatever the input lengths; the engine instance
    and request id enter only through a digest (both are also stored in
    their own columns). No user or client data."""
    digest = hashlib.sha256(f"{engine_instance}\n{request_id}".encode("utf-8")).hexdigest()[:32]
    return f"{CAUSE_START_NEXT}:li{log_item_id}:{slot}g{deck_generation}:{digest}"


def classify_start_next_observation(obs):
    """Return (StartNextCauseEnvelope, None) for a completed, eligible
    fade_and_start cut, else (None, reason). Conservative: any ambiguity is
    a non-cause (decision table, extension contract section 3)."""
    if obs.mode != "fade_and_start":
        return None, "not_fade_and_start"
    if not obs.completion_claimed:
        return None, "not_claimed"
    outgoing = obs.outgoing or {}
    if (
        not obs.deck_is_outgoing or not obs.deck_mapped
        or obs.deck_log_item_id is None or obs.deck_generation is None
        or outgoing.get("slot") != obs.slot
        or outgoing.get("log_item_id") != obs.deck_log_item_id
        or outgoing.get("generation") != obs.deck_generation
    ):
        return None, "identity_mismatch"
    if not obs.engine_instance or not obs.request_id:
        return None, "identity_unknown"
    if obs.phase == "to_silence":
        return None, "to_silence"
    if obs.phase != "advancing" or not obs.incoming_started:
        return None, "incoming_not_started"
    if obs.foreign_live_deck:
        return None, "foreign_live_deck"
    accept = obs.ownership_at_accept
    if not isinstance(accept, dict) or set(accept) != {"manual", "remote_dj", "mic"}:
        return None, "ownership_unknown"
    if accept["manual"] or obs.manual_mode:
        return None, "manual_hold"
    if accept["remote_dj"] or obs.remote_dj_owner:
        return None, "remote_dj_owner"
    if accept["mic"] or obs.mic_live:
        return None, "mic_live"
    if obs.vt_phase != "idle":
        return None, "vt_transition"
    if obs.seek_in_progress:
        return None, "seek_in_progress"
    if obs.occurrence_source != "scheduled":
        return None, "not_scheduled_occurrence"
    if obs.scheduled_category_kind_code != "music":
        return None, "not_music"
    if not obs.on_air:
        return None, "not_on_air"
    if obs.paused:
        return None, "paused"
    position = obs.position_seconds
    if (
        position is None or isinstance(position, bool) or not isinstance(position, (int, float))
        or not math.isfinite(position) or position < 0
    ):
        return None, "position_unknown"
    remaining = _ms(remaining_effective_airtime(
        position, obs.cue_in_seconds, obs.next_start_seconds, obs.duration_seconds,
    ))
    if remaining <= 0:
        return None, "no_remaining_airtime"
    return StartNextCauseEnvelope(
        source_key=start_next_source_key(
            obs.engine_instance, obs.slot, obs.deck_generation, obs.deck_log_item_id, obs.request_id,
        ),
        engine_instance=obs.engine_instance,
        request_id=obs.request_id,
        slot=obs.slot,
        deck_generation=obs.deck_generation,
        log_item_id=obs.deck_log_item_id,
        cut_position_seconds=_ms(position),
        remaining_effective_airtime_seconds=remaining,
    ), None


@dataclass(frozen=True)
class StartNextCauseEvidence:
    """Primitive-only snapshot of a completed, eligible Start Next cut,
    handed to the evidence worker. Built on the engine thread without any
    query. Never a play record, a fulfilled request or a schedule change."""
    envelope: StartNextCauseEnvelope
    occurred_at: datetime
    playlist_log_id: int
    log_date: date
    log_hour: int
    log_item_position: int
    scheduled_category_id: int | None
    scheduled_category_code: str
    scheduled_category_kind_code: str
    original_track_id: int
    original_track_title: str
    original_track_artist: str
    original_effective_airtime_seconds: float

    @property
    def source_key(self):
        return self.envelope.source_key

    def cause_fields(self):
        """HourRefillCause column values."""
        env = self.envelope
        return dict(
            source_key=env.source_key,
            cause_type=CAUSE_START_NEXT,
            occurred_at=self.occurred_at,
            playlist_log_id_snapshot=self.playlist_log_id,
            log_date=self.log_date,
            log_hour=self.log_hour,
            log_item_id_snapshot=env.log_item_id,
            log_item_position=self.log_item_position,
            scheduled_category_id_snapshot=self.scheduled_category_id,
            scheduled_category_code=self.scheduled_category_code,
            scheduled_category_kind_code=self.scheduled_category_kind_code,
            original_track_id_snapshot=self.original_track_id,
            original_track_title=self.original_track_title,
            original_track_artist=self.original_track_artist,
            original_effective_airtime_seconds=self.original_effective_airtime_seconds,
            observed_shortening_seconds=env.remaining_effective_airtime_seconds,
            engine_instance=env.engine_instance,
            deck_slot=env.slot,
            deck_generation=env.deck_generation,
            start_next_request_id=env.request_id,
            start_next_position_seconds=env.cut_position_seconds,
        )


def start_next_evidence_from_engine(engine, deck, active, *, occurred_at):
    """Called by PlaybackEngine._start_next_completion_due on the engine
    thread, only after deck.claim_completion("start_next") succeeded and
    before retirement. Reads in-memory engine/transition/deck state, the
    deck's current position (a GStreamer query, no database) and
    already-loaded LogItem/Track/Category fields. Returns
    (StartNextCauseEvidence, None) or (None, reason). Never queries."""
    log_item = deck.log_item
    track = deck.track
    category = _cached(log_item, "category")
    kind = _cached(category, "kind")
    kind_code = _loaded(kind, "code")
    slot = getattr(deck, "slot", None)
    incoming = active.get("incoming") or {}
    incoming_slot = active.get("incoming_slot")
    log_item_id = _loaded(log_item, "id")
    obs = StartNextObservation(
        engine_instance=getattr(engine, "_engine_session_id", "") or "",
        request_id=active.get("request_id") or "",
        mode=active.get("mode") or "",
        phase=active.get("phase") or "",
        slot=slot,
        deck_log_item_id=None if log_item_id is _MISSING else log_item_id,
        deck_generation=getattr(deck, "generation", None),
        outgoing=active.get("outgoing"),
        deck_is_outgoing=active.get("outgoing_deck") is deck,
        deck_mapped=engine.decks.get(slot) is deck,
        completion_claimed=bool(getattr(deck, "completion_claimed", False))
        and getattr(deck, "completion_reason", None) == "start_next",
        incoming_started=bool(incoming) and incoming.get("slot") == incoming_slot,
        foreign_live_deck=any(
            other is not deck and other.slot != incoming_slot for other in engine._live_decks()
        ),
        ownership_at_accept=active.get("ownership_at_accept"),
        manual_mode=bool(getattr(engine, "manual_mode", False)),
        remote_dj_owner=getattr(engine, "remote_dj_session", None) is not None,
        mic_live=bool(engine._any_mic_live()),
        vt_phase=(getattr(engine, "_vt", None) or {}).get("phase", "idle"),
        seek_in_progress=getattr(deck, "gated_seek", None) is not None,
        occurrence_source=_occurrence_source(deck, log_item),
        on_air=bool(getattr(deck, "air_start_recorded", False)),
        paused=bool(getattr(deck, "paused", False)),
        scheduled_category_kind_code="" if kind_code is _MISSING else kind_code,
        cue_in_seconds=None,
        next_start_seconds=None,
        duration_seconds=None,
    )
    # Cheap exclusions first (placeholder position/end point that cannot
    # themselves exclude); the position query only for a possible cause.
    envelope, reason = classify_start_next_observation(
        replace(obs, position_seconds=0.0, next_start_seconds=1.0)
    )
    if envelope is None:
        return None, reason
    values = {n: _loaded(track, n) for n in ("next_start_seconds", "duration_seconds", "cue_in_seconds")}
    playlist_log = _cached(log_item, "playlist_log")
    identity = {
        "log_date": _loaded(playlist_log, "date"),
        "log_hour": _loaded(playlist_log, "hour"),
        "position": _loaded(log_item, "position"),
        "track_id": _loaded(track, "id"),
        "item_track_id": _loaded(log_item, "track_id"),
        "log_id": _loaded(log_item, "playlist_log_id"),
        "category_id": _loaded(log_item, "category_id"),
    }
    if any(v is _MISSING for v in values.values()) or any(v is _MISSING for v in identity.values()):
        return None, "identity_unknown"
    if identity["track_id"] is None or identity["track_id"] != identity["item_track_id"]:
        return None, "identity_unknown"
    if _loaded(playlist_log, "id") != identity["log_id"]:
        return None, "identity_unknown"
    obs = replace(obs, position_seconds=engine._get_deck_position(deck), **values)
    envelope, reason = classify_start_next_observation(obs)
    if envelope is None:
        return None, reason
    title = _loaded(log_item, "track_title")
    artist = _loaded(log_item, "track_artist")
    code = _loaded(category, "code")
    return StartNextCauseEvidence(
        envelope=envelope,
        occurred_at=occurred_at,
        playlist_log_id=identity["log_id"],
        log_date=identity["log_date"],
        log_hour=identity["log_hour"],
        log_item_position=identity["position"],
        scheduled_category_id=identity["category_id"],
        scheduled_category_code="" if code is _MISSING else code,
        scheduled_category_kind_code=kind_code,
        original_track_id=identity["track_id"],
        original_track_title="" if title is _MISSING else title,
        original_track_artist="" if artist is _MISSING else artist,
        original_effective_airtime_seconds=_ms(_effective_airtime(
            values["next_start_seconds"], values["duration_seconds"], values["cue_in_seconds"],
        )),
    ), None


# ---------------------------------------------------------------------------
# Automatic skip and media failure (r0121)
# ---------------------------------------------------------------------------

def skip_reason_code(reason):
    """The internal code for one of _log_item_playable's two MEDIA reasons,
    else None (a deleted Track, a spoken or active-message refusal, or any
    other text is not media evidence). Never touches the filesystem."""
    if not isinstance(reason, str):
        return None
    if reason.startswith("file missing on disk: "):
        return "file_missing"
    if reason.startswith("track id=") and reason.endswith(" has no filepath"):
        return "no_filepath"
    return None


# (error domain, code) -> internal code. GStreamer posts the specific
# element error first; generic STREAM FAILED (flow/not-linked/negotiation),
# SEEK, core and library errors are deliberately absent.
_RESOURCE, _STREAM = "gst-resource-error-quark", "gst-stream-error-quark"
MEDIA_ERROR_CODES = {
    (_RESOURCE, 3): "resource_not_found",
    (_RESOURCE, 5): "resource_open_read",
    (_RESOURCE, 9): "resource_read",
    (_STREAM, 4): "stream_type_not_found",
    (_STREAM, 5): "stream_wrong_type",
    (_STREAM, 6): "stream_codec_not_found",
    (_STREAM, 7): "stream_decode",
    (_STREAM, 9): "stream_demux",
    (_STREAM, 11): "stream_format",
}


def media_error_code(domain, code):
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return MEDIA_ERROR_CODES.get((domain, code))


@dataclass(frozen=True)
class ScheduledOccurrence:
    """Loaded/cached identity and timing of one scheduled LogItem."""
    origin_source: str                    # "scheduled", another origin, or "unknown"
    kind_code: str
    log_approved: bool
    played: bool
    claimed: bool
    log_item_id: int | None = None
    playlist_log_id: int | None = None
    log_date: date | None = None
    log_hour: int | None = None
    position: int | None = None
    category_id: int | None = None
    category_code: str = ""
    track_id: int | None = None
    track_title: str = ""
    track_artist: str = ""
    cue_in_seconds: float | None = None
    next_start_seconds: float | None = None
    duration_seconds: float | None = None
    complete: bool = False                # every identity/timing value was loaded


def scheduled_occurrence(log_item):
    """Read ONLY already-loaded fields and cached relations; never queries.
    Anything missing leaves complete=False (fail closed)."""
    from library.models import LogItem

    category = _cached(log_item, "category")
    kind = _cached(category, "kind")
    playlist_log = _cached(log_item, "playlist_log")
    track = _cached(log_item, "track")
    origin = _loaded(log_item, "origin")
    values = {
        "log_item_id": _loaded(log_item, "id"),
        "playlist_log_id": _loaded(log_item, "playlist_log_id"),
        "log_date": _loaded(playlist_log, "date"),
        "log_hour": _loaded(playlist_log, "hour"),
        "position": _loaded(log_item, "position"),
        "category_id": _loaded(log_item, "category_id"),
        "track_id": _loaded(track, "id"),
        "cue_in_seconds": _loaded(track, "cue_in_seconds"),
        "next_start_seconds": _loaded(track, "next_start_seconds"),
        "duration_seconds": _loaded(track, "duration_seconds"),
    }
    played_at = _loaded(log_item, "played_at")
    claimed_at = _loaded(log_item, "playback_claimed_at")
    kind_code = _loaded(kind, "code")
    status = _loaded(playlist_log, "status")
    complete = (
        not any(v is _MISSING for v in values.values())
        and played_at is not _MISSING and claimed_at is not _MISSING
        and values["track_id"] is not None
        and _loaded(log_item, "track_id") == values["track_id"]
        and _loaded(playlist_log, "id") == values["playlist_log_id"]
    )
    if origin is _MISSING or not origin:
        source = "unknown"
    else:
        source = "scheduled" if origin == LogItem.ORIGIN_GENERATED_SCHEDULE else origin
    code = _loaded(category, "code")
    title = _loaded(log_item, "track_title")
    artist = _loaded(log_item, "track_artist")
    clean = {k: (None if v is _MISSING else v) for k, v in values.items()}
    return ScheduledOccurrence(
        origin_source=source,
        kind_code="" if kind_code is _MISSING else (kind_code or ""),
        log_approved=status == "approved",
        played=played_at not in (None, _MISSING),
        claimed=claimed_at not in (None, _MISSING),
        category_code="" if code is _MISSING else (code or ""),
        track_title="" if title is _MISSING else (title or ""),
        track_artist="" if artist is _MISSING else (artist or ""),
        complete=complete,
        **clean,
    )


def _occurrence_problem(occ):
    """Common eligibility reason, or None."""
    if not occ.complete:
        return "identity_unknown"
    if occ.origin_source != "scheduled":
        return "not_scheduled_occurrence"
    if occ.kind_code != "music":
        return "not_music"
    if not occ.log_approved:
        return "log_not_approved"
    return None


def _effective(occ):
    return _ms(_effective_airtime(occ.next_start_seconds, occ.duration_seconds, occ.cue_in_seconds))


@dataclass(frozen=True)
class AutomaticSkipObservation:
    occurrence: ScheduledOccurrence
    reason_code: str | None          # skip_reason_code() of the engine's own reason


def classify_automatic_skip(obs):
    """(lost_seconds, None) for a committed skip that is a cause, else
    (None, reason)."""
    if obs.reason_code is None:
        return None, "not_media_reason"
    occ = obs.occurrence
    problem = _occurrence_problem(occ)
    if problem:
        return None, problem
    if occ.played or occ.claimed:
        return None, "possible_replay"
    lost = _effective(occ)
    if lost <= 0:
        return None, "no_effective_airtime"
    return lost, None


def automatic_skip_source_key(log_item_id, track_id):
    """The LOGICAL occurrence: repeats, reloads and restarts collapse."""
    return f"{CAUSE_AUTOMATIC_SKIP}:li{log_item_id}:t{track_id}"


@dataclass(frozen=True)
class AutomaticSkipCauseEvidence:
    """Primitive-only snapshot of a committed automatic skip, built on the
    engine thread without any query. The key is formatted by the worker."""
    occurrence: ScheduledOccurrence
    reason_code: str
    lost_seconds: float
    occurred_at: datetime

    @property
    def source_key(self):
        return automatic_skip_source_key(self.occurrence.log_item_id, self.occurrence.track_id)

    def cause_fields(self):
        occ = self.occurrence
        return dict(
            source_key=self.source_key,
            cause_type=CAUSE_AUTOMATIC_SKIP,
            occurred_at=self.occurred_at,
            playlist_log_id_snapshot=occ.playlist_log_id,
            log_date=occ.log_date,
            log_hour=occ.log_hour,
            log_item_id_snapshot=occ.log_item_id,
            log_item_position=occ.position,
            scheduled_category_id_snapshot=occ.category_id,
            scheduled_category_code=occ.category_code,
            scheduled_category_kind_code=occ.kind_code,
            original_track_id_snapshot=occ.track_id,
            original_track_title=occ.track_title,
            original_track_artist=occ.track_artist,
            original_effective_airtime_seconds=self.lost_seconds,
            observed_shortening_seconds=self.lost_seconds,
            failure_phase=PHASE_PRE_AIR,
            failure_reason=self.reason_code,
        )


def automatic_skip_evidence_from_engine(log_item, reason, *, occurred_at):
    """Called at the committed cursor skip (_peek_playable_at_cursor /
    _next_queue_item walk) with the engine's own playability reason. Loaded
    fields only; never queries, never re-checks the file."""
    obs = AutomaticSkipObservation(scheduled_occurrence(log_item), skip_reason_code(reason))
    lost, why = classify_automatic_skip(obs)
    if lost is None:
        return None, why
    return AutomaticSkipCauseEvidence(obs.occurrence, obs.reason_code, lost, occurred_at), None


@dataclass(frozen=True)
class MediaFailureObservation:
    """What _on_deck_error knows right after claim_completion("error")."""
    occurrence: ScheduledOccurrence
    engine_instance: str
    slot: str
    deck_generation: int | None
    error_code: str | None                # media_error_code()
    completion_claimed: bool = False      # claimed, and for "error"
    deck_mapped: bool = False
    fresh: bool = False                   # air_start_eligible and no continuation
    seek_in_progress: bool = False
    start_next_active: bool = False
    vt_phase: str = "idle"
    other_live_deck: bool = False
    manual_mode: bool = False
    remote_dj_owner: bool = False
    mic_live: bool = False
    air_start_dispatched: bool = False
    air_start_recorded: bool = False
    media_buffers: int = 0
    position_seconds: float | None = None


def classify_media_failure(obs):
    """((phase, lost_seconds), None) for a whitelisted media error that ended
    an eligible scheduled occurrence, else (None, reason)."""
    if obs.error_code is None:
        return None, "not_media_error"
    if not obs.completion_claimed:
        return None, "not_claimed"
    if (not obs.engine_instance or len(obs.engine_instance) > 64 or obs.slot not in ("A", "B")
            or obs.deck_generation is None):
        return None, "identity_unknown"
    if not obs.deck_mapped:
        return None, "superseded"
    if obs.seek_in_progress:
        return None, "seek_in_progress"
    if not obs.fresh:
        return None, "not_fresh_occurrence"
    if obs.start_next_active:
        return None, "start_next_transition"
    if obs.vt_phase != "idle":
        return None, "vt_transition"
    if obs.other_live_deck:
        return None, "overlap"
    if obs.manual_mode:
        return None, "manual_hold"
    if obs.remote_dj_owner:
        return None, "remote_dj_owner"
    if obs.mic_live:
        return None, "mic_live"
    occ = obs.occurrence
    problem = _occurrence_problem(occ)
    if problem:
        return None, problem
    if (not obs.air_start_dispatched and not obs.air_start_recorded
            and obs.media_buffers == 0 and not occ.played):
        lost = _effective(occ)
        if lost <= 0:
            return None, "no_effective_airtime"
        return (PHASE_PRE_AIR, lost), None
    if not (obs.air_start_dispatched and obs.air_start_recorded):
        return None, "air_start_unsettled"
    position = obs.position_seconds
    if (position is None or isinstance(position, bool) or not isinstance(position, (int, float))
            or not math.isfinite(position) or position < 0):
        return None, "position_unknown"
    lost = _ms(remaining_effective_airtime(
        position, occ.cue_in_seconds, occ.next_start_seconds, occ.duration_seconds,
    ))
    if lost <= 0:
        return None, "no_remaining_airtime"
    return (PHASE_POST_AIR, lost), None


def media_failure_source_key(log_item_id, slot, deck_generation, engine_instance):
    """At most ~125 characters (engine_instance <= 64); no user data, no path."""
    return f"{CAUSE_MEDIA_FAILURE}:li{log_item_id}:{slot}g{deck_generation}:{engine_instance}"


@dataclass(frozen=True)
class MediaFailureCauseEvidence:
    occurrence: ScheduledOccurrence
    engine_instance: str
    slot: str
    deck_generation: int
    phase: str
    error_code: str
    lost_seconds: float
    position_seconds: float | None
    occurred_at: datetime

    @property
    def source_key(self):
        return media_failure_source_key(self.occurrence.log_item_id, self.slot, self.deck_generation,
                                         self.engine_instance)

    def cause_fields(self):
        occ = self.occurrence
        return dict(
            source_key=self.source_key,
            cause_type=CAUSE_MEDIA_FAILURE,
            occurred_at=self.occurred_at,
            playlist_log_id_snapshot=occ.playlist_log_id,
            log_date=occ.log_date,
            log_hour=occ.log_hour,
            log_item_id_snapshot=occ.log_item_id,
            log_item_position=occ.position,
            scheduled_category_id_snapshot=occ.category_id,
            scheduled_category_code=occ.category_code,
            scheduled_category_kind_code=occ.kind_code,
            original_track_id_snapshot=occ.track_id,
            original_track_title=occ.track_title,
            original_track_artist=occ.track_artist,
            original_effective_airtime_seconds=_effective(occ),
            observed_shortening_seconds=self.lost_seconds,
            engine_instance=self.engine_instance,
            deck_slot=self.slot,
            deck_generation=self.deck_generation,
            failure_phase=self.phase,
            failure_reason=self.error_code,
            failure_position_seconds=self.position_seconds,
        )


def media_failure_evidence_from_engine(engine, deck, error_domain, error_code, *, occurred_at):
    """Called by PlaybackEngine._on_deck_error on the engine thread, only
    after claim_completion("error") succeeded and before retirement. Reads
    in-memory engine/deck state, already-loaded LogItem/Track/Category
    fields and -- only for a possible post-air cause -- the deck's existing
    position read. Never queries."""
    slot = getattr(deck, "slot", None)
    obs = MediaFailureObservation(
        occurrence=scheduled_occurrence(deck.log_item),
        engine_instance=getattr(engine, "_engine_session_id", "") or "",
        slot=slot,
        deck_generation=getattr(deck, "generation", None),
        error_code=media_error_code(error_domain, error_code),
        completion_claimed=bool(getattr(deck, "completion_claimed", False))
        and getattr(deck, "completion_reason", None) == "error",
        deck_mapped=engine.decks.get(slot) is deck,
        fresh=bool(getattr(deck, "air_start_eligible", False)) and not getattr(deck, "continuation_reason", None),
        seek_in_progress=getattr(deck, "gated_seek", None) is not None,
        start_next_active=getattr(engine, "_start_next_active", None) is not None,
        vt_phase=(getattr(engine, "_vt", None) or {}).get("phase", "idle"),
        other_live_deck=any(other is not deck for other in engine._live_decks()),
        manual_mode=bool(getattr(engine, "manual_mode", False)),
        remote_dj_owner=getattr(engine, "remote_dj_session", None) is not None,
        mic_live=bool(engine._any_mic_live()),
        air_start_dispatched=bool(getattr(deck, "air_start_dispatched", False)),
        air_start_recorded=bool(getattr(deck, "air_start_recorded", False)),
        media_buffers=int(getattr(deck, "media_buffer_count", 0) or 0),
    )
    # Every exclusion first with a placeholder position; the position read
    # happens only for a possible post-air cause.
    result, reason = classify_media_failure(replace(obs, position_seconds=0.0))
    if result is None and reason != "no_remaining_airtime":
        return None, reason
    if result is not None and result[0] == PHASE_PRE_AIR:
        phase, lost = result
        position = None
    else:
        obs = replace(obs, position_seconds=engine._get_deck_position(deck))
        result, reason = classify_media_failure(obs)
        if result is None:
            return None, reason
        (phase, lost), position = result, _ms(obs.position_seconds)
    return MediaFailureCauseEvidence(
        occurrence=obs.occurrence, engine_instance=obs.engine_instance, slot=slot,
        deck_generation=obs.deck_generation, phase=phase, error_code=obs.error_code,
        lost_seconds=lost, position_seconds=position, occurred_at=occurred_at,
    ), None


# ---------------------------------------------------------------------------
# Dry-run trace
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CauseRecord:
    """The immutable payload Phase B will consume (contract §11)."""
    id: int | None
    source_key: str
    cause_type: str
    occurred_at: datetime
    log_id: int
    log_date: date
    log_hour: int
    log_item_id: int
    log_item_position: int
    scheduled_category_id: int | None
    scheduled_category_code: str
    scheduled_category_kind_code: str
    original_track_id: int | None
    original_effective_airtime_seconds: float
    replacement_track_id: int | None
    replacement_effective_airtime_seconds: float | None
    observed_shortening_seconds: float
    song_request_id: int | None = None
    engine_instance: str = ""
    deck_slot: str = ""
    deck_generation: int | None = None
    eject_position_seconds: float | None = None
    start_next_request_id: str = ""
    start_next_position_seconds: float | None = None
    failure_phase: str = ""
    failure_reason: str = ""
    failure_position_seconds: float | None = None

    @classmethod
    def from_model(cls, row):
        return cls(
            id=row.id,
            source_key=row.source_key,
            cause_type=row.cause_type,
            occurred_at=row.occurred_at,
            log_id=row.playlist_log_id_snapshot,
            log_date=row.log_date,
            log_hour=row.log_hour,
            log_item_id=row.log_item_id_snapshot,
            log_item_position=row.log_item_position,
            scheduled_category_id=row.scheduled_category_id_snapshot,
            scheduled_category_code=row.scheduled_category_code,
            scheduled_category_kind_code=row.scheduled_category_kind_code,
            original_track_id=row.original_track_id_snapshot,
            original_effective_airtime_seconds=row.original_effective_airtime_seconds,
            replacement_track_id=row.replacement_track_id_snapshot,
            replacement_effective_airtime_seconds=row.replacement_effective_airtime_seconds,
            observed_shortening_seconds=row.observed_shortening_seconds,
            song_request_id=row.song_request_id_snapshot,
            engine_instance=row.engine_instance,
            deck_slot=row.deck_slot,
            deck_generation=row.deck_generation,
            eject_position_seconds=row.eject_position_seconds,
            start_next_request_id=row.start_next_request_id,
            start_next_position_seconds=row.start_next_position_seconds,
            failure_phase=row.failure_phase,
            failure_reason=row.failure_reason,
            failure_position_seconds=row.failure_position_seconds,
        )


def load_cause_records(*, log_date=None, log_hour=None):
    """Read-only load, in deterministic causal order."""
    from library.models import HourRefillCause

    rows = HourRefillCause.objects.all()
    if log_date is not None:
        rows = rows.filter(log_date=log_date)
    if log_hour is not None:
        rows = rows.filter(log_hour=log_hour)
    return [CauseRecord.from_model(row) for row in rows.order_by("occurred_at", "id")]


@dataclass(frozen=True)
class CategorySource:
    category_id: int | None
    category_code: str
    cause_keys: tuple


@dataclass(frozen=True)
class CausalityTrace:
    active_log_date: date
    active_log_hour: int
    causes: tuple                    # counted, in causal order
    duplicates: tuple                # (source_key, reason) excluded from the sum
    other_hour_causes: tuple         # listed, never counted toward this hour
    original_category_sources: tuple  # CategorySource, first-seen causal order
    observed_shortening_seconds: float
    net_deficit_seconds: None = None  # Phase B owns this; Phase A never states it
    authoritative: bool = False
    notes: tuple = field(default=(
        "observed_shortening_seconds is the sum of per-occurrence shortening, NOT a net hour deficit",
        "no filler has been selected or installed",
    ))


def _causal_order(record):
    return (record.occurred_at, record.id if record.id is not None else 0, record.source_key)


def occurrence_family(cause_type):
    """The at-most-once family a cause type belongs to for ONE occurrence:
    every termination type shares one; any other type is its own."""
    return "termination" if cause_type in TERMINATION_CAUSES else cause_type


def build_causality_trace(records, *, active_log_date, active_log_hour):
    """Pure, deterministic summary of RAW causes for one station-local hour
    (Phase A dry run): per occurrence family, the first record in causal
    order, whether or not it is still valid. Never the basis for a refill:
    hour_refill_projection.partition_causes qualifies each record against
    the current occurrence BEFORE selecting one per family (r0121 P1)."""
    seen_keys = set()
    seen_occurrence = set()
    counted, duplicates, other_hour = [], [], []
    for record in sorted(records, key=_causal_order):
        if record.source_key in seen_keys:
            duplicates.append((record.source_key, "duplicate_source_key"))
            continue
        seen_keys.add(record.source_key)
        if (record.log_date, record.log_hour) != (active_log_date, active_log_hour):
            other_hour.append(record)
            continue
        occurrence = (occurrence_family(record.cause_type), record.log_item_id)
        if occurrence in seen_occurrence:
            duplicates.append((record.source_key, "duplicate_occurrence_cause"))
            continue
        seen_occurrence.add(occurrence)
        counted.append(record)

    sources = {}
    for record in counted:
        key = (record.scheduled_category_id, record.scheduled_category_code)
        sources.setdefault(key, []).append(record.source_key)
    category_sources = tuple(
        CategorySource(category_id=cid, category_code=code, cause_keys=tuple(keys))
        for (cid, code), keys in sources.items()
    )
    return CausalityTrace(
        active_log_date=active_log_date,
        active_log_hour=active_log_hour,
        causes=tuple(counted),
        duplicates=tuple(duplicates),
        other_hour_causes=tuple(other_hour),
        original_category_sources=category_sources,
        observed_shortening_seconds=_ms(sum(r.observed_shortening_seconds for r in counted)),
    )
