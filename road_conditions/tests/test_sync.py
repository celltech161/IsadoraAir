"""services.sync_events() tests -- the idempotent upsert/deactivate/
rescope core. Uses a FakeCarsClient (no network) and real Django
TestCase DB access (no mocking the ORM -- these are meant to catch
real create/update/deactivate/rescope bugs)."""
import copy
import json
from pathlib import Path

from django.test import TestCase
from django.utils import timezone

from monitoring.models import SystemEvent
from road_conditions.api import CarsApiError, CarsApiSchemaError, CarsApiTimeout
from road_conditions.models import RoadConditionsConfiguration, RoadConditionsSyncRun, RoadEvent
from road_conditions.services import sync_events

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def make_event(event_id="CARS5-TEST-1", county="Ottawa", route="US 81", priority=6,
                description="Test event", start_days_from_now=None, end_days_from_now=None):
    """Builds a syntactically-real CARSEvent dict from the real
    construction fixture as a template, with the fields tests actually
    vary overridden -- avoids every test hand-rolling the full nested
    shape while still exercising the real normalize_event() path."""
    raw = copy.deepcopy(load_fixture("event_construction.json"))
    raw["event-id"] = event_id
    raw["counties"] = [county] if county else []
    raw["description"] = description
    raw["priority"] = priority
    raw["details"][0]["locations"][0]["route-designator"] = route
    now_ms = int(timezone.now().timestamp() * 1000)
    if start_days_from_now is not None:
        raw["details"][0]["start-time"]["time"] = now_ms + int(start_days_from_now * 86400 * 1000)
    if end_days_from_now is not None:
        raw["details"][0]["end-time"]["time"] = now_ms + int(end_days_from_now * 86400 * 1000)
    return raw


def make_unparseable_event():
    """A record present in a raw fetch that will fail normalize_event()
    -- missing event-id."""
    raw = copy.deepcopy(load_fixture("event_construction.json"))
    del raw["event-id"]
    return raw


def make_event_with_empty_id():
    raw = copy.deepcopy(load_fixture("event_construction.json"))
    raw["event-id"] = "   "  # whitespace-only -- same as empty after .strip()
    return raw


def make_event_with_wrong_type_id():
    raw = copy.deepcopy(load_fixture("event_construction.json"))
    raw["event-id"] = 12345  # int, not a string
    return raw


def make_non_dict_raw_entry():
    """A raw /events array entry that isn't even a JSON object --
    defensive coverage for a genuinely malformed API response shape."""
    return "this is not an event object"


def make_multi_location_event(event_id, counties, routes):
    """Builds a real, normalize_event()-compatible CARSEvent with
    MULTIPLE counties and MULTIPLE distinct route locations -- for
    proving corridor/scope matching considers the complete
    routes/counties sets (RoadEvent.routes/counties), not just
    primary_route or a single county, exactly as normalize_event()
    itself does. `routes` becomes one location per route designator,
    all attached to the same single detail (mirrors a real multi-
    segment event)."""
    raw = copy.deepcopy(load_fixture("event_construction.json"))
    raw["event-id"] = event_id
    raw["counties"] = list(counties)
    template_location = raw["details"][0]["locations"][0]
    raw["details"][0]["locations"] = []
    for route in routes:
        loc = copy.deepcopy(template_location)
        loc["route-designator"] = route
        raw["details"][0]["locations"].append(loc)
    return raw


class FakeCarsClient:
    def __init__(self, events=None, error=None):
        self.events = events if events is not None else []
        self.error = error
        self.calls = []

    def get_events(self, event_classifications=None, route_designator=None):
        self.calls.append({"event_classifications": event_classifications, "route_designator": route_designator})
        if self.error:
            raise self.error
        return self.events


def _snapshot_db_state():
    """Everything sync_events could conceivably write to -- used by
    DryRunWritesNothingTests to prove a dry run touches none of it."""
    return {
        "road_events": list(
            RoadEvent.objects.order_by("external_id").values(
                "external_id", "payload_checksum", "source_active", "in_scope",
                "last_seen_at", "last_changed_at", "description",
            )
        ),
        "sync_runs": list(RoadConditionsSyncRun.objects.values()),
        "system_events": list(SystemEvent.objects.filter(category="road_conditions").values()),
        "config_last_fetch_attempted_at": None,  # filled in by the caller with a fresh config reload
        "config_last_fetch_succeeded_at": None,
        "config_last_error": None,
        "config_last_record_count": None,
    }


class SyncEventsCreateUpdateUnchangedTests(TestCase):
    def setUp(self):
        self.config = RoadConditionsConfiguration.load()

    def test_creates_new_event(self):
        client = FakeCarsClient(events=[make_event("CARS5-A")])
        result = sync_events(self.config, client=client)
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(result["unchanged_count"], 0)
        self.assertEqual(RoadEvent.objects.count(), 1)
        obj = RoadEvent.objects.get(external_id="CARS5-A")
        self.assertTrue(obj.source_active)
        self.assertTrue(obj.in_scope)
        self.assertIsNotNone(obj.first_seen_at)
        self.assertIsNotNone(obj.last_seen_at)
        self.assertIsNotNone(obj.last_changed_at)

    def test_second_identical_sync_is_unchanged(self):
        client = FakeCarsClient(events=[make_event("CARS5-A")])
        sync_events(self.config, client=client)
        obj_before = RoadEvent.objects.get(external_id="CARS5-A")

        result2 = sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-A")]))
        self.assertEqual(result2["created_count"], 0)
        self.assertEqual(result2["updated_count"], 0)
        self.assertEqual(result2["unchanged_count"], 1)

        obj_after = RoadEvent.objects.get(external_id="CARS5-A")
        self.assertEqual(obj_before.last_changed_at, obj_after.last_changed_at)
        self.assertGreaterEqual(obj_after.last_seen_at, obj_before.last_seen_at)

    def test_changed_payload_is_updated(self):
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-A", description="Original text")]))
        obj_before = RoadEvent.objects.get(external_id="CARS5-A")

        result2 = sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-A", description="Revised text")]))
        self.assertEqual(result2["updated_count"], 1)
        self.assertEqual(result2["created_count"], 0)
        self.assertEqual(result2["unchanged_count"], 0)

        obj_after = RoadEvent.objects.get(external_id="CARS5-A")
        self.assertEqual(obj_after.description, "Revised text")
        self.assertGreater(obj_after.last_changed_at, obj_before.last_changed_at)

    def test_multiple_events_counted_independently(self):
        client = FakeCarsClient(events=[make_event("CARS5-A"), make_event("CARS5-B"), make_event("CARS5-C")])
        result = sync_events(self.config, client=client)
        self.assertEqual(result["created_count"], 3)
        self.assertEqual(RoadEvent.objects.count(), 3)

    def test_a_sync_run_row_is_recorded(self):
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-A")]))
        run = RoadConditionsSyncRun.objects.get()
        self.assertEqual(run.outcome, "success")
        self.assertEqual(run.created_count, 1)
        self.assertIsNotNone(run.finished_at)
        self.assertGreaterEqual(run.finished_at, run.started_at)

    def test_config_last_fetch_fields_updated_on_success(self):
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-A")]))
        self.config.refresh_from_db()
        self.assertIsNotNone(self.config.last_fetch_attempted_at)
        self.assertIsNotNone(self.config.last_fetch_succeeded_at)
        self.assertEqual(self.config.last_error, "")
        self.assertEqual(self.config.last_record_count, 1)


class SyncEventsDeactivationTests(TestCase):
    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-GONE"), make_event("CARS5-STAYS")]))

    def test_event_absent_from_full_sync_is_deactivated(self):
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-STAYS")]))
        self.assertEqual(result["deactivated_count"], 1)
        gone = RoadEvent.objects.get(external_id="CARS5-GONE")
        self.assertFalse(gone.source_active)
        self.assertIsNotNone(gone.deactivated_at)
        stays = RoadEvent.objects.get(external_id="CARS5-STAYS")
        self.assertTrue(stays.source_active)

    def test_reappearing_event_is_reactivated(self):
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-STAYS")]))
        self.assertFalse(RoadEvent.objects.get(external_id="CARS5-GONE").source_active)

        result = sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-GONE"), make_event("CARS5-STAYS")]))
        gone = RoadEvent.objects.get(external_id="CARS5-GONE")
        self.assertTrue(gone.source_active)
        self.assertIsNone(gone.deactivated_at)
        self.assertEqual(result["updated_count"], 1)  # reactivation counts as an update

    def test_narrowed_by_county_never_deactivates(self):
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-STAYS")]), county_filter="Ottawa")
        self.assertEqual(result["deactivated_count"], 0)
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-GONE").source_active)

    def test_narrowed_by_classification_never_deactivates(self):
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-STAYS")]), event_classifications_filter=["constructionReports"])
        self.assertEqual(result["deactivated_count"], 0)
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-GONE").source_active)

    def test_narrowed_by_limit_never_deactivates(self):
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-STAYS")]), limit=1)
        self.assertEqual(result["deactivated_count"], 0)
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-GONE").source_active)


class ParseFailureProtectionTests(TestCase):
    """Item 1: an existing RoadEvent must not be deactivated merely
    because ITS OWN current source record failed normalization on one
    particular run -- the complete source id set is collected directly
    from the raw /events response, before any record is normalized,
    specifically so this stays true."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        # First sync: CARS5-PARSEFAIL exists normally.
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-PARSEFAIL"), make_event("CARS5-OTHER")]))
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-PARSEFAIL").source_active)

    def test_existing_event_whose_record_fails_to_parse_stays_active(self):
        # Second sync: the SAME event-id is present in the complete raw
        # response (so KDOT hasn't dropped it), but this particular
        # record is malformed enough to fail normalize_event() --
        # simulated here with a genuinely-circular nested structure.
        # (Recall: only a missing/invalid event-id is a genuine
        # EventParseError -- every other field degrades gracefully via
        # isinstance guards, so to exercise normalize_event()'s
        # belt-and-suspenders `except Exception` catch-all instead, the
        # record needs something that ACTUALLY raises. A circular
        # reference does: json.dumps() (inside compute_checksum(), even
        # with default=str) raises ValueError on a real cycle
        # regardless of the default= fallback, which only helps for
        # non-serializable LEAF values, not container cycles.)
        broken = make_event("CARS5-PARSEFAIL")
        circular = {}
        circular["self"] = circular
        broken["_circular_to_force_a_normalize_crash"] = circular

        result = sync_events(self.config, client=FakeCarsClient(events=[broken, make_event("CARS5-OTHER")]))

        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["outcome"], "partial")
        still_active = RoadEvent.objects.get(external_id="CARS5-PARSEFAIL")
        self.assertTrue(still_active.source_active, "an event present in the complete fetch must not be "
                                                      "deactivated just because ITS OWN record failed to parse this run")
        self.assertEqual(result["deactivated_count"], 0)

    def test_a_genuinely_different_event_is_still_correctly_deactivated_alongside_a_parse_failure(self):
        # Stronger version: prove the protection is precise, not just
        # "parse errors disable ALL deactivation for the whole run".
        # CARS5-OTHER is genuinely absent (and no record for it is
        # even present, parseable or not) in this run -- it must still
        # be deactivated, while CARS5-PARSEFAIL (present, but broken)
        # must not be.
        broken = make_event("CARS5-PARSEFAIL")
        circular = {}
        circular["self"] = circular
        broken["_circular_to_force_a_normalize_crash"] = circular

        result = sync_events(self.config, client=FakeCarsClient(events=[broken]))  # CARS5-OTHER not in this fetch at all

        self.assertEqual(result["error_count"], 1)
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-PARSEFAIL").source_active,
                         "present-but-unparseable -- protected")
        self.assertFalse(RoadEvent.objects.get(external_id="CARS5-OTHER").source_active,
                          "genuinely absent from the complete fetch -- correctly deactivated")
        self.assertEqual(result["deactivated_count"], 1)


class DryRunWritesNothingTests(TestCase):
    """Item 4: a dry run must perform ZERO persistent writes -- no
    RoadEvent change, no RoadConditionsConfiguration field update, no
    RoadConditionsSyncRun row, no monitoring SystemEvent. Snapshots
    the complete relevant DB state before and after each dry run and
    asserts byte-for-byte equality, rather than checking a few fields
    and hoping nothing else changed."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        # Pre-existing state a dry run must not disturb: one active
        # event, one config field, one already-logged real run.
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-EXISTING", description="v1")]))
        self.config.refresh_from_db()

    def _full_snapshot(self):
        self.config.refresh_from_db()
        snap = _snapshot_db_state()
        snap["config_last_fetch_attempted_at"] = self.config.last_fetch_attempted_at
        snap["config_last_fetch_succeeded_at"] = self.config.last_fetch_succeeded_at
        snap["config_last_error"] = self.config.last_error
        snap["config_last_record_count"] = self.config.last_record_count
        return snap

    def test_dry_run_create_scenario_writes_nothing(self):
        before = self._full_snapshot()
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-EXISTING", description="v1"), make_event("CARS5-NEW")]), dry_run=True)
        after = self._full_snapshot()
        self.assertEqual(result["created_count"], 1)  # correctly predicts what WOULD happen
        self.assertEqual(before, after, "dry run must not change ANY persisted state")
        self.assertEqual(RoadEvent.objects.count(), 1)  # CARS5-NEW was never actually created

    def test_dry_run_update_scenario_writes_nothing(self):
        before = self._full_snapshot()
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-EXISTING", description="v2 -- changed")]), dry_run=True)
        after = self._full_snapshot()
        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(before, after)
        self.assertEqual(RoadEvent.objects.get(external_id="CARS5-EXISTING").description, "v1")  # unchanged in DB

    def test_dry_run_deactivation_scenario_writes_nothing(self):
        before = self._full_snapshot()
        result = sync_events(self.config, client=FakeCarsClient(events=[]), dry_run=True)
        after = self._full_snapshot()
        self.assertEqual(result["deactivated_count"], 1)
        self.assertEqual(before, after)
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-EXISTING").source_active)  # still active in DB

    def test_dry_run_rescope_scenario_writes_nothing(self):
        self.config.counties = ""
        self.config.save()
        before = self._full_snapshot()
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-EXISTING", county="Sedgwick")]), dry_run=True)
        after = self._full_snapshot()
        self.assertEqual(result["updated_count"], 1)  # would rescope out
        self.assertEqual(before, after)
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-EXISTING").in_scope)  # still in_scope in DB

    def test_dry_run_partial_failure_scenario_writes_nothing(self):
        before = self._full_snapshot()
        result = sync_events(self.config, client=FakeCarsClient(events=[make_unparseable_event()]), dry_run=True)
        after = self._full_snapshot()
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(before, after, "a dry run must write nothing even when some records fail to parse "
                                         "-- no SystemEvent, no SyncRun row")

    def test_dry_run_total_failure_scenario_writes_nothing(self):
        before = self._full_snapshot()
        with self.assertRaises(CarsApiError):
            sync_events(self.config, client=FakeCarsClient(error=CarsApiTimeout("boom")), dry_run=True)
        after = self._full_snapshot()
        self.assertEqual(before, after, "a dry run must write nothing even on a total fetch failure "
                                         "-- no error SystemEvent, no failed SyncRun row, no config.last_error")

    def test_dry_run_never_creates_a_sync_run_row_at_all(self):
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-EXISTING", description="v1")]), dry_run=True)
        self.assertEqual(RoadConditionsSyncRun.objects.count(), 1)  # only the ONE real run from setUp

    def test_dry_run_never_emits_a_system_event_on_parse_errors(self):
        sync_events(self.config, client=FakeCarsClient(events=[make_unparseable_event()]), dry_run=True)
        self.assertFalse(SystemEvent.objects.filter(category="road_conditions").exists())


class SyncEventsTotalFailureTests(TestCase):
    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-EXISTING")]))

    def test_total_failure_raises(self):
        with self.assertRaises(CarsApiTimeout):
            sync_events(self.config, client=FakeCarsClient(error=CarsApiTimeout("simulated timeout")))

    def test_total_failure_does_not_deactivate_existing_active_rows(self):
        with self.assertRaises(CarsApiError):
            sync_events(self.config, client=FakeCarsClient(error=CarsApiTimeout("simulated timeout")))
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-EXISTING").source_active)

    def test_schema_error_also_does_not_deactivate(self):
        # "Any API response that cannot be proven complete" -- a
        # malformed/unexpected-shape response (CarsApiSchemaError) is
        # caught by the SAME except CarsApiError branch as a timeout;
        # this proves the protection isn't timeout-specific.
        with self.assertRaises(CarsApiError):
            sync_events(self.config, client=FakeCarsClient(error=CarsApiSchemaError("response was not a JSON array")))
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-EXISTING").source_active)

    def test_total_failure_records_a_failed_run(self):
        with self.assertRaises(CarsApiError):
            sync_events(self.config, client=FakeCarsClient(error=CarsApiTimeout("simulated timeout")))
        run = RoadConditionsSyncRun.objects.filter(outcome="failed").get()
        self.assertIn("simulated timeout", run.error_message)

    def test_total_failure_sets_config_last_error(self):
        # setUp() already ran one successful sync (to create
        # CARS5-EXISTING), so last_fetch_succeeded_at is already a real
        # timestamp before this failing sync runs -- a failure must
        # preserve that prior success timestamp, not clear it. Losing
        # the last-known-good timestamp on every subsequent failure
        # would make the "how long has this actually been broken"
        # signal useless.
        preexisting_success_at = self.config.last_fetch_succeeded_at
        self.assertIsNotNone(preexisting_success_at)

        with self.assertRaises(CarsApiError):
            sync_events(self.config, client=FakeCarsClient(error=CarsApiTimeout("simulated timeout")))
        self.config.refresh_from_db()
        self.assertIn("simulated timeout", self.config.last_error)
        self.assertEqual(self.config.last_fetch_succeeded_at, preexisting_success_at)


class SyncEventsPartialFailureTests(TestCase):
    def setUp(self):
        self.config = RoadConditionsConfiguration.load()

    def test_one_malformed_record_does_not_abort_the_whole_batch(self):
        good = make_event("CARS5-GOOD")
        bad = make_unparseable_event()
        result = sync_events(self.config, client=FakeCarsClient(events=[good, bad]))
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(RoadEvent.objects.count(), 1)

    def test_all_malformed_is_still_partial_not_failed(self):
        # A total FETCH failure (network/HTTP/schema) is "failed"; a
        # fetch that succeeded but whose records all failed to parse
        # is "partial" -- the distinction the task calls for between a
        # total API failure and a record-level parse failure.
        bad = make_unparseable_event()
        result = sync_events(self.config, client=FakeCarsClient(events=[bad]))
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["created_count"], 0)

    def test_zero_fetched_events_is_success_not_failure(self):
        result = sync_events(self.config, client=FakeCarsClient(events=[]))
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["fetched_count"], 0)

    def test_partial_parse_still_deactivates_genuinely_absent_events_when_identity_is_trustworthy(self):
        # A "partial" outcome does NOT blanket-disable deactivation for
        # the whole run -- only when the FAILURE ITSELF means the
        # complete id set can't be trusted (a missing/invalid event-id
        # -- see IdentityIntegrityTests in this same module) is
        # deactivation disabled. Here the failing record has a
        # perfectly good, valid identity (a genuine circular reference
        # deep in the payload is what makes it fail to normalize, not
        # anything about its id) -- so the complete id set IS still
        # trustworthy, and anything genuinely absent from it is still
        # correctly deactivated.
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-WILLVANISH")]))
        broken = make_event("CARS5-VALID-ID-BROKEN-CONTENT")
        circular = {}
        circular["self"] = circular
        broken["_circular_to_force_a_normalize_crash"] = circular
        result = sync_events(self.config, client=FakeCarsClient(events=[broken]))  # CARS5-WILLVANISH not present at all
        self.assertEqual(result["outcome"], "partial")
        self.assertFalse(RoadEvent.objects.get(external_id="CARS5-WILLVANISH").source_active)
        self.assertEqual(result["deactivated_count"], 1)


class ConfigScopeChangeTests(TestCase):
    """Item 3: RoadEvent.source_active (still in KDOT's feed) and
    RoadEvent.in_scope (matches this station's CURRENT configured
    coverage) are independent. The canonical scenario from the review:
    an Ottawa County event is imported and active; Ottawa County is
    later removed from configured coverage; a complete sync runs --
    the old event must not remain indefinitely presented as currently
    relevant, but it also must not be deleted or falsely marked as
    gone from the source."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        self.config.counties = "Ottawa"
        self.config.save()
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-OTTAWA", county="Ottawa")]))
        obj = RoadEvent.objects.get(external_id="CARS5-OTTAWA")
        self.assertTrue(obj.source_active)
        self.assertTrue(obj.in_scope)
        self.assertTrue(obj.is_current)

    def test_removing_county_from_coverage_rescopes_not_deletes(self):
        self.config.counties = "Saline"  # Ottawa removed
        self.config.save()

        # A complete, unnarrowed sync -- the event is STILL present in
        # KDOT's feed (still in the fetch), it just no longer matches
        # this station's configured counties.
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-OTTAWA", county="Ottawa")]))

        obj = RoadEvent.objects.get(external_id="CARS5-OTTAWA")
        self.assertTrue(obj.source_active, "KDOT still lists it -- source_active must stay True")
        self.assertFalse(obj.in_scope, "no longer matches configured coverage -- in_scope must flip False")
        self.assertFalse(obj.is_current, "not currently relevant -- but NOT deleted, NOT lost")
        self.assertEqual(RoadEvent.objects.count(), 1, "rescoping must not delete the row")
        self.assertEqual(result["updated_count"], 1)

    def test_widening_coverage_again_restores_in_scope_automatically(self):
        self.config.counties = "Saline"
        self.config.save()
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-OTTAWA", county="Ottawa")]))
        self.assertFalse(RoadEvent.objects.get(external_id="CARS5-OTTAWA").in_scope)

        self.config.counties = "Ottawa,Saline"  # widened back
        self.config.save()
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-OTTAWA", county="Ottawa")]))
        obj = RoadEvent.objects.get(external_id="CARS5-OTTAWA")
        self.assertTrue(obj.in_scope, "no manual un-rescoping needed -- the next complete sync fixes it on its own")
        self.assertTrue(obj.is_current)

    def test_out_of_scope_event_never_created_just_because_it_appears_unfiltered(self):
        # An event that was NEVER previously relevant (no local row
        # exists) must not get a row created just because it showed up
        # in the broader unfiltered fetch -- rescoping only ever
        # touches EXISTING rows, never creates new out-of-scope ones.
        self.config.counties = "Ottawa"
        self.config.save()
        sync_events(self.config, client=FakeCarsClient(events=[
            make_event("CARS5-OTTAWA", county="Ottawa"),
            make_event("CARS5-NEVER-RELEVANT", county="Sedgwick"),
        ]))
        self.assertFalse(RoadEvent.objects.filter(external_id="CARS5-NEVER-RELEVANT").exists())

    def test_rescoped_out_event_does_not_get_deactivated_too(self):
        # source_active and in_scope are independent -- going out of
        # scope must not ALSO flip source_active off (that would be
        # exactly the conflation this split exists to prevent).
        self.config.counties = "Saline"
        self.config.save()
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-OTTAWA", county="Ottawa")]))
        obj = RoadEvent.objects.get(external_id="CARS5-OTTAWA")
        self.assertFalse(obj.in_scope)
        self.assertTrue(obj.source_active)

    def test_narrowed_run_never_rescopes(self):
        # --county overrides config's real filter for one run -- must
        # not persist a rescope based on that temporary override.
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-OTTAWA", county="Ottawa")]), county_filter="Saline")
        obj = RoadEvent.objects.get(external_id="CARS5-OTTAWA")
        self.assertTrue(obj.in_scope, "a narrowed run's temporary filter override must not persist as a real rescope")
        self.assertEqual(result["updated_count"], 0)

    def test_route_removed_from_coverage_also_rescopes(self):
        self.config.counties = ""
        self.config.routes = "US 81"
        self.config.save()
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-ROUTE", route="US 81")]))
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-ROUTE").in_scope)

        self.config.routes = "KS 15"  # US 81 no longer covered
        self.config.save()
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-ROUTE", route="US 81")]))
        self.assertFalse(RoadEvent.objects.get(external_id="CARS5-ROUTE").in_scope)


class SyncEventsFilteringTests(TestCase):
    def setUp(self):
        self.config = RoadConditionsConfiguration.load()

    def test_county_filter_excludes_non_matching(self):
        self.config.counties = "Ottawa"
        self.config.save()
        client = FakeCarsClient(events=[make_event("CARS5-IN", county="Ottawa"), make_event("CARS5-OUT", county="Sedgwick")])
        result = sync_events(self.config, client=client)
        self.assertEqual(result["relevant_count"], 1)
        self.assertTrue(RoadEvent.objects.filter(external_id="CARS5-IN").exists())
        self.assertFalse(RoadEvent.objects.filter(external_id="CARS5-OUT").exists())

    def test_blank_counties_means_no_county_filtering(self):
        self.config.counties = ""
        self.config.save()
        client = FakeCarsClient(events=[make_event("CARS5-IN", county="Ottawa"), make_event("CARS5-ANY", county="Sedgwick")])
        result = sync_events(self.config, client=client)
        self.assertEqual(result["relevant_count"], 2)

    def test_route_filter_excludes_non_matching(self):
        self.config.counties = ""
        self.config.routes = "KS 15"
        self.config.save()
        client = FakeCarsClient(events=[make_event("CARS5-IN", route="KS 15"), make_event("CARS5-OUT", route="US 81")])
        result = sync_events(self.config, client=client)
        self.assertEqual(result["relevant_count"], 1)
        self.assertTrue(RoadEvent.objects.filter(external_id="CARS5-IN").exists())

    def test_min_priority_filter(self):
        self.config.counties = ""
        self.config.min_priority = 8
        self.config.save()
        client = FakeCarsClient(events=[make_event("CARS5-LOW", priority=3), make_event("CARS5-HIGH", priority=9)])
        result = sync_events(self.config, client=client)
        self.assertEqual(result["relevant_count"], 1)
        self.assertTrue(RoadEvent.objects.filter(external_id="CARS5-HIGH").exists())

    def test_missing_priority_is_not_filtered_by_min_priority(self):
        self.config.counties = ""
        self.config.min_priority = 8
        self.config.save()
        event = make_event("CARS5-NOPRI")
        del event["priority"]
        result = sync_events(self.config, client=FakeCarsClient(events=[event]))
        self.assertEqual(result["relevant_count"], 1)

    def test_max_event_age_days_excludes_old_events(self):
        self.config.counties = ""
        self.config.max_event_age_days = 3
        self.config.save()
        old_event = make_event("CARS5-OLD", end_days_from_now=-30)
        result = sync_events(self.config, client=FakeCarsClient(events=[old_event]))
        self.assertEqual(result["relevant_count"], 0)

    def test_lookahead_days_excludes_far_future_events(self):
        self.config.counties = ""
        self.config.lookahead_days = 30
        self.config.save()
        far_future = make_event("CARS5-FAR", start_days_from_now=365)
        result = sync_events(self.config, client=FakeCarsClient(events=[far_future]))
        self.assertEqual(result["relevant_count"], 0)

    def test_command_classification_override_is_sent_to_client(self):
        client = FakeCarsClient(events=[])
        sync_events(self.config, client=client, event_classifications_filter=["winterDriving"])
        self.assertEqual(client.calls[0]["event_classifications"], ["winterDriving"])


class IdentityIntegrityTests(TestCase):
    """Final pre-push check, item 1: a raw record whose event-id is
    missing, empty, or the wrong type -- or that isn't even a JSON
    object -- cannot be safely mapped to any existing RoadEvent. Such a
    record makes the WHOLE run's complete-id set untrustworthy, so
    presence-based deactivation must be disabled for the entire run,
    not just skipped for that one record."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        self.config.counties = ""  # keep filtering out of the way for these tests
        self.config.save()
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-EXISTING")]))
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-EXISTING").source_active)

    def test_missing_event_id_disables_deactivation(self):
        result = sync_events(self.config, client=FakeCarsClient(events=[make_unparseable_event()]))
        self.assertEqual(result["deactivated_count"], 0)
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-EXISTING").source_active)
        self.assertEqual(result["outcome"], "partial")

    def test_empty_event_id_disables_deactivation(self):
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event_with_empty_id()]))
        self.assertEqual(result["deactivated_count"], 0)
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-EXISTING").source_active)
        self.assertEqual(result["outcome"], "partial")

    def test_invalid_type_event_id_disables_deactivation(self):
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event_with_wrong_type_id()]))
        self.assertEqual(result["deactivated_count"], 0)
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-EXISTING").source_active)
        self.assertEqual(result["outcome"], "partial")

    def test_non_dict_raw_record_disables_deactivation(self):
        result = sync_events(self.config, client=FakeCarsClient(events=[make_non_dict_raw_entry()]))
        self.assertEqual(result["deactivated_count"], 0)
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-EXISTING").source_active)
        self.assertEqual(result["outcome"], "partial")

    def test_identity_untrustworthy_still_imports_other_valid_events(self):
        result = sync_events(self.config, client=FakeCarsClient(events=[
            make_unparseable_event(), make_event("CARS5-NEW-AND-VALID"),
        ]))
        self.assertEqual(result["created_count"], 1)
        self.assertTrue(RoadEvent.objects.filter(external_id="CARS5-NEW-AND-VALID").exists())
        self.assertEqual(result["deactivated_count"], 0)

    def test_identity_untrustworthy_does_not_block_rescoping(self):
        # Rescoping only touches ids we DID successfully identify --
        # an unrelated bad-identity record elsewhere in the response
        # must not stop it. CARS5-EXISTING goes out of scope (county
        # narrowed to something else) in the SAME run as a bad-identity
        # record; the rescope must still happen.
        self.config.counties = "Sedgwick"
        self.config.save()
        result = sync_events(self.config, client=FakeCarsClient(events=[
            make_unparseable_event(), make_event("CARS5-EXISTING", county="Ottawa"),
        ]))
        obj = RoadEvent.objects.get(external_id="CARS5-EXISTING")
        self.assertFalse(obj.in_scope, "rescoping must still work despite the unrelated bad-identity record")
        self.assertTrue(obj.source_active, "but deactivation must still be disabled for the whole run")
        self.assertEqual(result["deactivated_count"], 0)

    def test_identity_error_does_not_disable_deactivation_on_a_clean_run(self):
        # Sanity/regression check -- a run with NO identity problems
        # must still deactivate normally (this whole mechanism must
        # not become a blanket "deactivation never happens" bug).
        result = sync_events(self.config, client=FakeCarsClient(events=[]))
        self.assertEqual(result["deactivated_count"], 1)
        self.assertFalse(RoadEvent.objects.get(external_id="CARS5-EXISTING").source_active)

    def test_dry_run_identity_untrustworthy_predicts_zero_deactivation(self):
        result = sync_events(self.config, client=FakeCarsClient(events=[make_unparseable_event()]), dry_run=True)
        self.assertEqual(result["deactivated_count"], 0)
        self.assertTrue(RoadEvent.objects.get(external_id="CARS5-EXISTING").source_active)


class DuplicateEventIdTests(TestCase):
    """Item 1: duplicate event-ids within ONE response must be handled
    deterministically (first occurrence wins) and logged -- not
    silently processed as two independent writes, which would
    otherwise raise a real IntegrityError on RoadEvent.external_id's
    uniqueness constraint during the write phase."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        self.config.counties = ""
        self.config.save()

    def test_duplicate_new_event_does_not_raise_integrity_error(self):
        # Regression test for the exact latent bug: two raw records
        # sharing one event-id, neither pre-existing locally. Before
        # the fix, both would reach RoadEvent.objects.create() inside
        # the same transaction and the second would violate the
        # unique constraint, aborting the whole sync.
        first = make_event("CARS5-DUPE", description="version A")
        second = make_event("CARS5-DUPE", description="version B")
        result = sync_events(self.config, client=FakeCarsClient(events=[first, second]))  # must not raise
        self.assertEqual(RoadEvent.objects.filter(external_id="CARS5-DUPE").count(), 1)
        self.assertEqual(result["created_count"], 1)

    def test_first_occurrence_wins_deterministically(self):
        first = make_event("CARS5-DUPE", description="version A -- first, should win")
        second = make_event("CARS5-DUPE", description="version B -- second, should be skipped")
        sync_events(self.config, client=FakeCarsClient(events=[first, second]))
        self.assertEqual(RoadEvent.objects.get(external_id="CARS5-DUPE").description, "version A -- first, should win")

    def test_duplicate_is_counted_as_error_and_marks_partial(self):
        first = make_event("CARS5-DUPE")
        second = make_event("CARS5-DUPE")
        result = sync_events(self.config, client=FakeCarsClient(events=[first, second]))
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["outcome"], "partial")

    def test_duplicate_against_an_existing_row_also_first_wins(self):
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-DUPE", description="original")]))
        first = make_event("CARS5-DUPE", description="update A -- first this run")
        second = make_event("CARS5-DUPE", description="update B -- second this run")
        sync_events(self.config, client=FakeCarsClient(events=[first, second]))
        self.assertEqual(RoadEvent.objects.get(external_id="CARS5-DUPE").description, "update A -- first this run")

    def test_three_way_duplicate_still_only_creates_one_row(self):
        events = [make_event("CARS5-TRIPLE", description=f"copy {i}") for i in range(3)]
        result = sync_events(self.config, client=FakeCarsClient(events=events))
        self.assertEqual(RoadEvent.objects.filter(external_id="CARS5-TRIPLE").count(), 1)
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(result["error_count"], 2)  # two duplicates skipped

    def test_duplicate_does_not_disable_deactivation_on_its_own(self):
        # Unlike a missing/invalid identity, a plain duplicate of an
        # otherwise-valid id doesn't make the complete-id set
        # untrustworthy -- it's extra information about a known id, not
        # missing information. A genuinely different, absent event
        # must still be correctly deactivated in the same run.
        sync_events(self.config, client=FakeCarsClient(events=[make_event("CARS5-WILLVANISH")]))
        result = sync_events(self.config, client=FakeCarsClient(events=[
            make_event("CARS5-DUPE"), make_event("CARS5-DUPE"),
        ]))
        self.assertEqual(result["deactivated_count"], 1)
        self.assertFalse(RoadEvent.objects.get(external_id="CARS5-WILLVANISH").source_active)


class ExistingScopeSemanticsUnchangedTests(TestCase):
    """additional_route_coverage regression guard, part 1: the
    pre-existing counties/routes AND-combined behavior must be exactly
    unchanged now that _matches_scope() also considers corridor rules."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()

    def test_county_only_configuration_still_includes_all_matching_routes(self):
        self.config.counties = "Ottawa"
        self.config.routes = ""
        self.config.save()
        client = FakeCarsClient(events=[
            make_event("CARS5-A", county="Ottawa", route="KS 18"),
            make_event("CARS5-B", county="Ottawa", route="US 81"),
        ])
        result = sync_events(self.config, client=client)
        self.assertEqual(result["relevant_count"], 2)

    def test_route_only_configuration_still_matches_without_county_restriction(self):
        self.config.counties = ""
        self.config.routes = "US 81"
        self.config.save()
        client = FakeCarsClient(events=[
            make_event("CARS5-A", county="Sedgwick", route="US 81"),
            make_event("CARS5-B", county="Ottawa", route="US 81"),
        ])
        result = sync_events(self.config, client=client)
        self.assertEqual(result["relevant_count"], 2)

    def test_county_and_route_configuration_still_uses_and_semantics(self):
        self.config.counties = "Ottawa"
        self.config.routes = "US 81"
        self.config.save()
        client = FakeCarsClient(events=[
            make_event("CARS5-MATCH", county="Ottawa", route="US 81"),
            make_event("CARS5-WRONG-ROUTE", county="Ottawa", route="KS 18"),
            make_event("CARS5-WRONG-COUNTY", county="Saline", route="US 81"),
        ])
        result = sync_events(self.config, client=client)
        self.assertEqual(result["relevant_count"], 1)
        self.assertTrue(RoadEvent.objects.filter(external_id="CARS5-MATCH").exists())

    def test_blank_additional_route_coverage_is_identical_to_current_behavior(self):
        self.config.counties = "Ottawa"
        self.config.routes = ""
        self.config.additional_route_coverage = ""
        self.config.save()
        client = FakeCarsClient(events=[
            make_event("CARS5-IN", county="Ottawa", route="KS 18"),
            make_event("CARS5-OUT", county="Saline", route="US 81"),
        ])
        result = sync_events(self.config, client=client)
        self.assertEqual(result["relevant_count"], 1)
        self.assertTrue(RoadEvent.objects.filter(external_id="CARS5-IN").exists())
        self.assertFalse(RoadEvent.objects.filter(external_id="CARS5-OUT").exists())


class AdditionalRouteCoverageScopeTests(TestCase):
    """The immediate real-world configuration from the feature request:
    counties=Ottawa, routes=blank, additional_route_coverage=
    'US 81: Saline,Cloud' -- verifying every INCLUDE/EXCLUDE case
    called out explicitly."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        self.config.counties = "Ottawa"
        self.config.routes = ""
        self.config.additional_route_coverage = "US 81: Saline,Cloud"
        self.config.save()

    def _relevant(self, event_id, county, route):
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event(event_id, county=county, route=route)]))
        return result["relevant_count"] == 1 and RoadEvent.objects.filter(external_id=event_id).exists()

    def test_ottawa_ks18_included(self):
        self.assertTrue(self._relevant("CARS5-1", "Ottawa", "KS 18"))

    def test_ottawa_us81_included_via_normal_county_coverage(self):
        self.assertTrue(self._relevant("CARS5-2", "Ottawa", "US 81"))

    def test_saline_us81_included_via_corridor(self):
        self.assertTrue(self._relevant("CARS5-3", "Saline", "US 81"))

    def test_cloud_us81_included_via_corridor(self):
        self.assertTrue(self._relevant("CARS5-4", "Cloud", "US 81"))

    def test_saline_i70_excluded(self):
        self.assertFalse(self._relevant("CARS5-5", "Saline", "I-70"))

    def test_saline_ks140_excluded(self):
        self.assertFalse(self._relevant("CARS5-6", "Saline", "KS 140"))

    def test_cloud_us24_excluded(self):
        self.assertFalse(self._relevant("CARS5-7", "Cloud", "US 24"))

    def test_cloud_ks9_excluded(self):
        self.assertFalse(self._relevant("CARS5-8", "Cloud", "KS 9"))

    def test_us81_outside_ottawa_saline_cloud_excluded(self):
        self.assertFalse(self._relevant("CARS5-9", "Dickinson", "US 81"))

    def test_all_cases_together_in_one_sync(self):
        # Same scenario as above, but as a single fetch -- proves the
        # per-event outcomes hold simultaneously, not just in isolation.
        events = [
            make_event("CARS5-INC-1", county="Ottawa", route="KS 18"),
            make_event("CARS5-INC-2", county="Ottawa", route="US 81"),
            make_event("CARS5-INC-3", county="Saline", route="US 81"),
            make_event("CARS5-INC-4", county="Cloud", route="US 81"),
            make_event("CARS5-EXC-1", county="Saline", route="I-70"),
            make_event("CARS5-EXC-2", county="Saline", route="KS 140"),
            make_event("CARS5-EXC-3", county="Cloud", route="US 24"),
            make_event("CARS5-EXC-4", county="Cloud", route="KS 9"),
            make_event("CARS5-EXC-5", county="Dickinson", route="US 81"),
        ]
        result = sync_events(self.config, client=FakeCarsClient(events=events))
        self.assertEqual(result["relevant_count"], 4)
        for event_id in ("CARS5-INC-1", "CARS5-INC-2", "CARS5-INC-3", "CARS5-INC-4"):
            self.assertTrue(RoadEvent.objects.filter(external_id=event_id).exists(), event_id)
        for event_id in ("CARS5-EXC-1", "CARS5-EXC-2", "CARS5-EXC-3", "CARS5-EXC-4", "CARS5-EXC-5"):
            self.assertFalse(RoadEvent.objects.filter(external_id=event_id).exists(), event_id)


class MultipleCorridorRulesTests(TestCase):
    """Two independent corridor rules -- each must be evaluated on its
    own; a route matching one rule's counties must not leak coverage
    into an unrelated rule/route/county combination."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        # A county that appears on none of these test events -- blank
        # counties means NO county restriction at all (matches
        # everything), so isolating corridor behavior from normal
        # county/route coverage requires a normal filter that's
        # guaranteed to never match, not an empty one.
        self.config.counties = "Nonexistent County"
        self.config.routes = ""
        self.config.additional_route_coverage = "US 81: Saline,Cloud\nI-70: Dickinson"
        self.config.save()

    def _relevant(self, event_id, county, route):
        result = sync_events(self.config, client=FakeCarsClient(events=[make_event(event_id, county=county, route=route)]))
        return result["relevant_count"] == 1

    def test_us81_saline_included(self):
        self.assertTrue(self._relevant("CARS5-1", "Saline", "US 81"))

    def test_us81_cloud_included(self):
        self.assertTrue(self._relevant("CARS5-2", "Cloud", "US 81"))

    def test_i70_dickinson_included(self):
        self.assertTrue(self._relevant("CARS5-3", "Dickinson", "I-70"))

    def test_i70_saline_excluded(self):
        # I-70's rule only lists Dickinson -- Saline must not leak in
        # just because Saline is a county on the OTHER rule (US 81's).
        self.assertFalse(self._relevant("CARS5-4", "Saline", "I-70"))

    def test_us81_dickinson_excluded(self):
        # Symmetric case: US 81's rule only lists Saline/Cloud --
        # Dickinson must not leak in from I-70's rule.
        self.assertFalse(self._relevant("CARS5-5", "Dickinson", "US 81"))

    def test_unrelated_route_and_county_excluded(self):
        self.assertFalse(self._relevant("CARS5-6", "Sedgwick", "KS 4"))


class MultiRouteMultiCountyEventScopeTests(TestCase):
    """A real CARS event can carry multiple routes/counties at once
    (RoadEvent.routes/counties, populated from EVERY detail location --
    see services.all_routes()). A corridor rule must match if ANY of
    the event's routes matches the rule's route AND ANY of the event's
    counties is in the rule's county set -- never relying only on
    primary_route (the first location) or a single county."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        # Same reasoning as MultipleCorridorRulesTests.setUp -- blank
        # counties means unrestricted, so use a never-matching sentinel
        # to actually isolate corridor behavior.
        self.config.counties = "Nonexistent County"
        self.config.routes = ""
        self.config.additional_route_coverage = "US 81: Saline,Cloud"
        self.config.save()

    def test_matches_when_us81_is_a_secondary_route_not_primary(self):
        # KS 18 is the FIRST (primary) location; US 81 is a second,
        # later one on the same event -- primary_route alone would miss
        # this, RoadEvent.routes must not.
        raw = make_multi_location_event("CARS5-1", counties=["Saline"], routes=["KS 18", "US 81"])
        result = sync_events(self.config, client=FakeCarsClient(events=[raw]))
        self.assertEqual(result["relevant_count"], 1)

    def test_matches_when_matching_county_is_not_the_first_county(self):
        raw = make_multi_location_event("CARS5-2", counties=["Sedgwick", "Cloud"], routes=["US 81"])
        result = sync_events(self.config, client=FakeCarsClient(events=[raw]))
        self.assertEqual(result["relevant_count"], 1)

    def test_excluded_when_no_county_matches_despite_route_matching(self):
        raw = make_multi_location_event("CARS5-3", counties=["Sedgwick", "Dickinson"], routes=["US 81"])
        result = sync_events(self.config, client=FakeCarsClient(events=[raw]))
        self.assertEqual(result["relevant_count"], 0)

    def test_excluded_when_no_route_matches_despite_county_matching(self):
        raw = make_multi_location_event("CARS5-4", counties=["Saline"], routes=["KS 18", "I-70"])
        result = sync_events(self.config, client=FakeCarsClient(events=[raw]))
        self.assertEqual(result["relevant_count"], 0)


class KnownLimitationEventLevelCorridorMatchingTests(TestCase):
    """Documents a deliberate, accepted limitation -- NOT a bug --
    raised in review: corridor matching checks `route in event.routes`
    and `event.counties & rule.counties` independently against an
    event's COMPLETE routes/counties sets, not a verified route-to-
    county pairing for one specific segment. This is not a shortcut
    taken for convenience -- the source CARS API genuinely has no such
    pairing to check against:

      * confirmed via the live API's own swagger schema
        (scratchpad/road_conditions/cars-api-swagger.json, not
        committed): `counties` is a top-level CARSEvent property,
        sibling to `geometry`/`recipients`/etc; neither the `Detail`
        schema nor any `Location` variant (`LinkLocation`/`GeoLocation`/
        `RestAreaLocation`) has a county field at all;
      * confirmed via normalize_event()'s own extraction:
        RoadEvent.locations stores location_type/route_designator/
        direction/latitude/longitude per location -- deliberately no
        county, because the source never provides one to store.

    So a constructed event with routes={"US 81","KS 9"} and
    counties={"Ottawa","Cloud"} matches a "US 81: Cloud" rule below
    even though, hypothetically, the real-world correspondence could
    be US 81<->Ottawa and KS 9<->Cloud -- _matches_scope() has no way
    to tell the two apart and does not claim to (see its own
    docstring's "KNOWN LIMITATION" section).

    Per a real 231-event live capture (2026-08-04 reconnaissance,
    scratchpad/road_conditions/live_probes/events_all.json, not
    committed): 44 events had multiple counties (a single route
    spanning a county line -- no ambiguity, since there's only one
    route), but ZERO had more than one distinct route. This gap is
    real per the schema but has not been observed in practice."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        self.config.counties = "Nonexistent County"
        self.config.routes = ""
        self.config.additional_route_coverage = "US 81: Cloud"
        self.config.save()

    def test_documents_cross_route_county_match_despite_no_verified_pairing(self):
        # US 81 and Cloud both appear somewhere on this event -- current,
        # documented behavior matches it, even though the fixture makes
        # no claim (and the source data COULD NOT make a claim) that the
        # US 81 segment specifically is the one located in Cloud County.
        raw = make_multi_location_event("CARS5-AMBIGUOUS", counties=["Ottawa", "Cloud"], routes=["US 81", "KS 9"])
        result = sync_events(self.config, client=FakeCarsClient(events=[raw]))
        self.assertEqual(result["relevant_count"], 1)
        self.assertTrue(RoadEvent.objects.filter(external_id="CARS5-AMBIGUOUS").exists())

    def test_documents_match_even_when_hypothetical_true_pairing_would_be_the_other_way(self):
        # Same event shape, but with the routes/counties order reversed
        # in the source lists -- proves the match doesn't depend on
        # position/order (there's no per-position pairing to depend on).
        raw = make_multi_location_event("CARS5-REVERSED", counties=["Cloud", "Ottawa"], routes=["KS 9", "US 81"])
        result = sync_events(self.config, client=FakeCarsClient(events=[raw]))
        self.assertEqual(result["relevant_count"], 1)


class CorridorMatchStillObeysGlobalFiltersTests(TestCase):
    """A corridor rule only ever widens GEOGRAPHIC scope -- it must
    never exempt a matched event from min_priority/max_event_age_days/
    lookahead_days."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        self.config.counties = "Ottawa"
        self.config.routes = ""
        self.config.additional_route_coverage = "US 81: Saline,Cloud"
        self.config.save()

    def test_corridor_match_still_excluded_by_min_priority(self):
        self.config.min_priority = 8
        self.config.save()
        low_priority = make_event("CARS5-LOW", county="Saline", route="US 81", priority=3)
        result = sync_events(self.config, client=FakeCarsClient(events=[low_priority]))
        self.assertEqual(result["relevant_count"], 0)

    def test_corridor_match_included_when_priority_is_sufficient(self):
        self.config.min_priority = 8
        self.config.save()
        high_priority = make_event("CARS5-HIGH", county="Saline", route="US 81", priority=9)
        result = sync_events(self.config, client=FakeCarsClient(events=[high_priority]))
        self.assertEqual(result["relevant_count"], 1)

    def test_corridor_match_still_excluded_by_max_event_age_days(self):
        self.config.max_event_age_days = 3
        self.config.save()
        old_event = make_event("CARS5-OLD", county="Cloud", route="US 81", end_days_from_now=-30)
        result = sync_events(self.config, client=FakeCarsClient(events=[old_event]))
        self.assertEqual(result["relevant_count"], 0)

    def test_corridor_match_still_excluded_by_lookahead_days(self):
        self.config.lookahead_days = 30
        self.config.save()
        far_future = make_event("CARS5-FAR", county="Cloud", route="US 81", start_days_from_now=365)
        result = sync_events(self.config, client=FakeCarsClient(events=[far_future]))
        self.assertEqual(result["relevant_count"], 0)


class MalformedAdditionalRouteCoverageDoesNotBroadenSyncTests(TestCase):
    """Belt-and-suspenders alongside AdditionalRouteCoverageParsingTests
    (test_config.py) -- proves malformed corridor text never widens
    matching during an actual sync, even if it somehow got saved
    without going through RoadConditionsConfiguration.clean() (e.g.
    direct ORM/shell access, which does not call full_clean())."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        self.config.counties = "Ottawa"
        self.config.routes = ""

    def test_bare_route_with_no_colon_matches_nothing_statewide(self):
        self.config.additional_route_coverage = "US 81"
        self.config.save()
        client = FakeCarsClient(events=[make_event("CARS5-1", county="Sedgwick", route="US 81")])
        result = sync_events(self.config, client=client)
        self.assertEqual(result["relevant_count"], 0)

    def test_county_only_line_matches_no_route(self):
        self.config.additional_route_coverage = ": Saline"
        self.config.save()
        client = FakeCarsClient(events=[make_event("CARS5-1", county="Saline", route="KS 4")])
        result = sync_events(self.config, client=client)
        self.assertEqual(result["relevant_count"], 0)
