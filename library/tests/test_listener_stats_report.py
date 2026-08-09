"""Listener Stats tab (/reports/) -- bucketed per-stream + aggregate
listener time series built from IcecastSample rows.

Covers the pure aggregation/label-mapping service functions
(library/services/royalty_reports.py's _encoder_label_map and
compute_listener_series) and the read-only JSON view/permissions.
Uses real IcecastSample/Encoder rows -- IcecastSample.sampled_at is
auto_now_add, so tests backdate it via a bulk .update() (bypasses
auto_now_add, which only fires in Model.save())."""
from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from encoders.models import Encoder
from library.models import IcecastSample
from library.services.royalty_reports import (
    _encoder_label_map,
    compute_listener_series,
)


def make_encoder(**overrides):
    defaults = dict(
        name="test-encoder", enabled=True, protocol="icecast",
        host="192.168.1.112", port=8000, mount="/stream", password="secret",
        format="mp3", bitrate_kbps=192,
    )
    defaults.update(overrides)
    return Encoder.objects.create(**defaults)


def make_sample(sampled_at, listeners_total, listeners_by_mount=None):
    obj = IcecastSample.objects.create(
        listeners_total=listeners_total,
        listeners_by_mount=listeners_by_mount or {},
    )
    IcecastSample.objects.filter(pk=obj.pk).update(sampled_at=sampled_at)
    obj.refresh_from_db()
    return obj


class EncoderLabelMapTests(TestCase):
    def test_icecast_maps_host_port_mount_to_name(self):
        make_encoder(name="Main MP3", protocol="icecast", host="1.2.3.4", port=8000, mount="/stream")
        self.assertEqual(_encoder_label_map(), {"1.2.3.4:8000/stream": "Main MP3"})

    def test_icecast_mount_without_leading_slash_normalized(self):
        make_encoder(name="Main MP3", protocol="icecast", host="1.2.3.4", port=8000, mount="stream")
        self.assertEqual(_encoder_label_map(), {"1.2.3.4:8000/stream": "Main MP3"})

    def test_shoutcast2_uses_shoutcast_sid(self):
        make_encoder(name="SC2 Stream 4", protocol="shoutcast2", host="1.2.3.4", port=8010, mount="/4")
        self.assertEqual(_encoder_label_map(), {"1.2.3.4:8010/4": "SC2 Stream 4"})

    def test_shoutcast1_always_sid_1(self):
        make_encoder(name="SC1 Stream", protocol="shoutcast1", host="1.2.3.4", port=8020, mount="")
        self.assertEqual(_encoder_label_map(), {"1.2.3.4:8020/1": "SC1 Stream"})

    def test_disabled_encoder_excluded(self):
        # Deliberately excluded, not just unlabeled -- disabling a
        # stream in admin must remove it from the Listener Stats
        # report, same as it removing another station's stream (see
        # ComputeListenerSeriesTests.test_foreign_and_disabled_streams_excluded_from_total).
        make_encoder(name="Retired Stream", protocol="icecast", host="1.2.3.4", port=8000, mount="/old", enabled=False)
        self.assertEqual(_encoder_label_map(), {})

    def test_multiple_encoders_all_mapped(self):
        make_encoder(name="MP3", protocol="icecast", host="1.2.3.4", port=8000, mount="/mp3")
        make_encoder(name="AAC", protocol="icecast", host="1.2.3.4", port=8000, mount="/aac")
        labels = _encoder_label_map()
        self.assertEqual(labels["1.2.3.4:8000/mp3"], "MP3")
        self.assertEqual(labels["1.2.3.4:8000/aac"], "AAC")


class ComputeListenerSeriesTests(TestCase):
    def test_no_samples_returns_empty_series(self):
        today = timezone.localdate()
        result = compute_listener_series(today, today)
        self.assertEqual(result["points"], [])
        self.assertEqual(result["stream_labels"], [])
        self.assertEqual(result["sample_count"], 0)

    def test_bucket_size_short_range_is_15_minutes(self):
        today = timezone.localdate()
        result = compute_listener_series(today, today)
        self.assertEqual(result["bucket_seconds"], 900)

    def test_bucket_size_month_range_is_hourly(self):
        today = timezone.localdate()
        result = compute_listener_series(today - timedelta(days=30), today)
        self.assertEqual(result["bucket_seconds"], 3600)

    def test_bucket_size_long_range_is_daily(self):
        today = timezone.localdate()
        result = compute_listener_series(today - timedelta(days=120), today)
        self.assertEqual(result["bucket_seconds"], 86400)

    def test_aggregate_is_mean_of_owned_sum_per_sample_in_bucket(self):
        # listeners_total on the sample rows below (10, 20) is
        # deliberately never read by compute_listener_series -- total
        # is recomputed from listeners_by_mount so it can never include
        # another station's listeners (see
        # test_foreign_stream_excluded_from_total_alongside_our_own).
        # Passing the same values here just keeps this test's own math
        # easy to follow; they coincidentally match because this
        # sample's only stream is fully owned.
        make_encoder(name="Main", protocol="icecast", host="h", port=8000, mount="/s")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 10, {"h:8000/s": 10})
        make_sample(base + timedelta(minutes=5), 20, {"h:8000/s": 20})
        result = compute_listener_series(today, today)
        # Both samples land in the same 15-minute bucket -- mean of 10/20.
        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(result["points"][0]["total"], 15.0)
        self.assertEqual(result["points"][0]["streams"]["Main"], 15.0)

    def test_stream_label_mapping_applied(self):
        make_encoder(name="Friendly Name", protocol="icecast", host="1.2.3.4", port=8000, mount="/mount")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 5, {"1.2.3.4:8000/mount": 5})
        result = compute_listener_series(today, today)
        self.assertIn("Friendly Name", result["stream_labels"])
        self.assertEqual(result["points"][0]["streams"]["Friendly Name"], 5.0)

    def test_unmapped_key_excluded_entirely(self):
        # No Encoder row at all matches this key -- not this station's
        # stream (e.g. another station sharing the same physical
        # Icecast/Shoutcast server), so it's dropped rather than shown
        # under its raw key. With nothing else in the sample, the
        # bucket still exists (the row was sampled) but carries no
        # streams and a zero owned total.
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 5, {"9.9.9.9:9999/ghost": 5})
        result = compute_listener_series(today, today)
        self.assertEqual(result["stream_labels"], [])
        self.assertEqual(result["points"][0]["streams"], {})
        self.assertEqual(result["points"][0]["total"], 0.0)

    def test_foreign_stream_excluded_from_total_alongside_our_own(self):
        # The scenario this feature exists for: a shared Shoutcast
        # server reporting both this station's configured stream AND
        # another station's stream in the SAME sample. Only the
        # configured one may contribute to stream_labels/streams/total.
        make_encoder(name="Ours", protocol="icecast", host="h", port=8000, mount="/ours")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 999, {"h:8000/ours": 4, "h:8000/theirs": 50})
        result = compute_listener_series(today, today)
        self.assertEqual(result["stream_labels"], ["Ours"])
        self.assertEqual(result["points"][0]["streams"], {"Ours": 4.0})
        # NOT 54 (4+50) and NOT the raw listeners_total field (999) --
        # only the sum of streams this station actually owns.
        self.assertEqual(result["points"][0]["total"], 4.0)

    def test_disabled_encoder_stream_excluded_from_series(self):
        make_encoder(name="Retired", protocol="icecast", host="h", port=8000, mount="/old", enabled=False)
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 7, {"h:8000/old": 7})
        result = compute_listener_series(today, today)
        self.assertEqual(result["stream_labels"], [])
        self.assertEqual(result["points"][0]["streams"], {})
        self.assertEqual(result["points"][0]["total"], 0.0)

    def test_stream_absent_from_bucket_leaves_a_gap_not_a_zero(self):
        make_encoder(name="A", protocol="icecast", host="h", port=8000, mount="/a")
        make_encoder(name="B", protocol="icecast", host="h", port=8000, mount="/b")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        # Bucket 0 (00:00-00:15): both streams present.
        make_sample(base, 10, {"h:8000/a": 6, "h:8000/b": 4})
        # Bucket 1 (00:20, still within the 15-min window boundary at
        # :15-:30): only stream A reports -- B's Encoder was briefly
        # unreachable for this bucket.
        make_sample(base + timedelta(minutes=20), 6, {"h:8000/a": 6})
        result = compute_listener_series(today, today)
        self.assertEqual(len(result["points"]), 2)
        self.assertIn("A", result["points"][1]["streams"])
        self.assertNotIn("B", result["points"][1]["streams"])

    def test_averaging_only_over_samples_that_reported_the_stream(self):
        make_encoder(name="A", protocol="icecast", host="h", port=8000, mount="/a")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        # Same 15-min bucket: one sample with stream A, one without
        # (e.g. a transient fetch failure for that one server that
        # cycle) -- A's per-stream average must be computed only from
        # the sample that actually reported it (10), not (10+0)/2.
        make_sample(base, 10, {"h:8000/a": 10})
        make_sample(base + timedelta(minutes=2), 10, {})
        result = compute_listener_series(today, today)
        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(result["points"][0]["streams"]["A"], 10.0)
        # total, unlike the per-stream average, is the mean of each
        # SAMPLE's own owned-sum -- the second sample genuinely owned
        # zero (empty listeners_by_mount), so mean(10, 0) = 5.0, not 10.
        self.assertEqual(result["points"][0]["total"], 5.0)

    def test_values_rounded_to_two_decimals(self):
        make_encoder(name="A", protocol="icecast", host="h", port=8000, mount="/a")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 1, {"h:8000/a": 1})
        make_sample(base + timedelta(minutes=1), 2, {"h:8000/a": 2})
        make_sample(base + timedelta(minutes=2), 2, {"h:8000/a": 2})
        result = compute_listener_series(today, today)
        # mean(1, 2, 2) = 1.6666... -> rounded to 1.67
        self.assertEqual(result["points"][0]["total"], 1.67)

    def test_points_outside_range_excluded(self):
        make_encoder(name="A", protocol="icecast", host="h", port=8000, mount="/a")
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        base_today = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        base_yesterday = timezone.make_aware(timezone.datetime.combine(yesterday, timezone.datetime.min.time()))
        make_sample(base_today, 5, {"h:8000/a": 5})
        make_sample(base_yesterday, 99, {"h:8000/a": 99})
        result = compute_listener_series(today, today)
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["points"][0]["total"], 5.0)

    def test_sample_count_reflects_raw_row_count_not_bucket_count(self):
        make_encoder(name="A", protocol="icecast", host="h", port=8000, mount="/a")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        for i in range(5):
            make_sample(base + timedelta(minutes=i), 1, {"h:8000/a": 1})
        result = compute_listener_series(today, today)
        self.assertEqual(result["sample_count"], 5)
        self.assertEqual(len(result["points"]), 1)  # all 5 land in the same 15-min bucket


@override_settings(SECURE_SSL_REDIRECT=False)
class ApiReportsListenerStatsViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("lsstaff", "lsstaff@example.invalid", "pw")

    def test_anonymous_user_redirected_to_login(self):
        # LoginRequiredMiddleware gates every view before
        # _reports_permission_check ever runs -- 302, not 403, same as
        # every other unauthenticated request in this project.
        resp = self.client.get(reverse("library:api-reports-listener-stats"))
        self.assertEqual(resp.status_code, 302)

    def test_non_staff_user_rejected(self):
        ro = User.objects.create_user("lsro", "lsro@example.invalid", "pw")
        Group.objects.get_or_create(name="remote_dj")[0].user_set.add(ro)
        self.client.force_login(ro)
        resp = self.client.get(reverse("library:api-reports-listener-stats"))
        self.assertEqual(resp.status_code, 403)

    def test_staff_gets_ok_response_with_no_data(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("library:api-reports-listener-stats"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["points"], [])

    def test_defaults_to_current_calendar_month(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("library:api-reports-listener-stats"))
        data = resp.json()
        today = timezone.localdate()
        self.assertEqual(data["start"], today.replace(day=1).isoformat())
        self.assertEqual(data["end"], today.isoformat())

    def test_explicit_date_range_honored(self):
        make_encoder(name="A", protocol="icecast", host="h", port=8000, mount="/a")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 7, {"h:8000/a": 7})
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("library:api-reports-listener-stats"), {
            "start": today.isoformat(), "end": today.isoformat(),
        })
        data = resp.json()
        self.assertEqual(len(data["points"]), 1)
        self.assertEqual(data["points"][0]["total"], 7.0)

    def test_invalid_date_format_rejected(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("library:api-reports-listener-stats"), {"start": "not-a-date", "end": "2026-08-01"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    def test_start_after_end_rejected(self):
        self.client.force_login(self.staff)
        today = timezone.localdate()
        resp = self.client.get(reverse("library:api-reports-listener-stats"), {
            "start": today.isoformat(), "end": (today - timedelta(days=1)).isoformat(),
        })
        self.assertEqual(resp.status_code, 400)

    def test_response_includes_bucket_seconds_and_stream_labels(self):
        make_encoder(name="Main", protocol="icecast", host="h", port=8000, mount="/s")
        today = timezone.localdate()
        base = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
        make_sample(base, 3, {"h:8000/s": 3})
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("library:api-reports-listener-stats"), {
            "start": today.isoformat(), "end": today.isoformat(),
        })
        data = resp.json()
        self.assertIn("bucket_seconds", data)
        self.assertEqual(data["stream_labels"], ["Main"])


@override_settings(SECURE_SSL_REDIRECT=False)
class ReportsPageListenerStatsTabTests(TestCase):
    """Static-content regression coverage for the new tab, matching the
    existing pattern for the Hidden Track Detection tab (see
    HiddenTrackScanUXStaticContentTests in test_hidden_track_detection.py)."""

    def setUp(self):
        self.staff = User.objects.create_superuser("lstabstaff", "lstabstaff@example.invalid", "pw")
        self.client.force_login(self.staff)

    def _get_reports_page(self):
        resp = self.client.get(reverse("library:reports"))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_listener_stats_tab_button_present(self):
        html = self._get_reports_page()
        self.assertIn('id="tabBtnListeners"', html)
        self.assertIn("Listener Stats", html)

    def test_listener_stats_panel_present(self):
        html = self._get_reports_page()
        self.assertIn('id="tabPanelListeners"', html)
        self.assertIn('id="lsCanvas"', html)

    def test_date_inputs_default_to_current_calendar_month(self):
        html = self._get_reports_page()
        today = timezone.localdate()
        self.assertIn(f'id="lsStart" type="date" value="{today.replace(day=1).isoformat()}"', html)
        self.assertIn(f'id="lsEnd" type="date" value="{today.isoformat()}"', html)

    def test_load_listener_stats_js_function_present(self):
        html = self._get_reports_page()
        self.assertIn("function loadListenerStats()", html)
        # {% url %} must have actually resolved into the real endpoint
        # path, not been left as a literal template tag or a typo'd name.
        self.assertIn(reverse("library:api-reports-listener-stats"), html)

    def test_switch_tab_wires_up_listeners_tab(self):
        html = self._get_reports_page()
        self.assertIn("REPORT_TABS = ['royalty', 'hidden', 'listeners']", html)

    def test_existing_tabs_still_present(self):
        # Regression guard -- adding the third tab must not have
        # displaced either existing one.
        html = self._get_reports_page()
        self.assertIn('id="tabBtnRoyalty"', html)
        self.assertIn('id="tabBtnHidden"', html)
