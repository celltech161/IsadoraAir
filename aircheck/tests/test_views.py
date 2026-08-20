"""Tests for aircheck/views.py's aircheck:api-status -- the enriched
status payload (buffer size/limit, maintenance heartbeat health, most
recently stopped session) the /monitoring/ Aircheck card polls.

Reuses AircheckRecorderFixtureMixin from test_recorder.py so
AIRCHECK_CURRENT_PATH/AIRCHECK_LOCK_PATH/AIRCHECK_BUFFER_STATE_PATH are
redirected to a per-test tempdir -- this suite must never touch the
real production /run/isadoraair paths.
"""
import json
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from aircheck.models import AircheckSession
from aircheck.services import recorder
from aircheck.tests.test_recorder import AircheckRecorderFixtureMixin


@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; the
# plain-HTTP Django test client would otherwise get a 301 on every request
class AircheckStatusApiTests(AircheckRecorderFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        # LoginRequiredMiddleware protects every view by default (see
        # isadoraair/settings.py) -- api_aircheck_status has no
        # login_required=False exemption, matching real usage (the
        # /monitoring/ dashboard that polls it already requires staff
        # login to view at all).
        staff = User.objects.create_superuser("aircheck-status-test", "aircheck-status-test@example.invalid", "password")
        self.client.force_login(staff)

    def status(self):
        response = self.client.get(reverse("aircheck:api-status"))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def write_heartbeat(self, **overrides):
        state = {
            "checked_at": time.time(), "result": "below_limit",
            "size_bytes": 100, "max_bytes": recorder.AIRCHECK_IDLE_BUFFER_MAX_BYTES,
            "last_rollover_at": None,
        }
        state.update(overrides)
        Path(self.buffer_state_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.buffer_state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        return state

    # --- buffer size/limit ---------------------------------------------

    def test_active_recording_includes_live_working_file_size(self):
        with patch.object(recorder, "_send_telnet", return_value="ok"):
            recorder.start_recording()
        self.write_working_file(654321)
        # A stale/None AircheckSession.size_bytes (only populated by Stop)
        # must not be what the card sees while recording.
        session = AircheckSession.objects.get(still_running=True)
        self.assertIsNone(session.size_bytes)

        data = self.status()
        self.assertTrue(data["recording"])
        self.assertEqual(data["buffer"]["size_bytes"], 654321)
        self.assertEqual(data["buffer"]["exists"], True)

    def test_idle_status_includes_working_buffer_size_and_max(self):
        self.write_working_file(12345)
        data = self.status()
        self.assertFalse(data["recording"])
        self.assertEqual(data["buffer"]["size_bytes"], 12345)
        self.assertEqual(data["buffer"]["max_bytes"], recorder.AIRCHECK_IDLE_BUFFER_MAX_BYTES)

    def test_missing_working_file_returns_safe_status_not_500(self):
        self.assertFalse(self.working_path.exists())
        data = self.status()
        self.assertEqual(data["buffer"]["exists"], False)
        self.assertIsNone(data["buffer"]["size_bytes"])

    # --- maintenance heartbeat -------------------------------------------

    def test_missing_heartbeat_is_unknown(self):
        self.assertFalse(Path(self.buffer_state_path).exists())
        data = self.status()
        m = data["buffer"]["maintenance"]
        self.assertIsNone(m["result"])
        self.assertIsNone(m["checked_at"])
        self.assertIsNone(m["stale"])  # unknown, not "not stale"

    def test_fresh_heartbeat_is_not_stale(self):
        self.write_heartbeat(checked_at=time.time() - 5, result="below_limit")
        data = self.status()
        m = data["buffer"]["maintenance"]
        self.assertEqual(m["result"], "below_limit")
        self.assertFalse(m["stale"])

    def test_old_heartbeat_is_stale(self):
        self.write_heartbeat(checked_at=time.time() - (recorder.AIRCHECK_BUFFER_HEARTBEAT_STALE_SECONDS + 30))
        data = self.status()
        self.assertTrue(data["buffer"]["maintenance"]["stale"])

    def test_malformed_heartbeat_json_returns_safe_status_not_500(self):
        Path(self.buffer_state_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.buffer_state_path, "w", encoding="utf-8") as f:
            f.write("{definitely not json")
        data = self.status()
        m = data["buffer"]["maintenance"]
        self.assertIsNone(m["result"])
        self.assertIsNone(m["stale"])

    def test_rolled_heartbeat_reports_last_rollover_at(self):
        rollover_ts = time.time() - 3600
        self.write_heartbeat(result="below_limit", last_rollover_at=rollover_ts)
        data = self.status()
        self.assertEqual(data["buffer"]["maintenance"]["last_rollover_at"], rollover_ts)

    # --- recent_session / finalization classification ---------------------

    def _make_session(self, still_running, exit_note="", started_offset=0):
        s = AircheckSession.objects.create(
            filename="/tmp/aircheck-x.m4a", audio_format="he_aac", bitrate="64k",
            source_device="airtap", still_running=still_running, exit_note=exit_note,
        )
        if not still_running:
            from django.utils import timezone
            from datetime import timedelta
            s.ended_at = timezone.now() + timedelta(seconds=started_offset)
            s.save(update_fields=["ended_at"])
        return s

    def test_most_recent_completed_session_returned(self):
        self._make_session(False, exit_note="", started_offset=0)
        newest = self._make_session(False, exit_note="", started_offset=10)
        data = self.status()
        self.assertEqual(data["recent_session"]["id"], newest.id)

    def test_he_aac_pending_session_classified_finalizing(self):
        self._make_session(False, exit_note=recorder.REMUX_PENDING_NOTE)
        data = self.status()
        self.assertEqual(data["recent_session"]["finalization"], "finalizing")

    def test_successful_completed_session_classified_complete(self):
        self._make_session(False, exit_note="")
        data = self.status()
        self.assertEqual(data["recent_session"]["finalization"], "complete")

    def test_failed_remux_classified_error(self):
        self._make_session(False, exit_note="remux failed: ffmpeg exit 1: bad input")
        data = self.status()
        self.assertEqual(data["recent_session"]["finalization"], "error")

    def test_other_nonempty_exit_note_classified_warning(self):
        self._make_session(False, exit_note="telnet reopen failed at Stop: cannot reach liquidsoap telnet")
        data = self.status()
        self.assertEqual(data["recent_session"]["finalization"], "warning")

    def test_no_recent_session_is_null(self):
        data = self.status()
        self.assertIsNone(data["recent_session"])

    # --- backward compatibility -------------------------------------------

    def test_existing_fields_remain_backward_compatible(self):
        data = self.status()
        self.assertIn("recording", data)
        self.assertIn("session", data)
        self.assertIn("server_time", data)
