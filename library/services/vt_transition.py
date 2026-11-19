"""Roadmap 2.25 B1 -- pure Evergreen Voice Track transition planning.

Side-effect free: no Django ORM, no GStreamer, no GLib, no clock, no engine
state. Callers describe the transition facts explicitly; the module returns
independent outro/intro eligibility (with a reason when suppressed) and,
for eligible sides, the running-time plan of the approved contract
(docs/roadmap/2_25_PHASE_A_VT_TRANSITION_CONTRACT.md sections 4-5).

Running times are seconds on the program pipeline's running-time axis at
which a sample is RENDERED. Media positions are seconds within a file.

Approved intro model (D1, D2, D10, D11):
    V = validated playable VT duration
    C = effective incoming music start (Cue In)
    I = Intro Until
    W = max(0, I - C)
    M = 0.150 s
    U = max(0, V - W + M)   when a usable window exists (0 <= C < I <= duration)
    U = V + M                when it does not (the whole VT, M, then the song)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

VOCAL_SAFETY_MARGIN_S = 0.150      # M (D11): fixed engine constant, no admin field
READY_LEAD_S = 0.050               # a fire must be ready this long before admission
FIRST_AUDIO_GRACE_S = 1.0          # first-audio deadline after admission
COMPLETION_GRACE_S = 2.0           # completion deadline after V
SCHEDULED_DECK_READY_S = 0.100     # scheduled deck readiness deadline after open (F15 bound)
REOPEN_LEAD_S = 0.150              # re-admission lead for a regenerated held occurrence (B7: measured,
                                   # readiness <= 53 ms in every representative load; 100 ms missed
                                   # READY_LEAD_S under a busy GLib loop -- see the B7 qualification doc)
REOPEN_MAX_ATTEMPTS = 2

# -- B3/B4 integration (engine transition coordinator) --------------------------
PLAN_HORIZON_S = 45.0              # start planning when the segue is this close
OUTRO_PREPARE_LEAD_S = 1.0         # prepare the outro fire this long before Outro Starts renders
MIN_PREPARE_LEAD_S = 0.120         # never prepare a fire for an admission closer than this
OUTRO_LATE_TOLERANCE_S = 0.250     # a missed outro preparation later than this is suppressed, not fired late
COMMIT_LEAD_S = 0.600              # planned commit (claim + scheduled deck open) before min(S_v, deck offset)
UNPLANNED_LEAD_S = 0.300           # an automatic start that finds an eligible intro VT at its claim
DECK_MIN_LEAD_S = 0.120            # a scheduled deck's media-0 admission must be at least this far ahead
TRANSITION_GRACE_S = 3.0           # transition deadline after its planned end (contract section 7)
URGENT_FADE_MS = 50                # audible outro limited at the segue for an urgent item (section 5)
OUTRO_DELIVERY_MEMORY = 512        # bounded in-process outro delivery record (D13)

# -- B5 operator controls ------------------------------------------------------
SPEECH_CANCEL_FADE_MS = 50         # D6/D8: audible VT speech cancelled by mic, Start Next, operator command
HOLD_MIN_LEAD_S = 0.120            # retired by B7 supplemental C7 (kept for readers; not used for decisions)
MIXER_RECALL_GUARD_S = 0.020       # C7: deck-mixer output this close to an admission is treated as pushed
START_NEXT_ADMIT_LEAD_S = 0.150    # Start Next re-admits a held claimed occurrence this far ahead

# -- B6 asynchronous lookups (F13) ----------------------------------------------
LOOKUP_REFRESH_S = 2.0             # a planning answer older than this is re-requested
LOOKUP_PLAN_MAX_AGE_S = 30.0       # a (tentative) planning answer older than this is not used
CONFIG_REFRESH_S = 5.0             # VoiceTrackConfig re-read interval (as r0144's 5 s cache)
LOOKUP_AUTH_MAX_AGE_S = 3.0        # an outro is authorized only by an answer requested this recently
LOOKUP_PENDING = "vt_lookup_pending"           # no answer yet: not plannable this poll (never sticky)
LOOKUP_UNAVAILABLE = "vt_lookup_unavailable"   # the worker could not answer in time / at all
RECORDING_CHANGED = "recording_changed_after_claim"

OUTRO = "outro"
INTRO = "intro"


class InvalidTransitionInput(ValueError):
    """A programming-level fact that cannot describe a real transition
    (non-finite running time, negative gap or ramp, positive duck gain)."""


# -- start kinds (D5, D13) ----------------------------------------------------
class StartKind:
    AUTOMATIC = "automatic"                  # trigger, natural/error/watchdog completion, VT hold release
    START_NEXT = "start_next"                # explicit operator Start Next
    MANUAL = "manual"                        # operator manual deck start / Play Now
    EJECT_REPLACEMENT = "eject_replacement"  # started as a direct consequence of Eject
    MANUAL_TO_AUTO_HANDOFF = "manual_to_auto_handoff"   # deferred start released by Return to Auto
    RESUME = "resume"                        # interrupted-occurrence recovery (restart auto-resume)
    CONTINUATION = "continuation"            # seek / pause-resume / re-creation generation

    ALL = (AUTOMATIC, START_NEXT, MANUAL, EJECT_REPLACEMENT, MANUAL_TO_AUTO_HANDOFF, RESUME, CONTINUATION)


# -- item roles (D14) ---------------------------------------------------------
class ItemRole:
    ORDINARY = "ordinary"                    # music, promos, IDs: an attached evergreen VT is allowed
    URGENT = "urgent"                        # Weather/AMBER (wx_alert_playout) urgent item
    DEDICATION_INTRO = "dedication_intro"
    DEDICATION_FOLLOWUP = "dedication_followup"
    ACTIVE_MESSAGE = "active_message"

    ALL = (ORDINARY, URGENT, DEDICATION_INTRO, DEDICATION_FOLLOWUP, ACTIVE_MESSAGE)


# -- suppression reasons -------------------------------------------------------
class Reason:
    NO_RECORDING = "no_recording"
    RECORDING_UNPLAYABLE = "recording_unplayable"          # record exists, no playable media
    INVALID_VT_DURATION = "invalid_vt_duration"
    INVALID_TRACK_DURATION = "invalid_track_duration"
    INVALID_CUE_IN = "invalid_cue_in"
    OUTRO_MARKER_MISSING = "outro_marker_missing"          # D4
    OUTRO_MARKER_INVALID = "outro_marker_invalid"          # D4
    OUTRO_MARKER_AT_OR_AFTER_SEGUE = "outro_marker_at_or_after_segue"   # D7
    NOT_AUTO = "not_auto"
    MIC_LIVE = "mic_live"                                  # D6
    REMOTE_DJ_LIVE = "remote_dj_live"                      # D6
    OPERATOR_START = "operator_start"                      # D5
    MANUAL_TO_AUTO_HANDOFF = "manual_to_auto_handoff"      # D5
    INTERRUPTED_OCCURRENCE_RECOVERY = "interrupted_occurrence_recovery"   # D13
    CONTINUATION_GENERATION = "continuation_generation"    # D13
    ALREADY_CLAIMED = "already_claimed"                    # D13
    OUTRO_ALREADY_AIRED = "outro_already_aired"            # D13
    OUTRO_DELIVERY_UNCERTAIN = "outro_delivery_uncertain"  # D13
    GENERATION_STARTED_PAST_MARKER = "generation_started_past_marker"
    PROTECTED_URGENT = "protected_urgent"                  # D14
    PROTECTED_DEDICATION = "protected_dedication"          # D14
    PROTECTED_ACTIVE_MESSAGE = "protected_active_message"  # D14
    URGENT_NEXT_NO_HOLD = "urgent_next_no_hold"            # D3 urgent rule


# -- facts ---------------------------------------------------------------------
@dataclass(frozen=True)
class Recording:
    """What playable_audio() resolved for one VoiceTrack, or no record.

    ``attached`` -- a VoiceTrack row exists for (track, position).
    ``playable`` -- playable_audio() returned a present file (ProductionMedia or
                    the legacy fallback exactly as it defines it).
    ``duration`` -- the validated duration of that playable file.
    """
    attached: bool = False
    playable: bool = False
    duration: Optional[float] = None
    source: str = ""          # "production_media" | "legacy" | ""


@dataclass(frozen=True)
class Markers:
    """Track markers as stored (media positions, seconds; None = unset)."""
    duration: Optional[float]
    cue_in: Optional[float] = None
    next_start: Optional[float] = None
    outro_starts: Optional[float] = None
    intro_until: Optional[float] = None


@dataclass(frozen=True)
class LiveContext:
    auto_mode: bool = True
    mic_live: bool = False
    remote_dj_live: bool = False


@dataclass(frozen=True)
class OutroFacts:
    recording: Recording
    markers: Markers
    generation_start_position: float = 0.0      # media position this generation started at
    is_continuation: bool = False               # seek / resume / re-creation generation
    resume_staleness_s: float = 0.0             # saved-position age + state write interval (resume only)
    already_aired: bool = False                 # this occurrence's outro VT aired in this process


@dataclass(frozen=True)
class IntroFacts:
    recording: Recording
    markers: Markers
    start_kind: str = StartKind.AUTOMATIC
    role: str = ItemRole.ORDINARY
    already_claimed: bool = False               # playback_claimed_at existed before this start


@dataclass(frozen=True)
class SideEligibility:
    side: str
    eligible: bool
    reason: str = ""
    duration: Optional[float] = None            # V when eligible


# -- validation helpers ----------------------------------------------------------
def _finite(value):
    return value is not None and isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def effective_cue_in(markers: Markers) -> Optional[float]:
    """C: Cue In, 0.0 when unset; None when unusable (negative, non-finite,
    or not before the track's duration)."""
    if not _finite(markers.duration) or markers.duration <= 0:
        return None
    cue = 0.0 if markers.cue_in is None else markers.cue_in
    if not _finite(cue) or cue < 0 or cue >= markers.duration:
        return None
    return float(cue)


def effective_next_start(markers: Markers) -> Optional[float]:
    if not _finite(markers.duration) or markers.duration <= 0:
        return None
    if markers.next_start is None:
        return float(markers.duration)
    if not _finite(markers.next_start) or markers.next_start <= 0 or markers.next_start > markers.duration:
        return None
    return float(markers.next_start)


def usable_intro_window(markers: Markers) -> Optional[float]:
    """W when a usable window exists (0 <= C < I <= duration), else None."""
    cue = effective_cue_in(markers)
    intro = markers.intro_until
    if cue is None or not _finite(intro) or intro <= cue or intro > markers.duration:
        return None
    return float(intro) - cue


def underlap(v: float, markers: Markers, margin: float = VOCAL_SAFETY_MARGIN_S) -> float:
    """U per the approved model. Requires a valid V and a valid Cue In."""
    if not _finite(v) or v <= 0:
        raise InvalidTransitionInput(f"V must be a positive finite duration, got {v!r}")
    window = usable_intro_window(markers)
    if window is None:
        return _ns(v + margin)
    # Quantized to the pipeline's 1 ns running-time resolution, so an exact
    # fit (V = W - M) is exactly zero rather than floating-point residue.
    return max(0.0, _ns(v - window + margin))


def _ns(seconds: float) -> float:
    return round(seconds, 9)


def _recording_reason(recording: Recording):
    if recording is None or not recording.attached:
        return Reason.NO_RECORDING
    if not recording.playable:
        return Reason.RECORDING_UNPLAYABLE
    if not _finite(recording.duration) or recording.duration <= 0:
        return Reason.INVALID_VT_DURATION
    return ""


def _live_reason(live: LiveContext):
    if not live.auto_mode:
        return Reason.NOT_AUTO
    if live.mic_live:
        return Reason.MIC_LIVE
    if live.remote_dj_live:
        return Reason.REMOTE_DJ_LIVE
    return ""


# -- eligibility -----------------------------------------------------------------
def outro_eligibility(facts: OutroFacts, live: LiveContext) -> SideEligibility:
    """Outgoing side (D4, D6, D7, D13). Independent of the incoming."""
    reason = _recording_reason(facts.recording)
    if not reason:
        markers = facts.markers
        cue = effective_cue_in(markers)
        next_start = effective_next_start(markers)
        if not _finite(markers.duration) or markers.duration <= 0:
            reason = Reason.INVALID_TRACK_DURATION
        elif cue is None:
            reason = Reason.INVALID_CUE_IN
        elif markers.outro_starts is None:
            reason = Reason.OUTRO_MARKER_MISSING
        elif not _finite(markers.outro_starts) or markers.outro_starts <= cue or next_start is None:
            reason = Reason.OUTRO_MARKER_INVALID
        elif markers.outro_starts >= next_start:
            reason = Reason.OUTRO_MARKER_AT_OR_AFTER_SEGUE
    if not reason:
        reason = _live_reason(live)
    if not reason and facts.already_aired:
        reason = Reason.OUTRO_ALREADY_AIRED
    if not reason:
        start = facts.generation_start_position
        if not _finite(start) or start < 0:
            raise InvalidTransitionInput(f"generation start position must be finite and >= 0, got {start!r}")
        staleness = facts.resume_staleness_s
        if (not isinstance(staleness, (int, float)) or isinstance(staleness, bool)
                or math.isnan(staleness) or staleness < 0):
            # +inf is allowed: unknown evidence age (always uncertain).
            raise InvalidTransitionInput(f"resume staleness must be >= 0, got {staleness!r}")
        outro = facts.markers.outro_starts
        if facts.is_continuation:
            # D13: the generation started where saved evidence said; the true
            # position at the interruption may have been up to ``staleness``
            # later. Fire only if delivery cannot have happened.
            if start + staleness >= outro:
                reason = Reason.OUTRO_DELIVERY_UNCERTAIN
        elif start >= outro:
            reason = Reason.GENERATION_STARTED_PAST_MARKER
    if reason:
        return SideEligibility(OUTRO, False, reason)
    return SideEligibility(OUTRO, True, "", float(facts.recording.duration))


def intro_eligibility(facts: IntroFacts, live: LiveContext) -> SideEligibility:
    """Incoming side (D5, D6, D10, D13, D14). Independent of the outgoing.

    A missing or unusable Intro Until does not suppress the intro VT (D10);
    it only changes the timing (U = V + M)."""
    if facts.start_kind not in StartKind.ALL:
        raise InvalidTransitionInput(f"unknown start kind {facts.start_kind!r}")
    if facts.role not in ItemRole.ALL:
        raise InvalidTransitionInput(f"unknown item role {facts.role!r}")
    reason = ""
    role = facts.role
    if role == ItemRole.URGENT:
        reason = Reason.PROTECTED_URGENT
    elif role in (ItemRole.DEDICATION_INTRO, ItemRole.DEDICATION_FOLLOWUP):
        reason = Reason.PROTECTED_DEDICATION
    elif role == ItemRole.ACTIVE_MESSAGE:
        reason = Reason.PROTECTED_ACTIVE_MESSAGE
    if not reason:
        kind = facts.start_kind
        if kind in (StartKind.START_NEXT, StartKind.MANUAL, StartKind.EJECT_REPLACEMENT):
            reason = Reason.OPERATOR_START
        elif kind == StartKind.MANUAL_TO_AUTO_HANDOFF:
            reason = Reason.MANUAL_TO_AUTO_HANDOFF
        elif kind == StartKind.RESUME:
            reason = Reason.INTERRUPTED_OCCURRENCE_RECOVERY
        elif kind == StartKind.CONTINUATION:
            reason = Reason.CONTINUATION_GENERATION
        elif facts.already_claimed:
            reason = Reason.ALREADY_CLAIMED
    if not reason:
        reason = _recording_reason(facts.recording)
    if not reason:
        markers = facts.markers
        if not _finite(markers.duration) or markers.duration <= 0:
            reason = Reason.INVALID_TRACK_DURATION
        elif effective_cue_in(markers) is None:
            reason = Reason.INVALID_CUE_IN
    if not reason:
        reason = _live_reason(live)
    if reason:
        return SideEligibility(INTRO, False, reason)
    return SideEligibility(INTRO, True, "", float(facts.recording.duration))


# -- plan --------------------------------------------------------------------------
@dataclass(frozen=True)
class TransitionInput:
    """Running-time facts for one transition.

    ``segue_at`` (A_seg): running time at which the outgoing's Next Start is
    rendered. ``outro_at`` (O_s): running time at which the outgoing's Outro
    Starts is rendered (only used when the outro side is eligible).
    """
    segue_at: float
    outro: SideEligibility
    intro: SideEligibility
    incoming_markers: Markers
    outro_at: Optional[float] = None
    gap_s: float = 0.2                    # VoiceTrackConfig.min_gap_ms / 1000
    duck_db: float = -6.0                 # VoiceTrackConfig.program_duck_db
    ramp_s: float = 0.3                   # VoiceTrackConfig.duck_ramp_ms / 1000
    next_is_urgent: bool = False
    margin_s: float = VOCAL_SAFETY_MARGIN_S


@dataclass(frozen=True)
class TransitionPlan:
    segue_at: float                       # A_seg
    music_at: float                       # A_I: incoming Cue In rendered
    deck_offset: float                    # A_I - C: scheduled deck admission (media 0)
    hold_until: Optional[float]           # set when the segue is held for outro speech (D3)
    outro_start: Optional[float] = None   # O_s
    outro_end: Optional[float] = None     # O_end
    intro_start: Optional[float] = None   # S_v
    intro_end: Optional[float] = None     # S_v + V
    underlap: Optional[float] = None      # U
    vocal_at: Optional[float] = None      # Intro Until rendered (None when no usable window)
    clearance: Optional[float] = None     # vocal_at - intro_end
    duck_gain: float = 1.0
    duck_points: tuple = ()
    outro_reason: str = ""
    intro_reason: str = ""
    suppressed: dict = field(default_factory=dict)

    @property
    def has_speech(self):
        return self.outro_start is not None or self.intro_start is not None


def duck_gain_for(duck_db: float) -> float:
    if not _finite(duck_db) or duck_db > 0:
        raise InvalidTransitionInput(f"program duck must be finite and <= 0 dB, got {duck_db!r}")
    return 10 ** (duck_db / 20.0)


def duck_points(intervals, ramp_s: float, gain: float):
    """Speech-owned duck (D12): attack one ramp BEFORE each speech start,
    release from each speech end. Intervals closer than 2 x ramp merge.
    Returns ((running_time, gain), ...) with strictly non-decreasing times;
    empty when there is no speech or no attenuation."""
    if not _finite(ramp_s) or ramp_s < 0:
        raise InvalidTransitionInput(f"ramp must be finite and >= 0, got {ramp_s!r}")
    if gain >= 1.0 or not intervals:
        return ()
    merged = []
    for start, end in sorted(intervals):
        if end < start:
            raise InvalidTransitionInput(f"speech interval ends before it starts: {(start, end)!r}")
        if merged and start - ramp_s <= merged[-1][1] + ramp_s:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    points = []
    for start, end in merged:
        points += [(start - ramp_s, 1.0), (start, gain), (end, gain), (end + ramp_s, 1.0)]
    return tuple(points)


def plan_transition(inp: TransitionInput) -> TransitionPlan:
    """The approved section 4 plan for eligible sides; suppressed sides
    contribute nothing. Never raises for marker problems (those are
    eligibility); raises InvalidTransitionInput for non-physical inputs."""
    for name in ("segue_at", "gap_s", "ramp_s", "margin_s"):
        value = getattr(inp, name)
        if not _finite(value):
            raise InvalidTransitionInput(f"{name} must be finite, got {value!r}")
    if inp.gap_s < 0 or inp.ramp_s < 0 or inp.margin_s < 0:
        raise InvalidTransitionInput("gap, ramp and margin must be >= 0")
    gain = duck_gain_for(inp.duck_db)
    markers = inp.incoming_markers
    cue = effective_cue_in(markers)
    if cue is None:
        raise InvalidTransitionInput("incoming Cue In/duration unusable: the incoming cannot be admitted")

    suppressed = {}
    outro_start = outro_end = None
    outro_reason = "" if inp.outro.eligible else inp.outro.reason
    if inp.outro.eligible:
        if not _finite(inp.outro_at):
            raise InvalidTransitionInput("an eligible outro side needs a finite outro_at")
        o_s = float(inp.outro_at)
        o_e = o_s + inp.outro.duration
        if inp.next_is_urgent and o_e > inp.segue_at:
            # D3/D14 urgent rule: never hold an urgent item for recorded speech.
            outro_reason = Reason.URGENT_NEXT_NO_HOLD
            suppressed[OUTRO] = outro_reason
        else:
            outro_start, outro_end = o_s, o_e
    elif inp.outro.reason:
        suppressed[OUTRO] = inp.outro.reason

    a0 = max(inp.segue_at, outro_end) if outro_end is not None else inp.segue_at
    hold_until = a0 if a0 > inp.segue_at else None

    intro_start = intro_end = u = vocal_at = clearance = None
    intro_reason = "" if inp.intro.eligible else inp.intro.reason
    if inp.intro.eligible:
        v = inp.intro.duration
        intro_start = max(a0, outro_end + inp.gap_s) if outro_end is not None else a0
        u = underlap(v, markers, inp.margin_s)
        music_at = intro_start + u
        intro_end = intro_start + v
        window = usable_intro_window(markers)
        if window is not None:
            vocal_at = music_at + window
            clearance = vocal_at - intro_end
    else:
        if inp.intro.reason:
            suppressed[INTRO] = inp.intro.reason
        music_at = a0

    speech = []
    if outro_start is not None:
        speech.append((outro_start, outro_end))
    if intro_start is not None:
        speech.append((intro_start, intro_end))
    return TransitionPlan(
        segue_at=inp.segue_at,
        music_at=music_at,
        deck_offset=music_at - cue,
        hold_until=hold_until,
        outro_start=outro_start,
        outro_end=outro_end,
        intro_start=intro_start,
        intro_end=intro_end,
        underlap=u,
        vocal_at=vocal_at,
        clearance=clearance,
        duck_gain=gain,
        duck_points=duck_points(speech, inp.ramp_s, gain),
        outro_reason=outro_reason,
        intro_reason=intro_reason,
        suppressed=suppressed,
    )


def fire_deadlines(admission_at: float, duration: float):
    """Running-time deadlines for one scheduled VT fire (contract section 7).

    A branch correctly waiting for a future admission is never "stalled":
    readiness is due before admission, first audio and completion are
    measured from admission, not from preparation."""
    if not (_finite(admission_at) and _finite(duration) and duration > 0):
        raise InvalidTransitionInput("admission and duration must be finite; duration > 0")
    return {
        "ready_by": admission_at - READY_LEAD_S,
        "first_audio_by": admission_at + FIRST_AUDIO_GRACE_S,
        "complete_by": admission_at + duration + COMPLETION_GRACE_S,
    }


def outro_resume_staleness(state_age_seconds, write_interval_seconds=1.0, margin_seconds=2.0):
    """D13 bound for a continuation generation's outro decision: the age of
    the saved resume evidence plus the state-write interval plus a margin.
    Unknown age is treated as unbounded (suppress)."""
    if not _finite(state_age_seconds) or state_age_seconds < 0:
        return math.inf
    return float(state_age_seconds) + write_interval_seconds + margin_seconds
