"""Roadmap 2.27 Phase A -- hour-refill causal evidence (evidence only).

Pure helpers plus the one guarded write used by the web-request swap.
Nothing here selects, installs or plays anything, and nothing consumes the
records yet; see docs/roadmap/2_27_PHASE_A_CAUSALITY_CONTRACT.md.

- request substitution: build_request_substitution_evidence() decides,
  from plain values, whether a real swap is a cause;
  record_request_substitution_cause() persists it inside the caller's
  transaction, in a savepoint, and never lets a failure escape.
- operator Eject: classify_eject_observation() is the contract for a
  later live hook. No engine code calls it in Phase A.
- trace: build_causality_trace() orders, deduplicates and summarises
  causes. It reports observed shortening, never a net hour deficit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from django.db import transaction

from library.services.log_builder import effective_airtime_seconds

CAUSE_REQUEST_SUBSTITUTION = "request_substitution"
CAUSE_OPERATOR_EJECT = "operator_eject"

# Phase B rule for a web-request-triggered MUSIC refill: strictly MORE than
# 60 seconds of NET deficit. Never applied at cause-capture time.
REQUEST_MUSIC_TRIGGER_THRESHOLD_SECONDS = 60


def request_music_threshold_exceeded(net_deficit_seconds):
    """True only for a net deficit strictly greater than 60 seconds."""
    return net_deficit_seconds is not None and net_deficit_seconds > REQUEST_MUSIC_TRIGGER_THRESHOLD_SECONDS


def _ms(value):
    return round(float(value), 3)


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
            HourRefillCause.objects.create(
                source_key=evidence.source_key,
                cause_type=CAUSE_REQUEST_SUBSTITUTION,
                occurred_at=evidence.occurred_at,
                playlist_log_id_snapshot=evidence.playlist_log_id,
                log_date=evidence.log_date,
                log_hour=evidence.log_hour,
                log_item_id_snapshot=evidence.log_item_id,
                log_item_position=evidence.log_item_position,
                scheduled_category_id_snapshot=evidence.scheduled_category_id,
                scheduled_category_code=evidence.scheduled_category_code,
                scheduled_category_kind_code=evidence.scheduled_category_kind_code,
                original_track_id_snapshot=evidence.original_track_id,
                original_track_title=evidence.original_track_title,
                original_track_artist=evidence.original_track_artist,
                original_effective_airtime_seconds=evidence.original_effective_airtime_seconds,
                replacement_track_id_snapshot=evidence.replacement_track_id,
                replacement_effective_airtime_seconds=evidence.replacement_effective_airtime_seconds,
                observed_shortening_seconds=evidence.observed_shortening_seconds,
                song_request_id_snapshot=evidence.song_request_id,
            )
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


# ---------------------------------------------------------------------------
# Operator Eject (contract only -- no live hook in Phase A)
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
    occurrence_source: str = "scheduled"   # scheduled | insert | forced | resume | play_now
    on_air: bool = False                   # first real buffer recorded
    paused: bool = False
    other_live_deck: bool = False
    vt_phase: str = "idle"
    manual_mode: bool = False
    remote_dj_owner: bool = False
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
    if obs.vt_phase != "idle":
        return None, "vt_transition"
    if obs.other_live_deck:
        return None, "overlap"
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


def build_causality_trace(records, *, active_log_date, active_log_hour):
    """Pure, deterministic summary of causes for one station-local hour."""
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
        occurrence = (record.cause_type, record.log_item_id)
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
