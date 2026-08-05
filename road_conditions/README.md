# Road Conditions

Ingests Kansas DOT road/construction events from the state's CARS API
and normalizes them into `RoadEvent` rows for later use by a spoken
road-conditions report (not yet built — see "What this app does NOT do
yet" below). Sits alongside Weather Configuration and AMBER Alert
Configuration in Django admin, following the same singleton-config
pattern as both.

## Status

Off by default (`RoadConditionsConfiguration.enabled = False`). Review
the coverage-area settings in Django admin (**Road Conditions > Road
Conditions Configuration**) before turning it on. Nothing polls the
CARS API or writes a `RoadEvent` row while it's off.

## The API

Kansas DOT's CARS API: `https://kscars.kandrive.gov/carsapi_v1/api`
(the `ks.carsprogram.org` console redirects here). Verified working
with **no credentials** as of 2026-08-04 reconnaissance — the Swagger
spec declares HTTP Basic on every endpoint, but every endpoint this
app uses returns real data anonymously. No credential settings are
wired in from Django settings in this version as a result (removed
during a 2026-08-04 review pass, having been added speculatively
without a concrete need); `road_conditions.api.CarsApiClient` still
accepts `username`/`password` directly and is tested doing so
(`tests/test_api_client.py`) if a future round needs to add real
settings back once KDOT actually requires auth. Never log either
value if that happens.

Key facts worth knowing before touching this code (all verified live,
not assumed from the Swagger doc — see
`scratchpad/road_conditions/` for the full reconnaissance notes, not
committed):

- **No pagination.** `GET /events` returns the entire current dataset
  (~230 events, ~8MB) in one response, gzip-supported.
- **No HTTP caching support.** Checked live (2026-08-04): `/events`
  returns no `ETag`, `Last-Modified`, or `Cache-Control` header, and
  ignores `If-None-Match`/`If-Modified-Since` entirely (always a plain
  200, never 304). There is no conditional-request/304 workflow to
  build here — not a gap in this project's implementation.
- **Only two real server-side filters**: `eventClassifications`
  (comma list; real values: `truckersReports`, `roadReports`,
  `winterDriving`, `weatherWarningsAreaEvents`, `constructionReports`)
  and `routeDesignator` (exact full-string match only — no substring,
  no OR). County/city/bbox/lat-lon/radius/status/severity/"changed-
  since" filters do **not** exist server-side; `RoadConditionsConfiguration.counties`/
  `.routes` are applied client-side after fetch.
- **Kansas state highways are `"KS <number>"`, never `"K-<number>"`**
  (e.g. `"KS 15"`, not `"K-15"`) — a real, verified discrepancy from
  the commonly-used shorthand.
- **`description`** (top-level, plain string) is KDOT's own human-
  written summary and the field future TTS report generation should
  build from — present on every live event at reconnaissance time.
  `full-report-texts` and `estimated-re-opening-time`, which the
  schema suggests might carry similar text, were empty on every
  single live event.
- **`status` is always `"UPDATED"`** in this deployment — not a
  reliable active/inactive signal. `RoadEvent.source_active` is driven
  by *presence in a complete fetch*, not any status field (see
  "Source presence vs. configured coverage" below and
  `services.sync_events`).
- **No crash/incident data at all.** This feed is construction/
  closure/restriction/condition-focused; there is no incident/crash
  classification or content in it.
- `event-id` is the stable identifier. Events are commonly revised in
  place (same `event-id`, `update-number` increments) — confirmed
  live, not theoretical.
- **A real event can be much larger than "typical."** A K-15 closure
  captured live carried a 1,872-point turn-by-turn detour route under
  `details[].descriptions[].locations-on-detour` — undocumented in the
  API's own Swagger spec, ~890KB unsanitized for that one field alone.
  See "Raw payload storage" below.

## Terminology: two different things are both called "classification"

Deliberately kept separate throughout this app -- do not conflate them:

- **`RoadConditionsConfiguration.event_classifications`** (and the
  `--event-type` CLI flag, and `sync_events(event_classifications_filter=...)`)
  is the CARS **API's own query-level filter**: `constructionReports`,
  `roadReports`, `winterDriving`, `weatherWarningsAreaEvents`,
  `truckersReports`. Which of these to *request from the API*.
- **`RoadEvent.headline_category`/`.headline_code`** is a single
  event's **own taxonomy** (source `headline.category`/`headline.code`),
  e.g. `"roadwork"`/`"construction work"`, `"closure"`/`"lane is closed"`.
  Which *kind of event this particular record is*.

An event can match `constructionReports` at the API level while its own
`headline_category` is `"closure"` -- these are genuinely independent
axes, not two names for the same thing.

## Source presence vs. configured coverage

`RoadEvent` has two independent boolean flags -- an earlier revision of
this model conflated them into a single `active` field, which is wrong:
a station narrowing its own configured coverage is not the same fact
as KDOT dropping an event from its feed, and treating them as the same
flag makes it impossible to tell which one happened.

- **`source_active`** -- is this event-id still present in KDOT's
  *complete* `/events` feed? Set False only by a complete, unnarrowed,
  non-failed sync that doesn't see it anymore.
- **`in_scope`** -- does this event currently match this station's
  *configured* coverage (`counties`/`routes`/`event_classifications`/
  `min_priority`/`max_event_age_days`/`lookahead_days`)? Independent of
  whether KDOT still lists the event at all.

"Currently relevant" (`RoadEvent.is_current`) is `source_active AND
in_scope`, not either flag alone. If an operator removes a county from
coverage, the next complete sync flips affected rows to
`in_scope=False` -- they are **not deleted**, and if coverage is
widened again later, the next complete sync flips them back to
`in_scope=True` automatically, no manual intervention needed.

`RoadEvent` is fully read-only in the admin (no manual override
action) -- an earlier revision had a `mark_inactive` bulk action;
removed with no demonstrated operational need, since forcing
`source_active`/`in_scope` off by hand would just get silently
reverted (or briefly disagree with reality) the next time
`sync_road_conditions` runs and sees the event again.

## Raw payload storage

`RoadEvent.raw_payload` is the source record, **sanitized** for
storage: any list longer than `services._MAX_LIST_ITEMS_IN_STORED_PAYLOAD`
(25) is truncated to its first 25 items plus a marker noting how many
were omitted -- this exists specifically because of the 1,872-point
detour route mentioned above, which would otherwise store ~890KB for
one event. The top-level `geometry` object is explicitly **exempted**
from truncation -- a LineString's coordinate precision (a real event
carries 369 points) is the primary, wanted content, not clutter.

`RoadEvent.payload_checksum` is computed from the **complete,
unsanitized** source record, before truncation -- so a real content
change inside a field that gets truncated in storage still changes the
checksum and correctly triggers an "updated" write, even though the
stored `raw_payload` itself won't show that specific change.

## Multi-location events

`RoadEvent.primary_route`/`.primary_direction`/`.latitude`/`.longitude`
are convenience fields for admin display -- they reflect only the
FIRST detail location on an event. `RoadEvent.routes` (every distinct
route across all locations) and `.locations` (every distinct location,
fully normalized) preserve the complete event; `.counties`/`.districts`
were always complete arrays and were never reduced to a single value.
(No genuinely multi-location event happened to appear in the live
dataset at reconnaissance time -- every one of 231 live events had
exactly one detail/one location -- but the schema clearly supports
more, and `tests/test_normalize.py`'s `NormalizeMultiLocationEventTests`
covers it with a constructed, schema-faithful fixture.)

## Files

```
road_conditions/
    api.py          CarsApiClient -- HTTP transport only, no Django ORM, mockable
    services.py      normalize_event() + sync_events() -- the idempotent upsert/deactivate/rescope core
    models.py         RoadConditionsConfiguration (singleton), RoadEvent, RoadConditionsSyncRun
    admin.py           read-only RoadEvent/RoadConditionsSyncRun admin + singleton config admin
    management/commands/sync_road_conditions.py
    tests/              fixture-driven, no live network requests
```

## Running a sync manually

```bash
python manage.py sync_road_conditions --dry-run
```

Reports what would be created/updated/deactivated/rescoped without
writing **anything** to the database (not a RoadEvent change, not a
RoadConditionsConfiguration field, not a RoadConditionsSyncRun row, not
a monitoring SystemEvent) -- safe to run at any time, including while
disabled (nothing runs while disabled unless you also pass `--force-full`).

```bash
python manage.py sync_road_conditions
```

Prints a summary:

```
Fetched: 142
Relevant: 18
Created: 3
Updated: 4
Unchanged: 11
Deactivated: 1
Errors: 0
```

("Updated" also covers a row whose `in_scope` changed due to a
configured-coverage edit, not just source content changes.)

Exits with a nonzero status only on a **total** fetch failure (network/
HTTP/auth/schema problem) — a partial per-record parse failure is
reported as `Errors: N` with a distinct "Partial sync" warning line,
not treated as a hard failure. A record that fails to parse on one run
does NOT get its event deactivated -- the complete source id set is
collected directly from the raw fetch, before any record is
normalized, specifically so a parse failure can't be mistaken for the
event disappearing from KDOT.

**Identity-incomplete responses**: if any raw record in the response is
missing an `event-id`, has an empty/wrong-typed one, or isn't a JSON
object at all, the run is marked partial (that record can't be
imported) **and deactivation is disabled for the entire run** -- an
unidentifiable record could correspond to any existing `RoadEvent` we'd
otherwise conclude is gone, so the whole id set can't be trusted as
complete that cycle. Every other, identifiable record is still
imported/updated/rescoped normally. A **duplicate** `event-id` within
one response is resolved deterministically (first occurrence wins,
every later duplicate logged and counted as an error) rather than
silently processed twice.

Useful flags: `--dry-run`, `--force-full` (run even while
`RoadConditionsConfiguration.enabled` is off — no incremental mode
exists to force, since the API always returns its complete current
data), `--verbose`, `--event-type CLASSIFICATION` (repeatable),
`--county NAME`, `--limit N`.

**Important**: `--event-type`/`--county`/`--limit` mark a run
"narrowed" and print a warning saying so — a narrowed run never
deactivates anything and never changes any row's `in_scope`, because it
deliberately only looked at part of the real feed or used a temporary
filter override, and neither "not seen this way in this one run" nor
"didn't match this run's override" means "gone from KDOT" or "out of
the station's real configured coverage." Only a plain
`sync_road_conditions` (or `--force-full`) run using the full
configured scope can deactivate or rescope `RoadEvent` rows.

## Scheduling (proposed, NOT installed)

`deploy/isadoraair-sync-road-conditions.service` and `.timer` exist as
a proposal, guarded by the same Postgres-advisory-lock overlap-
protection idiom `generate_dedication_intros` already uses (an
overlapping firing just exits immediately). They are **not** rendered,
installed, or enabled by this change — do that deliberately once
you've reviewed the coverage-area settings and run a few manual
`--dry-run`s.

**Cadence design (Option B, matching this project's own OGRemote
poller idiom)**: the timer itself fires every ~1 minute, but
`sync_road_conditions` checks `RoadConditionsConfiguration.poll_cadence_minutes`
against `last_fetch_attempted_at` and exits cleanly -- writing nothing,
logging no run -- whenever a sync isn't due yet. `--force-full` bypasses
this (and the disabled check) for manual verification. This is
deliberate, not incidental: the live CARS API has **no pagination and
no ETag/Last-Modified/conditional-request support at all** (checked
live, 2026-08-04) -- every actual sync re-downloads the complete
current dataset (~8MB at reconnaissance time), so the cadence
genuinely controls bandwidth, not just "freshness":

| Cadence | Per hour | Per day | Per 30-day month |
|---|---|---|---|
| 5 min | ~96MB | ~2.3GB | ~69GB |
| 15 min (default) | ~32MB | ~768MB | ~23GB |

15 minutes is the default because this feed carries no live crash/
incident data (construction/closure/condition only), so sub-5-minute
freshness isn't buying much; lower `poll_cadence_minutes` in the admin
(no code/timer change needed, takes effect within about a minute) if a
shorter interval is worth the bandwidth trade-off -- e.g. for winter
road-condition operations.

```bash
# Render @@ISA_USER@@/@@ISA_ROOT@@ per deploy/README.md's convention, then:
sudo cp deploy/isadoraair-sync-road-conditions.service deploy/isadoraair-sync-road-conditions.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now isadoraair-sync-road-conditions.timer
```

## What this app does NOT do yet

By design, per this round's scope:

- **No spoken/on-air announcements.** `RoadEvent.description` is the
  field a future TTS pipeline should read, but nothing synthesizes or
  schedules speech from it yet.
- **No `PlaylistLog`/engine integration.** Nothing here touches
  `library/services/engine.py`, the GLib main loop, or any real-time
  audio path — this app only ever talks to the CARS API and the
  database, from a management command invoked by a systemd timer,
  same isolation as `generate_dedication_intros`.
- **No bounding-box/coordinate-radius filtering** — the live API has
  no such server-side support, and county/route filtering already
  covers "administrator-configurable coverage" without inventing a
  feature nothing backs.

Full API reconnaissance notes (the live Swagger spec, sample captures,
per-endpoint probe results) live in `scratchpad/road_conditions/` —
not committed, but regenerable by re-running the same discovery
requests documented there if the API ever changes shape.
