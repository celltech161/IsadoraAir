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
from encoders.services import lkg as lkg_module
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
        # Phase 3 test-isolation fix: this file never patched lkg_module.
        # LKG_DIR, only encoder_manager.STATE_DIR -- harmless as long as
        # no REAL last-known-good has ever been promoted on whatever box
        # runs these tests (lkg.read_lkg("airtap") then correctly finds
        # nothing and every test's own group-state/audio-state fixture is
        # the only signal). Once this project's own first real production
        # LKG was promoted (2026-08-10, Phase 2 deployment), probe_
        # encoder_group's own resolve_expected_destinations call started
        # silently preferring that REAL on-disk LKG's destinations over
        # each test's own fabricated single encoder for every launch_kind
        # other than "candidate" (see lkg.resolve_expected_destinations) --
        # a genuine, pre-existing test-isolation gap, invisible until
        # production state on the actual dev box changed, unrelated to
        # any Phase 3 code change. Patched here the same way STATE_DIR
        # already is.
        lkg_patcher = patch.object(lkg_module, "LKG_DIR", Path(self._tmpdir.name) / "lkg")
        lkg_patcher.start()
        self.addCleanup(lkg_patcher.stop)
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


# ---------------------------------------------------------------------
# Phase 2 review-fix pass 2, Issue 1: after a rollback (or any time the
# DB's desired configuration has drifted from what's actually accepted/
# running), the ordinary dashboard probe must check the LKG's own
# frozen destinations, never the DB's rejected/not-yet-applied ones.
# ---------------------------------------------------------------------
class PostRollbackDestinationResolutionTests(ProbeEncoderGroupFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        lkg_tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(lkg_tmpdir.cleanup)
        base = Path(lkg_tmpdir.name)
        for patcher in (
            patch.object(lkg_module, "CANDIDATE_DIR", base / "candidate"),
            patch.object(lkg_module, "LKG_DIR", base / "lkg"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write_lkg_sid1(self, host="10.0.0.5", port=8000):
        # protocol="shoutcast2" (roadmap 3.10): real production LKG
        # metadata has carried this since Phase 3M, well before provider
        # presets existed -- included here so these SID-resolution tests
        # keep routing through the external Shoutcast DNAS /statistics
        # probe they're actually testing, rather than the new generic
        # Liquidsoap destination-connection signal reserved for
        # Icecast/non-generic-provider destinations (see monitoring/
        # services/probes.py's _uses_shoutcast_dnas_probe). `provider`
        # is deliberately left OUT -- exercising destinations_from_lkg_
        # meta's legacy "no provider recorded" -> "generic" default.
        lkg_module.write_lkg("airtap", 'generation = "lkg-gen"\n', {
            "fingerprint": "lkg-fp",
            "destinations": [{
                "encoder_id": 1, "name": "lkg-enc", "host": host, "port": port,
                "shoutcast_sid": "1", "protocol": "shoutcast2",
            }],
        })

    def test_a_rollback_group_checks_lkg_sid_not_rejected_db_sid(self):
        """Test A from the review: LKG SID 1, DB desired SID 5, SID 1
        up, SID 5 down. EXPECTED: healthy, and the destination checked
        is SID 1, not SID 5."""
        make_encoder(name="desired-bad", mount="/5")  # the rejected DB row
        self._write_lkg_sid1()
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)

        status, detail = self.run_probe(shoutcast_stats={"1": {"up": True, "listeners": 2}, "5": {"up": False, "listeners": 0}})

        self.assertEqual(status, "ok")
        self.assertEqual([d["sid"] for d in detail["destinations"]], ["1"])

    def test_b_rollback_group_does_not_false_green_off_the_rejected_sid(self):
        """Test B from the review: LKG SID 1 down, DB desired SID 5
        (rejected) up. EXPECTED: critical -- must NOT report healthy
        just because the rejected SID happens to be reachable; it is
        not what's actually running."""
        make_encoder(name="desired-bad", mount="/5")
        self._write_lkg_sid1()
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)

        status, detail = self.run_probe(shoutcast_stats={"1": {"up": False, "listeners": 0}, "5": {"up": True, "listeners": 9}})

        self.assertEqual(status, "critical")
        self.assertEqual([d["sid"] for d in detail["destinations"]], ["1"])

    def test_rollback_launch_kind_also_checks_lkg_not_db(self):
        """launch_kind="rollback" (still mid-requalification, not yet
        settled back to "accepted") must resolve the same way."""
        make_encoder(name="desired-bad", mount="/5")
        self._write_lkg_sid1()
        self.write_group_state(launch_kind="rollback")
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)

        status, detail = self.run_probe(shoutcast_stats={"1": {"up": True, "listeners": 2}})

        self.assertEqual(status, "ok")
        self.assertEqual([d["sid"] for d in detail["destinations"]], ["1"])

    def test_candidate_launch_kind_checks_db_rows_directly(self):
        """The complementary case: while a group is actively proving a
        NEW candidate (launch_kind="candidate"), the DB's desired rows
        ARE what's running -- the probe must check THOSE, not a stale
        LKG left over from a previous, different configuration."""
        make_encoder(name="new-candidate", mount="/9")
        self._write_lkg_sid1()  # a DIFFERENT, still-valid old LKG
        self.write_group_state(launch_kind="candidate")
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)

        status, detail = self.run_probe(shoutcast_stats={"9": {"up": True, "listeners": 1}})

        self.assertEqual(status, "ok")
        self.assertEqual([d["sid"] for d in detail["destinations"]], ["9"])

    def test_host_port_change_scenario(self):
        """Host/port variant of Test A/B, per the review's explicit
        ask -- a server migration, not just a SID edit."""
        make_encoder(name="desired-bad", host="10.0.0.99", port=9000, mount="/1")
        self._write_lkg_sid1(host="10.0.0.5", port=8000)
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)

        def fake_fetch(host, port, timeout=3.0):
            if (host, port) == ("10.0.0.5", 8000):
                return {"1": {"up": True, "listeners": 4}}
            return {}  # the rejected/abandoned server -- must never be consulted

        with patch.object(probes, "probe_systemd", return_value=("ok", {})), \
             patch("monitoring.services.shoutcast.fetch_shoutcast_stats", side_effect=fake_fetch):
            status, detail = probes.probe_encoder_group(self.check)

        self.assertEqual(status, "ok")

    def test_no_lkg_bootstrap_still_checks_db_rows(self):
        """No LKG exists at all (fresh install / never promoted) --
        must fall back to the DB's own desired rows, matching this
        group's pre-Phase-2 (and pre-first-promotion) behavior."""
        make_encoder(name="only-config", mount="/3")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)

        status, detail = self.run_probe(shoutcast_stats={"3": {"up": True, "listeners": 1}})

        self.assertEqual(status, "ok")
        self.assertEqual([d["sid"] for d in detail["destinations"]], ["3"])

    def test_missing_group_state_file_falls_back_to_db_rows_via_accepted_default(self):
        """No group-state file at all -- _read_group_launch_kind
        defaults to "accepted", and with no LKG either, resolve_
        expected_destinations' own bootstrap fallback applies. (A
        missing group-state file is ALSO independently caught earlier,
        by evaluate_encoder_group_health's own "no group-state file
        yet" critical check -- this test only confirms the destination-
        resolution step itself doesn't error out before reaching that.)"""
        make_encoder(name="only-config", mount="/3")
        status, detail = self.run_probe(shoutcast_stats={"3": {"up": True, "listeners": 1}})
        self.assertEqual(status, "critical")
        self.assertIn("no group-state file", detail["reason"])


# ---------------------------------------------------------------------
# Roadmap 3.10 -- generic Liquidsoap destination connection-state signal
# (the audio-state file's own "destinations" list, written by
# encoder_manager.py's build_liquidsoap_script via each output's
# on_connect/on_disconnect). Routed by _uses_shoutcast_dnas_probe:
# provider=="generic" Shoutcast 1/2 keeps the external DNAS
# /statistics probe (unchanged, covered above); everything else
# (Icecast of any provider, Radio.co) uses this signal instead.
# ---------------------------------------------------------------------
class GenericDestinationConnectionStateTests(ProbeEncoderGroupFixtureMixin, TestCase):
    def _fetch_stats_must_not_be_called(self, host, port, timeout=3.0):
        raise AssertionError(
            f"fetch_shoutcast_stats({host!r}, {port!r}) was called -- a non-DNAS "
            "destination must never be routed through the external Shoutcast "
            "/statistics probe."
        )

    def run_probe_no_dnas(self, systemd_status="ok"):
        """Like ProbeEncoderGroupFixtureMixin.run_probe, but asserts the
        external Shoutcast stats fetcher is never even called -- for
        scenarios where every configured destination should route
        through the generic connection-state signal instead."""
        with patch.object(probes, "probe_systemd", return_value=(systemd_status, {})), \
             patch("monitoring.services.shoutcast.fetch_shoutcast_stats", side_effect=self._fetch_stats_must_not_be_called):
            return probes.probe_encoder_group(self.check)

    def test_connected_icecast_destination_qualifies(self):
        enc = make_encoder(name="icecast-1", protocol="icecast", provider="generic", mount="/stream", username="source")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[{"id": str(enc.id), "connected": True, "since": self.now - 200}],
        )
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "ok")
        self.assertEqual(detail["generic_destinations"], [
            {"encoder_id": enc.id, "name": "icecast-1", "connected": True, "reporting": True},
        ])
        # And the (DNAS-only) "destinations" key stays empty -- nothing
        # here was ever a Shoutcast DNAS candidate.
        self.assertEqual(detail["destinations"], [])

    def test_disconnected_icecast_destination_cannot_qualify(self):
        enc = make_encoder(name="icecast-1", protocol="icecast", provider="generic", mount="/stream", username="source")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[{"id": str(enc.id), "connected": False, "since": self.now - 5}],
        )
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "critical")
        self.assertIn("not connected", detail["reason"])

    def test_never_reported_destination_cannot_qualify(self):
        """No "destinations" key at all in the audio-state file (e.g. a
        generation launched before this feature existed) -- must fail
        closed, never read as healthy just because nothing explicitly
        said "false.\""""
        make_encoder(name="icecast-1", protocol="icecast", provider="generic", mount="/stream", username="source")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)  # no "destinations" override at all
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "critical")
        self.assertIn("not yet reported", detail["reason"])

    def test_stale_old_generation_connected_state_cannot_qualify_new_child(self):
        """A "destinations": connected=true entry belonging to a
        SUPERSEDED generation must not make a brand-new child look
        healthy -- covered generically by evaluate_encoder_group_
        health's existing generation/pid cross-check (which runs before
        the destinations logic at all, so it protects the WHOLE
        audio-state document, this new field included, with no
        additional code) -- this test locks that in explicitly for the
        new field specifically."""
        enc = make_encoder(name="icecast-1", protocol="icecast", provider="generic", mount="/stream", username="source")
        self.write_group_state(launch_kind="accepted", generation="NEW-GEN", pid=999)
        self.write_audio_state(
            generation="OLD-GEN", pid=555,  # stale -- doesn't match group_state's generation/pid
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[{"id": str(enc.id), "connected": True, "since": self.now - 200}],
        )
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "critical")
        self.assertIn("stale", detail["reason"])

    def test_multiple_destinations_all_must_be_connected(self):
        e1 = make_encoder(name="icecast-1", protocol="icecast", provider="generic", mount="/one", username="source")
        e2 = make_encoder(name="icecast-2", protocol="icecast", provider="generic", mount="/two", username="source")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[
                {"id": str(e1.id), "connected": True, "since": self.now - 200},
                {"id": str(e2.id), "connected": False, "since": self.now - 5},
            ],
        )
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "critical")
        self.assertIn(f"icecast-2 (id={e2.id})", detail["reason"])
        self.assertNotIn(f"icecast-1 (id={e1.id})", detail["reason"])

        # Now both connected -- must qualify.
        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[
                {"id": str(e1.id), "connected": True, "since": self.now - 200},
                {"id": str(e2.id), "connected": True, "since": self.now - 200},
            ],
        )
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "ok")

    def test_live365_icecast_destination_not_routed_through_dnas(self):
        enc = make_encoder(name="live365-1", protocol="icecast", provider="live365", mount="/live", username="source", format="mp3")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[{"id": str(enc.id), "connected": True, "since": self.now - 200}],
        )
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "ok")
        self.assertEqual(detail["destinations"], [])

    def test_radio_co_shoutcast1_destination_not_routed_through_dnas(self):
        """Radio.co uses protocol=="shoutcast1" -- the SAME protocol a
        generic Shoutcast 1 row would use -- but must NOT be assumed to
        expose a normal DNAS /statistics endpoint just because the wire
        protocol matches."""
        enc = make_encoder(name="radio-co-1", protocol="shoutcast1", provider="radio_co", mount="")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[{"id": str(enc.id), "connected": True, "since": self.now - 200}],
        )
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "ok")
        self.assertEqual(detail["destinations"], [])
        self.assertEqual(detail["generic_destinations"], [
            {"encoder_id": enc.id, "name": "radio-co-1", "connected": True, "reporting": True},
        ])

    def test_generic_shoutcast_sibling_still_uses_dnas_alongside_generic_icecast(self):
        """A mixed group -- one generic Shoutcast 2 destination (DNAS)
        and one Live365 Icecast destination (generic signal) -- both
        must independently qualify for the group to be "ok", and each
        must be checked through its OWN correct path."""
        sc = make_encoder(name="generic-sc", protocol="shoutcast2", provider="generic", mount="/1")
        ic = make_encoder(name="live365-ic", protocol="icecast", provider="live365", mount="/live", username="source")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[{"id": str(ic.id), "connected": True, "since": self.now - 200}],
        )
        with patch.object(probes, "probe_systemd", return_value=("ok", {})), \
             patch("monitoring.services.shoutcast.fetch_shoutcast_stats", return_value={"1": {"up": True, "listeners": 2}}) as mock_fetch:
            status, detail = probes.probe_encoder_group(self.check)
        self.assertEqual(status, "ok")
        mock_fetch.assert_called_once()
        self.assertEqual([d["sid"] for d in detail["destinations"]], ["1"])
        self.assertEqual([d["connected"] for d in detail["generic_destinations"]], [True])

        # Break ONLY the generic Icecast side -- group must go critical
        # even though the Shoutcast DNAS side is still fine.
        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[{"id": str(ic.id), "connected": False, "since": self.now - 5}],
        )
        with patch.object(probes, "probe_systemd", return_value=("ok", {})), \
             patch("monitoring.services.shoutcast.fetch_shoutcast_stats", return_value={"1": {"up": True, "listeners": 2}}):
            status, detail = probes.probe_encoder_group(self.check)
        self.assertEqual(status, "critical")

    def test_mixed_group_live365_healthy_generic_shoutcast_streamstatus_down(self):
        """The mirror image of the sibling test above -- Live365's own
        generic signal is fine, but the generic Shoutcast side's real
        DNAS STREAMSTATUS is down. Group must still be critical; a
        healthy Icecast/generic-signal destination must never mask a
        genuinely down Shoutcast destination."""
        sc = make_encoder(name="generic-sc", protocol="shoutcast2", provider="generic", mount="/1")
        ic = make_encoder(name="live365-ic", protocol="icecast", provider="live365", mount="/live", username="source")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[{"id": str(ic.id), "connected": True, "since": self.now - 200}],
        )
        with patch.object(probes, "probe_systemd", return_value=("ok", {})), \
             patch("monitoring.services.shoutcast.fetch_shoutcast_stats", return_value={"1": {"up": False, "listeners": 0}}):
            status, detail = probes.probe_encoder_group(self.check)
        self.assertEqual(status, "critical")
        self.assertIn("destination(s) down", detail["reason"])
        self.assertEqual([d["connected"] for d in detail["generic_destinations"]], [True])
        self.assertEqual([d["up"] for d in detail["destinations"]], [False])

    def test_mixed_generic_shoutcast_and_radio_co_only_generic_queried_via_dnas(self):
        """Three-provider-shape mix: generic Shoutcast2 (DNAS-eligible)
        alongside Radio.co (Shoutcast1, explicitly NOT DNAS-eligible
        despite sharing the Shoutcast wire protocol). Spy on the fetcher
        to prove exactly one call was made, for exactly the generic
        Shoutcast destination's own host:port -- never Radio.co's."""
        sc = make_encoder(name="generic-sc", protocol="shoutcast2", provider="generic",
                           host="10.0.0.1", port=8010, mount="/1")
        rc = make_encoder(name="radio-co-1", protocol="shoutcast1", provider="radio_co",
                           host="listen.radio.co", port=8000, mount="")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[{"id": str(rc.id), "connected": True, "since": self.now - 200}],
        )
        with patch.object(probes, "probe_systemd", return_value=("ok", {})), \
             patch("monitoring.services.shoutcast.fetch_shoutcast_stats", return_value={"1": {"up": True, "listeners": 2}}) as mock_fetch:
            status, detail = probes.probe_encoder_group(self.check)
        self.assertEqual(status, "ok")
        mock_fetch.assert_called_once_with("10.0.0.1", 8010)
        self.assertEqual([d["sid"] for d in detail["destinations"]], ["1"])
        self.assertEqual([d["connected"] for d in detail["generic_destinations"]], [True])

    def test_multiple_hosted_provider_destinations_all_must_be_connected(self):
        """Multiple hosted-provider destinations (Live365 + Radio.co)
        sharing one group -- every one of them must independently be
        connected; one being fine never covers for another being
        down."""
        live365 = make_encoder(name="live365-1", protocol="icecast", provider="live365",
                                mount="/live", username="source", host="stream.live365.com")
        radio_co = make_encoder(name="radio-co-1", protocol="shoutcast1", provider="radio_co",
                                 mount="", host="listen.radio.co")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[
                {"id": str(live365.id), "connected": True, "since": self.now - 200},
                {"id": str(radio_co.id), "connected": False, "since": self.now - 5},
            ],
        )
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "critical")
        self.assertIn(f"radio-co-1 (id={radio_co.id})", detail["reason"])

        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[
                {"id": str(live365.id), "connected": True, "since": self.now - 200},
                {"id": str(radio_co.id), "connected": True, "since": self.now - 200},
            ],
        )
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "ok")
        self.assertEqual(detail["destinations"], [])  # neither ever queried via DNAS

    def test_destination_state_cooperates_with_stabilization_gate(self):
        """All destinations connected, but not yet stable long enough --
        must still be "warning", not "ok". The generic destination
        signal doesn't bypass the existing stabilization requirement."""
        enc = make_encoder(name="icecast-1", protocol="icecast", provider="generic", mount="/stream", username="source")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(
            since=self.now - 2,  # well under STABILIZATION_SECONDS
            destinations=[{"id": str(enc.id), "connected": True, "since": self.now - 200}],
        )
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "warning")
        self.assertIn("stabilizing", detail["reason"])

    def test_generic_destination_detail_never_contains_password(self):
        """Credential redaction: evaluate_encoder_group_health's own
        detail dict (surfaced to the dashboard and to emitted
        monitoring events) must never carry a destination's password --
        the new generic_destinations/destinations detail entries only
        ever hold id/name/connected/reporting, never a raw Encoder
        field dump."""
        secret = "sUp3rS3cr3tPassw0rd!!"
        enc = make_encoder(name="icecast-1", protocol="icecast", provider="generic", mount="/stream", username="source", password=secret)
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[{"id": str(enc.id), "connected": False, "since": self.now - 5}],
        )
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "critical")
        self.assertNotIn(secret, json.dumps(detail))

    def test_destination_state_cooperates_with_blank_audio_check(self):
        """Silent audio must still short-circuit to critical BEFORE the
        destination-connection check is ever consulted, regardless of
        what the destinations list says."""
        enc = make_encoder(name="icecast-1", protocol="icecast", provider="generic", mount="/stream", username="source")
        self.write_group_state(launch_kind="accepted")
        self.write_audio_state(
            is_blank=True,
            since=self.now - em.STABILIZATION_SECONDS - 5,
            destinations=[{"id": str(enc.id), "connected": True, "since": self.now - 200}],
        )
        status, detail = self.run_probe_no_dnas()
        self.assertEqual(status, "critical")
        self.assertIn("silent", detail["reason"])


# ---------------------------------------------------------------------
# Roadmap 3.10 pre-commit review, item 7 -- the ENCODER_CONFIG_FORMAT_
# VERSION 1 -> 2 transition. A pre-existing v1 LKG script (rendered
# before this feature existed) structurally never writes a
# "destinations" key into its audio-state file at all -- proves the
# REAL evaluate_encoder_group_health against that exact legacy shape,
# for a generic Shoutcast2 destination (the real, current production
# topology), independent of test_reconciliation.py's own
# ReconciliationFixtureMixin (which mocks this function for its whole
# test body by design and so can't exercise the real thing here).
# ---------------------------------------------------------------------
class LegacyPreV2AudioStateDnasStillWorksTests(ProbeEncoderGroupFixtureMixin, TestCase):
    def test_missing_destinations_key_entirely_does_not_block_dnas_qualification(self):
        enc = make_encoder(name="generic-sc", protocol="shoutcast2", provider="generic", mount="/1")
        self.write_group_state(launch_kind="rollback")
        # No "destinations" override at all -- write_audio_state's own
        # defaults don't include one either, exactly matching what a
        # legacy pre-3.10 Liquidsoap script's write_state() has always
        # produced (the field didn't exist before this feature).
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)
        status, detail = self.run_probe(shoutcast_stats={"1": {"up": True, "listeners": 4}})
        self.assertEqual(status, "ok")
        self.assertEqual([d["sid"] for d in detail["destinations"]], ["1"])
        # The (empty, absent-key) generic side never even factors in --
        # this destination was never generic-signal-eligible to begin
        # with (provider="generic" + protocol="shoutcast2").
        self.assertEqual(detail["generic_destinations"], [])

    def test_missing_destinations_key_with_dnas_down_is_still_correctly_critical(self):
        """Companion negative case -- absence of the new field must not
        accidentally soften a REAL Shoutcast outage either; DNAS is
        still the authoritative, independent signal it always was."""
        make_encoder(name="generic-sc", protocol="shoutcast2", provider="generic", mount="/1")
        self.write_group_state(launch_kind="rollback")
        self.write_audio_state(since=self.now - em.STABILIZATION_SECONDS - 5)
        status, detail = self.run_probe(shoutcast_stats={"1": {"up": False, "listeners": 0}})
        self.assertEqual(status, "critical")
        self.assertIn("destination(s) down", detail["reason"])
