"""Event selection, ordering, and deterministic listener-facing text
generation for KanDrive road reports. No generative model anywhere in
this module -- every sentence is built from fixed templates filled in
with verified fields off the normalized RoadEvent (mainly its own
`description`, KDOT's own human-written summary -- see RoadEvent's
docstring on why that field is the principal source) plus a small,
explicitly-listed set of structured fields (route, county, start_time,
detour presence). Nothing here infers a fact the event doesn't already
state.

LENGTH, verified against the real live dataset (14 currently-in-scope
north-central Kansas events at reconnaissance time): the full,
consolidated report runs ~780 words, roughly 5.2-6 minutes of spoken
audio at typical Kokoro/radio pacing -- longer than the ~60-180s a
single "traffic and road conditions" segment usually runs. Per the
task's own escalation order, two length-reduction techniques were
applied where they genuinely help without weakening content:

  1. Boilerplate removal (text_normalize.dedupe_similar_sentences):
     KDOT's own description field frequently repeats itself in a
     trailing "Comment: ..." section that substantially restates the
     auto-generated portion above it. Removing that measured
     boilerplate duplication (~930 -> ~780 words across the live
     dataset) is the one length reduction implemented so far, and it
     is lossless -- every fact stated exactly once, none removed.

  2. Route-based grouping (the task's second suggested technique,
     merging repeated route/county framing across adjacent events on
     the same route) was evaluated against the live dataset and NOT
     implemented this round: only two small groups exist (I-135's 3
     closure-tier events; U.S. 24's 2 roadwork events), and in both
     cases nearly all of each event's own word count is genuinely
     distinct content (different mileposts, different restrictions,
     different dates) -- the shared "On <route>, in <county>, KDOT
     reports:" framing this would collapse is a small fraction of the
     total, so the realistic saving is on the order of 15-20 words,
     not enough to materially change the report's length, while adding
     real design complexity and a new way to misattribute a detail
     between events if done carelessly.

Splitting into multiple files/tracks (the task's fourth technique) was
already rejected architecturally in the output-unit design (see
PROJECT_NOTES.md / the implementation report) -- RotationSlot's own
"not a guarantee" random-weighted selection means a multi-part report
could play out of logical order.

Net conclusion, reported rather than silently engineered around per
the task's explicit "do not arbitrarily discard high-impact events to
meet a short duration target": ~5-6 minutes is what fourteen genuinely
distinct, currently-active KDOT advisories across a multi-county region
actually amount to once real duplication is removed. This is treated as
an accurate reflection of current road conditions, not a defect. If
future experience shows this is impractical for on-air use, the
sharpest next lever is operator-side (covering fewer counties/routes,
raising RoadConditionsConfiguration.min_priority, or shortening
max_event_age_days/lookahead_days) -- all already-existing, already-
configurable knobs -- rather than a new report-side length cap.
"""
import hashlib
import json
from datetime import timedelta

from django.utils import timezone as dj_timezone

from .models import RoadConditionsConfiguration, RoadEvent
from .synthesis import AUDIO_FORMAT_VERSION
from .text_normalize import normalize_for_speech, normalize_route_notation

# Schema/pipeline version for compute_report_fingerprint()'s own
# payload shape -- separate from synthesis.AUDIO_FORMAT_VERSION (which
# versions the actual audio-encoding pipeline; see that constant's own
# comment). Bump THIS one when what's fingerprinted, or how it's
# interpreted, materially changes (e.g. a new field is added/removed
# from the payload, or an existing field's meaning changes) -- that
# forces every existing "unchanged" report to regenerate on its next
# cycle, deliberately, without needing an actual source/text/voice
# change to trigger it. Never derived from a git commit or timestamp.
REPORT_FINGERPRINT_VERSION = 1

# device-status ("automated traffic signals" etc.) is infrastructure/
# administrative status, not something a motorist needs to plan a
# drive around -- excluded from selection entirely. Every other real
# headline_category observed live (closure, roadwork, restriction,
# warning, mobile-situation, special-event) carries genuine motorist-
# relevant content and is eligible.
EXCLUDED_HEADLINE_CATEGORIES = {"device-status"}

# Coarse severity tiers, matching the task's own suggested order
# (1 full closures, 2 serious restrictions/hazards, 3 significant
# construction, 4 routine construction, 5 planned future work) --
# "planned" is checked FIRST and always sorts last regardless of
# category, per "distinguish future work from a restriction already in
# effect." Within a tier, RoadEvent.priority (KDOT's own 1-10 signal)
# breaks ties -- deliberately NOT keyword-matching headline_code
# strings, which KDOT can add to at any time (see services.py's
# normalize_event docstring on the same principle).
_TIER_CLOSURE = 0
_TIER_SERIOUS = 1
_TIER_SIGNIFICANT_CONSTRUCTION = 2
_TIER_ROUTINE_CONSTRUCTION = 3
_TIER_PLANNED = 4

# Priority (1-10) at or above which an in-effect roadwork/mobile-
# situation/special-event record counts as "significant" rather than
# "routine" -- the midpoint of KDOT's own scale, not an arbitrary
# keyword guess.
_SIGNIFICANT_PRIORITY_THRESHOLD = 5


def feed_freshness(config=None):
    """One of "fresh" / "stale" / "failed" / "disabled" -- the signal
    used to decide whether "no current events" may ever be spoken as an
    all-clear (see build_no_events_message()). Never conflates a
    failed/stale fetch with "no road problems" -- see the task's own
    explicit requirement on this point."""
    config = config or RoadConditionsConfiguration.load()
    if not config.enabled:
        return "disabled"
    if config.last_error:
        # last_error is cleared on every success/partial outcome (see
        # services._record_run) -- non-empty means the MOST RECENT
        # attempt is the one that failed, not a stale leftover from
        # some earlier failure that a later success already overwrote.
        return "failed"
    if config.is_stale:
        return "stale"
    return "fresh"


def select_events(now=None):
    """Every currently speakable RoadEvent, ordered by severity tier
    then KDOT's own priority. Filters on source_active=True,
    in_scope=True -- exactly RoadEvent.is_current's own two flags,
    applied at the queryset level for efficiency; equivalent to
    `[e for e in RoadEvent.objects.all() if e.is_current]`. `in_scope`
    already reflects every one of RoadConditionsConfiguration's
    coverage filters (counties/routes/event_classifications/
    min_priority/max_event_age_days/lookahead_days) as of the last
    complete sync -- nothing here re-applies or second-guesses those.

    Excludes device-status/administrative records (no motorist action
    to take) and any record with a blank description (nothing to
    speak, and normalize_event() guarantees this is rare/anomalous,
    not the normal case -- 231/231 live events carried one at
    reconnaissance time).

    Does not attempt cross-event "same real-world condition, different
    event-id" deduplication -- the schema provides no reliable signal
    to detect that (two different CARS events on the same route near
    each other could be genuinely distinct incidents), and services.py
    already deduplicates literal repeated event-ids within one API
    response. Documented as a known limitation, not silently guessed
    at with a fragile text-similarity heuristic."""
    now = now or dj_timezone.now()
    qs = (
        RoadEvent.objects
        .filter(source_active=True, in_scope=True)
        .exclude(headline_category__in=EXCLUDED_HEADLINE_CATEGORIES)
        .exclude(description="")
    )
    events = list(qs)
    events.sort(key=lambda e: (_severity_tier(e, now), -(e.priority or 0), e.external_id))
    return events


def _is_planned(event, now):
    return event.start_time is not None and event.start_time > now


def _severity_tier(event, now):
    if _is_planned(event, now):
        return _TIER_PLANNED
    category = event.headline_category
    if category == "closure":
        return _TIER_CLOSURE
    if category in ("warning", "restriction"):
        return _TIER_SERIOUS
    if category in ("roadwork", "mobile-situation", "special-event"):
        priority = event.priority if event.priority is not None else _SIGNIFICANT_PRIORITY_THRESHOLD
        return _TIER_SIGNIFICANT_CONSTRUCTION if priority >= _SIGNIFICANT_PRIORITY_THRESHOLD else _TIER_ROUTINE_CONSTRUCTION
    # An unrecognized future headline_category -- KDOT can introduce
    # one at any time. Land it in the routine bucket (spoken, not
    # silently dropped) rather than guessing a severity it hasn't earned.
    return _TIER_ROUTINE_CONSTRUCTION


def has_detour(event):
    """True if the source payload documents a detour anywhere in
    details[].descriptions[] -- PRESENCE only. Never narrates the
    detour's own turn-by-turn locations-on-detour list (a real live
    event carried 1,872 waypoints there -- see RoadEvent's raw_payload
    docstring) -- the task explicitly wants a detour mentioned without
    reading a long coordinate list, and "a detour exists" is the one
    fact from that structure safe to state on air."""
    details = event.raw_payload.get("details") if isinstance(event.raw_payload, dict) else None
    if not isinstance(details, list):
        return False
    for detail in details:
        if not isinstance(detail, dict):
            continue
        descriptions = detail.get("descriptions")
        if not isinstance(descriptions, list):
            continue
        for desc in descriptions:
            if isinstance(desc, dict) and desc.get("description-type") == "DetourDescription":
                return True
    return False


def _format_routes(event):
    routes = event.routes or ([event.primary_route] if event.primary_route else [])
    routes = [normalize_route_notation(r) for r in routes if r]
    if not routes:
        return ""
    if len(routes) == 1:
        return routes[0]
    return " and ".join(routes)


def _format_counties(counties):
    """Plain "X, Y, and Z counties" -- deliberately does NOT claim
    "county line"/"boundary" even when 3+ counties are listed (a real
    style example in the task phrases it that way, but that's the
    event's own boundary framing where it happens to be one -- this
    project has no reliable signal that a given multi-county event IS
    a shared boundary point rather than a long route simply passing
    through several counties, so it states only the verified fact:
    which counties are involved)."""
    counties = [c for c in (counties or []) if c]
    if not counties:
        return ""
    if len(counties) == 1:
        return f"{counties[0]} County"
    if len(counties) == 2:
        return f"{counties[0]} and {counties[1]} counties"
    return f"{', '.join(counties[:-1])}, and {counties[-1]} counties"


def _planned_prefix(event, now):
    if not _is_planned(event, now):
        return ""
    local_start = dj_timezone.localtime(event.start_time)
    return f"Beginning {local_start.strftime('%B %-d')}, this is planned work: "


def build_event_script(event, now=None):
    """One event's complete spoken sentence(s). See the module
    docstring for the sourcing rule: structured fields (route, county,
    start_time, detour presence) for the lead-in and framing only;
    event.description (normalized for speech) carries the actual
    condition/boundary/impact/timing content, verbatim as KDOT wrote
    it -- never independently re-narrated, which would risk
    contradicting or duplicating it."""
    now = now or dj_timezone.now()
    normalized_description = normalize_for_speech(event.description)
    route_display = _format_routes(event)
    county_display = _format_counties(event.counties)

    lead_bits = []
    if route_display:
        lead_bits.append(f"On {route_display}")
    if county_display:
        lead_bits.append(f"in {county_display}")
    lead = ", ".join(lead_bits)

    sentence = _planned_prefix(event, now)
    if lead:
        sentence += f"{lead}, KDOT reports: {normalized_description}"
    else:
        sentence += f"KDOT reports: {normalized_description}"

    if has_detour(event):
        sentence += " Motorists should use the posted detour."

    return sentence


class ReportBuildError(Exception):
    """Raised when an individual event's script cannot be safely built.
    The whole generation cycle is treated as failed rather than silently
    dropping that one event (which could leave a closure or restriction
    missing with nothing to indicate why) or airing malformed text --
    see the task's own "individual event formatting failures... must
    not silently produce misleading audio" requirement. The message
    always names the offending event's external_id so the failure is
    reportable and actionable, not a bare traceback."""


def build_event_scripts(events, now=None):
    """Every event's own script, in the already-sorted order -- the same
    per-event pieces build_full_report() joins into one string, exposed
    separately for callers that need the individual audio-segment
    boundaries (e.g. inserting a transition sound effect between items --
    see synthesis.py's synthesize_road_report() `segments` argument and
    compose_report_segments() below). Kept as the single place this
    per-event loop and its error isolation live, so build_full_report()
    and any segment-aware caller can never drift on what "one event's
    script" means or how a per-event failure is reported.

    Raises ReportBuildError (naming the specific event) rather than
    letting a per-event formatting bug propagate as a bare, unattributed
    exception -- every event is built inside its own try/except so a
    failure is always traceable to the one event that caused it, even
    though the practical effect (this generation cycle produces nothing)
    is the same as any other complete failure."""
    now = now or dj_timezone.now()
    scripts = []
    for event in events:
        try:
            scripts.append(build_event_script(event, now))
        except Exception as exc:
            raise ReportBuildError(
                f"Failed to build script for event {event.external_id!r}: {exc!r}"
            ) from exc
    return scripts


def build_full_report(events, now=None):
    """Concatenates every event's script, in the already-sorted order.
    Returns "" for an empty list -- callers decide separately whether
    that means "retire the existing audio" or "speak a no-advisory
    message" (see build_no_events_message() / feed_freshness()).

    Raises ReportBuildError (naming the specific event) -- see
    build_event_scripts(), which this is a thin wrapper around."""
    return " ".join(build_event_scripts(events, now))


def build_no_events_message():
    """Only ever appropriate when feed_freshness() == "fresh" AND
    select_events() is empty -- callers are responsible for that
    check; this function itself doesn't re-verify freshness, so it
    must never be called from a stale/failed/disabled state."""
    return (
        "As of the latest KanDrive update, KDOT is not reporting any "
        "significant road conditions for the north-central Kansas area."
    )


def compose_report_script(body, config, announcer_name=""):
    """Assembles the final on-air script: [preamble] + [body] + [postamble].

    Station framing is kept entirely separate from event/body generation
    on purpose -- build_event_script()/build_full_report()/
    build_no_events_message() know nothing about Oak Grove Radio's own
    on-air branding, and this function knows nothing about KDOT events;
    it only assembles already-built text. `body` is normally the return
    value of build_full_report() or build_no_events_message(), but this
    function doesn't care which -- either way it's just "the middle."

    `{announcer_name}` is the one supported substitution token, applied
    to BOTH config.report_preamble and config.report_postamble (not just
    the postamble) so an operator can freely use it in either admin
    field -- the config help text doesn't restrict it to one field, and
    there's no reason it should be silently unsupported in the other.
    Deliberately a plain, explicit str.replace() rather than Python's
    own str.format(): admin-entered text must never be treated as a
    format string, so an unrelated/unmatched literal '{' or '}' a
    station manager types or pastes into either field can never raise a
    KeyError/IndexError or otherwise misbehave -- it just passes through
    unchanged, exactly like any other character.

    A blank preamble or postamble (after stripping whitespace) simply
    omits that piece -- only the genuinely nonblank pieces are joined,
    with a single space between them, so two blank fields plus a body
    produce just the body, with no stray leading/trailing whitespace or
    doubled-up spacing."""
    preamble, postamble = _resolve_framing(config, announcer_name)
    pieces = [piece for piece in (preamble, body, postamble) if piece]
    return " ".join(pieces)


def _resolve_framing(config, announcer_name):
    """Trim + {announcer_name}-substitute config.report_preamble/
    report_postamble -- the one piece of logic compose_report_script()
    (single combined string) and compose_report_segments() (per-item
    audio-segment list, below) share, so the two can never define
    "blank" or handle {announcer_name} differently. See
    compose_report_script()'s own docstring for why this is a plain
    str.replace() rather than str.format()."""
    preamble = (config.report_preamble or "").strip().replace("{announcer_name}", announcer_name)
    postamble = (config.report_postamble or "").strip().replace("{announcer_name}", announcer_name)
    return preamble, postamble


def compose_report_segments(body_pieces, config, announcer_name=""):
    """Like compose_report_script(), but for a caller that needs the
    report as a LIST of separately-synthesizable segments instead of
    one joined string -- specifically, synthesize_road_report()'s
    transition-sound path (see synthesis.py), which needs a real
    audio-level boundary between each road-condition item to insert a
    sound effect at. `body_pieces` is normally build_event_scripts()'s
    own return value (one piece per item).

    The preamble is folded into the FIRST piece and the postamble into
    the LAST -- never their own separate pieces -- so a transition
    sound is only ever inserted strictly BETWEEN two items, never
    between the preamble and the first item or between the last item
    and the postamble (matching the KNS show ingest script's own
    woosh-between-stories convention: never immediately next to the
    bumper either). Concatenating the returned list with a single
    space joiner reproduces compose_report_script()'s own output
    exactly, given the same body joined with spaces as `body` there --
    the two share `_resolve_framing()` specifically so this equivalence
    can't drift.

    Blank pieces in `body_pieces` are dropped entirely (nothing to
    synthesize). If every piece is blank, returns a single-element list
    containing just the nonblank preamble/postamble (space-joined), or
    an empty list if those are blank too -- callers should treat a
    length-0 or length-1 result as "no meaningful segment boundary",
    i.e. no transition sound is possible."""
    preamble, postamble = _resolve_framing(config, announcer_name)
    segments = [piece for piece in body_pieces if piece]
    if preamble:
        segments = [f"{preamble} {segments[0]}", *segments[1:]] if segments else [preamble]
    if postamble:
        segments = [*segments[:-1], f"{segments[-1]} {postamble}"] if segments else [postamble]
    return segments


def compute_report_fingerprint(text, voice_slot, voice, transition_active,
                                transition_sound_fingerprint=None, segment_count=None):
    """SHA-256 hex digest of a deterministic, canonical JSON payload
    representing the effective on-air artifact this generation cycle
    would produce -- used by generate_road_condition_audio.py to skip
    the expensive Kokoro/ffmpeg/analysis pipeline when nothing that
    actually affects the resulting audio has changed since the last
    successful generation (see RoadConditionsConfiguration.
    last_report_fingerprint / last_report_generated_at).

    Deliberately fingerprints the FINAL, fully-resolved generation
    inputs -- never RoadEvent.payload_checksum or any raw KDOT
    response -- because the on-air result can change for reasons a
    source checksum alone can't capture: preamble/postamble edits,
    {announcer_name} resolution, voice slot/model changes, an event's
    own wording changing purely because wall-clock time crossed its
    start_time (planned -> in-effect -- see report._planned_prefix()),
    and transition-sound state. This is also why callers must compute
    the fingerprint only AFTER event selection, text composition,
    voice resolution, AND transition-sound resolution have all
    already happened -- moving the skip decision any earlier would
    mean deciding "unchanged" without having actually looked at
    everything that determines the final audio.

    `text` must be the EXACT final composed script (after event
    selection/ordering, no-events handling, and preamble/postamble/
    {announcer_name} resolution) -- see compose_report_script()/
    compose_report_segments() above.

    `voice_slot`/`voice['engine']`/`voice['model']` identify the
    SYNTHESIS identity, not the listener-facing announcer name -- the
    model behind an announcer name could change without the name
    itself changing, and that must still force regeneration. `slot` is
    kept as its own field (not folded into the model string) so a
    schedule change that resolves the same model under a different
    slot name is still an explicit, visible difference here.

    `transition_active`/`transition_sound_fingerprint`/`segment_count`
    must reflect the ACTUAL behavior this run would produce, not
    merely the config's own enabled/path settings -- a configured-but-
    currently-unusable (missing/unreadable) transition sound must be
    passed here as `transition_active=False`, exactly matching the
    plain-synthesis fallback the caller actually performs in that case
    (see generate_road_condition_audio.py's own transition-sound
    decision block, which runs BEFORE this function and is what the
    two arguments here are computed from). `transition_sound_fingerprint`
    is the transition file's own content hash (see synthesis.
    hash_file_sha256()) -- content, not pathname, so replacing the
    file in place while keeping the same configured path still changes
    the fingerprint. `segment_count` (the number of separately-
    synthesized, transition-spliced pieces) is included so that a
    theoretical case where two DIFFERENT event selections happen to
    join into byte-identical final `text` (extremely unlikely in
    practice -- every real event script includes its own route/county/
    KDOT description specifics) still can't produce an identical
    fingerprint if it would actually insert a different number of
    transition sounds; irrelevant, and therefore ignored, whenever
    transition_active is False, since item/segment count has zero
    effect on a single-Kokoro-call synthesis.

    `version` is REPORT_FINGERPRINT_VERSION and `audio_format_version`
    is synthesis.AUDIO_FORMAT_VERSION -- two independent counters (see
    each constant's own comment) a future change can bump to
    deliberately force every existing "unchanged" report to regenerate,
    without needing an actual source/text/voice change to trigger it.

    Canonical serialization: sort_keys=True, compact separators
    (no incidental whitespace), UTF-8 -- so byte-identical inputs
    always hash identically regardless of dict insertion order."""
    payload = {
        "version": REPORT_FINGERPRINT_VERSION,
        "text": text,
        "voice": {
            "slot": voice_slot,
            "engine": voice.get("engine"),
            "model": voice.get("model"),
        },
        "transition": {
            "active": bool(transition_active),
            "sound_fingerprint": transition_sound_fingerprint if transition_active else None,
            "segment_count": segment_count if transition_active else None,
        },
        "audio_format_version": AUDIO_FORMAT_VERSION,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
