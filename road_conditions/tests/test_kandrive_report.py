"""road_conditions/report.py tests: event selection (source_active/
in_scope and its exclusions), severity ordering, listener-facing script
wording (closure/restriction/construction/planned/detour), missing
optional fields, multi-county/multi-route formatting, feed_freshness's
four states, and ReportBuildError isolation. Uses real RoadEvent model
instances built by a small local factory -- no CARS API fixtures needed
here (those belong to services.py/normalize_event's own tests); this
module operates entirely on already-normalized RoadEvent rows."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone as dj_timezone

from road_conditions.models import RoadConditionsConfiguration, RoadEvent
from road_conditions.report import (
    ReportBuildError,
    build_event_script,
    build_event_scripts,
    build_full_report,
    build_no_events_message,
    compose_report_script,
    compose_report_segments,
    feed_freshness,
    has_detour,
    select_events,
)


def make_road_event(external_id="CARS5-TEST-1", headline_category="roadwork",
                     description="Test event description.", priority=5,
                     counties=None, routes=None, primary_route=None,
                     source_active=True, in_scope=True, start_time=None,
                     raw_payload=None, now=None):
    now = now or dj_timezone.now()
    if routes is None:
        routes = [primary_route] if primary_route else ["US 81"]
    resolved_primary_route = primary_route if primary_route is not None else (routes[0] if routes else "")
    return RoadEvent.objects.create(
        external_id=external_id,
        headline_category=headline_category,
        description=description,
        priority=priority,
        counties=counties if counties is not None else ["Ottawa"],
        primary_route=resolved_primary_route,
        routes=routes,
        start_time=start_time,
        last_seen_at=now,
        source_active=source_active,
        in_scope=in_scope,
        raw_payload=raw_payload if raw_payload is not None else {},
        payload_checksum="test-checksum",
    )


def make_detour_payload():
    """Structurally-real shape of the one fact has_detour() looks for
    -- a DetourDescription anywhere in details[].descriptions[]."""
    return {
        "details": [
            {"descriptions": [{"description-type": "DetourDescription", "description": "irrelevant text"}]},
        ],
    }


class EventSelectionTests(TestCase):
    def test_source_active_and_in_scope_are_selected(self):
        make_road_event(external_id="A", source_active=True, in_scope=True)
        events = select_events()
        self.assertEqual([e.external_id for e in events], ["A"])

    def test_source_inactive_excluded(self):
        make_road_event(external_id="A", source_active=False, in_scope=True)
        self.assertEqual(select_events(), [])

    def test_out_of_scope_excluded(self):
        make_road_event(external_id="A", source_active=True, in_scope=False)
        self.assertEqual(select_events(), [])

    def test_both_flags_false_excluded(self):
        make_road_event(external_id="A", source_active=False, in_scope=False)
        self.assertEqual(select_events(), [])

    def test_device_status_headline_category_excluded(self):
        make_road_event(external_id="A", headline_category="device-status")
        self.assertEqual(select_events(), [])

    def test_blank_description_excluded(self):
        make_road_event(external_id="A", description="")
        self.assertEqual(select_events(), [])

    def test_other_headline_categories_included(self):
        for i, category in enumerate(["closure", "roadwork", "restriction", "warning", "mobile-situation", "special-event"]):
            make_road_event(external_id=f"CAT-{i}", headline_category=category)
        self.assertEqual(len(select_events()), 6)


class SeverityOrderingTests(TestCase):
    def test_closure_before_serious_before_construction_before_planned(self):
        now = dj_timezone.now()
        make_road_event(external_id="planned", headline_category="roadwork", priority=9,
                         start_time=now + timedelta(days=10))
        make_road_event(external_id="routine-construction", headline_category="roadwork", priority=1)
        make_road_event(external_id="significant-construction", headline_category="roadwork", priority=8)
        make_road_event(external_id="serious", headline_category="warning", priority=1)
        make_road_event(external_id="closure", headline_category="closure", priority=1)

        ordered = [e.external_id for e in select_events(now)]
        self.assertEqual(ordered, [
            "closure", "serious", "significant-construction", "routine-construction", "planned",
        ])

    def test_priority_breaks_ties_within_a_tier(self):
        make_road_event(external_id="low", headline_category="closure", priority=2)
        make_road_event(external_id="high", headline_category="closure", priority=9)
        ordered = [e.external_id for e in select_events()]
        self.assertEqual(ordered, ["high", "low"])

    def test_planned_detection_uses_future_start_time_only(self):
        now = dj_timezone.now()
        make_road_event(external_id="past-start", headline_category="roadwork", priority=5,
                         start_time=now - timedelta(days=1))
        make_road_event(external_id="future-start", headline_category="roadwork", priority=5,
                         start_time=now + timedelta(days=1))
        make_road_event(external_id="no-start", headline_category="roadwork", priority=5, start_time=None)
        ordered = [e.external_id for e in select_events(now)]
        # Planned (future-start) always sorts last regardless of priority tie.
        self.assertEqual(ordered[-1], "future-start")
        self.assertIn("past-start", ordered[:-1])
        self.assertIn("no-start", ordered[:-1])


class ScriptWordingTests(TestCase):
    def test_current_event_has_no_planned_prefix(self):
        event = make_road_event(description="Road closed for construction.", start_time=None)
        script = build_event_script(event)
        self.assertNotIn("planned work", script)
        self.assertIn("KDOT reports:", script)
        self.assertIn("Road closed for construction.", script)

    def test_planned_event_has_planned_prefix_with_date(self):
        now = dj_timezone.now()
        future = now + timedelta(days=5)
        event = make_road_event(description="Alternating lane closures.", start_time=future)
        script = build_event_script(event, now)
        self.assertIn("this is planned work:", script)
        self.assertIn(future.strftime("%B"), script)

    def test_detour_presence_appends_motorist_line(self):
        event = make_road_event(description="Road closed.", raw_payload=make_detour_payload())
        self.assertTrue(has_detour(event))
        script = build_event_script(event)
        self.assertIn("Motorists should use the posted detour.", script)

    def test_no_detour_no_motorist_line(self):
        event = make_road_event(description="Road closed.", raw_payload={})
        self.assertFalse(has_detour(event))
        script = build_event_script(event)
        self.assertNotIn("posted detour", script)

    def test_route_and_county_lead_in(self):
        event = make_road_event(description="Construction work.", primary_route="US 81",
                                 routes=["US 81"], counties=["Ottawa"])
        script = build_event_script(event)
        self.assertTrue(script.startswith("On U.S. 81, in Ottawa County, KDOT reports:"))

    def test_missing_route_and_county_still_builds_gracefully(self):
        event = make_road_event(description="Construction work.", primary_route="",
                                 routes=[], counties=[])
        script = build_event_script(event)
        self.assertNotIn(" On , in , ", script)
        self.assertNotIn(", , KDOT", script)
        self.assertTrue(script.startswith("KDOT reports:"))
        self.assertIn("Construction work.", script)

    def test_missing_county_only(self):
        event = make_road_event(description="Construction work.", primary_route="US 81",
                                 routes=["US 81"], counties=[])
        script = build_event_script(event)
        self.assertTrue(script.startswith("On U.S. 81, KDOT reports:"))

    def test_route_normalization_applied_in_lead_in(self):
        event = make_road_event(description="Bridge work.", primary_route="I-70", routes=["I-70"])
        script = build_event_script(event)
        self.assertIn("On Interstate 70,", script)


class MultiCountyMultiRouteTests(TestCase):
    def test_two_counties_worded_with_and(self):
        event = make_road_event(counties=["Saline", "McPherson"])
        script = build_event_script(event)
        self.assertIn("in Saline and McPherson counties", script)

    def test_three_plus_counties_oxford_comma_list(self):
        event = make_road_event(counties=["Ottawa", "Dickinson", "Clay"])
        script = build_event_script(event)
        self.assertIn("in Ottawa, Dickinson, and Clay counties", script)

    def test_single_county_singular_wording(self):
        event = make_road_event(counties=["Saline"])
        script = build_event_script(event)
        self.assertIn("in Saline County,", script)
        self.assertNotIn("counties", script)

    def test_two_routes_joined_with_and(self):
        event = make_road_event(primary_route="KS 4", routes=["KS 4", "KS 140"])
        script = build_event_script(event)
        self.assertIn("On Kansas Highway 4 and Kansas Highway 140,", script)


class ReportBuildErrorTests(TestCase):
    def test_individual_event_failure_names_the_event_and_raises(self):
        good = make_road_event(external_id="GOOD-1", description="Fine.")
        bad = make_road_event(external_id="BAD-1", description="Also fine.")
        # Force a failure specifically attributable to the "bad" event
        # without needing a genuinely malformed row -- an int isn't
        # iterable, so _format_counties's `for c in (counties or [])`
        # raises a TypeError building this one event's script.
        RoadEvent.objects.filter(external_id="BAD-1").update(counties=123)
        bad.refresh_from_db()

        with self.assertRaises(ReportBuildError) as ctx:
            build_full_report([good, bad])
        self.assertIn("BAD-1", str(ctx.exception))

    def test_no_failure_produces_normal_joined_report(self):
        a = make_road_event(external_id="A", headline_category="closure", description="Closed.")
        b = make_road_event(external_id="B", headline_category="roadwork", description="Construction.")
        report = build_full_report([a, b])
        self.assertIn("Closed.", report)
        self.assertIn("Construction.", report)


class NoEventsMessageTests(TestCase):
    def test_no_events_message_is_static_and_non_alarming(self):
        message = build_no_events_message()
        self.assertIn("not reporting any significant road conditions", message)


class BuildEventScriptsTests(TestCase):
    """build_event_scripts() -- the per-event LIST build_full_report()
    is now a thin wrapper around, exposed separately for callers that
    need real audio-segment boundaries (e.g. inserting a transition
    sound effect between items -- see synthesis.py)."""

    def test_returns_one_script_per_event_in_order(self):
        a = make_road_event(external_id="A", headline_category="closure", description="Closed.")
        b = make_road_event(external_id="B", headline_category="roadwork", description="Construction.")
        scripts = build_event_scripts([a, b])
        self.assertEqual(len(scripts), 2)
        self.assertIn("Closed.", scripts[0])
        self.assertIn("Construction.", scripts[1])

    def test_empty_events_returns_empty_list(self):
        self.assertEqual(build_event_scripts([]), [])

    def test_equivalent_to_build_full_report_when_joined(self):
        a = make_road_event(external_id="A", headline_category="closure", description="Closed.")
        b = make_road_event(external_id="B", headline_category="roadwork", description="Construction.")
        now = dj_timezone.now()
        self.assertEqual(" ".join(build_event_scripts([a, b], now)), build_full_report([a, b], now))

    def test_individual_event_failure_names_the_event_and_raises(self):
        good = make_road_event(external_id="GOOD-2", description="Fine.")
        bad = make_road_event(external_id="BAD-2", description="Also fine.")
        RoadEvent.objects.filter(external_id="BAD-2").update(counties=123)
        bad.refresh_from_db()

        with self.assertRaises(ReportBuildError) as ctx:
            build_event_scripts([good, bad])
        self.assertIn("BAD-2", str(ctx.exception))


class ComposeReportSegmentsTests(TestCase):
    """compose_report_segments() -- the per-item audio-segment-list
    counterpart to compose_report_script(), used by the transition-
    sound path (synthesis.py's synthesize_road_report(segments=...))."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()

    def test_preamble_folded_into_first_segment_only(self):
        self.config.report_preamble = "PREAMBLE."
        self.config.report_postamble = ""
        segments = compose_report_segments(["one", "two", "three"], self.config)
        self.assertEqual(segments, ["PREAMBLE. one", "two", "three"])

    def test_postamble_folded_into_last_segment_only(self):
        self.config.report_preamble = ""
        self.config.report_postamble = "POSTAMBLE."
        segments = compose_report_segments(["one", "two", "three"], self.config)
        self.assertEqual(segments, ["one", "two", "three POSTAMBLE."])

    def test_both_preamble_and_postamble(self):
        self.config.report_preamble = "PRE."
        self.config.report_postamble = "POST."
        segments = compose_report_segments(["one", "two", "three"], self.config)
        self.assertEqual(segments, ["PRE. one", "two", "three POST."])

    def test_segment_count_matches_input_when_both_blank(self):
        self.config.report_preamble = ""
        self.config.report_postamble = ""
        segments = compose_report_segments(["one", "two", "three"], self.config)
        self.assertEqual(segments, ["one", "two", "three"])

    def test_announcer_name_substitution(self):
        self.config.report_preamble = ""
        self.config.report_postamble = "I'm {announcer_name}."
        segments = compose_report_segments(["one", "two"], self.config, announcer_name="Claira Sky")
        self.assertEqual(segments[-1], "two I'm Claira Sky.")

    def test_single_item_with_framing_produces_single_segment(self):
        # Exactly the case that must NOT get a transition sound
        # inserted -- there's no second item to insert one before.
        self.config.report_preamble = "PRE."
        self.config.report_postamble = "POST."
        segments = compose_report_segments(["only item"], self.config)
        self.assertEqual(segments, ["PRE. only item POST."])

    def test_empty_body_pieces_with_framing(self):
        self.config.report_preamble = "PRE."
        self.config.report_postamble = "POST."
        segments = compose_report_segments([], self.config)
        self.assertEqual(segments, ["PRE. POST."])

    def test_empty_body_pieces_no_framing_returns_empty_list(self):
        self.config.report_preamble = ""
        self.config.report_postamble = ""
        self.assertEqual(compose_report_segments([], self.config), [])

    def test_joined_result_matches_compose_report_script(self):
        self.config.report_preamble = "PRE {announcer_name}."
        self.config.report_postamble = "POST {announcer_name}."
        pieces = ["alpha", "beta", "gamma"]
        segments = compose_report_segments(pieces, self.config, announcer_name="Max Weatherly")
        single = compose_report_script(" ".join(pieces), self.config, announcer_name="Max Weatherly")
        self.assertEqual(" ".join(segments), single)


class FeedFreshnessTests(TestCase):
    def test_disabled(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = False
        config.save()
        self.assertEqual(feed_freshness(config), "disabled")

    def test_failed(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.last_error = "CARS API timed out"
        config.last_fetch_succeeded_at = dj_timezone.now()
        config.save()
        self.assertEqual(feed_freshness(config), "failed")

    def test_stale(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.last_error = ""
        config.last_fetch_succeeded_at = dj_timezone.now() - timedelta(days=3)
        config.stale_data_threshold_minutes = 60
        config.save()
        self.assertEqual(feed_freshness(config), "stale")

    def test_fresh(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.last_error = ""
        config.last_fetch_succeeded_at = dj_timezone.now()
        config.stale_data_threshold_minutes = 60
        config.save()
        self.assertEqual(feed_freshness(config), "fresh")

    def test_disabled_takes_precedence_over_stale(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = False
        config.last_error = ""
        config.last_fetch_succeeded_at = dj_timezone.now() - timedelta(days=30)
        config.save()
        self.assertEqual(feed_freshness(config), "disabled")

    def test_failed_takes_precedence_over_stale(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.last_error = "boom"
        config.last_fetch_succeeded_at = dj_timezone.now() - timedelta(days=30)
        config.save()
        self.assertEqual(feed_freshness(config), "failed")


class ComposeReportScriptTests(TestCase):
    """compose_report_script() -- station framing assembly, kept
    entirely separate from build_event_script()/build_full_report()/
    build_no_events_message(), which know nothing about it (see
    report.py's own module docstring and compose_report_script's)."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()

    def test_preamble_appears_before_the_body(self):
        self.config.report_preamble = "PREAMBLE TEXT."
        self.config.report_postamble = ""
        result = compose_report_script("BODY TEXT.", self.config)
        self.assertTrue(result.startswith("PREAMBLE TEXT."))
        self.assertLess(result.index("PREAMBLE TEXT."), result.index("BODY TEXT."))

    def test_postamble_appears_after_the_body(self):
        self.config.report_preamble = ""
        self.config.report_postamble = "POSTAMBLE TEXT."
        result = compose_report_script("BODY TEXT.", self.config)
        self.assertTrue(result.endswith("POSTAMBLE TEXT."))
        self.assertLess(result.index("BODY TEXT."), result.index("POSTAMBLE TEXT."))

    def test_announcer_name_resolves_in_postamble(self):
        self.config.report_preamble = ""
        self.config.report_postamble = "I'm {announcer_name}."
        result = compose_report_script("BODY.", self.config, announcer_name="Claira")
        self.assertIn("I'm Claira.", result)
        self.assertNotIn("{announcer_name}", result)

    def test_announcer_name_resolves_in_preamble_too(self):
        # Not just the postamble -- the config help text doesn't scope
        # the token to one field, so an operator using it in the
        # preamble (e.g. "Hi, I'm {announcer_name} with...") must work
        # identically.
        self.config.report_preamble = "Hi, I'm {announcer_name}."
        self.config.report_postamble = ""
        result = compose_report_script("BODY.", self.config, announcer_name="Max")
        self.assertIn("Hi, I'm Max.", result)
        self.assertNotIn("{announcer_name}", result)

    def test_blank_preamble_omits_that_piece(self):
        self.config.report_preamble = ""
        self.config.report_postamble = "POSTAMBLE."
        result = compose_report_script("BODY.", self.config)
        self.assertEqual(result, "BODY. POSTAMBLE.")

    def test_blank_postamble_omits_that_piece(self):
        self.config.report_preamble = "PREAMBLE."
        self.config.report_postamble = ""
        result = compose_report_script("BODY.", self.config)
        self.assertEqual(result, "PREAMBLE. BODY.")

    def test_both_blank_produces_just_the_body_no_stray_whitespace(self):
        self.config.report_preamble = ""
        self.config.report_postamble = ""
        result = compose_report_script("BODY.", self.config)
        self.assertEqual(result, "BODY.")
        self.assertFalse(result.startswith(" "))
        self.assertFalse(result.endswith(" "))
        self.assertNotIn("  ", result)

    def test_whitespace_only_fields_treated_as_blank(self):
        self.config.report_preamble = "   \n  "
        self.config.report_postamble = "  "
        result = compose_report_script("BODY.", self.config)
        self.assertEqual(result, "BODY.")

    def test_ordinary_unrelated_braces_pass_through_unchanged(self):
        # Must never be routed through str.format() -- an operator's
        # own text (a typo'd brace, a pasted URL query string, etc.)
        # must never raise or otherwise be treated as a format spec.
        self.config.report_preamble = "Braces like {this} and {0} are just text."
        self.config.report_postamble = ""
        result = compose_report_script("BODY.", self.config)
        self.assertIn("{this}", result)
        self.assertIn("{0}", result)

    def test_unmatched_brace_does_not_raise(self):
        self.config.report_preamble = "An unmatched brace: { oops"
        self.config.report_postamble = "another one: } oops"
        result = compose_report_script("BODY.", self.config)  # must not raise
        self.assertIn("{ oops", result)
        self.assertIn("} oops", result)

    def test_default_config_values_compose_the_documented_example(self):
        # Regression guard on the suggested defaults themselves --
        # RoadConditionsConfiguration.load() returns a fresh singleton
        # with report_preamble/report_postamble already set.
        result = compose_report_script("BODY.", self.config, announcer_name="Claira")
        self.assertTrue(result.startswith("Now here's the current and upcoming road report"))
        self.assertIn("BODY.", result)
        self.assertTrue(result.endswith("I'm Claira."))
