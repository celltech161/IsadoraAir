from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from road_conditions.models import RoadConditionsConfiguration


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
