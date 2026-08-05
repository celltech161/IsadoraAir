"""encoders/services/encoder_manager.py tests -- script generation and
the EncoderManager supervision/retry/backoff logic added in the
2026-08-05 hardening pass. No live Liquidsoap process is ever spawned
here: subprocess.Popen is always mocked. A separate, PATH-gated test
verifies the generated script's real syntax against the actual
installed Liquidsoap on boxes that have it (matching this project's
own established "verified against this box's real installed
Liquidsoap" practice), skipping cleanly where it's absent so the suite
never depends on live infrastructure."""
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase

import encoders.services.encoder_manager as em
from encoders.models import Encoder


def make_encoder(name="test-mp3", **overrides):
    defaults = dict(
        name=name, enabled=True, protocol="shoutcast2", host="192.168.1.112",
        port=8000, mount="/1", password="secret", format="mp3", bitrate_kbps=320,
        station_name="Test Station", genre="Variety", url="https://example.com", public=False,
    )
    defaults.update(overrides)
    return Encoder(**defaults)


# ---------------------------------------------------------------------
# build_liquidsoap_script
# ---------------------------------------------------------------------
class BuildLiquidsoapScriptTests(TestCase):
    # TestCase, not SimpleTestCase: the host_aircheck=True path reads
    # AircheckConfig (a real singleton DB row) via _aircheck_block().
    def test_startup_state_is_starting_not_healthy(self):
        """Regression test for the actual root cause of the 2026-08-05
        outage: the unconditional startup write must be "starting"/null,
        never an optimistic healthy default."""
        script = em.build_liquidsoap_script("airtap", [make_encoder()], generation="gen1")
        self.assertIn('write_state("starting", null, time())', script)
        self.assertNotIn('write_state(false, time())', script)  # the old buggy call shape

    def test_generation_baked_in_as_literal(self):
        script = em.build_liquidsoap_script("airtap", [make_encoder()], generation="mygen123")
        self.assertIn('generation = "mygen123"', script)

    def test_input_device_baked_in(self):
        script = em.build_liquidsoap_script("plughw:3,1", [make_encoder()], generation="g")
        self.assertIn('input_device_str = "plughw:3,1"', script)

    def test_pid_comes_from_process_pid_not_a_baked_literal(self):
        """PID can't be known at script-generation time (the process
        doesn't exist yet) -- must be Liquidsoap's own process.pid()
        call, not something Python tried to guess/pass in."""
        script = em.build_liquidsoap_script("airtap", [make_encoder()], generation="g")
        self.assertIn("pid = process.pid()", script)

    def test_on_blank_and_on_noise_never_write_null(self):
        """Only the startup line may write is_blank=null -- a real
        transition callback firing means blank.detect has definitively
        observed something, so null (meaning "not yet verified") must
        never appear on either of these two lines."""
        script = em.build_liquidsoap_script("airtap", [make_encoder()], generation="g")
        self.assertIn('write_state("silent", true, time())', script)
        self.assertIn('write_state("audio_ok", false, time())', script)

    def test_aircheck_and_telnet_gated_together(self):
        with_aircheck = em.build_liquidsoap_script("airtap", [make_encoder()], host_aircheck=True, generation="g")
        without_aircheck = em.build_liquidsoap_script("airtap", [make_encoder()], host_aircheck=False, generation="g")
        self.assertIn("settings.server.telnet.set(true)", with_aircheck)
        self.assertIn("aircheck_output = output.file(", with_aircheck)
        self.assertNotIn("settings.server.telnet.set(true)", without_aircheck)
        self.assertNotIn("aircheck_output = output.file(", without_aircheck)

    def test_multiple_encoders_fan_out_from_one_source(self):
        script = em.build_liquidsoap_script(
            "airtap", [make_encoder(name="a", mount="/1"), make_encoder(name="b", mount="/2")],
            generation="g",
        )
        self.assertEqual(script.count("source = input.alsa"), 1)
        self.assertEqual(script.count("output.shoutcast"), 2)


class BuildLiquidsoapScriptRealSyntaxTests(TestCase):
    # TestCase: host_aircheck=True below reads AircheckConfig.
    """Actually invokes `liquidsoap --check` against generated output --
    skipped cleanly if liquidsoap isn't on PATH (CI/dev boxes without
    it), matching this project's own convention of live-verifying
    generated Liquidsoap scripts rather than only asserting on their
    text content."""

    def test_generated_script_is_valid_liquidsoap(self):
        if shutil.which("liquidsoap") is None:
            self.skipTest("liquidsoap not installed on this box")
        script = em.build_liquidsoap_script(
            "airtap",
            [make_encoder(name="a", format="mp3", mount="/1"), make_encoder(name="b", format="aac", mount="/2", bitrate_kbps=64)],
            host_aircheck=True, generation="synxtest",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "test.liq"
            script_path.write_text(script, encoding="utf-8")
            result = subprocess.run(
                ["liquidsoap", "--check", str(script_path)],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


# ---------------------------------------------------------------------
# EncoderManager
# ---------------------------------------------------------------------
class EncoderManagerFixtureMixin:
    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.state_dir = Path(self._tmpdir.name)
        self.script_dir = self.state_dir / "liquidsoap"
        self.script_dir.mkdir(parents=True)
        for patcher in (
            patch.object(em, "STATE_DIR", self.state_dir),
            patch.object(em, "SCRIPT_DIR", self.script_dir),
            patch.object(em, "NOW_PLAYING_PATH", str(self.state_dir / "now_playing.json")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self._fake_pid_counter = 1000
        self._popen_should_fail = False
        self._live_procs = {}  # pid -> MagicMock, so tests can flip .poll() later

        def fake_popen(cmd, **kwargs):
            if self._popen_should_fail:
                raise OSError("no such file or directory: liquidsoap")
            self._fake_pid_counter += 1
            proc = MagicMock()
            proc.pid = self._fake_pid_counter
            proc.poll.return_value = None
            proc.returncode = None
            self._live_procs[proc.pid] = proc
            return proc

        popen_patcher = patch.object(em.subprocess, "Popen", side_effect=fake_popen)
        popen_patcher.start()
        self.addCleanup(popen_patcher.stop)

    def read_audio_state(self, slug="airtap"):
        return json.loads(em._audio_state_path_for_slug(slug).read_text())

    def read_group_state(self, slug="airtap"):
        return json.loads(em._group_state_path_for_slug(slug).read_text())

    def exit_current_child(self, manager, input_device, returncode=1):
        """Simulate the currently-running child dying -- flips its
        mock .poll()/.returncode and calls the manager's own exit
        handling, matching what _check_health would observe."""
        current = manager._current[input_device]
        proc = self._live_procs[current["pid"]]
        proc.poll.return_value = returncode
        proc.returncode = returncode


class LaunchTests(EncoderManagerFixtureMixin, TransactionTestCase):
    def test_successful_launch_writes_starting_state_not_healthy(self):
        manager = em.EncoderManager()
        ok = manager._start_group("airtap", [make_encoder()])
        self.assertTrue(ok)

        audio_state = self.read_audio_state()
        self.assertEqual(audio_state["status"], "starting")
        self.assertIsNone(audio_state["is_blank"])
        self.assertFalse(audio_state["audio_observed"])
        self.assertEqual(audio_state["input_device"], "airtap")

        group_state = self.read_group_state()
        self.assertEqual(group_state["consecutive_failures"], 0)
        self.assertIsNotNone(group_state["pid"])
        self.assertIsNotNone(group_state["generation"])

    def test_repeated_launch_uses_a_fresh_generation_each_time(self):
        manager = em.EncoderManager()
        manager._start_group("airtap", [make_encoder()])
        gen1 = manager._current["airtap"]["generation"]
        manager._start_group("airtap", [make_encoder()])
        gen2 = manager._current["airtap"]["generation"]
        self.assertNotEqual(gen1, gen2)

    def test_popen_failure_returns_false_and_records_failure(self):
        manager = em.EncoderManager()
        self._popen_should_fail = True
        ok = manager._start_group("airtap", [make_encoder()])
        self.assertFalse(ok)
        meta = manager._group_meta("airtap")
        self.assertEqual(meta["consecutive_failures"], 1)
        self.assertIn("Popen failed", meta["last_failure_message"])

    def test_popen_failure_enters_retry_schedule_not_abandoned(self):
        """Phase 7 req #1: a failed initial launch must enter the same
        retry schedule a later child-exit would."""
        manager = em.EncoderManager()
        self._popen_should_fail = True
        with patch.object(em, "_group_by_input_device", return_value={"airtap": [make_encoder()]}):
            ok = manager._start_group("airtap", [make_encoder()])
            self.assertFalse(ok)
            delay = manager._schedule_retry("airtap")
        self.assertEqual(delay, em.RETRY_BACKOFF_SECONDS[0])
        self.assertIn("airtap", manager._retry_at)

    def test_invalidates_stale_audio_state_from_previous_generation(self):
        """Phase 8 req #2/#7: a monitoring poll landing between Popen()
        returning and the script's own first line executing must never
        see a PREVIOUS generation's possibly-"audio_ok" content."""
        em._audio_state_path_for_slug("airtap").parent.mkdir(parents=True, exist_ok=True)
        em._atomic_write_json(em._audio_state_path_for_slug("airtap"), {
            "status": "audio_ok", "is_blank": False, "generation": "old-gen", "pid": 1,
            "timestamp": time.time(),
        })
        manager = em.EncoderManager()
        manager._start_group("airtap", [make_encoder()])
        audio_state = self.read_audio_state()
        self.assertEqual(audio_state["status"], "starting")
        self.assertNotEqual(audio_state["generation"], "old-gen")


class StabilizationTests(EncoderManagerFixtureMixin, TransactionTestCase):
    def _write_audio_state(self, generation, is_blank, since, pid=None):
        em._atomic_write_json(em._audio_state_path_for_slug("airtap"), {
            "status": "audio_ok" if is_blank is False else "starting",
            "is_blank": is_blank, "audio_observed": True, "generation": generation,
            "pid": pid, "since": since, "timestamp": time.time(),
        })

    def test_resets_backoff_after_sustained_health(self):
        manager = em.EncoderManager()
        manager._start_group("airtap", [make_encoder()])
        manager._retry_index["airtap"] = 3  # simulate prior failures
        gen = manager._current["airtap"]["generation"]
        pid = manager._current["airtap"]["pid"]
        self._write_audio_state(gen, False, time.time() - em.STABILIZATION_SECONDS - 1, pid=pid)

        manager._check_stabilization("airtap")

        self.assertTrue(manager._stabilized["airtap"])
        self.assertEqual(manager._retry_index["airtap"], 0)
        self.assertEqual(manager._group_meta("airtap")["consecutive_failures"], 0)

    def test_not_yet_long_enough_does_not_reset(self):
        manager = em.EncoderManager()
        manager._start_group("airtap", [make_encoder()])
        manager._retry_index["airtap"] = 2
        gen = manager._current["airtap"]["generation"]
        self._write_audio_state(gen, False, time.time() - 2)  # only 2s, not enough

        manager._check_stabilization("airtap")

        self.assertFalse(manager._stabilized.get("airtap", False))
        self.assertEqual(manager._retry_index["airtap"], 2)

    def test_mismatched_generation_never_counts(self):
        """A stale read (audio-state file not yet touched by the
        CURRENT child) must never trigger a stabilization reset."""
        manager = em.EncoderManager()
        manager._start_group("airtap", [make_encoder()])
        manager._retry_index["airtap"] = 4
        self._write_audio_state("some-other-generation", False, time.time() - 1000)

        manager._check_stabilization("airtap")

        self.assertFalse(manager._stabilized.get("airtap", False))
        self.assertEqual(manager._retry_index["airtap"], 4)

    def test_is_blank_null_never_counts_as_stable(self):
        manager = em.EncoderManager()
        manager._start_group("airtap", [make_encoder()])
        gen = manager._current["airtap"]["generation"]
        self._write_audio_state(gen, None, time.time() - 1000)

        manager._check_stabilization("airtap")

        self.assertFalse(manager._stabilized.get("airtap", False))

    def test_successful_popen_alone_does_not_reset_backoff(self):
        """Phase 7 req #6 -- launching successfully is not the same as
        being healthy."""
        manager = em.EncoderManager()
        manager._retry_index["airtap"] = 3
        manager._start_group("airtap", [make_encoder()])
        self.assertEqual(manager._retry_index["airtap"], 3)  # unchanged by the launch itself


class CrashLoopRegressionTests(EncoderManagerFixtureMixin, TransactionTestCase):
    """The exact scenario required by the hardening spec: Liquidsoap
    launches, never fires on_noise, exits with an ALSA-style failure,
    the manager retries, the replacement also dies before observing
    audio, repeated several times. Monitoring must stay unable to see
    "healthy" throughout -- a fresh startup timestamp must never look
    like a stable, confirmed-good stream."""

    def test_repeated_crash_before_any_audio_observed_never_looks_healthy(self):
        manager = em.EncoderManager()
        encoders = [make_encoder()]

        with patch.object(em, "_group_by_input_device", return_value={"airtap": encoders}):
            ok = manager._start_group("airtap", encoders)
            self.assertTrue(ok)

            for i in range(5):
                # Audio state at this point must be exactly "starting"/null
                # -- never anything that could read as healthy.
                audio_state = self.read_audio_state()
                self.assertEqual(audio_state["status"], "starting", f"iteration {i}")
                self.assertIsNone(audio_state["is_blank"], f"iteration {i}")

                self.exit_current_child(manager, "airtap", returncode=1)
                manager._handle_exit("airtap", 1)

                # The manager's own state must reflect the crash loop,
                # not silently look like nothing happened.
                group_state = self.read_group_state()
                self.assertEqual(group_state["consecutive_failures"], i + 1)
                self.assertIsNone(group_state["pid"])  # no live child right now

                # Force the retry to fire immediately (real backoff
                # timing is exercised separately in BackoffTests).
                manager._retry_at["airtap"] = 0
                self._popen_should_fail = False
                ok = manager._start_group("airtap", encoders)
                self.assertTrue(ok)

        # Final state after 5 straight crashes: still "starting", never
        # audio_ok, consecutive_failures reflects the full history, and
        # a naive "is there a state file with a fresh timestamp" check
        # would be WRONG to call this healthy.
        final_audio = self.read_audio_state()
        self.assertEqual(final_audio["status"], "starting")
        self.assertIsNone(final_audio["is_blank"])
        self.assertFalse(final_audio["audio_observed"])

    def test_consecutive_failures_reaching_three_reads_as_crash_looping(self):
        """Cross-check against the monitoring probe's own decision
        table threshold (probe_encoder_group treats consecutive_failures
        >= 3 as critical crash-looping) -- exercised end-to-end via
        probes in monitoring/tests/test_probe_encoder_group.py; this
        test just confirms the manager-side counter that feeds it
        actually reaches and holds that value correctly."""
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        manager._start_group("airtap", encoders)
        for _ in range(3):
            self.exit_current_child(manager, "airtap", returncode=1)
            manager._handle_exit("airtap", 1)
            manager._retry_at["airtap"] = 0
            manager._start_group("airtap", encoders)
        self.assertGreaterEqual(self.read_group_state()["consecutive_failures"], 3)


class BackoffTests(EncoderManagerFixtureMixin, TransactionTestCase):
    def test_exponential_progression(self):
        manager = em.EncoderManager()
        delays = [manager._schedule_retry("airtap") for _ in range(len(em.RETRY_BACKOFF_SECONDS) + 2)]
        self.assertEqual(delays[:len(em.RETRY_BACKOFF_SECONDS)], em.RETRY_BACKOFF_SECONDS)
        # Caps at the final value rather than raising/growing further.
        self.assertEqual(delays[-1], em.RETRY_BACKOFF_SECONDS[-1])
        self.assertEqual(delays[-2], em.RETRY_BACKOFF_SECONDS[-1])

    def test_two_groups_retry_independently(self):
        manager = em.EncoderManager()
        manager._schedule_retry("airtap")
        manager._schedule_retry("airtap")
        delay_b = manager._schedule_retry("plughw:3,1")
        self.assertEqual(delay_b, em.RETRY_BACKOFF_SECONDS[0])  # unaffected by airtap's own progress
        self.assertEqual(manager._retry_index["airtap"], 2)
        self.assertEqual(manager._retry_index["plughw:3,1"], 1)

    def test_does_not_block_manager_loop(self):
        """_schedule_retry must be a pure scheduling call -- no sleep()."""
        manager = em.EncoderManager()
        with patch.object(em.time, "sleep") as mock_sleep:
            manager._schedule_retry("airtap")
        mock_sleep.assert_not_called()


class DisabledEncoderRetryTests(EncoderManagerFixtureMixin, TransactionTestCase):
    def test_no_enabled_encoders_left_drops_retry_bookkeeping(self):
        manager = em.EncoderManager()
        manager._retry_index["airtap"] = 3
        manager._meta["airtap"] = manager._group_meta("airtap")
        manager._retry_at["airtap"] = 0.0  # due immediately

        with patch.object(em, "_group_by_input_device", return_value={}):
            manager._check_health()

        self.assertNotIn("airtap", manager._retry_index)
        self.assertNotIn("airtap", manager._meta)
        self.assertFalse(em._group_state_path_for_slug("airtap").exists())

    def test_re_enabling_later_starts_backoff_fresh(self):
        """A group dropped for "no enabled encoders" and later
        re-enabled must resume the backoff schedule from the front
        (index 0), not from wherever a long-abandoned retry sequence
        had escalated to (index 3 here) -- Phase 7 req #10/#11."""
        manager = em.EncoderManager()
        manager._retry_index["airtap"] = 3
        manager._retry_at["airtap"] = 0.0
        with patch.object(em, "_group_by_input_device", return_value={}):
            manager._check_health()
        self.assertNotIn("airtap", manager._retry_index)

        # Re-enable, and have THIS attempt fail too -- the resulting
        # backoff delay must be the schedule's first step, not a
        # continuation of the old index-3 escalation.
        manager._retry_at["airtap"] = 0.0
        self._popen_should_fail = True
        with patch.object(em, "_group_by_input_device", return_value={"airtap": [make_encoder()]}):
            manager._check_health()
        group_state = self.read_group_state()
        self.assertAlmostEqual(group_state["next_retry_at"] - time.time(), em.RETRY_BACKOFF_SECONDS[0], delta=2)


class StopTests(EncoderManagerFixtureMixin, TransactionTestCase):
    def test_stop_marks_audio_state_dead_and_removes_group_state(self):
        manager = em.EncoderManager()
        manager._start_group("airtap", [make_encoder()])
        manager.stop()

        audio_state = self.read_audio_state()
        self.assertEqual(audio_state["status"], "dead")
        self.assertFalse(em._group_state_path_for_slug("airtap").exists())

    def test_stop_terminates_the_child_process(self):
        manager = em.EncoderManager()
        manager._start_group("airtap", [make_encoder()])
        proc = self._live_procs[manager._current["airtap"]["pid"]]
        manager.stop()
        proc.terminate.assert_called_once()


class ExitHandlingTests(EncoderManagerFixtureMixin, TransactionTestCase):
    def test_exit_marks_audio_state_dead_immediately(self):
        manager = em.EncoderManager()
        manager._start_group("airtap", [make_encoder()])
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)
        self.assertEqual(self.read_audio_state()["status"], "dead")
        self.assertNotIn("airtap", manager._current)  # Phase 3 req #4: no live child right now

    def test_crash_looping_escalates_event_severity(self):
        """Third+ consecutive failure in a row should escalate to
        error-level, not stay at warning forever."""
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        manager._start_group("airtap", encoders)
        with patch("encoders.services.encoder_manager.emit_event") as mock_emit:
            for _ in range(3):
                self.exit_current_child(manager, "airtap", returncode=1)
                manager._handle_exit("airtap", 1)
                manager._retry_at["airtap"] = 0
                manager._start_group("airtap", encoders)
        levels = [call.kwargs.get("level") for call in mock_emit.call_args_list]
        self.assertIn("error", levels)
