"""normalize_event() tests against real (sanitized) CARS API fixtures
captured during reconnaissance (2026-08-04) plus hand-built edge cases.
No network, no DB -- normalize_event() is pure."""
import json
from datetime import datetime, timezone
from pathlib import Path

from django.test import SimpleTestCase

from road_conditions.services import (
    EventParseError,
    _MAX_LIST_ITEMS_IN_STORED_PAYLOAD,
    _parse_zoned_datetime,
    compute_checksum,
    normalize_event,
    sanitize_payload_for_storage,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


class NormalizeConstructionEventTests(SimpleTestCase):
    """CARS5-11556 -- real Ottawa County US-81 construction event,
    LineString geometry, single detail/location."""

    def setUp(self):
        self.raw = load_fixture("event_construction.json")
        self.normalized = normalize_event(self.raw)

    def test_external_id(self):
        self.assertEqual(self.normalized["external_id"], "CARS5-11556")

    def test_headline_category_and_code(self):
        self.assertEqual(self.normalized["headline_category"], "roadwork")
        self.assertEqual(self.normalized["headline_code"], "construction work")

    def test_description_is_the_broadcast_relevant_text(self):
        self.assertIn("US 81", self.normalized["description"])
        self.assertIn("Minneapolis", self.normalized["description"])

    def test_priority(self):
        self.assertEqual(self.normalized["priority"], 6)

    def test_counties_and_districts(self):
        self.assertEqual(self.normalized["counties"], ["Ottawa"])
        self.assertEqual(self.normalized["districts"], ["DISTRICT 2"])

    def test_primary_route_and_direction_extracted_from_link_location(self):
        self.assertEqual(self.normalized["primary_route"], "US 81")
        self.assertEqual(self.normalized["primary_direction"], "BOTH_DIRECTIONS")

    def test_routes_and_locations_reflect_the_single_location(self):
        self.assertEqual(self.normalized["routes"], ["US 81"])
        self.assertEqual(len(self.normalized["locations"]), 1)
        self.assertEqual(self.normalized["locations"][0]["route_designator"], "US 81")
        self.assertEqual(self.normalized["locations"][0]["direction"], "BOTH_DIRECTIONS")

    def test_lat_lon_extracted_from_link_location_primary(self):
        self.assertAlmostEqual(self.normalized["latitude"], 39.11173473368899)
        self.assertAlmostEqual(self.normalized["longitude"], -97.66069999839544)

    def test_geometry_preserved_as_linestring(self):
        self.assertEqual(self.normalized["geometry"]["type"], "LineString")
        self.assertGreater(len(self.normalized["geometry"]["coordinates"]), 1)

    def test_times_parsed(self):
        self.assertIsNotNone(self.normalized["start_time"])
        self.assertIsNotNone(self.normalized["end_time"])
        self.assertIsNotNone(self.normalized["source_update_time"])

    def test_update_number(self):
        self.assertEqual(self.normalized["update_number"], 1)

    def test_raw_payload_unaffected_by_sanitization_when_nothing_is_oversized(self):
        # This fixture has no list anywhere near
        # _MAX_LIST_ITEMS_IN_STORED_PAYLOAD, so sanitize_payload_for_storage()
        # is a true no-op here and raw_payload equals the source exactly.
        # See PayloadSanitizationTests for the case where it isn't.
        self.assertEqual(self.normalized["raw_payload"], self.raw)

    def test_checksum_is_deterministic(self):
        self.assertEqual(self.normalized["payload_checksum"], compute_checksum(self.raw))
        self.assertEqual(len(self.normalized["payload_checksum"]), 64)  # sha256 hex


class NormalizeClosureRevisionEventTests(SimpleTestCase):
    """CARS5-11605 -- real Dickinson County KS-15 closure with
    update-number=3, i.e. an event KDOT has revised twice while
    keeping the same event-id -- exactly the "can be revised while
    retaining the same identifier" case."""

    def setUp(self):
        self.raw = load_fixture("event_closure.json")
        self.normalized = normalize_event(self.raw)

    def test_update_number_reflects_a_real_revision(self):
        self.assertEqual(self.normalized["update_number"], 3)

    def test_route_is_ks_format_not_k_dash_format(self):
        # KDOT's real route-designator format is "KS 15", never "K-15" --
        # confirmed via live reconnaissance. Regression guard against
        # ever assuming the wrong format.
        self.assertEqual(self.normalized["primary_route"], "KS 15")

    def test_headline_is_closure(self):
        self.assertEqual(self.normalized["headline_category"], "closure")
        self.assertEqual(self.normalized["headline_code"], "closed")


class NormalizePointGeometryEventTests(SimpleTestCase):
    def setUp(self):
        self.raw = load_fixture("event_point_geometry.json")
        self.normalized = normalize_event(self.raw)

    def test_geometry_type_point(self):
        self.assertEqual(self.normalized["geometry"]["type"], "Point")

    def test_lat_lon_still_extracted(self):
        self.assertIsNotNone(self.normalized["latitude"])
        self.assertIsNotNone(self.normalized["longitude"])
        self.assertTrue(-180 <= self.normalized["longitude"] <= 180)
        self.assertTrue(-90 <= self.normalized["latitude"] <= 90)


class NormalizeMultiLocationEventTests(SimpleTestCase):
    """CARS5-TEST-MULTILOCATION -- CONSTRUCTED, not a captured live
    event (the 231-event live dataset at reconnaissance time had zero
    multi-detail/multi-location events -- every real event was single-
    detail, single-location; the schema clearly supports more, this
    just wasn't observed live). Built from the real
    event_construction.json capture with a second detail/location
    added -- details[0] is unmodified real data; see the fixture's own
    _fixture_note field. Confirms normalize_event() does NOT reduce a
    multi-location event down to only its first route/direction/
    location -- routes/locations must carry the complete set."""

    def setUp(self):
        self.raw = load_fixture("event_multi_location.json")
        self.normalized = normalize_event(self.raw)

    def test_primary_fields_reflect_only_the_first_location(self):
        # Convenience fields -- explicitly first-only, not a summary
        # of the whole event. See test_routes_preserves_both below for
        # proof the SECOND location isn't simply dropped.
        self.assertEqual(self.normalized["primary_route"], "US 81")
        self.assertEqual(self.normalized["primary_direction"], "BOTH_DIRECTIONS")

    def test_routes_preserves_both_distinct_routes(self):
        self.assertEqual(self.normalized["routes"], ["KS 18", "US 81"])  # sorted

    def test_locations_preserves_both_complete_location_records(self):
        locations = self.normalized["locations"]
        self.assertEqual(len(locations), 2)
        routes = {loc["route_designator"] for loc in locations}
        directions = {loc["direction"] for loc in locations}
        self.assertEqual(routes, {"US 81", "KS 18"})
        self.assertEqual(directions, {"BOTH_DIRECTIONS", "NORTHBOUND"})

    def test_locations_each_carry_their_own_coordinates(self):
        locations = self.normalized["locations"]
        lats = {round(loc["latitude"], 2) for loc in locations}
        # Two visibly different latitudes -- proves each location kept
        # its OWN coordinates rather than all sharing the first one's.
        self.assertEqual(len(lats), 2)

    def test_counties_and_districts_already_complete_arrays(self):
        # These were never reduced to a single value even before this
        # round -- confirming the pre-existing correct behavior stays
        # correct alongside the new routes/locations fields.
        self.assertEqual(set(self.normalized["counties"]), {"Ottawa", "Saline"})
        self.assertEqual(set(self.normalized["districts"]), {"DISTRICT 2", "DISTRICT 3"})

    def test_nothing_from_the_second_location_leaks_into_primary_lat_lon(self):
        # latitude/longitude are convenience copies of the FIRST
        # location only -- must match location[0], not location[1].
        first_loc = self.normalized["locations"][0]
        self.assertAlmostEqual(self.normalized["latitude"], first_loc["latitude"])
        self.assertAlmostEqual(self.normalized["longitude"], first_loc["longitude"])


class NormalizeMalformedRecordTests(SimpleTestCase):
    def test_missing_event_id_raises_parse_error(self):
        raw = load_fixture("event_missing_id.json")
        with self.assertRaises(EventParseError):
            normalize_event(raw)

    def test_non_object_record_raises_parse_error(self):
        raw = load_fixture("event_not_an_object.json")
        with self.assertRaises(EventParseError):
            normalize_event(raw)

    def test_empty_dict_raises_parse_error_not_crash(self):
        with self.assertRaises(EventParseError):
            normalize_event({})

    def test_event_id_present_but_blank_string_raises_parse_error(self):
        with self.assertRaises(EventParseError):
            normalize_event({"event-id": "   "})

    def test_event_id_wrong_type_raises_parse_error(self):
        with self.assertRaises(EventParseError):
            normalize_event({"event-id": 12345})


class NormalizeUnknownEnumValueTests(SimpleTestCase):
    """A record using headline/status values not seen during
    reconnaissance must be preserved and stored, not rejected --
    KDOT can introduce a new value at any time."""

    def setUp(self):
        self.raw = load_fixture("event_unknown_enum.json")
        self.normalized = normalize_event(self.raw)

    def test_unrecognized_headline_preserved(self):
        self.assertEqual(self.normalized["headline_category"], "futureNewCategory")
        self.assertEqual(self.normalized["headline_code"], "a brand new code KDOT hasn't used yet")

    def test_unrecognized_status_preserved(self):
        self.assertEqual(self.normalized["source_status"], "SOME_NEW_STATUS_VALUE")

    def test_does_not_raise(self):
        # setUp already called normalize_event() without raising --
        # this test exists to make that expectation explicit/named.
        self.assertEqual(self.normalized["external_id"], "CARS5-TEST-UNKNOWN-ENUM")


class NormalizeMissingOptionalFieldsTests(SimpleTestCase):
    """Every field except event-id must degrade gracefully -- these
    strip fields one at a time from an otherwise-valid record and
    confirm normalize_event() still succeeds with a sane default."""

    def setUp(self):
        self.raw = load_fixture("event_construction.json")

    def test_missing_headline(self):
        raw = dict(self.raw)
        del raw["headline"]
        normalized = normalize_event(raw)
        self.assertEqual(normalized["headline_category"], "")
        self.assertEqual(normalized["headline_code"], "")

    def test_missing_description(self):
        raw = dict(self.raw)
        del raw["description"]
        normalized = normalize_event(raw)
        self.assertEqual(normalized["description"], "")

    def test_missing_priority(self):
        raw = dict(self.raw)
        del raw["priority"]
        normalized = normalize_event(raw)
        self.assertIsNone(normalized["priority"])

    def test_non_integer_priority(self):
        raw = dict(self.raw)
        raw["priority"] = "not a number"
        normalized = normalize_event(raw)
        self.assertIsNone(normalized["priority"])

    def test_missing_counties_and_districts(self):
        raw = dict(self.raw)
        del raw["counties"]
        del raw["districts"]
        normalized = normalize_event(raw)
        self.assertEqual(normalized["counties"], [])
        self.assertEqual(normalized["districts"], [])

    def test_missing_details_yields_no_route_no_times_no_locations(self):
        raw = dict(self.raw)
        del raw["details"]
        normalized = normalize_event(raw)
        self.assertEqual(normalized["primary_route"], "")
        self.assertEqual(normalized["primary_direction"], "")
        self.assertEqual(normalized["routes"], [])
        self.assertEqual(normalized["locations"], [])
        self.assertIsNone(normalized["start_time"])
        self.assertIsNone(normalized["end_time"])

    def test_missing_geometry_falls_back_to_none_lat_lon_still_from_details(self):
        raw = dict(self.raw)
        del raw["geometry"]
        normalized = normalize_event(raw)
        self.assertIsNone(normalized["geometry"])
        # lat/lon still available from the LinkLocation in `details`
        self.assertIsNotNone(normalized["latitude"])

    def test_missing_both_geometry_and_details_yields_none_lat_lon(self):
        raw = dict(self.raw)
        del raw["geometry"]
        del raw["details"]
        normalized = normalize_event(raw)
        self.assertIsNone(normalized["latitude"])
        self.assertIsNone(normalized["longitude"])

    def test_malformed_zoned_datetime_yields_none_not_crash(self):
        raw = json.loads(json.dumps(self.raw))
        raw["update-time"] = {"utcOffset": -18000000, "timeZoneId": "America/Chicago"}  # missing "time"
        normalized = normalize_event(raw)
        self.assertIsNone(normalized["source_update_time"])

    def test_non_dict_zoned_datetime_yields_none_not_crash(self):
        raw = json.loads(json.dumps(self.raw))
        raw["update-time"] = "not a zoned datetime object"
        normalized = normalize_event(raw)
        self.assertIsNone(normalized["source_update_time"])


class ComputeChecksumTests(SimpleTestCase):
    def test_same_payload_same_checksum(self):
        raw = load_fixture("event_construction.json")
        self.assertEqual(compute_checksum(raw), compute_checksum(json.loads(json.dumps(raw))))

    def test_key_order_does_not_matter(self):
        raw = load_fixture("event_construction.json")
        reordered = dict(reversed(list(raw.items())))
        self.assertEqual(compute_checksum(raw), compute_checksum(reordered))

    def test_different_payload_different_checksum(self):
        raw = load_fixture("event_construction.json")
        changed = dict(raw)
        changed["description"] = changed["description"] + " EDITED"
        self.assertNotEqual(compute_checksum(raw), compute_checksum(changed))


class PayloadSanitizationTests(SimpleTestCase):
    """A real live event (a K-15 closure) carried a 1,872-point turn-
    by-turn detour route under details[].descriptions[].locations-on-
    detour -- ~890KB for that one field alone, undocumented in the
    API's own Swagger spec, and not mapped to any RoadEvent field.
    These build a synthetic oversized list (not a committed giant
    fixture -- see the git-history note in PHASE1 report about why a
    ~890KB fixture was rejected) to prove sanitize_payload_for_storage()
    actually bounds storage, that `geometry` is exempted, and that
    change detection still works on content that gets truncated away
    in the stored copy."""

    def _oversized_event(self, extra_items=15):
        raw = json.loads(json.dumps(load_fixture("event_construction.json")))
        big_list = [{"waypoint": i, "lat": 39.0 + i * 0.001, "lon": -97.0 - i * 0.001}
                    for i in range(_MAX_LIST_ITEMS_IN_STORED_PAYLOAD + extra_items)]
        raw["details"][0]["descriptions"][0]["locations-on-detour"] = big_list
        return raw, big_list

    def test_oversized_list_is_truncated_in_stored_payload(self):
        raw, big_list = self._oversized_event()
        normalized = normalize_event(raw)
        stored_list = normalized["raw_payload"]["details"][0]["descriptions"][0]["locations-on-detour"]
        # First _MAX items preserved verbatim, plus one truncation marker.
        self.assertEqual(len(stored_list), _MAX_LIST_ITEMS_IN_STORED_PAYLOAD + 1)
        self.assertEqual(stored_list[:_MAX_LIST_ITEMS_IN_STORED_PAYLOAD], big_list[:_MAX_LIST_ITEMS_IN_STORED_PAYLOAD])
        self.assertTrue(stored_list[-1]["_truncated"])
        self.assertEqual(stored_list[-1]["_original_length"], len(big_list))

    def test_geometry_is_never_truncated_even_if_huge(self):
        raw = json.loads(json.dumps(load_fixture("event_construction.json")))
        many_points = [[-97.0 - i * 0.0001, 39.0 + i * 0.0001] for i in range(500)]
        raw["geometry"] = {"type": "LineString", "coordinates": many_points}
        normalized = normalize_event(raw)
        self.assertEqual(len(normalized["raw_payload"]["geometry"]["coordinates"]), 500)
        self.assertEqual(normalized["geometry"]["coordinates"], many_points)

    def test_checksum_computed_before_sanitization_detects_changes_inside_truncated_region(self):
        raw, big_list = self._oversized_event()
        normalized_a = normalize_event(raw)

        # Change an item WELL PAST the truncation cutoff -- the stored
        # (sanitized) raw_payload would be IDENTICAL before and after
        # this edit (both truncate to the same first N items), but the
        # checksum must still differ, because it's computed from the
        # complete, unsanitized record.
        raw2 = json.loads(json.dumps(raw))
        raw2["details"][0]["descriptions"][0]["locations-on-detour"][-1]["lat"] = 999.0
        normalized_b = normalize_event(raw2)

        self.assertEqual(normalized_a["raw_payload"], normalized_b["raw_payload"],
                          "sanity: stored payloads should be identical -- the edit is past the truncation cutoff")
        self.assertNotEqual(normalized_a["payload_checksum"], normalized_b["payload_checksum"],
                             "checksum must still detect a change inside the truncated-away region")

    def test_small_lists_are_not_truncated(self):
        raw = load_fixture("event_construction.json")
        normalized = normalize_event(raw)
        # counties/districts/details/locations are all well under the
        # threshold in a normal event -- confirm no truncation marker
        # anywhere in the stored payload for an ordinary record.
        serialized = json.dumps(normalized["raw_payload"])
        self.assertNotIn("_truncated", serialized)

    def test_sanitize_payload_for_storage_directly_on_non_dict_input(self):
        self.assertEqual(sanitize_payload_for_storage("not a dict"), "not a dict")
        self.assertIsNone(sanitize_payload_for_storage(None))


class ParseZonedDatetimeTests(SimpleTestCase):
    """The source's ZonedDateTime is {"time": epoch-ms, "utcOffset":
    ..., "timeZoneId": ...} -- `time` is deliberately treated as an
    already-complete, unambiguous UTC instant; utcOffset/timeZoneId
    are display metadata this project intentionally ignores for
    computing the instant itself. These prove that directly, across a
    CST timestamp, a CDT timestamp, and one with an unusual explicit
    offset -- specifically proving utcOffset is NOT added/subtracted
    from `time` (a common, wrong, alternate implementation)."""

    def test_cst_winter_timestamp(self):
        # 2026-01-15 12:00:00 UTC -- CST is UTC-6 in January (no DST).
        instant = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        zdt = {"time": int(instant.timestamp() * 1000), "utcOffset": -21600000, "timeZoneId": "America/Chicago"}
        result = _parse_zoned_datetime(zdt)
        self.assertEqual(result, instant)
        self.assertEqual(result.utcoffset().total_seconds(), 0)  # returned datetime is UTC-aware, not offset-shifted

    def test_cdt_summer_timestamp(self):
        # 2026-07-15 12:00:00 UTC -- CDT is UTC-5 in July (DST active).
        instant = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
        zdt = {"time": int(instant.timestamp() * 1000), "utcOffset": -18000000, "timeZoneId": "America/Chicago"}
        result = _parse_zoned_datetime(zdt)
        self.assertEqual(result, instant)

    def test_utcoffset_is_not_applied_arithmetically(self):
        # If a (wrong) implementation added utcOffset milliseconds to
        # `time` before interpreting it, this would resolve to a
        # different, incorrect instant. Use a large, unusual offset
        # (+9 hours, as if authored in a JST-like zone) to make any
        # such bug obvious rather than accidentally masked by a small
        # offset.
        instant = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
        zdt = {"time": int(instant.timestamp() * 1000), "utcOffset": 32400000, "timeZoneId": "Asia/Tokyo"}
        result = _parse_zoned_datetime(zdt)
        self.assertEqual(result, instant)  # NOT instant +/- 9 hours

    def test_dst_spring_forward_transition_instant(self):
        # US Central 2026 DST spring-forward: 2026-03-08 02:00 CST ->
        # 03:00 CDT, i.e. 2026-03-08 08:00:00 UTC. No local-time
        # arithmetic happens in _parse_zoned_datetime at all, so this
        # is mostly a no-crash/no-off-by-one guard at a real transition
        # instant, not a meaningfully different code path -- but worth
        # asserting explicitly given how often DST logic is the thing
        # that breaks in date-handling code.
        instant = datetime(2026, 3, 8, 8, 0, 0, tzinfo=timezone.utc)
        zdt = {"time": int(instant.timestamp() * 1000), "utcOffset": -21600000, "timeZoneId": "America/Chicago"}
        result = _parse_zoned_datetime(zdt)
        self.assertEqual(result, instant)

    def test_missing_time_key(self):
        self.assertIsNone(_parse_zoned_datetime({"utcOffset": -18000000, "timeZoneId": "America/Chicago"}))

    def test_time_wrong_type(self):
        self.assertIsNone(_parse_zoned_datetime({"time": "not a number"}))

    def test_non_dict_input(self):
        self.assertIsNone(_parse_zoned_datetime("not a zoned datetime"))
        self.assertIsNone(_parse_zoned_datetime(None))
        self.assertIsNone(_parse_zoned_datetime([1, 2, 3]))

    def test_result_is_always_utc_aware_never_naive(self):
        zdt = {"time": 1700000000000, "utcOffset": -18000000, "timeZoneId": "America/Chicago"}
        result = _parse_zoned_datetime(zdt)
        self.assertIsNotNone(result.tzinfo)
        self.assertEqual(result.tzinfo, timezone.utc)
