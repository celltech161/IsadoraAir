"""probe_encoder_group tests -- the truthful aggregate check added in
the 2026-08-05 hardening pass, covering the full decision table:
manager systemd state, group-state staleness/crash-loop, generation
cross-checking against the audio-state file, the startup-allowance
window, the stabilization gate, and real Shoutcast SID connectivity
(mocked -- no live server). No live Kokoro/Liquidsoap/network calls
anywhere in this file."""
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import encoders.services.encoder_manager as em
from encoders.models import Encoder
from monitoring.models import MonitorCheck
from monitoring.services import probes


def make_check(**overrides):
    defaults = dict(
        name="Encoder Stream Health (airtap)", kind="encoder_group",
        encoder_group_slug="airtap", encoder_group_systemd_unit="isadoraair-encoders.service",
    )
    defaults.update(overrides)
    return MonitorCheck(**defaults)


def make_encoder(**overrides):
    defaults = dict(
        name="test-mp3", enabled=True, protocol="shoutcast2", host="192.168.1.112",
        port=8000, mount="/1", password="secret", format="mp3", bitrate_kbps=320, input_device="airtap",
    )
    defaults.update(overrides)
    return Encoder.objects.create(**defaults)


class ProbeEncoderGroupFixtureMixin:
    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        patcher = patch.object(em, "STATE_DIR", Path(self._tmpdir.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.check = make_check()
        self.now = time.time()

    def write_group_state(self, **overrides):
        state = dict(
            input_device="airtap", pid=555, generation="gen-1", launched_at=self.now - 100,
            last_successful_start=self.now - 100, last_exit_at=None, last_exit_code=None,
            consecutive_failures=0, last_failure_message="", next_retry_at=None,
            timestamp=self.now,
        )
        state.update(overrides)
        em._atomic_write_json(em._group_state_path_for_slug("airtap"), state)

    def write_audio_state(self, **overrides):
        state = dict(
            status="audio_ok", is_blank=False, audio_observed=True,
            input_device="airtap", pid=555, generation="gen-1",
            started_at=self.now - 100, since=self.now - 100, timestamp=self.now,
        )
        state.update(overrides)
        em._atomic_write_json(em._audio_state_path_for_slug("airtap"), state)

    def run_probe(self, systemd_status="ok", shoutcast_stats=None):
        if shoutcast_stats is None:
            shoutcast_stats = {"1": {"up": True, "listeners": 3}}
        with patch.object(probes, "probe_systemd", return_value=(systemd_status, {})), \
             patch("monitoring.services.shoutcast.fetch_shoutcast_stats", return_value=shoutcast_stats):
            return probes.probe_encoder_group(self.check)


class NoEncodersOrManagerTests(ProbeEncoderGroupFixtureMixin, TestCase):
    def test_no_enabled_encoders_for_this_group_is_unknown(self):
        status, detail = self.run_probe()
        self.assertEqual(status, "unknown")
        self.assertIn("no enabled encoders", detail["reason"])

    def test_manager_systemd_not_active_is_critical(self):
        make_encoder()
        status, detail = self.run_probe(systemd_status="critical")
        self.assertEqual(status, "critical")
        self.assertIn("not active", detail["reason"])

    def test_no_group_state_file_is_critical(self):
        make_encoder()
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")
        self.assertIn("no group-state file", detail["reason"])


class GroupStateStalenessTests(ProbeEncoderGroupFixtureMixin, TestCase):
    def test_stale_group_state_is_critical(self):
        make_encoder()
        self.write_group_state(timestamp=self.now - 1000)
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")
        self.assertIn("not reporting", detail["reason"])

    def test_fresh_group_state_passes_this_gate(self):
        make_encoder()
        self.write_group_state()
        self.write_audio_state()
        status, detail = self.run_probe()
        self.assertNotEqual(status, "critical")


class CrashLoopDecisionTests(ProbeEncoderGroupFixtureMixin, TestCase):
    def test_three_consecutive_failures_is_critical(self):
        make_encoder()
        self.write_group_state(consecutive_failures=3, last_failure_message="Liquidsoap exited with code 1", pid=None, generation=None)
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")
        self.assertIn("exited 3 times", detail["reason"])

    def test_two_consecutive_failures_not_yet_crash_loop_critical(self):
        """Below the crash-loop threshold -- falls through to the "no
        live child" critical instead (pid/generation are None), a
        DIFFERENT but still-correctly-critical reason -- confirms the
        threshold itself, not just "critical or not"."""
        make_encoder()
        self.write_group_state(consecutive_failures=2, pid=None, generation=None)
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")
        self.assertIn("no live child", detail["reason"])


class GenerationMismatchTests(ProbeEncoderGroupFixtureMixin, TestCase):
    def test_stale_audio_state_generation_is_critical(self):
        """Phase 8's core hardening point: a monitoring poll reading a
        PREVIOUS generation's audio-state file must never be fooled
        into thinking it reflects the current process."""
        make_encoder()
        self.write_group_state(generation="NEW-GEN", pid=999)
        self.write_audio_state(generation="OLD-GEN", pid=999)
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")
        self.assertIn("stale", detail["reason"])

    def test_stale_audio_state_pid_mismatch_is_critical(self):
        make_encoder()
        self.write_group_state(generation="gen-1", pid=999)
        self.write_audio_state(generation="gen-1", pid=111)  # different pid, same generation string
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")

    def test_no_audio_state_file_yet_is_critical(self):
        make_encoder()
        self.write_group_state()
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")
        self.assertIn("no audio-state file", detail["reason"])


class StartupAllowanceTests(ProbeEncoderGroupFixtureMixin, TestCase):
    def test_within_allowance_is_warning(self):
        make_encoder()
        self.write_group_state(launched_at=self.now - 5)
        self.write_audio_state(status="starting", is_blank=None, audio_observed=False, since=self.now - 5)
        status, detail = self.run_probe()
        self.assertEqual(status, "warning")
        self.assertEqual(detail["reason"], "starting")

    def test_past_allowance_without_audio_is_critical(self):
        make_encoder()
        self.write_group_state(launched_at=self.now - (probes.ENCODER_STARTUP_ALLOWANCE_SECONDS + 5))
        self.write_audio_state(status="starting", is_blank=None, audio_observed=False, since=self.now - 5)
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")
        self.assertIn("startup allowance", detail["reason"])


class SilentTests(ProbeEncoderGroupFixtureMixin, TestCase):
    def test_is_blank_true_is_critical(self):
        make_encoder()
        self.write_group_state()
        self.write_audio_state(status="silent", is_blank=True)
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")
        self.assertIn("silent", detail["reason"])


class ShoutcastDecisionTests(ProbeEncoderGroupFixtureMixin, TestCase):
    def test_sid_disconnected_is_critical(self):
        make_encoder(mount="/1")
        self.write_group_state()
        self.write_audio_state(since=self.now - 100)
        status, detail = self.run_probe(shoutcast_stats={"1": {"up": False, "listeners": 0}})
        self.assertEqual(status, "critical")
        self.assertIn("STREAMSTATUS=0", detail["reason"])

    def test_sid_absent_is_critical_with_absent_wording(self):
        make_encoder(mount="/1")
        self.write_group_state()
        self.write_audio_state(since=self.now - 100)
        status, detail = self.run_probe(shoutcast_stats={"2": {"up": True, "listeners": 5}})
        self.assertEqual(status, "critical")
        self.assertIn("absent", detail["reason"])

    def test_server_unreachable_is_warning_not_critical(self):
        make_encoder(mount="/1")
        self.write_group_state()
        self.write_audio_state(since=self.now - 100)
        status, detail = self.run_probe(shoutcast_stats={})
        self.assertEqual(status, "warning")
        self.assertIn("unreachable", detail["reason"])

    def test_zero_listeners_but_connected_is_not_a_failure_signal(self):
        """Listener count must never participate in the health
        decision -- a stream with zero listeners can still be healthy."""
        make_encoder(mount="/1")
        self.write_group_state()
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)
        status, detail = self.run_probe(shoutcast_stats={"1": {"up": True, "listeners": 0}})
        self.assertEqual(status, "ok")
        self.assertNotIn("down", (detail.get("reason") or ""))

    def test_multiple_encoders_all_must_be_up(self):
        make_encoder(name="a", mount="/1")
        make_encoder(name="b", mount="/2")
        self.write_group_state()
        self.write_audio_state(since=self.now - 100)
        status, detail = self.run_probe(shoutcast_stats={"1": {"up": True, "listeners": 1}, "2": {"up": False, "listeners": 0}})
        self.assertEqual(status, "critical")


class StabilizationGateTests(ProbeEncoderGroupFixtureMixin, TestCase):
    def test_recently_stable_is_warning(self):
        make_encoder(mount="/1")
        self.write_group_state()
        self.write_audio_state(since=self.now - 2)
        status, detail = self.run_probe()
        self.assertEqual(status, "warning")
        self.assertIn("stabilizing", detail["reason"])

    def test_stable_long_enough_is_ok(self):
        make_encoder(mount="/1")
        self.write_group_state()
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)
        status, detail = self.run_probe()
        self.assertEqual(status, "ok")
        self.assertEqual(detail["reason"], "healthy")


class ReasonTextTests(ProbeEncoderGroupFixtureMixin, TestCase):
    def test_reason_never_exposes_password(self):
        make_encoder(mount="/1", password="super-secret-password")
        self.write_group_state(consecutive_failures=5, last_failure_message="Liquidsoap exited with code 1")
        status, detail = self.run_probe()
        blob = json.dumps(detail)
        self.assertNotIn("super-secret-password", blob)
