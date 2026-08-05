"""Normalization (CARSEvent dict -> RoadEvent field dict) and the
idempotent sync (fetch -> filter -> upsert/deactivate) that
sync_road_conditions calls. Pure Python + Django ORM -- no GLib, no
audio-engine imports, nothing here runs on the playback thread. See
api.py for the actual HTTP client this module drives.
"""
import hashlib
import json
import logging
import time
from datetime import timezone as dt_timezone
from datetime import datetime

from django.db import transaction
from django.utils import timezone as dj_timezone

from monitoring.models import emit_event

from .api import CarsApiClient, CarsApiError
from .models import RoadConditionsSyncRun, RoadEvent

logger = logging.getLogger(__name__)

# Cap on how many per-record parse-error samples ride along in one
# summary emit_event -- keeps a bad batch from producing a huge detail
# blob while still giving the operator enough to diagnose from.
_MAX_ERROR_SAMPLES = 5

# Any list longer than this, anywhere in a source record EXCEPT the
# top-level `geometry` object, is truncated before being written to
# RoadEvent.raw_payload -- see sanitize_payload_for_storage()'s
# docstring for why (a real live event's turn-by-turn detour route
# carried 1,872 points, ~890KB for that one field alone). Chosen to
# comfortably fit every other real array shape seen during
# reconnaissance (locations, descriptions, counties, districts are all
# single digits) while still catching genuinely pathological data.
_MAX_LIST_ITEMS_IN_STORED_PAYLOAD = 25


class EventParseError(Exception):
    """Raised by normalize_event() only when a record can't be
    identified at all (currently: missing/invalid 'event-id'). Every
    other field degrades to blank/None rather than raising -- an
    unrecognized enum value, a missing nested object, or an
    unexpected type anywhere else in the payload is preserved as best
    it can be or simply left blank, never a reason to discard the
    whole record. See normalize_event()'s docstring."""


def build_client(config):
    """No credentials are wired in from settings in this version --
    anonymous access was verified working for every endpoint this app
    uses (2026-08-04), and CarsApiClient remains architecturally able
    to accept username/password directly (and is tested doing so --
    see tests/test_api_client.py) if a future round needs to add that
    back once there's a concrete reason to."""
    return CarsApiClient(
        base_url=config.api_base_url,
        timeout_seconds=config.request_timeout_seconds,
    )


def compute_checksum(raw_payload):
    """Computed from the COMPLETE, unsanitized source record -- called
    BEFORE sanitize_payload_for_storage() truncates anything, so a real
    content change inside a truncated array still changes the checksum
    and triggers an update, even though the stored raw_payload won't
    show the change itself."""
    canonical = json.dumps(raw_payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _truncate_large_lists(value, max_items=_MAX_LIST_ITEMS_IN_STORED_PAYLOAD):
    if isinstance(value, dict):
        return {k: _truncate_large_lists(v, max_items) for k, v in value.items()}
    if isinstance(value, list):
        truncated = [_truncate_large_lists(v, max_items) for v in value[:max_items]]
        if len(value) > max_items:
            truncated.append({
                "_truncated": True,
                "_original_length": len(value),
                "_note": f"{len(value) - max_items} more item(s) omitted for local storage -- see the live CARS API for the complete list.",
            })
        return truncated
    return value


def sanitize_payload_for_storage(raw):
    """Deep copy of `raw` with any list longer than
    _MAX_LIST_ITEMS_IN_STORED_PAYLOAD truncated -- protects against a
    pathological nested array (see the module-level constant's
    comment) inflating RoadEvent.raw_payload storage unboundedly for
    data this project doesn't even map to a field.

    `geometry` is exempted at the top level -- deliberately NEVER
    truncated, since a LineString's coordinate precision IS the
    primary, wanted content (a real event carries 369 points; that's
    normal geographic detail, not clutter) and must never be silently
    degraded. Nothing else in the real schema nests a second
    `geometry` key, so a top-level-only exemption is sufficient.

    Does not affect payload_checksum, which is always computed from
    the untouched `raw` dict beforehand -- see compute_checksum()."""
    if not isinstance(raw, dict):
        return raw
    return {k: (v if k == "geometry" else _truncate_large_lists(v)) for k, v in raw.items()}


def _safe_str(value):
    return value if isinstance(value, str) else ""


def _safe_int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_zoned_datetime(zdt):
    """The source's ZonedDateTime shape is {"time": <epoch ms>,
    "utcOffset": ..., "timeZoneId": ...}. `time` is the absolute
    instant (epoch milliseconds) -- utcOffset/timeZoneId are metadata
    about which zone KDOT's editor was in when authoring the record,
    NOT an adjustment to apply on top of `time`. Verified against live
    data: interpreting `time` directly as a UTC epoch lines up exactly
    with the plain-English dates embedded in that same event's own
    `description` text (e.g. "Until December 18, 2026 at about
    11:59PM CDT" matches `time` converted straight to UTC and then
    displayed in America/Chicago -- not `time` shifted by `utcOffset`
    first). This function therefore ignores utcOffset/timeZoneId
    entirely for computing the instant, and never attaches Django's
    current default timezone (or any other timezone) to an offset-less
    value -- the epoch millisecond value is already a complete,
    unambiguous instant on its own; there is no "offset-less value"
    case to handle. Returns an aware UTC datetime, or None for
    anything malformed (missing/wrong-typed `time`, non-dict input)."""
    if not isinstance(zdt, dict):
        return None
    ms = zdt.get("time")
    if not isinstance(ms, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=dt_timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _iter_details(raw):
    details = raw.get("details")
    return details if isinstance(details, list) else []


def _iter_locations(raw):
    for detail in _iter_details(raw):
        if not isinstance(detail, dict):
            continue
        locations = detail.get("locations")
        if not isinstance(locations, list):
            continue
        for loc in locations:
            if isinstance(loc, dict):
                yield loc


def _location_geo(loc):
    """A location's own lat/lon, if it has one directly (GeoLocation)
    or via its primary-location (LinkLocation). Returns (lat, lon) or
    (None, None)."""
    geo = None
    if loc.get("location-type") == "GeoLocation":
        geo = loc
    elif loc.get("location-type") == "LinkLocation":
        primary = loc.get("primary-location")
        if isinstance(primary, dict):
            geo = primary.get("geo-location")
    if isinstance(geo, dict):
        lat, lon = geo.get("latitude"), geo.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return float(lat), float(lon)
    return None, None


def _extract_all_locations(raw):
    """Every distinct detail location on this event, normalized --
    preserves multi-route/multi-direction/multi-location events
    completely, unlike primary_route/primary_direction/latitude/
    longitude (which only ever reflect the FIRST location). Used to
    populate RoadEvent.locations directly."""
    out = []
    for loc in _iter_locations(raw):
        lat, lon = _location_geo(loc)
        out.append({
            "location_type": _safe_str(loc.get("location-type")),
            "route_designator": _safe_str(loc.get("route-designator")),
            "direction": _safe_str(loc.get("link-direction")),
            "latitude": lat,
            "longitude": lon,
        })
    return out


def all_routes(raw):
    """Every distinct route-designator across every detail location on
    this event, sorted -- broader than primary_route (which only
    keeps the first), used both for RoadEvent.routes and for --county/
    routes filtering so a multi-segment event isn't missed just
    because its FIRST location isn't the matching one."""
    routes = set()
    for loc in _iter_locations(raw):
        rd = loc.get("route-designator")
        if isinstance(rd, str) and rd:
            routes.add(rd)
    return sorted(routes)


def _first_point(coords):
    # GeoJSon Point coordinates: [lon, lat]. LineString/MultiPoint:
    # [[lon,lat], ...]. Polygon/MultiLineString: [[[lon,lat], ...], ...].
    # MultiPolygon: one level deeper still. Walk down until two bare
    # numbers are found, bounded so a malformed/deeply-nested value
    # can't loop forever.
    node = coords
    for _ in range(5):
        if isinstance(node, list) and len(node) == 2 and all(isinstance(v, (int, float)) for v in node):
            return node
        if isinstance(node, list) and node:
            node = node[0]
        else:
            return None
    return None


def _extract_lat_lon(raw, locations):
    for loc_dict in locations:
        if loc_dict["latitude"] is not None:
            return loc_dict["latitude"], loc_dict["longitude"]
    geometry = raw.get("geometry")
    if isinstance(geometry, dict):
        point = _first_point(geometry.get("coordinates"))
        if point:
            lon, lat = point[0], point[1]
            return float(lat), float(lon)
    return None, None


def _extract_start_time(raw):
    times = [t for t in (_parse_zoned_datetime(d.get("start-time")) for d in _iter_details(raw) if isinstance(d, dict)) if t]
    return min(times) if times else None


def _extract_end_time(raw):
    times = [t for t in (_parse_zoned_datetime(d.get("end-time")) for d in _iter_details(raw) if isinstance(d, dict)) if t]
    return max(times) if times else None


def normalize_event(raw):
    """CARSEvent dict -> RoadEvent field dict (everything except
    source_active/in_scope/first_seen_at/last_seen_at/last_changed_at,
    which sync_events manages). Never raises except for a genuinely
    unidentifiable record (see EventParseError) -- every other field
    degrades gracefully to blank/None. Deliberately does not validate
    headline_category/headline_code, source_status, or any other enum
    against the values seen during reconnaissance: KDOT can introduce
    a new classification or status string at any time, and an
    unrecognized value should be stored and kept filterable/
    searchable, not treated as an error.

    Multi-location events are preserved completely in `routes`/
    `locations` (see those fields' help_text) -- primary_route/
    primary_direction/latitude/longitude are convenience copies of the
    FIRST location only, for admin display, never the sole record of
    a multi-location event."""
    if not isinstance(raw, dict):
        raise EventParseError("event record is not a JSON object")
    external_id = raw.get("event-id")
    if not isinstance(external_id, str) or not external_id.strip():
        raise EventParseError("event record is missing a valid 'event-id'")

    headline = raw.get("headline")
    if not isinstance(headline, dict):
        headline = {}

    counties = [c for c in (raw.get("counties") or []) if isinstance(c, str)] if isinstance(raw.get("counties"), list) else []
    districts = [d for d in (raw.get("districts") or []) if isinstance(d, str)] if isinstance(raw.get("districts"), list) else []

    locations = _extract_all_locations(raw)
    primary_route = locations[0]["route_designator"] if locations else ""
    primary_direction = locations[0]["direction"] if locations else ""
    latitude, longitude = _extract_lat_lon(raw, locations)
    geometry = raw.get("geometry") if isinstance(raw.get("geometry"), dict) else None

    checksum = compute_checksum(raw)  # BEFORE sanitization -- see compute_checksum()'s docstring

    return {
        "external_id": external_id,
        "headline_category": _safe_str(headline.get("category")),
        "headline_code": _safe_str(headline.get("code")),
        "description": _safe_str(raw.get("description")),
        "priority": _safe_int(raw.get("priority")),
        "source_status": _safe_str(raw.get("status")),
        "counties": counties,
        "districts": districts,
        "primary_route": primary_route,
        "primary_direction": primary_direction,
        "routes": all_routes(raw),
        "locations": locations,
        "latitude": latitude,
        "longitude": longitude,
        "geometry": geometry,
        "start_time": _extract_start_time(raw),
        "end_time": _extract_end_time(raw),
        "source_update_time": _parse_zoned_datetime(raw.get("update-time")),
        "source_next_update_time": _parse_zoned_datetime(raw.get("next-update-time")),
        "source_update_expiry_time": _parse_zoned_datetime(raw.get("update-expiry-time")),
        "update_number": _safe_int(raw.get("update-number")),
        "raw_payload": sanitize_payload_for_storage(raw),
        "payload_checksum": checksum,
    }


def _matches_scope(normalized, counties_filter, routes_filter, config, now):
    """Does this normalized event currently match
    RoadConditionsConfiguration's coverage filters? Populates
    RoadEvent.in_scope -- see that field's docstring. Distinct from
    whether the record parsed at all (EventParseError) or whether
    KDOT still lists it (RoadEvent.source_active)."""
    if counties_filter and not (set(normalized["counties"]) & counties_filter):
        return False
    if routes_filter and not (set(normalized["routes"]) & routes_filter):
        return False
    priority = normalized["priority"]
    if priority is not None and priority < config.min_priority:
        return False
    end_time = normalized["end_time"]
    if end_time is not None and (now - end_time).days > config.max_event_age_days:
        return False
    start_time = normalized["start_time"]
    if start_time is not None and (start_time - now).days > config.lookahead_days:
        return False
    return True


def _record_run(config, result, started_at):
    """Writes a RoadConditionsSyncRun row and updates
    RoadConditionsConfiguration's last_fetch_* fields. Only ever
    called for a REAL (non-dry-run) invocation -- a dry run must write
    NOTHING to the database (see sync_events's dry_run branch and
    tests/test_sync.py's DryRunWritesNothingTests), so this function
    is simply never called when dry_run is True."""
    finished_at = dj_timezone.now()
    RoadConditionsSyncRun.objects.create(
        started_at=started_at, finished_at=finished_at, outcome=result["outcome"],
        fetched_count=result["fetched_count"], relevant_count=result["relevant_count"],
        created_count=result["created_count"], updated_count=result["updated_count"],
        unchanged_count=result["unchanged_count"], deactivated_count=result["deactivated_count"],
        error_count=result["error_count"], latency_ms=result["latency_ms"],
        error_message=result["error_message"],
    )
    config.last_fetch_attempted_at = started_at
    if result["outcome"] in ("success", "partial"):
        config.last_fetch_succeeded_at = finished_at
        config.last_error = ""
    else:
        config.last_error = result["error_message"]
    config.last_record_count = result["fetched_count"]
    config.save(update_fields=["last_fetch_attempted_at", "last_fetch_succeeded_at", "last_error", "last_record_count"])


def _raw_event_ids(raw_events):
    """Every syntactically-valid event-id found directly in the RAW,
    unnormalized fetch -- deliberately computed BEFORE normalize_event()
    runs on anything, so a record that fails to normalize this cycle
    still has its id counted as "present in the complete source". An
    existing RoadEvent must never be deactivated just because ITS OWN
    record happened to fail parsing on one particular run -- that's a
    parse failure, not evidence the event is gone from KDOT. See
    tests/test_sync.py's ParseFailureProtectionTests.

    Returns (ids, identity_error_count). identity_error_count counts
    every raw entry that is NOT a dict, or IS a dict but has a missing/
    empty/wrong-typed event-id -- i.e. every record whose identity we
    genuinely cannot determine. A raw record with an unusable identity
    cannot be safely attributed to any existing RoadEvent OR safely
    concluded to be a new/different one -- so its presence means `ids`
    is not a trustworthy stand-in for "the complete set of ids KDOT
    currently has": some existing RoadEvent could correspond to exactly
    this unreadable record and we'd have no way to tell. Callers must
    treat identity_error_count > 0 as disabling ordinary presence-based
    deactivation for the whole run -- see sync_events()."""
    ids = set()
    identity_error_count = 0
    for raw in raw_events:
        if not isinstance(raw, dict):
            identity_error_count += 1
            continue
        event_id = raw.get("event-id")
        if isinstance(event_id, str) and event_id.strip():
            ids.add(event_id)
        else:
            identity_error_count += 1
    return ids, identity_error_count


def sync_events(config, *, dry_run=False, event_classifications_filter=None, county_filter=None, limit=None, client=None):
    """Fetch -> filter -> normalize -> upsert/deactivate/rescope.

    Returns the same count fields RoadConditionsSyncRun stores, as a
    dict. Raises a CarsApiError subclass (never a bare Exception) for a
    total fetch failure -- the caller (sync_road_conditions) turns that
    into a nonzero exit code. A per-record parse failure never raises
    out of here; it's counted in result["error_count"] and the overall
    outcome becomes "partial" instead of "success".

    `event_classifications_filter`/`county_filter`/`limit` are
    troubleshooting overrides (the management command's --event-type/
    --county/--limit) layered OVER config, not instead of it -- passing
    any of them marks this run "narrowed". A narrowed run NEVER
    deactivates anything and NEVER changes any existing row's
    `in_scope`, because it deliberately only looked at part of the
    real source/real configured scope, and "not seen this way in this
    one run" would not mean "gone from KDOT" or "out of the STATION'S
    real configured coverage" the way it does for a full, unnarrowed
    run using the persisted config as-is.

    Deactivation is ALSO disabled (independent of `narrowed`) whenever
    any raw record in the response has a missing/empty/wrong-typed
    event-id, or isn't a JSON object at all -- see _raw_event_ids()'s
    docstring for why an identity-incomplete response can't be trusted
    to conclude anything is gone. That run is still marked "partial"
    (each unidentifiable record also fails normalize_event() and is
    counted in error_count) and every OTHER, identifiable record is
    still imported/updated/rescoped normally -- only deactivation is
    suppressed for the whole run. A duplicate event-id within one
    response is resolved deterministically (first occurrence wins,
    every later duplicate logged and counted as an error) rather than
    processed twice, which would otherwise raise a real IntegrityError
    on RoadEvent.external_id's uniqueness constraint during the write
    phase and abort the whole transaction.

    dry_run=True performs ZERO persistent writes of any kind -- no
    RoadEvent change, no RoadConditionsConfiguration field update, no
    RoadConditionsSyncRun row, no monitoring SystemEvent. It only
    fetches, computes, and returns what WOULD happen. See
    tests/test_sync.py's DryRunWritesNothingTests.
    """
    narrowed = event_classifications_filter is not None or county_filter is not None or limit is not None
    classifications = event_classifications_filter if event_classifications_filter is not None else config.event_classifications_list
    counties_filter = {county_filter} if county_filter else config.counties_set
    routes_filter = config.routes_set

    started_at = dj_timezone.now()
    result = dict(fetched_count=0, relevant_count=0, created_count=0, updated_count=0,
                  unchanged_count=0, deactivated_count=0, error_count=0, outcome="failed",
                  latency_ms=None, error_message="")

    client = client or build_client(config)

    fetch_started = time.monotonic()
    try:
        raw_events = client.get_events(event_classifications=classifications or None)
    except CarsApiError as exc:
        result["latency_ms"] = int((time.monotonic() - fetch_started) * 1000)
        result["error_message"] = str(exc)
        logger.warning("road_conditions: sync fetch failed: %s", exc)
        if not dry_run:
            emit_event(category="road_conditions", level="error", title="CARS API fetch failed",
                       detail={"error": str(exc), "error_type": type(exc).__name__},
                       dedupe_key=f"road_conditions|fetch-failed|{type(exc).__name__}")
            _record_run(config, result, started_at)
        raise

    result["latency_ms"] = int((time.monotonic() - fetch_started) * 1000)
    result["fetched_count"] = len(raw_events)

    # Item 1: the complete set of source ids, from the RAW response,
    # BEFORE any truncation (--limit) or normalization -- see
    # _raw_event_ids()'s docstring. Used only for the true-deactivation
    # step below; --limit still truncates what actually gets
    # NORMALIZED/considered relevant, same as before.
    complete_source_ids, identity_error_count = _raw_event_ids(raw_events)
    identity_untrustworthy = identity_error_count > 0
    if limit is not None:
        raw_events = raw_events[:limit]

    now = dj_timezone.now()
    in_scope_batch = []
    out_of_scope_ids = set()
    error_samples = []
    seen_in_this_run = set()
    duplicate_count = 0

    for index, raw in enumerate(raw_events):
        try:
            normalized = normalize_event(raw)
        except EventParseError as exc:
            result["error_count"] += 1
            if len(error_samples) < _MAX_ERROR_SAMPLES:
                error_samples.append({"index": index, "error": str(exc)})
            if config.debug_logging:
                logger.info("road_conditions: skipping unparseable event at index %s: %s", index, exc)
            continue
        except Exception as exc:  # noqa: BLE001 -- one bad record must never abort the whole sync
            result["error_count"] += 1
            if len(error_samples) < _MAX_ERROR_SAMPLES:
                error_samples.append({"index": index, "error": repr(exc)})
            logger.warning("road_conditions: unexpected error normalizing event at index %s: %r", index, exc)
            continue

        # Duplicate event-id within the SAME response -- handled
        # deterministically (first occurrence in the raw list wins,
        # matching this project's existing "earliest wins" idiom
        # elsewhere -- see generate_dedication_intros), logged, and
        # counted as an error (contributing to a "partial" outcome)
        # rather than silently processed as two independent writes,
        # which would otherwise reach the write phase below and raise
        # a real IntegrityError on RoadEvent.external_id's uniqueness
        # constraint, aborting the entire transaction.
        if normalized["external_id"] in seen_in_this_run:
            result["error_count"] += 1
            duplicate_count += 1
            if len(error_samples) < _MAX_ERROR_SAMPLES:
                error_samples.append({"index": index, "error": f"duplicate event-id {normalized['external_id']!r} in this response -- first occurrence kept, this one skipped"})
            logger.warning("road_conditions: duplicate event-id %r at index %s -- first occurrence already kept, skipping this one", normalized["external_id"], index)
            continue
        seen_in_this_run.add(normalized["external_id"])

        if _matches_scope(normalized, counties_filter, routes_filter, config, now):
            result["relevant_count"] += 1
            in_scope_batch.append(normalized)
            if config.debug_logging:
                logger.info("road_conditions: keeping %s (%s / %s)", normalized["external_id"],
                            normalized["headline_category"], normalized["headline_code"])
        else:
            out_of_scope_ids.add(normalized["external_id"])

    if result["error_count"]:
        logger.warning("road_conditions: %s of %s fetched records failed to parse", result["error_count"], result["fetched_count"])
        if not dry_run:
            emit_event(category="road_conditions", level="warning", title="Some CARS events failed to parse",
                       detail={"error_count": result["error_count"], "fetched_count": result["fetched_count"], "samples": error_samples},
                       dedupe_key="road_conditions|parse-errors")

    if identity_untrustworthy:
        # Item: a raw record with no usable stable id means the
        # complete-id set can't be trusted as complete -- log this
        # distinctly from the generic parse-error summary above (an
        # operator needs to know DEACTIVATION was skipped this run for
        # a specific, different reason than "some records didn't
        # parse"), and never on a narrowed run's already-suppressed path.
        logger.warning("road_conditions: %s record(s) in this response had a missing/invalid event-id -- "
                        "deactivation disabled for this run (the complete id set cannot be trusted)", identity_error_count)
        if not dry_run:
            emit_event(category="road_conditions", level="warning", title="CARS response contained unidentifiable records",
                       detail={"identity_error_count": identity_error_count, "fetched_count": result["fetched_count"]},
                       dedupe_key="road_conditions|identity-error")

    if dry_run:
        in_scope_ids = {n["external_id"] for n in in_scope_batch}
        existing = {e.external_id: e for e in RoadEvent.objects.filter(external_id__in=in_scope_ids)}
        for normalized in in_scope_batch:
            existing_obj = existing.get(normalized["external_id"])
            if existing_obj is None:
                result["created_count"] += 1
            elif existing_obj.payload_checksum != normalized["payload_checksum"] or not existing_obj.in_scope or not existing_obj.source_active:
                result["updated_count"] += 1
            else:
                result["unchanged_count"] += 1
        if not narrowed:
            # Mirror the real write path's rescoping prediction too --
            # an existing row about to be flipped out of scope (or
            # reactivated-but-out-of-scope) would count as "updated"
            # on a real run; a dry run should predict that, not just
            # the create/update/unchanged counts for the in-scope batch.
            # Rescoping only ever touches rows whose OWN record we
            # successfully identified/parsed/scope-checked this run --
            # an unrelated identity-untrustworthy record elsewhere in
            # the response doesn't affect confidence about THESE ids,
            # so rescoping is gated by `narrowed` only, not by
            # identity_untrustworthy (which gates deactivation below).
            existing_out_of_scope = RoadEvent.objects.filter(external_id__in=out_of_scope_ids)
            for obj in existing_out_of_scope:
                if obj.in_scope or not obj.source_active:
                    result["updated_count"] += 1
                else:
                    result["unchanged_count"] += 1
            if not identity_untrustworthy:
                result["deactivated_count"] = RoadEvent.objects.filter(source_active=True).exclude(external_id__in=complete_source_ids).count()
            # else: a raw record with no usable identity means
            # complete_source_ids can't be trusted as complete --
            # predicting 0 deactivations matches what the real write
            # path below actually does in this situation.
        result["outcome"] = "success" if result["error_count"] == 0 else "partial"
        return result  # no _record_run -- dry runs write nothing at all

    with transaction.atomic():
        touched_ids = {n["external_id"] for n in in_scope_batch} | out_of_scope_ids
        existing_map = {
            e.external_id: e
            for e in RoadEvent.objects.select_for_update().filter(external_id__in=touched_ids)
        }
        for normalized in in_scope_batch:
            obj = existing_map.get(normalized["external_id"])
            if obj is None:
                RoadEvent.objects.create(**normalized, last_seen_at=now, last_changed_at=now,
                                          source_active=True, in_scope=True)
                result["created_count"] += 1
                continue

            obj.last_seen_at = now
            changed = obj.payload_checksum != normalized["payload_checksum"]
            if changed:
                for field, value in normalized.items():
                    setattr(obj, field, value)
                obj.last_changed_at = now
            if not obj.source_active:
                obj.source_active = True
                obj.deactivated_at = None
                changed = True
            if not obj.in_scope:
                obj.in_scope = True
                changed = True
            obj.save()
            if changed:
                result["updated_count"] += 1
            else:
                result["unchanged_count"] += 1

        if not narrowed:
            # Item 3: an event still present in the source but no
            # longer matching this station's configured coverage gets
            # rescoped (in_scope=False), NOT deleted and NOT left
            # forever presented as current. Only touches rows that
            # already exist locally -- an out-of-scope record that was
            # never previously relevant never gets created just
            # because it showed up in the unfiltered /events response.
            for external_id in out_of_scope_ids:
                obj = existing_map.get(external_id)
                if obj is None:
                    continue
                changed = False
                if obj.in_scope:
                    obj.in_scope = False
                    changed = True
                if not obj.source_active:
                    obj.source_active = True
                    obj.deactivated_at = None
                    changed = True
                obj.last_seen_at = now
                obj.save()
                if changed:
                    result["updated_count"] += 1
                else:
                    result["unchanged_count"] += 1

            # Item 1: true deactivation uses the COMPLETE raw id set
            # (complete_source_ids), not the in-scope batch -- an
            # event whose record failed to parse THIS run, or that's
            # merely out-of-scope, is still present in
            # complete_source_ids and is therefore protected here.
            # ALSO gated on `not identity_untrustworthy`: if any raw
            # record in this response had no usable event-id,
            # complete_source_ids cannot be trusted as the true
            # complete set (that unreadable record could correspond to
            # any existing RoadEvent we'd otherwise conclude is gone)
            # -- so deactivation is skipped for the WHOLE run, same as
            # for a narrowed run. Rescoping above is NOT gated on this,
            # since it only touches ids we DID successfully identify.
            if not identity_untrustworthy:
                deactivated = (
                    RoadEvent.objects.filter(source_active=True)
                    .exclude(external_id__in=complete_source_ids)
                    .update(source_active=False, deactivated_at=now)
                )
                result["deactivated_count"] = deactivated
        # A narrowed run (--event-type/--county/--limit) only ever saw
        # part of the real source or used a temporary override of the
        # real config -- it must never deactivate anything or change
        # any row's in_scope, since neither "not in this batch" nor
        # "didn't match THIS run's override filters" means "gone from
        # KDOT" or "out of the station's REAL configured coverage".

    result["outcome"] = "success" if result["error_count"] == 0 else "partial"
    _record_run(config, result, started_at)
    return result
