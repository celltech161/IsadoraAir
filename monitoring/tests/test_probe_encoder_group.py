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
from unittest.mock import MagicMock, patch

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


class StateFreshnessTests(ProbeEncoderGroupFixtureMixin, TestCase):
    """Item 3 of the 2026-08-05 pre-deploy review: probe_encoder_group
    must distrust state using each file's own JSON PAYLOAD timestamp
    (never bare filesystem mtime -- write_group_state/write_audio_state
    always leave a fresh mtime, so any test here that still gets
    rejected proves the payload field, not the mtime, is what's being
    read), must reject a timestamp that has drifted implausibly into
    the future rather than silently treating a negative age as
    "infinitely fresh," and must reject a missing/non-numeric timestamp
    outright rather than raising or defaulting to fresh. Ordinary
    clock-skew jitter (a few seconds either direction) must NOT be
    rejected -- see CLOCK_SKEW_TOLERANCE_SECONDS in probes.py."""

    def test_fresh_matching_state_is_healthy(self):
        make_encoder(mount="/1")
        self.write_group_state()
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)
        status, detail = self.run_probe()
        self.assertEqual(status, "ok")

    def test_stale_group_state_is_rejected(self):
        make_encoder()
        self.write_group_state(timestamp=self.now - (probes.ENCODER_GROUP_STALE_SECONDS + 10))
        self.write_audio_state()
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")
        self.assertIn("not reporting", detail["reason"])

    def test_stale_audio_state_is_rejected(self):
        make_encoder()
        self.write_group_state()
        self.write_audio_state(timestamp=self.now - (probes.AUDIO_STATE_STALE_SECONDS + 10))
        status, detail = self.run_probe()
        self.assertEqual(status, "unknown")
        self.assertIn("heartbeat stopped", detail["reason"])

    def test_both_files_stale_but_mutually_matching_is_still_rejected(self):
        """Same generation/pid pairing on both sides -- the exact shape
        that would fool a probe checking only generation/pid agreement
        -- but neither file has been refreshed in a very long time, so
        the freshness gate alone must independently reject this."""
        make_encoder()
        old_ts = self.now - 10_000
        self.write_group_state(timestamp=old_ts)
        self.write_audio_state(timestamp=old_ts)
        status, detail = self.run_probe()
        self.assertIn(status, ("critical", "unknown"))

    def test_group_state_timestamp_far_in_the_future_is_rejected(self):
        make_encoder()
        self.write_group_state(timestamp=self.now + 10_000)
        self.write_audio_state()
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")
        self.assertIn("implausible", detail["reason"])

    def test_audio_state_timestamp_far_in_the_future_is_rejected(self):
        make_encoder()
        self.write_group_state()
        self.write_audio_state(timestamp=self.now + 10_000)
        status, detail = self.run_probe()
        self.assertEqual(status, "unknown")
        self.assertIn("implausible", detail["reason"])

    def test_timestamp_slightly_ahead_within_clock_skew_tolerance_still_passes(self):
        """A few seconds ahead of this process's own clock is ordinary
        jitter between two time.time() calls, not corruption -- must
        not be rejected by the future-timestamp guard."""
        make_encoder(mount="/1")
        self.write_group_state(timestamp=self.now + 5)
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)
        status, detail = self.run_probe()
        self.assertEqual(status, "ok")

    def test_missing_group_state_timestamp_is_rejected_not_treated_as_fresh(self):
        make_encoder()
        self.write_group_state()
        path = em._group_state_path_for_slug("airtap")
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["timestamp"]
        em._atomic_write_json(path, data)
        self.write_audio_state()
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")
        self.assertIn("missing or implausible", detail["reason"])

    def test_malformed_group_state_timestamp_is_rejected_not_raised(self):
        make_encoder()
        self.write_group_state(timestamp="not-a-number")
        self.write_audio_state()
        status, detail = self.run_probe()
        self.assertEqual(status, "critical")
        self.assertIn("missing or implausible", detail["reason"])

    def test_missing_audio_state_timestamp_is_rejected_not_treated_as_fresh(self):
        make_encoder()
        self.write_group_state()
        self.write_audio_state()
        path = em._audio_state_path_for_slug("airtap")
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["timestamp"]
        em._atomic_write_json(path, data)
        status, detail = self.run_probe()
        self.assertEqual(status, "unknown")
        self.assertIn("missing or implausible", detail["reason"])

    def test_malformed_audio_state_timestamp_is_rejected_not_raised(self):
        make_encoder()
        self.write_group_state()
        self.write_audio_state(timestamp=None)
        status, detail = self.run_probe()
        self.assertEqual(status, "unknown")
        self.assertIn("missing or implausible", detail["reason"])


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

    def test_one_unreachable_server_does_not_block_results_for_another_server(self):
        """Item 5 of the 2026-08-05 pre-deploy review: encoders are
        grouped by (host, port) and fetched per-server, one server at a
        time -- confirm the loop is NOT short-circuited by an early
        failure. Uses side_effect (not run_probe()'s single fixed-dict
        helper, which can't distinguish calls by argument) so server A
        can fail while server B succeeds within the SAME probe call."""
        make_encoder(name="a-server-down", host="192.168.1.112", mount="/1")
        make_encoder(name="b-server-up", host="192.168.1.200", mount="/1")
        self.write_group_state()
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)

        def fake_fetch(host, port, timeout=3.0):
            if host == "192.168.1.112":
                return {}  # unreachable/timed out
            return {"1": {"up": True, "listeners": 2}}

        with patch.object(probes, "probe_systemd", return_value=("ok", {})), \
             patch("monitoring.services.shoutcast.fetch_shoutcast_stats", side_effect=fake_fetch):
            status, detail = probes.probe_encoder_group(self.check)

        # The unreachable server's own destination must still show up
        # (not silently dropped from the report), and the reachable
        # server's destination must be correctly evaluated as up --
        # neither result contaminates the other. This is the core of
        # item 5's requirement: the per-server loop is not short-
        # circuited by one server's failure.
        by_name = {d["name"]: d for d in detail["destinations"]}
        self.assertFalse(by_name["a-server-down"]["up"])
        self.assertTrue(by_name["b-server-up"]["up"])
        # Overall severity: server_unreachable is a single flag for the
        # WHOLE group (not tracked per-server), so ANY unreachable
        # server in the group -- even alongside an unrelated,
        # definitively-reachable one -- downgrades to "warning" rather
        # than "critical". Documented as a deliberate, conservative
        # choice (never claim CERTAIN failure off a cycle where some
        # part of the picture was unknowable) rather than a bug fixed
        # here -- production has exactly one physical Shoutcast server
        # today, so this multi-server interaction is not a live risk;
        # see the pre-deploy review report for the full note.
        self.assertEqual(status, "warning")


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


class RestartStaleStateRegressionTests(ProbeEncoderGroupFixtureMixin, TestCase):
    """Item 2 of the 2026-08-05 pre-deploy safety review: starting from
    two OLD, mutually-matching, healthy-looking state files (exactly
    what a previous manager generation would have left behind right
    before a restart), simulate the actual restart sequence and prove
    the probe cannot remain "ok" on the old generation."""

    def test_old_healthy_pair_cannot_survive_a_real_restart(self):
        make_encoder(mount="/1")

        # Old, fully healthy, mutually-consistent pair -- if the probe
        # were run against JUST this (no restart), it would correctly
        # say "ok". That's the baseline this test proves changes.
        self.write_group_state(generation="OLD-GEN", pid=42, consecutive_failures=0)
        self.write_audio_state(generation="OLD-GEN", pid=42, is_blank=False, since=self.now - 9999)
        baseline_status, _ = self.run_probe()
        self.assertEqual(baseline_status, "ok", "test setup sanity check -- the old pair must look healthy on its own")

        # Simulate the actual restart sequence: a brand-new
        # EncoderManager (fresh process, all in-memory state gone) runs
        # _start_group for this device, exactly what start() does for
        # every configured group on process startup.
        with patch.object(em.subprocess, "Popen", return_value=MagicMock(pid=999, poll=MagicMock(return_value=None))), \
             patch.object(em, "SCRIPT_DIR", Path(self._tmpdir.name) / "liquidsoap"), \
             patch.object(em, "NOW_PLAYING_PATH", str(Path(self._tmpdir.name) / "now_playing.json")):
            (Path(self._tmpdir.name) / "liquidsoap").mkdir(parents=True, exist_ok=True)
            manager = em.EncoderManager()
            ok = manager._start_group("airtap", [make_encoder(name="post-restart")])
            self.assertTrue(ok)

        # Immediately after _start_group returns (the earliest any
        # poll could plausibly land, in-process) -- must never be "ok",
        # and specifically must not be reporting the OLD generation.
        status, detail = self.run_probe()
        self.assertNotEqual(status, "ok")
        self.assertEqual(detail.get("liquidsoap_status"), "starting")

        group_state_now = json.loads(em._group_state_path_for_slug("airtap").read_text())
        audio_state_now = json.loads(em._audio_state_path_for_slug("airtap").read_text())
        self.assertNotEqual(group_state_now["generation"], "OLD-GEN")
        self.assertNotEqual(audio_state_now["generation"], "OLD-GEN")
        self.assertEqual(audio_state_now["status"], "starting")
        self.assertIsNone(audio_state_now["is_blank"])
        self.assertEqual(group_state_now["consecutive_failures"], 0)  # fresh process, not inheriting old failure history either

    def test_removal_of_stale_state_is_scoped_to_the_restarting_slug_only(self):
        """A second, unrelated group's state must be completely
        unaffected by another group's restart."""
        make_encoder(mount="/1")
        make_encoder(name="other-group", mount="/2", input_device="plughw:3,1")

        self.write_group_state(generation="OLD-GEN-A", pid=42)
        self.write_audio_state(generation="OLD-GEN-A", pid=42, is_blank=False, since=self.now - 9999)

        other_state = dict(
            input_device="plughw:3,1", pid=77, generation="OTHER-GEN", launched_at=self.now - 500,
            last_successful_start=self.now - 500, last_exit_at=None, last_exit_code=None,
            consecutive_failures=0, last_failure_message="", next_retry_at=None, timestamp=self.now,
        )
        em._atomic_write_json(em._group_state_path_for_slug("plughw_3_1"), other_state)
        em._atomic_write_json(em._audio_state_path_for_slug("plughw_3_1"), {
            "status": "audio_ok", "is_blank": False, "audio_observed": True,
            "input_device": "plughw:3,1", "pid": 77, "generation": "OTHER-GEN",
            "started_at": self.now - 500, "since": self.now - 500, "timestamp": self.now,
        })

        with patch.object(em.subprocess, "Popen", return_value=MagicMock(pid=999, poll=MagicMock(return_value=None))), \
             patch.object(em, "SCRIPT_DIR", Path(self._tmpdir.name) / "liquidsoap"), \
             patch.object(em, "NOW_PLAYING_PATH", str(Path(self._tmpdir.name) / "now_playing.json")):
            (Path(self._tmpdir.name) / "liquidsoap").mkdir(parents=True, exist_ok=True)
            manager = em.EncoderManager()
            manager._start_group("airtap", [make_encoder(name="post-restart")])

        # The OTHER group's files must be byte-for-byte untouched.
        untouched = json.loads(em._group_state_path_for_slug("plughw_3_1").read_text())
        self.assertEqual(untouched["generation"], "OTHER-GEN")
        self.assertEqual(untouched["pid"], 77)
