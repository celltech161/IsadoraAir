"""Roadmap 2.5D seed-data closeout tests."""
from django.test import TestCase

from authz.models import Capability, Role


class StationAdministratorSeedTests(TestCase):
    def test_station_administrator_contains_the_complete_capability_vocabulary(self):
        station_administrator = Role.objects.get(name="Station Administrator")
        self.assertSetEqual(
            set(station_administrator.capabilities.values_list("slug", flat=True)),
            set(Capability.objects.values_list("slug", flat=True)),
        )

    def test_station_administrator_receives_queue_monitoring_and_aircheck_closeout_capabilities(self):
        station_administrator = Role.objects.get(name="Station Administrator")
        self.assertTrue(
            station_administrator.capabilities.filter(slug="playout.queue_manage").exists()
        )
        self.assertTrue(
            station_administrator.capabilities.filter(
                slug="monitoring.reset_listener_counters"
            ).exists()
        )
        self.assertTrue(
            station_administrator.capabilities.filter(slug="aircheck.control").exists()
        )

    def test_remote_host_does_not_receive_monitoring_or_aircheck_administration(self):
        remote_host = Role.objects.get(name="Remote Host")
        self.assertFalse(
            remote_host.capabilities.filter(
                slug__in=("monitoring.reset_listener_counters", "aircheck.control")
            ).exists()
        )
