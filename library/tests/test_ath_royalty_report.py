"""SoundExchange Aggregate Tuning Hours (ATH) -- library/services/
royalty_reports.py's compute_ath(), the shared _owned_listener_total
ownership primitive it now shares with compute_listener_series, and
generate_soundexchange_nce()'s manual ath_override precedence.

The bug this covers: compute_ath() used to integrate IcecastSample's
raw `listeners_total` -- the WHOLE sampled server's listener count,
which can include another station's streams when this station shares
a physical Icecast/Shoutcast server with one. It now integrates only
`listeners_by_mount` counts belonging to a currently enabled IsadoraAir
Encoder row, via the exact same ownership map (_encoder_label_map)
Listener Stats already used correctly. See library/services/
royalty_reports.py's own module-level docstrings for the full
rationale.

Reuses make_encoder/make_sample from test_listener_stats_report.py
rather than re-declaring an unrelated fixture universe -- both files
exercise the same IcecastSample/Encoder ownership model, just from
two different report surfaces (Listener Stats chart vs SoundExchange
ATH)."""
import csv
import io
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from library.services.royalty_reports import (
    _owned_listener_total,
    compute_ath,
    generate_soundexchange_nce,
)
from library.tests.test_listener_stats_report import make_encoder, make_sample


def _day_bounds(day):
    """Same construction _period_bounds() uses internally, exposed here
    so tests can compute the EXACT expected dt to period-end rather
    than hand-approximating it (period end is 23:59:59.999999, not a
    clean 24:00:00)."""
    from library.services.royalty_reports import _period_bounds
    return _period_bounds(day, day)


class OwnedListenerTotalTests(TestCase):
    """Direct unit coverage of the shared primitive, independent of
    both its callers."""

    def test_sums_only_owned_keys(self):
        total = _owned_listener_total(
            {"h:8000/ours": 4, "h:8000/theirs": 50},
            {"h:8000/ours": "Ours"},
        )
        self.assertEqual(total, 4)

    def test_empty_mount_dict_is_zero(self):
        self.assertEqual(_owned_listener_total({}, {"h:8000/ours": "Ours"}), 0)

    def test_none_mount_dict_is_zero(self):
        self.assertEqual(_owned_listener_total(None, {"h:8000/ours": "Ours"}), 0)

    def test_empty_owned_map_is_zero(self):
        self.assertEqual(_owned_listener_total({"h:8000/ours": 4}, {}), 0)


class ComputeAthTests(TestCase):
    def test_no_samples_returns_zero(self):
        today = timezone.localdate()
        self.assertEqual(compute_ath(today, today), 0.0)

    def test_owned_icecast_stream_only(self):
        # 00:00 owned=10, 00:30 owned=10.
        # sample @00:00: dt to next sample = 1800s -> 10 * 1800 = 18000
        # sample @00:30 (last): dt to period end, capped at 3600s
        #                       -> 10 * 3600 = 36000
        # ath_seconds = 18000 + 36000 = 54000 -> 54000 / 3600 = 15.0
        make_encoder(name="Main", protocol="icecast", host="h", port=8000, mount="/s")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 999999, {"h:8000/s": 10})  # absurd listeners_total: must be ignored
        make_sample(base + timedelta(minutes=30), 999999, {"h:8000/s": 10})
        self.assertAlmostEqual(compute_ath(today, today), 15.0, places=4)

    def test_foreign_stream_alongside_owned_stream_not_54_not_999(self):
        # Single sample: listeners_total=999 (deliberately absurd),
        # listeners_by_mount owns only /ours=4; /theirs=50 is foreign.
        # dt = to period end, capped at 3600s -> 4 * 3600 = 14400
        # ath = 14400 / 3600 = 4.0 -- NOT 54 (4+50), NOT 999.
        make_encoder(name="Ours", protocol="icecast", host="h", port=8000, mount="/ours")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 999, {"h:8000/ours": 4, "h:8000/theirs": 50})
        self.assertAlmostEqual(compute_ath(today, today), 4.0, places=4)

    def test_shared_shoutcast_server_foreign_sids_excluded(self):
        # The exact deployment shape from the roadmap spec:
        #   SID 1 (ours, enabled)  = 5
        #   SID 2 (foreign)        = 20
        #   SID 3 (ours, enabled)  = 3
        #   SID 4 (foreign)        = 10
        # owned = 5 + 3 = 8 (never touches SID 2/4, never the raw total).
        # Single sample: dt capped at 3600s -> 8 * 3600 = 28800 -> ath = 8.0
        make_encoder(name="SC Stream 1", protocol="shoutcast2", host="h", port=8010, mount="/1")
        make_encoder(name="SC Stream 3", protocol="shoutcast2", host="h", port=8010, mount="/3")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 38, {
            "h:8010/1": 5, "h:8010/2": 20, "h:8010/3": 3, "h:8010/4": 10,
        })
        self.assertAlmostEqual(compute_ath(today, today), 8.0, places=4)

    def test_disabled_encoder_stream_contributes_zero(self):
        # Sample reports both a disabled-Encoder stream (6) and an
        # enabled one (elsewhere in the same sample would also work,
        # but this test isolates the disabled-only case): owned = 0
        # since the only matching Encoder row is disabled.
        make_encoder(name="Retired", protocol="icecast", host="h", port=8000, mount="/old", enabled=False)
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 6, {"h:8000/old": 6})
        self.assertAlmostEqual(compute_ath(today, today), 0.0, places=4)

    def test_disabled_encoder_alongside_enabled_only_enabled_contributes(self):
        # Same sample: a disabled stream (0) and an enabled stream (6).
        # owned = 6. dt capped at 3600s -> 6*3600=21600 -> ath = 6.0
        make_encoder(name="Retired", protocol="icecast", host="h", port=8000, mount="/old", enabled=False)
        make_encoder(name="Live", protocol="icecast", host="h", port=8000, mount="/new")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 12, {"h:8000/old": 6, "h:8000/new": 6})
        self.assertAlmostEqual(compute_ath(today, today), 6.0, places=4)

    def test_empty_mount_map_fails_to_zero_not_raw_total(self):
        # listeners_total=100 (absurd/deliberate), listeners_by_mount={}
        # -- must NOT fall back to the raw total. owned=0 -> ath=0.0.
        make_encoder(name="Main", protocol="icecast", host="h", port=8000, mount="/s")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 100, {})
        self.assertAlmostEqual(compute_ath(today, today), 0.0, places=4)

    def test_multiple_owned_encoders_summed_with_foreign_excluded(self):
        # MP3=5, AAC=3 (both ours), foreign=20 (not ours).
        # owned = 5 + 3 = 8. dt capped at 3600s -> 8*3600=28800 -> ath=8.0
        make_encoder(name="MP3", protocol="icecast", host="h", port=8000, mount="/mp3")
        make_encoder(name="AAC", protocol="icecast", host="h", port=8000, mount="/aac")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 28, {"h:8000/mp3": 5, "h:8000/aac": 3, "h:8000/theirs": 20})
        self.assertAlmostEqual(compute_ath(today, today), 8.0, places=4)

    def test_sampling_gap_capped_at_one_hour(self):
        # sample A @00:00 owned=10, sample B @05:00 owned=10 (5h gap).
        # Uncapped, A's interval would be 5h -> 10*5=50.0 ATH just from
        # A. WITH the existing 1h cap: dtA is clamped to 3600s.
        # dtA (capped) = 3600s -> 10*3600 = 36000
        # dtB (last sample -> period end, itself > 1h away) also
        #     capped at 3600s -> 10*3600 = 36000
        # ath_seconds = 72000 -> ath = 72000/3600 = 20.0 (NOT 50.0+).
        make_encoder(name="Main", protocol="icecast", host="h", port=8000, mount="/s")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 10, {"h:8000/s": 10})
        make_sample(base + timedelta(hours=5), 10, {"h:8000/s": 10})
        result = compute_ath(today, today)
        self.assertAlmostEqual(result, 20.0, places=4)
        self.assertLess(result, 50.0)  # the cap must have actually bound something

    def test_last_sample_interval_runs_to_period_end_not_further(self):
        # Single sample near the end of the period, well under the 1h
        # cap, so this specifically isolates "dt = period_end -
        # sample_time" rather than the cap ceiling. owned=7.
        today = timezone.localdate()
        make_encoder(name="Main", protocol="icecast", host="h", port=8000, mount="/s")
        sample_time = timezone.make_aware(
            timezone.datetime.combine(today, timezone.datetime.min.time())
        ) + timedelta(hours=23, minutes=30)
        make_sample(sample_time, 7, {"h:8000/s": 7})
        _start, end = _day_bounds(today)
        expected_dt = min(max((end - sample_time).total_seconds(), 0.0), 3600.0)
        expected_ath = 7 * expected_dt / 3600.0
        self.assertAlmostEqual(compute_ath(today, today), expected_ath, places=4)
        # Sanity: this sample is genuinely under the cap (not testing
        # the cap path by accident).
        self.assertLess(expected_dt, 3600.0)


class ManualAthOverrideTests(TestCase):
    def _ath_cell(self, csv_text):
        rows = list(csv.reader(io.StringIO(csv_text)))
        return rows[1][5]  # header row 0, data row 1, ATH is column index 5

    def test_override_wins_regardless_of_sample_data(self):
        # Deliberately conflicting sample data: a real owned stream
        # that would compute to a very different ATH if used.
        make_encoder(name="Main", protocol="icecast", host="h", port=8000, mount="/s")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 999, {"h:8000/s": 999})  # would compute to a huge ATH if used

        text, ext = generate_soundexchange_nce(today, today, ath_override=42.5)
        self.assertEqual(ext, "csv")
        self.assertEqual(self._ath_cell(text), "42.50")

    def test_compute_ath_not_called_when_override_supplied(self):
        today = timezone.localdate()
        with patch("library.services.royalty_reports.compute_ath") as mock_compute:
            text, _ext = generate_soundexchange_nce(today, today, ath_override=10.0)
            mock_compute.assert_not_called()
        self.assertEqual(self._ath_cell(text), "10.00")

    def test_no_override_falls_through_to_compute_ath(self):
        today = timezone.localdate()
        with patch("library.services.royalty_reports.compute_ath", return_value=3.25) as mock_compute:
            text, _ext = generate_soundexchange_nce(today, today)
            mock_compute.assert_called_once_with(today, today)
        self.assertEqual(self._ath_cell(text), "3.25")
