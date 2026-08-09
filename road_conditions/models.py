from django.core.exceptions import ValidationError
from django.db import models


# Verified live against the real API (2026-08-04 reconnaissance, see
# scratchpad/road_conditions/ -- not committed): GET /eventClassifications
# on Kansas's CARS deployment (https://kscars.kandrive.gov/carsapi_v1/api)
# returns exactly these five values. These are the API's own QUERY-level
# buckets (the `eventClassifications` filter parameter) -- a completely
# different concept from an individual event's own `headline_category`/
# `headline_code` below (e.g. "roadwork"/"construction work"). Keep the
# word "classification" scoped to this API-query concept only; RoadEvent
# never uses it, to avoid the two ideas reading as the same thing.
# "truckersReports" is left out of the default on purpose -- it overlaps
# heavily with constructionReports/roadReports (189 of 231 live events
# matched it) and skews toward trucking-specific detail (weight/width
# restrictions, weigh-station status) that's less central to a general-
# audience road report; the operator can add it back in the admin.
DEFAULT_EVENT_CLASSIFICATIONS = "constructionReports,roadReports,winterDriving,weatherWarningsAreaEvents"

# Same eight north-central Kansas counties AmberAlertConfig already
# defaults to (see weather/models.py DEFAULT_AMBER_SAME_CODES) -- these
# are real county names as KDOT's CARS API returns them on the
# `counties` field (plain strings like "Ottawa", not FIPS codes -- FIPS
# only shows up buried in a couple of unused nested schema objects the
# live API never actually populates).
DEFAULT_COUNTIES = "Ottawa,Saline,Cloud,Lincoln,Mitchell,Clay,Dickinson,Ellsworth"


def parse_additional_route_coverage(text):
    """Parses RoadConditionsConfiguration.additional_route_coverage's
    one-rule-per-line `ROUTE: County,County` format (see that field's
    help_text for the exact contract). Returns (rules, errors):

      * `rules` -- {route: {county, ...}}, one entry per syntactically
        well-formed nonblank line. Two lines for the same route merge
        their county sets rather than the later line silently
        overwriting the earlier one.
      * `errors` -- a human-readable string per nonblank line that
        could NOT be parsed as a well-formed rule (no ':', blank route
        before it, or zero usable counties after it, e.g. bare "US 81"
        or ": Saline"). Used by RoadConditionsConfiguration.clean() to
        reject the config at admin save time -- see that method.

    A malformed line NEVER contributes a rule to `rules`, independent
    of `errors` -- callers that only want the parsed rules (see
    RoadConditionsConfiguration.additional_route_coverage_rules) can
    safely ignore `errors` and still never get broader matching than
    intended, even if malformed text somehow ends up saved outside the
    admin's own clean()-enforced path (e.g. loaddata, a direct ORM/
    shell edit -- Model.save() does not call full_clean() on its own).
    """
    rules = {}
    errors = []
    for lineno, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            errors.append(f"line {lineno}: missing ':' -- expected 'ROUTE: County,County' (got {raw_line!r})")
            continue
        route_part, counties_part = line.split(":", 1)
        route = route_part.strip()
        counties = {c.strip() for c in counties_part.split(",") if c.strip()}
        if not route:
            errors.append(f"line {lineno}: no route given before ':' (got {raw_line!r})")
            continue
        if not counties:
            errors.append(f"line {lineno}: no counties listed for route {route!r} (got {raw_line!r})")
            continue
        rules.setdefault(route, set()).update(counties)
    return rules, errors


class RoadConditionsConfiguration(models.Model):
    """Singleton -- KDOT CARS API polling + filtering settings, matching
    the WeatherConfig/AmberAlertConfig singleton convention (see
    weather/models.py). Off by default, same reasoning as
    AmberAlertConfig: a fresh install shouldn't start polling an
    external government API or building RoadEvent rows before the
    operator has reviewed the coverage-area filters.

    Deliberately does NOT include a bounding-box or "geographic
    filtering mode" field -- the live API has no bbox/radius/lat-lon
    query support at all (verified 2026-08-04; only `eventClassifications`
    and an exact-match `routeDesignator` are real server-side filters).
    `counties` and `routes` below do the real work, matching fields the
    API actually returns/accepts rather than a speculative geometry
    feature with nothing behind it.

    Changing counties/routes/event_classifications/min_priority/
    max_event_age_days/lookahead_days here changes what counts as
    `RoadEvent.in_scope` on the NEXT complete (unnarrowed) sync -- see
    RoadEvent's docstring for the source_active/in_scope split. A
    config change alone never touches existing rows; nothing recomputes
    scope until a real sync_road_conditions run does it.
    """

    enabled = models.BooleanField(
        default=False,
        help_text=(
            "Master switch. When off, sync_road_conditions fetches nothing "
            "and writes nothing -- everything else on this page is inert "
            "while off. Off by default so a fresh install doesn't start "
            "pulling KDOT data before the coverage filters are reviewed."
        ),
    )

    # On-air station framing -- kept entirely separate from the actual
    # road-condition report body (see report.py's compose_report_script(),
    # which is the only place these two fields are ever read). Both are
    # plain TextFields, blank-allowed: an operator who wants no preamble
    # or no postamble at all just clears the field, and that piece is
    # simply omitted rather than leaving a stray blank sentence on air.
    # {announcer_name} is the one supported substitution token in either
    # field -- resolved from the SAME voice already selected for this
    # report (road_conditions/voice.py's resolve_voice()); there is
    # deliberately no separate announcer-name config field here, since
    # that voice metadata is already the single source of truth for the
    # name (currently "Claira Sky"/"Max Weatherly" -- the FULL on-air
    # name, same as weather's own scripts use for their own spoken
    # sign-off, e.g. wx_forecast.py's "I'm Claira Sky." -- not just the
    # short "Claira"/"Max" used for internal bookkeeping like ID3
    # titles; see voice.py's module docstring), and a second,
    # independently-editable copy of that name here would just be one
    # more way for the two to drift apart.
    report_preamble = models.TextField(
        blank=True,
        default="Now here's the current and upcoming road report for the Oak Grove Radio listening area.",
        help_text=(
            "Spoken immediately BEFORE the generated road-condition report "
            "body (or the no-current-events message), every time. Optional -- "
            "blank means no preamble at all. May include the literal token "
            "{announcer_name}, replaced with the full listener-facing name "
            "(e.g. 'Claira Sky') of whichever voice is currently selected "
            "for this report."
        ),
    )
    report_postamble = models.TextField(
        blank=True,
        default="For more information visit KanDrive dot gov. That's the Oak Grove Radio road report, I'm {announcer_name}.",
        help_text=(
            "Spoken immediately AFTER the generated road-condition report "
            "body (or the no-current-events message), every time. Optional -- "
            "blank means no postamble at all. Same {announcer_name} "
            "substitution support as report_preamble above."
        ),
    )

    # Item transition sound effect -- reuses the same "woosh" audio file
    # the Kansas News Service show ingest script (get_kns.py) already
    # plays between stories, spliced in between individual road-
    # condition items the same way (see report.py's
    # compose_report_segments() and synthesis.py's
    # _synthesize_with_transitions()). Off by default -- a fresh
    # install/upgrade shouldn't start doing extra per-item Kokoro calls
    # and ffmpeg concat work until an operator opts in. Never inserted
    # before the first item or after the last (i.e. never between the
    # preamble/postamble and the adjacent item, matching the KNS
    # script's own convention of never wooshing next to its bumper) --
    # and never possible at all with 0 or 1 items, since there's no
    # boundary to insert it at.
    transition_sound_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Insert a short transition sound effect between individual "
            "road-condition items when the report speaks more than one. "
            "Off by default. Requires transition_sound_path below to "
            "point at a real, readable audio file at generation time; if "
            "it doesn't, the report is generated WITHOUT the transition "
            "sound rather than failing the whole cycle."
        ),
    )
    transition_sound_path = models.CharField(
        max_length=1024, blank=True,
        default="/home/jreed/syndicated-ingest/kns/woosh.wav",
        help_text=(
            "Absolute filesystem path to the transition sound effect "
            "audio file (e.g. a short chime or sting). Only read when "
            "transition_sound_enabled above is on. If this path is "
            "blank, missing, or unreadable at generation time, the "
            "report is generated without the transition sound (a "
            "warning is printed) rather than failing -- if the file "
            "itself is present but corrupt/unplayable, ffmpeg will fail "
            "and the whole generation cycle is reported as failed, same "
            "as any other synthesis error."
        ),
    )

    api_base_url = models.URLField(
        max_length=200,
        default="https://kscars.kandrive.gov/carsapi_v1/api",
        help_text=(
            "CARS API root. The Kansas deployment is confirmed live at this "
            "URL as of 2026-08-04 (the ks.carsprogram.org console redirects "
            "here). Only change this if KDOT moves the endpoint."
        ),
    )
    request_timeout_seconds = models.PositiveIntegerField(
        default=20,
        help_text=(
            "Read timeout in seconds for each CARS API request. A short, "
            "fixed 5-second connect timeout is always applied in code on "
            "top of this -- not admin-editable, since a slow DNS/TCP "
            "handshake should fail fast regardless of how patient the "
            "operator is willing to be for the response body itself."
        ),
    )
    poll_cadence_minutes = models.PositiveIntegerField(
        default=15,
        help_text=(
            "How often sync_road_conditions actually runs -- ACTIVELY "
            "ENFORCED by the command itself (not just documentation): "
            "the systemd timer fires roughly every minute, and the "
            "command exits cleanly, writing nothing, whenever less than "
            "this many minutes have passed since the last attempt (see "
            "deploy/ for the proposed, not-yet-installed timer). A "
            "change here takes effect within about a minute, no timer "
            "edit or systemd reload needed -- same idiom as OGRemote's "
            "own poll_interval_minutes. 15 minutes by default: the live "
            "CARS API has no ETag/Last-Modified/conditional-request "
            "support and no pagination, so every poll re-downloads the "
            "COMPLETE current dataset (~8MB at reconnaissance time) -- "
            "at 5 minutes that's roughly 69GB/month; at 15 minutes "
            "roughly 23GB/month. This feed has no live crash/incident "
            "data (construction/closure/condition only -- see "
            "RoadEvent's docstring), so 15 minutes is a reasonable "
            "default; lower it (e.g. for winter road-condition "
            "operations) if that trade-off is worth it to you."
        ),
    )

    event_classifications = models.CharField(
        max_length=200,
        default=DEFAULT_EVENT_CLASSIFICATIONS,
        help_text=(
            "Comma-separated CARS API event classifications to request "
            "from the API's own eventClassifications query filter -- NOT "
            "the same thing as an individual event's headline_category/ "
            "headline_code (see RoadEvent). The live API recognizes "
            "exactly five values (confirmed via GET /eventClassifications): "
            "truckersReports, roadReports, winterDriving, "
            "weatherWarningsAreaEvents, constructionReports. There is no "
            "separate 'incidents' classification or crash/accident data "
            "in this feed at all -- KDOT's CARS API is construction/"
            "closure/condition-focused, not a crash feed."
        ),
    )
    counties = models.CharField(
        max_length=500, blank=True, default=DEFAULT_COUNTIES,
        help_text=(
            "Comma-separated Kansas county names (as KDOT's API spells "
            "them, e.g. 'Ottawa', 'Saline' -- not FIPS codes) to keep. "
            "An event is kept if ANY of its counties match. Blank means "
            "no county filtering -- every event matching "
            "event_classifications above is kept, statewide."
        ),
    )
    routes = models.CharField(
        max_length=300, blank=True, default="",
        help_text=(
            "Optional comma-separated exact route designators to further "
            "restrict to (applied client-side -- see note below). An "
            "event is kept if ANY of its locations' route matches ANY of "
            "these exactly. Verified live format: Kansas state highways "
            "are 'KS <number>' (e.g. 'KS 15', 'KS 18', 'KS 9') -- NOT "
            "'K-15'/'K-18'/'K-9'. US/Interstate routes use 'US 81', "
            "'I-70', 'US 24', etc. Blank means no route filtering. "
            "The API's own routeDesignator query param requires an exact "
            "full-string match with no OR support (confirmed live: "
            "'US 81' returns real hits, the bare substring '81' returns "
            "none) -- so this is applied client-side against every "
            "route on a fetched event instead of sent to the API, which "
            "lets multiple routes be configured at once."
        ),
    )
    additional_route_coverage = models.TextField(
        blank=True, default="",
        help_text=(
            "Optional exceptions to the Counties/Routes coverage above, one "
            "rule per line, in the form 'ROUTE: County,County' -- e.g. "
            "'US 81: Saline,Cloud'. Counties above define the normal "
            "coverage area; Routes above, if also set, further RESTRICTS "
            "Counties (an event must match both). This field instead ADDS "
            "coverage: an event is also kept if it matches the exact route "
            "on a line here AND is in one of that line's listed counties -- "
            "regardless of whether it would otherwise pass Counties/Routes "
            "above. Each line only ever adds that one route in those exact "
            "counties, never statewide. Blank means no additional coverage "
            "(current behavior, unchanged). All other filters (event "
            "classifications, minimum priority, event age/lookahead) still "
            "apply normally to anything matched here -- this field only "
            "widens geographic scope, nothing else. Malformed lines (no "
            "':', no route, or no counties listed) are rejected when this "
            "configuration is saved rather than silently broadening "
            "coverage. Known limitation: matching is EVENT-level, not "
            "per-segment -- the source API gives no route-to-county "
            "association for an event with more than one route (e.g. an "
            "event listing both US 81 and KS 9 with counties Ottawa and "
            "Cloud doesn't say which route is in which county), so a rule "
            "matches whenever its route and one of its counties both "
            "appear ANYWHERE on the event. Harmless for the vast majority "
            "of real events (each has exactly one route in practice; see "
            "services._matches_scope()'s docstring), but worth knowing for "
            "the rare event that genuinely spans multiple distinct routes."
        ),
    )

    min_priority = models.PositiveSmallIntegerField(
        default=1,
        help_text=(
            "Minimum CARS priority (1-10, as the API defines it) to "
            "keep. 1 keeps everything. Every live event has always "
            "carried a priority value in reconnaissance testing."
        ),
    )
    max_event_age_days = models.PositiveIntegerField(
        default=3,
        help_text=(
            "Skip creating/updating a RoadEvent whose end-time (if the "
            "source provided one) is already more than this many days "
            "in the past. Defensive only -- the API describes /events "
            "as 'active and future' events, so this should rarely "
            "trigger; it guards against a stale end-time on an "
            "otherwise-still-listed record rather than being load-"
            "bearing for normal deactivation (that's source-presence-"
            "based -- see RoadEvent.source_active)."
        ),
    )
    lookahead_days = models.PositiveIntegerField(
        default=30,
        help_text=(
            "Skip creating/updating a RoadEvent whose start-time is "
            "more than this many days in the future. Keeps long-lead-"
            "time planned construction from filling the table months "
            "before it's ever relevant; raise it for earlier visibility "
            "into far-future projects."
        ),
    )
    stale_data_threshold_minutes = models.PositiveIntegerField(
        default=30,
        help_text=(
            "If the last successful fetch is older than this many "
            "minutes, the admin shows a stale-data warning. Purely "
            "informational -- does not stop anything from running."
        ),
    )

    debug_logging = models.BooleanField(
        default=False,
        help_text="Log full per-event sync decisions (kept/skipped/why) at INFO level. Off keeps routine polling quiet in the journal.",
    )

    last_fetch_attempted_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_fetch_succeeded_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.TextField(blank=True, editable=False)
    last_record_count = models.PositiveIntegerField(null=True, blank=True, editable=False)

    # Report-generation change-detection state -- see report.py's
    # compute_report_fingerprint() for what's hashed and why (the
    # final composed script, resolved voice identity, and actual
    # transition-sound behavior -- deliberately NOT just RoadEvent.
    # payload_checksum, which can't capture a preamble edit, a voice
    # model change, or an event's own wording changing purely because
    # wall-clock time crossed its start_time). Both fields represent
    # the LAST generation cycle that completed successfully start to
    # finish (Kokoro synthesis, FLAC installation, Track update, AND
    # waveform analysis) -- generate_road_condition_audio.py only ever
    # writes these together, via a single targeted
    # save(update_fields=[...]), after that entire cycle has already
    # succeeded. Never written by --dry-run, --text-only, --event-id,
    # a skipped/unchanged cycle, a stale/failed/disabled retirement,
    # or any failed cycle -- see that command's own comment at its
    # write site.
    last_report_fingerprint = models.CharField(
        max_length=64, blank=True, editable=False,
        help_text=(
            "SHA-256 hex digest of the effective on-air artifact from the last "
            "generate_road_condition_audio cycle that completed synthesis, FLAC "
            "installation, Track update, AND waveform analysis -- see report.py's "
            "compute_report_fingerprint(). Read-only. A later run compares its own "
            "freshly-computed fingerprint against this (plus a live health check on "
            "the existing FLAC/Track/text file) to decide whether the report is "
            "genuinely unchanged and synthesis can be skipped."
        ),
    )
    last_report_generated_at = models.DateTimeField(
        null=True, blank=True, editable=False,
        help_text=(
            "When last_report_fingerprint above was last set -- i.e. the last time "
            "generate_road_condition_audio actually completed a full synthesis "
            "cycle, not merely the last time the command RAN (a skipped-because-"
            "unchanged run, or --dry-run/--text-only/--event-id, never updates this)."
        ),
    )

    class Meta:
        verbose_name = "Road Conditions Configuration"
        verbose_name_plural = "Road Conditions Configuration"

    def __str__(self):
        return f"Road Conditions Configuration ({'enabled' if self.enabled else 'disabled'})"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def clean(self):
        """Rejects a malformed additional_route_coverage at save time
        (called by ModelForm._post_clean -- i.e. any admin save -- via
        Model.full_clean(); NOT called by a plain .save(), same as
        Django generally). Per-line errors are attached to the field
        itself so the admin shows them right where the operator needs
        to fix them, not as a generic non-field error. See
        parse_additional_route_coverage() for exactly what counts as
        malformed (e.g. bare 'US 81' with no ':', or ': Saline' with
        no route)."""
        super().clean()
        _rules, errors = parse_additional_route_coverage(self.additional_route_coverage)
        if errors:
            raise ValidationError({"additional_route_coverage": errors})

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def event_classifications_list(self):
        return [c.strip() for c in self.event_classifications.split(",") if c.strip()]

    @property
    def counties_set(self):
        return {c.strip() for c in self.counties.split(",") if c.strip()}

    @property
    def routes_set(self):
        return {r.strip() for r in self.routes.split(",") if r.strip()}

    @property
    def additional_route_coverage_rules(self):
        """{route: {county, ...}} parsed from additional_route_coverage --
        see parse_additional_route_coverage()'s docstring. Malformed
        lines are silently dropped here (never contribute a rule,
        never raise) -- clean() below is what stops malformed text from
        being saved via the admin in the first place; this property
        stays safe on its own regardless, since Model.save() alone
        does not enforce that."""
        rules, _errors = parse_additional_route_coverage(self.additional_route_coverage)
        return rules

    @property
    def is_stale(self):
        """True if the last successful fetch is older than
        stale_data_threshold_minutes, or there has never been one.
        Purely informational -- see the field's help text."""
        from django.utils import timezone
        if self.last_fetch_succeeded_at is None:
            return True
        age = timezone.now() - self.last_fetch_succeeded_at
        return age.total_seconds() > self.stale_data_threshold_minutes * 60


class RoadEvent(models.Model):
    """A normalized local copy of one KDOT CARS event (source: GET
    /events, https://kscars.kandrive.gov/carsapi_v1/api -- the IEEE
    1512-style SITUATION/CARSEvent schema). Fields here are the ones
    verified (2026-08-04 reconnaissance) to be reliably present and
    useful across live events -- NOT a mechanical transcription of the
    full nested schema, most of which (Reference/Contact/Comments/
    ConfidenceLevel/DetectionMethod/etc.) is either always empty in
    this deployment or too technical to be broadcast- or filter-
    relevant. The full source record is kept (sanitized -- see
    raw_payload below) for anything not promoted to its own column.

    `description` is the standout field -- KDOT's own human-written
    plain-English summary (route, direction, location, cause, and
    duration wording), present on 231/231 live events at reconnaissance
    time, e.g. "US 81 in both directions: Construction work. Between
    KS 18 and KS 41 (1 mile south of the Minneapolis area)... Until
    December 18, 2026 at about 11:59PM CDT." This is the field future
    TTS report generation should build from; `full-report-texts` and
    `estimated-re-opening-time`, which the schema suggests might carry
    similar information, were empty on every single live event and are
    not stored as their own columns as a result.

    There is no crash/incident data in this feed at all (verified: zero
    of 231 live events, and no such event classification exists) -- this
    model does not attempt to represent one.

    ## source_active vs in_scope

    Two independent booleans, deliberately NOT conflated into one
    `active` flag (an earlier revision of this model made that mistake):

      * `source_active` -- is this event-id still present in KDOT's
        complete /events feed? Set False only by a COMPLETE, unnarrowed,
        non-failed sync that doesn't see it anymore (see
        services.sync_events). This is a fact about the SOURCE, entirely
        independent of this station's own configured coverage.
      * `in_scope` -- does this event currently match
        RoadConditionsConfiguration's counties/routes/event_classifications/
        min_priority/max_event_age_days/lookahead_days filters? This is a
        fact about THIS STATION's current configuration, entirely
        independent of whether KDOT still lists the event at all.

    A row that's `source_active=True, in_scope=False` means: KDOT still
    has this event live, but it stopped matching this station's
    configured coverage (e.g. an operator removed a county) -- it should
    not be treated as currently relevant, but it is NOT lying about the
    source disappearing, and it does not need "un-scoping" ever undone
    by hand; if coverage is widened again, the next complete sync flips
    it back to in_scope=True on its own.

    "Currently relevant" for any future consumer (admin display, a
    future TTS pipeline) is `source_active AND in_scope`, not either
    flag alone -- see RoadEvent.is_current.
    """

    external_id = models.CharField(
        max_length=64, unique=True, db_index=True,
        help_text="The source 'event-id', e.g. 'CARS5-11556'. Stable across revisions (update-number increments; event-id does not change).",
    )

    headline_category = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="Source 'headline.category' -- this event's OWN taxonomy, e.g. 'roadwork', 'closure', 'restriction', 'warning', 'device-status', 'mobile-situation', 'special-event'. Distinct from RoadConditionsConfiguration.event_classifications, which is the API's separate QUERY-level filter (constructionReports/roadReports/etc.) -- do not confuse the two.",
    )
    headline_code = models.CharField(
        max_length=128, blank=True,
        help_text="Source 'headline.code' -- this event's own coded subtype, e.g. 'construction work', 'lane is closed', 'width restriction'.",
    )
    cause_categories = models.JSONField(
        default=list, blank=True,
        help_text=(
            "Every distinct category from this event's own details[].descriptions[] "
            "entries marked is-cause=true (sorted), e.g. [\"closure\", \"roadwork\"] -- "
            "see services.extract_cause_categories(). Distinct from headline_category "
            "above: KDOT's own top-level headline is sometimes the EFFECT rather than "
            "the cause (confirmed on real live events: a headline of 'automated traffic "
            "signals'/'device-status' with 'closure'/'roadwork' recorded here as the "
            "real is-cause reason). report.py's select_events()/_severity_tier() use "
            "this so a genuine lane closure isn't dropped or under-prioritized just "
            "because of what KDOT chose as the record's top-level headline. Empty for "
            "the common case where the source provided no is-cause PhraseDescription "
            "entries (i.e. the top-level headline already IS the real reason)."
        ),
    )
    description = models.TextField(
        blank=True,
        help_text="Source top-level 'description' -- KDOT's own plain-English summary. The primary field for listener-facing text.",
    )
    priority = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="Source 'priority', 1 (low) - 10 (high) per the API's own schema.",
    )
    source_status = models.CharField(
        max_length=32, blank=True,
        help_text="Raw source 'status' string, e.g. 'UPDATED'. Observed constant across the live dataset -- NOT a reliable active/inactive signal; see `source_active` below.",
    )

    counties = models.JSONField(default=list, blank=True, help_text='Source "counties" array, e.g. ["Ottawa"]. Already complete -- never reduced to a single value.')
    districts = models.JSONField(default=list, blank=True, help_text='Source "districts" array, e.g. ["DISTRICT 2"]. Already complete -- never reduced to a single value.')

    primary_route = models.CharField(
        max_length=128, blank=True, db_index=True,
        help_text="Convenience field for admin display/sorting: the route from the FIRST detail location that has one, e.g. 'US 81', 'KS 15', 'I-70'. For a multi-route event, see `routes` below for the complete set -- this field alone does not represent the whole event.",
    )
    primary_direction = models.CharField(
        max_length=32, blank=True,
        help_text="Convenience field: the link-direction from that same first location, e.g. 'BOTH_DIRECTIONS', 'NORTHBOUND'. See `locations` below for the complete per-location detail.",
    )
    routes = models.JSONField(
        default=list, blank=True,
        help_text="Every distinct route designator across every detail location on this event (sorted), not just the first -- e.g. a two-segment event might list [\"KS 4\", \"KS 140\"]. Used for route-filtering (RoadConditionsConfiguration.routes) so a multi-segment event isn't missed just because its first location isn't the matching one.",
    )
    locations = models.JSONField(
        default=list, blank=True,
        help_text="Every distinct detail location on this event, normalized (location_type, route_designator, direction, latitude, longitude) -- preserves multi-location events completely; primary_route/primary_direction/latitude/longitude above are convenience copies of the first entry here, not a substitute for it.",
    )

    latitude = models.FloatField(null=True, blank=True, help_text="Convenience field: latitude of the first location with coordinates. See `locations` for the complete set.")
    longitude = models.FloatField(null=True, blank=True, help_text="Convenience field: longitude of the first location with coordinates. See `locations` for the complete set.")
    geometry = models.JSONField(null=True, blank=True, help_text="Raw source GeoJSON 'geometry' (Point or LineString in every live event seen) -- stored verbatim, never truncated (see raw_payload's sanitization note -- geometry is explicitly exempted from it).")

    start_time = models.DateTimeField(null=True, blank=True, help_text="Earliest detail start-time, if the source provided one.")
    end_time = models.DateTimeField(null=True, blank=True, help_text="Latest detail end-time, if the source provided one.")
    source_update_time = models.DateTimeField(null=True, blank=True, help_text="Source top-level update-time -- when KDOT last revised this event.")
    source_next_update_time = models.DateTimeField(null=True, blank=True)
    source_update_expiry_time = models.DateTimeField(
        null=True, blank=True,
        help_text="Source update-expiry-time, stored as-is. Observed to sometimes be a generic far-future placeholder rather than a real expiry -- not used to drive deactivation.",
    )
    update_number = models.PositiveIntegerField(null=True, blank=True, help_text="Source revision counter -- increments on each KDOT edit, event-id stays the same.")

    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(help_text="Updated every sync run where this event's id was present in a COMPLETE fetch (whether or not it parsed or matched current filters -- see services.sync_events).")
    last_changed_at = models.DateTimeField(null=True, blank=True, help_text="Updated only when payload_checksum changed -- i.e. meaningful source content changed, not just re-seen unchanged or re-scoped.")

    source_active = models.BooleanField(
        default=True, db_index=True,
        help_text="False once this event-id is absent from a COMPLETE, unnarrowed sync's fetch. Never flipped false after a failed/partial/narrowed sync -- see services.sync_events. Independent of `in_scope` -- see the model docstring.",
    )
    in_scope = models.BooleanField(
        default=True, db_index=True,
        help_text="True if this event currently matches RoadConditionsConfiguration's coverage filters (as of the last COMPLETE, unnarrowed sync). False if the operator's configured coverage changed and this event no longer matches -- independent of whether KDOT still lists it; see the model docstring.",
    )
    deactivated_at = models.DateTimeField(null=True, blank=True, help_text="When source_active last flipped to False. Unrelated to in_scope changes.")

    raw_payload = models.JSONField(
        help_text=(
            "The source CARSEvent record, sanitized for storage: any "
            "list longer than services._MAX_LIST_ITEMS_IN_STORED_PAYLOAD "
            "is truncated (with a marker noting how many items were "
            "omitted) EXCEPT the top-level `geometry` object, which is "
            "always stored verbatim. This exists because a real live "
            "event (a K-15 closure) carried a 1,872-point turn-by-turn "
            "detour route under details[].descriptions[].locations-on-"
            "detour -- undocumented in the API's own Swagger spec, not "
            "mapped to any RoadEvent field, and ~890KB unsanitized for "
            "that one record alone. payload_checksum below is computed "
            "from the COMPLETE, unsanitized source record, before this "
            "truncation -- so a real content change inside a truncated "
            "array still changes the checksum and triggers an update, "
            "even though the stored copy here won't show the change."
        ),
    )
    payload_checksum = models.CharField(max_length=64, db_index=True, help_text="sha256 of the canonicalized, UNSANITIZED raw source payload -- lets sync detect 'unchanged' without a deep field-by-field diff, and still detects changes inside content that gets truncated in the stored raw_payload above.")

    class Meta:
        verbose_name = "Road Event"
        ordering = ["-last_seen_at"]
        indexes = [
            models.Index(fields=["source_active", "in_scope", "-last_seen_at"]),
            models.Index(fields=["headline_category", "source_active"]),
        ]

    def __str__(self):
        label = self.primary_route or self.headline_code or self.headline_category
        return f"{label or 'Road Event'} ({self.external_id})"

    @property
    def is_current(self):
        """"Currently relevant" -- both still present in KDOT's feed AND
        matching this station's configured coverage. See the model
        docstring for why these are two separate flags."""
        return self.source_active and self.in_scope


class RoadConditionsSyncRun(models.Model):
    """One row per REAL (non-dry-run) sync_road_conditions invocation --
    the job-run model for this app (as distinct from
    RoadConditionsConfiguration, which only keeps the latest attempt/
    success/error for the at-a-glance admin view). Mirrors the counts
    the management command itself prints, so the same numbers are
    queryable/auditable after the fact.

    A --dry-run NEVER creates a row here (or writes anything else to
    the database at all) -- see services.sync_events's dry_run branch
    and tests/test_sync.py's DryRunWritesNothingTests."""

    OUTCOME_CHOICES = [
        ("success", "Success"),
        ("partial", "Partial (some records failed to parse)"),
        ("failed", "Failed (could not complete the fetch)"),
    ]

    # NOT auto_now_add -- this must reflect when the fetch attempt
    # actually started, which services.sync_events records explicitly.
    # A slow or timed-out fetch could take many seconds; auto_now_add
    # would instead stamp the moment this row is finally written
    # (after the attempt already finished), which is the wrong value
    # for a field whose whole purpose is measuring when the attempt began.
    started_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, default="failed")

    fetched_count = models.PositiveIntegerField(default=0)
    relevant_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    unchanged_count = models.PositiveIntegerField(default=0)
    deactivated_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)

    latency_ms = models.PositiveIntegerField(null=True, blank=True, help_text="CARS API fetch latency in milliseconds.")
    error_message = models.TextField(blank=True, help_text="Set when outcome is 'failed' -- the exception/HTTP error that stopped the whole run.")

    class Meta:
        verbose_name = "Road Conditions Sync Run"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.started_at:%Y-%m-%d %H:%M} ({self.outcome}, fetched={self.fetched_count})"
