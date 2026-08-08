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
from datetime import timedelta

from django.utils import timezone as dj_timezone

from .models import RoadConditionsConfiguration, RoadEvent
from .text_normalize import normalize_for_speech, normalize_route_notation

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


def build_full_report(events, now=None):
    """Concatenates every event's script, in the already-sorted order.
    Returns "" for an empty list -- callers decide separately whether
    that means "retire the existing audio" or "speak a no-advisory
    message" (see build_no_events_message() / feed_freshness()).

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
    return " ".join(scripts)


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
    preamble = (config.report_preamble or "").strip().replace("{announcer_name}", announcer_name)
    postamble = (config.report_postamble or "").strip().replace("{announcer_name}", announcer_name)
    pieces = [piece for piece in (preamble, body, postamble) if piece]
    return " ".join(pieces)
