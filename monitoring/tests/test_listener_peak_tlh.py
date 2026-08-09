"""Total Listening Hours (TLH) regression coverage -- the running
cumulative listener-hours figure added to ListenerPeak (monitoring/
models.py) alongside its existing peak-listeners tracking, accumulated
by MonitorManager._poll_shoutcast_listeners (monitoring/services/
monitor.py) every POLL_SECONDS tick and surfaced on the dashboard
listener widget next to Listeners/Peak.

fetch_shoutcast_stats is always mocked here -- no live Shoutcast server
involved, matching monitoring/tests/test_probe_encoder_group.py's own
approach for the same underlying fetcher."""
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone as django_tz

import monitoring.services.monitor as monitor_module
from encoders.models import Encoder
from monitoring.models import ListenerPeak


def make_encoder(**overrides):
    defaults = dict(
        name="test-mp3", enabled=True, protocol="shoutcast2", host="192.168.1.112",
        port=8000, mount="/1", password="secret", format="mp3", bitrate_kbps=320,
        station_name="Test Station", genre="Variety", url="https://example.com", public=False,
    )
    defaults.update(overrides)
    return Encoder.objects.create(**defaults)


def _first_of_previous_month(now):
    if now.month == 1:
        return now.replace(year=now.year - 1, month=12, day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.replace(month=now.month - 1, day=1, hour=0, minute=0, second=0, microsecond=0)


class ListenerPeakTlhModelTests(TestCase):
    def test_defaults(self):
        obj = ListenerPeak.load()
        self.assertEqual(obj.tlh_hours, 0.0)
        self.assertIsNotNone(obj.tlh_since_at)


class PollShoutcastListenersTlhTests(TestCase):
    def setUp(self):
        self.encoder = make_encoder()  # mount "/1" -> shoutcast_sid "1"
        self.manager = monitor_module.MonitorManager()

    def _poll(self, listeners, up=True):
        with patch.object(
            monitor_module, "fetch_shoutcast_stats",
            return_value={"1": {"up": up, "listeners": listeners}},
        ):
            self.manager._poll_shoutcast_listeners()

    def test_accumulates_expected_hours_for_one_tick(self):
        self._poll(listeners=36)
        tlh = ListenerPeak.load()
        # 36 listeners x (POLL_SECONDS / 3600) hours.
        expected = 36 * (monitor_module.POLL_SECONDS / 3600.0)
        self.assertAlmostEqual(tlh.tlh_hours, expected, places=9)

    def test_accumulates_across_multiple_ticks_not_overwritten(self):
        self._poll(listeners=10)
        self._poll(listeners=20)
        self._poll(listeners=5)
        tlh = ListenerPeak.load()
        expected = (10 + 20 + 5) * (monitor_module.POLL_SECONDS / 3600.0)
        self.assertAlmostEqual(tlh.tlh_hours, expected, places=9)

    def test_zero_listeners_tick_does_not_change_total_or_write(self):
        self._poll(listeners=7)
        after_first = ListenerPeak.load().tlh_hours

        # A tick with nobody listening must be a genuine no-op on TLH --
        # no query at all for that field's update, matching _poll_
        # shoutcast_listeners' own "skip the write, adding zero can't
        # change the total" comment.
        with self.assertNumQueries(2):  # ListenerPeak.load() (get_or_create) + nothing else
            self._poll(listeners=0)
        self.assertEqual(ListenerPeak.load().tlh_hours, after_first)

    def test_down_encoder_contributes_zero(self):
        self._poll(listeners=99, up=False)
        self.assertEqual(ListenerPeak.load().tlh_hours, 0.0)

    def test_no_encoders_configured_preserves_persisted_tlh(self):
        # Seed a nonzero TLH, then disable the only encoder -- the
        # early-return "no encoders" path must surface the persisted
        # value, not silently report/imply zero.
        self._poll(listeners=12)
        preserved = ListenerPeak.load().tlh_hours
        self.assertGreater(preserved, 0.0)

        self.encoder.enabled = False
        self.encoder.save(update_fields=["enabled"])
        self.manager._poll_shoutcast_listeners()

        self.assertEqual(ListenerPeak.load().tlh_hours, preserved)

    def test_peak_and_tlh_combined_into_single_save_when_both_change(self):
        # Raising the peak AND accumulating TLH in the same tick should
        # still cost exactly one UPDATE on ListenerPeak, not two. Seed
        # the singleton row first (outside the assertNumQueries block)
        # so get_or_create() inside it takes the cheap 1-query
        # already-exists path rather than the 2-query create path.
        ListenerPeak.load()
        with self.assertNumQueries(3):  # encoders query + load() SELECT + one combined save()
            self._poll(listeners=50)
        tlh = ListenerPeak.load()
        self.assertEqual(tlh.peak_total, 50)
        self.assertGreater(tlh.tlh_hours, 0.0)


class MaybeResetTlhForNewMonthTests(TestCase):
    """Unit-level coverage of the static reset-decision helper itself --
    precise control over tlh_since_at vs "now" without needing a real
    tick/encoder/DB round trip."""

    def test_same_month_no_reset(self):
        now = django_tz.now()
        peak = ListenerPeak(tlh_hours=12.5, tlh_since_at=now.replace(day=1))
        changed = monitor_module.MonitorManager._maybe_reset_tlh_for_new_month(peak, now)
        self.assertFalse(changed)
        self.assertEqual(peak.tlh_hours, 12.5)  # untouched

    def test_previous_calendar_month_resets(self):
        now = django_tz.now()
        peak = ListenerPeak(tlh_hours=99.9, tlh_since_at=_first_of_previous_month(now))
        changed = monitor_module.MonitorManager._maybe_reset_tlh_for_new_month(peak, now)
        self.assertTrue(changed)
        self.assertEqual(peak.tlh_hours, 0.0)
        self.assertEqual(peak.tlh_since_at, now)

    def test_year_boundary_counts_as_a_new_month(self):
        # December of the previous year vs January -- same "month
        # number" difference as any other month pair, but must not be
        # missed just because the calendar year also rolled over.
        now = django_tz.now().replace(month=1, day=3)
        since = now.replace(year=now.year - 1, month=12, day=15)
        peak = ListenerPeak(tlh_hours=5.0, tlh_since_at=since)
        changed = monitor_module.MonitorManager._maybe_reset_tlh_for_new_month(peak, now)
        self.assertTrue(changed)
        self.assertEqual(peak.tlh_hours, 0.0)


class PollShoutcastListenersMonthlyResetTests(TestCase):
    """End-to-end: the reset actually fires from within a real
    _poll_shoutcast_listeners() tick, through both call sites (the
    normal path and the no-encoders-configured early return)."""

    def setUp(self):
        self.encoder = make_encoder()
        self.manager = monitor_module.MonitorManager()

    def _seed_previous_month_tlh(self, hours):
        peak = ListenerPeak.load()
        peak.tlh_hours = hours
        peak.tlh_since_at = _first_of_previous_month(django_tz.now())
        peak.save(update_fields=["tlh_hours", "tlh_since_at"])
        return peak

    def test_normal_path_discards_old_month_and_accumulates_fresh(self):
        self._seed_previous_month_tlh(500.0)
        with patch.object(
            monitor_module, "fetch_shoutcast_stats",
            return_value={"1": {"up": True, "listeners": 20}},
        ):
            self.manager._poll_shoutcast_listeners()

        tlh = ListenerPeak.load()
        expected = 20 * (monitor_module.POLL_SECONDS / 3600.0)
        self.assertAlmostEqual(tlh.tlh_hours, expected, places=9)  # old 500.0 discarded, not added onto
        self.assertGreater(tlh.tlh_since_at, django_tz.now() - timedelta(minutes=1))

    def test_no_encoders_path_still_resets(self):
        self._seed_previous_month_tlh(500.0)
        self.encoder.enabled = False
        self.encoder.save(update_fields=["enabled"])

        self.manager._poll_shoutcast_listeners()

        tlh = ListenerPeak.load()
        self.assertEqual(tlh.tlh_hours, 0.0)
        self.assertGreater(tlh.tlh_since_at, django_tz.now() - timedelta(minutes=1))

    def test_same_month_does_not_reset(self):
        peak = ListenerPeak.load()
        peak.tlh_hours = 8.0
        peak.save(update_fields=["tlh_hours"])  # tlh_since_at stays "now" (this month)

        with patch.object(
            monitor_module, "fetch_shoutcast_stats",
            return_value={"1": {"up": True, "listeners": 10}},
        ):
            self.manager._poll_shoutcast_listeners()

        tlh = ListenerPeak.load()
        expected = 8.0 + 10 * (monitor_module.POLL_SECONDS / 3600.0)
        self.assertAlmostEqual(tlh.tlh_hours, expected, places=9)  # accumulated onto, not reset


@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; the
# plain-HTTP Django test client would otherwise get a 301 on every request
class ApiListenerTlhResetViewTests(TestCase):
    def setUp(self):
        # LoginRequiredMiddleware + GroupBasedAccessMiddleware gate every
        # view by default; staff/superuser bypasses the group-access
        # checks the same way road_conditions/tests/test_admin.py's
        # admin tests already establish.
        self.staff = User.objects.create_superuser("admin", "admin@example.invalid", "password")
        self.client.force_login(self.staff)

    def test_reset_zeroes_tlh_and_updates_since_at(self):
        tlh = ListenerPeak.load()
        tlh.tlh_hours = 42.5
        tlh.save(update_fields=["tlh_hours"])

        resp = self.client.post("/monitoring/api/listeners/reset-tlh/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["tlh_hours"], 0.0)
        self.assertIn("tlh_since_at", data)

        tlh.refresh_from_db()
        self.assertEqual(tlh.tlh_hours, 0.0)

    def test_reset_does_not_touch_peak(self):
        peak = ListenerPeak.load()
        peak.peak_total = 17
        peak.tlh_hours = 3.0
        peak.save(update_fields=["peak_total", "tlh_hours"])

        self.client.post("/monitoring/api/listeners/reset-tlh/")

        peak.refresh_from_db()
        self.assertEqual(peak.peak_total, 17)  # untouched by the TLH-only reset

    def test_get_not_allowed(self):
        resp = self.client.get("/monitoring/api/listeners/reset-tlh/")
        self.assertEqual(resp.status_code, 405)


@override_settings(SECURE_SSL_REDIRECT=False)
class ApiListenerStatusFallbackTests(TestCase):
    """api_listener_status (monitoring/views.py) when LISTENER_STATE_PATH
    is missing or unreadable -- confirms the fallback payload includes
    the new tlh_hours/tlh_since_at keys so the dashboard JS never sees
    an undefined field on a cold/broken monitoring service."""

    def setUp(self):
        self.staff = User.objects.create_superuser("admin2", "admin2@example.invalid", "password")
        self.client.force_login(self.staff)

    def test_missing_state_file_includes_tlh_keys(self):
        import monitoring.views as views_module
        with patch.object(views_module, "LISTENER_STATE_PATH", Path("/nonexistent/listeners.json")):
            resp = self.client.get("/monitoring/api/listeners/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("tlh_hours", data)
        self.assertIn("tlh_since_at", data)
        self.assertEqual(data["tlh_hours"], 0.0)
        self.assertIsNone(data["tlh_since_at"])
