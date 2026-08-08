from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from road_conditions.models import RoadConditionsConfiguration, parse_additional_route_coverage


class RoadConditionsConfigurationSingletonTests(TestCase):
    def test_load_creates_singleton_with_defaults(self):
        config = RoadConditionsConfiguration.load()
        self.assertEqual(config.pk, 1)
        self.assertFalse(config.enabled)
        self.assertEqual(config.api_base_url, "https://kscars.kandrive.gov/carsapi_v1/api")
        # 15 minutes, not 5 -- the live API has no pagination or caching
        # support, so every actual sync re-downloads the complete ~8MB
        # dataset; this default is a deliberate bandwidth/freshness
        # trade-off (see the field's own help_text), not incidental.
        self.assertEqual(config.poll_cadence_minutes, 15)

    def test_load_is_idempotent(self):
        first = RoadConditionsConfiguration.load()
        first.enabled = True
        first.save()
        second = RoadConditionsConfiguration.load()
        self.assertEqual(second.pk, 1)
        self.assertTrue(second.enabled)
        self.assertEqual(RoadConditionsConfiguration.objects.count(), 1)

    def test_save_always_forces_pk_1(self):
        config = RoadConditionsConfiguration(pk=999)
        config.save()
        self.assertEqual(config.pk, 1)
        self.assertEqual(RoadConditionsConfiguration.objects.count(), 1)


class RoadConditionsConfigurationPropertyTests(TestCase):
    def setUp(self):
        self.config = RoadConditionsConfiguration.load()

    def test_event_classifications_list_parses_csv(self):
        self.config.event_classifications = "constructionReports, roadReports ,winterDriving"
        self.assertEqual(self.config.event_classifications_list, ["constructionReports", "roadReports", "winterDriving"])

    def test_event_classifications_list_empty_string(self):
        self.config.event_classifications = ""
        self.assertEqual(self.config.event_classifications_list, [])

    def test_counties_set_parses_csv(self):
        self.config.counties = "Ottawa, Saline,Cloud"
        self.assertEqual(self.config.counties_set, {"Ottawa", "Saline", "Cloud"})

    def test_counties_set_empty_string(self):
        self.config.counties = ""
        self.assertEqual(self.config.counties_set, set())

    def test_routes_set_parses_csv(self):
        self.config.routes = "US 81, KS 15"
        self.assertEqual(self.config.routes_set, {"US 81", "KS 15"})

    def test_default_counties_are_north_central_kansas(self):
        # Same coverage area as AmberAlertConfig's own defaults --
        # regression guard against the two configs silently drifting apart.
        self.assertIn("Ottawa", self.config.counties_set)
        self.assertIn("Saline", self.config.counties_set)


class AdditionalRouteCoverageParsingTests(TestCase):
    """parse_additional_route_coverage() -- pure function, no DB needed."""

    def test_blank_text_produces_no_rules_and_no_errors(self):
        rules, errors = parse_additional_route_coverage("")
        self.assertEqual(rules, {})
        self.assertEqual(errors, [])

    def test_single_rule(self):
        rules, errors = parse_additional_route_coverage("US 81: Saline,Cloud")
        self.assertEqual(rules, {"US 81": {"Saline", "Cloud"}})
        self.assertEqual(errors, [])

    def test_multiple_rules(self):
        text = "US 81: Saline,Cloud\nI-70: Saline,Dickinson\nUS 24: Mitchell,Cloud"
        rules, errors = parse_additional_route_coverage(text)
        self.assertEqual(rules, {
            "US 81": {"Saline", "Cloud"},
            "I-70": {"Saline", "Dickinson"},
            "US 24": {"Mitchell", "Cloud"},
        })
        self.assertEqual(errors, [])

    def test_blank_lines_ignored(self):
        rules, errors = parse_additional_route_coverage("\nUS 81: Saline,Cloud\n\n\n")
        self.assertEqual(rules, {"US 81": {"Saline", "Cloud"}})
        self.assertEqual(errors, [])

    def test_whitespace_around_route_and_counties_is_trimmed(self):
        rules, errors = parse_additional_route_coverage(" US 81 : Saline, Cloud ")
        self.assertEqual(rules, {"US 81": {"Saline", "Cloud"}})
        self.assertEqual(errors, [])

    def test_equivalent_to_tightly_formatted_version(self):
        loose = parse_additional_route_coverage(" US 81 : Saline, Cloud \n")[0]
        tight = parse_additional_route_coverage("US 81: Saline,Cloud")[0]
        self.assertEqual(loose, tight)

    def test_duplicate_route_lines_merge_counties(self):
        text = "US 81: Saline\nUS 81: Cloud"
        rules, errors = parse_additional_route_coverage(text)
        self.assertEqual(rules, {"US 81": {"Saline", "Cloud"}})
        self.assertEqual(errors, [])

    def test_missing_colon_is_malformed_and_produces_no_rule(self):
        rules, errors = parse_additional_route_coverage("US 81")
        self.assertEqual(rules, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("line 1", errors[0])

    def test_empty_route_before_colon_is_malformed_and_produces_no_rule(self):
        rules, errors = parse_additional_route_coverage(": Saline")
        self.assertEqual(rules, {})
        self.assertEqual(len(errors), 1)

    def test_empty_county_list_is_malformed_and_produces_no_rule(self):
        rules, errors = parse_additional_route_coverage("US 81:")
        self.assertEqual(rules, {})
        self.assertEqual(len(errors), 1)

    def test_county_list_of_only_commas_is_malformed(self):
        rules, errors = parse_additional_route_coverage("US 81: , ,")
        self.assertEqual(rules, {})
        self.assertEqual(len(errors), 1)

    def test_one_malformed_line_does_not_block_other_valid_lines(self):
        text = "US 81: Saline,Cloud\nbogus line\nI-70: Dickinson"
        rules, errors = parse_additional_route_coverage(text)
        self.assertEqual(rules, {"US 81": {"Saline", "Cloud"}, "I-70": {"Dickinson"}})
        self.assertEqual(len(errors), 1)

    def test_malformed_line_never_broadens_to_statewide_route_rule(self):
        # "US 81" alone must NOT become "US 81 matches everywhere" --
        # it must simply be dropped.
        rules, _errors = parse_additional_route_coverage("US 81")
        self.assertNotIn("US 81", rules)

    def test_malformed_line_never_broadens_to_all_route_county_rule(self):
        # ": Saline" must NOT become "any route in Saline" -- dropped.
        rules, _errors = parse_additional_route_coverage(": Saline")
        self.assertEqual(rules, {})


class AdditionalRouteCoverageModelTests(TestCase):
    def setUp(self):
        self.config = RoadConditionsConfiguration.load()

    def test_additional_route_coverage_rules_property_parses(self):
        self.config.additional_route_coverage = "US 81: Saline,Cloud"
        self.assertEqual(self.config.additional_route_coverage_rules, {"US 81": {"Saline", "Cloud"}})

    def test_additional_route_coverage_rules_property_blank_by_default(self):
        self.assertEqual(self.config.additional_route_coverage, "")
        self.assertEqual(self.config.additional_route_coverage_rules, {})

    def test_additional_route_coverage_rules_property_silently_drops_malformed_lines(self):
        # Same safety guarantee as parse_additional_route_coverage() itself --
        # the property never raises, and a malformed line never yields a rule,
        # even though clean() (used by admin saves) would reject this text.
        self.config.additional_route_coverage = "US 81"
        self.assertEqual(self.config.additional_route_coverage_rules, {})

    def test_clean_accepts_blank(self):
        self.config.additional_route_coverage = ""
        self.config.clean()  # must not raise

    def test_clean_accepts_well_formed_rules(self):
        self.config.additional_route_coverage = "US 81: Saline,Cloud\nI-70: Dickinson"
        self.config.clean()  # must not raise

    def test_clean_rejects_malformed_rule(self):
        self.config.additional_route_coverage = "US 81"
        with self.assertRaises(ValidationError) as ctx:
            self.config.clean()
        self.assertIn("additional_route_coverage", ctx.exception.message_dict)

    def test_clean_rejects_route_only_colon(self):
        self.config.additional_route_coverage = ": Saline"
        with self.assertRaises(ValidationError):
            self.config.clean()

    def test_full_clean_via_admin_style_save_rejects_malformed_text(self):
        # Mirrors what ModelForm._post_clean does on every admin save
        # (Model.save() alone does NOT call full_clean() -- this is
        # specifically checking the admin-save enforcement path).
        self.config.additional_route_coverage = "US 81"
        with self.assertRaises(ValidationError):
            self.config.full_clean()


class RoadConditionsConfigurationStalenessTests(TestCase):
    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        self.config.stale_data_threshold_minutes = 30

    def test_never_succeeded_is_stale(self):
        self.config.last_fetch_succeeded_at = None
        self.assertTrue(self.config.is_stale)

    def test_recent_success_is_not_stale(self):
        self.config.last_fetch_succeeded_at = timezone.now() - timedelta(minutes=5)
        self.assertFalse(self.config.is_stale)

    def test_old_success_is_stale(self):
        self.config.last_fetch_succeeded_at = timezone.now() - timedelta(minutes=45)
        self.assertTrue(self.config.is_stale)

    def test_exactly_at_threshold_boundary(self):
        self.config.last_fetch_succeeded_at = timezone.now() - timedelta(minutes=31)
        self.assertTrue(self.config.is_stale)
