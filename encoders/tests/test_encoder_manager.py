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

    def test_blank_detect_left_at_default_start_blank(self):
        """2026-08-06 post-deployment investigation, round 2:
        blank.detect's own documented default is start_blank=false
        (assumes NOISE from the first frame), so a restart landing on
        already-continuously-non-silent audio never fires on_noise at
        all (no blank->noise transition to observe) -- confirmed
        empirically via an isolated synthetic Liquidsoap script.
        start_blank=true was tried as the fix and reverted: also
        confirmed empirically to spuriously fire on_noise within
        ~0.05s for genuinely continuous SILENCE (a false "audio_ok",
        worse than the original bug). Neither value is safe to rely on
        alone -- the startup classifier (see StartupClassifierScriptTests)
        resolves both cold-start shapes correctly without needing
        either override, so start_blank is deliberately left
        unspecified (Liquidsoap's own default, false)."""
        script = em.build_liquidsoap_script("airtap", [make_encoder()], generation="g")
        self.assertNotIn("start_blank", script)
        self.assertIn("classify_startup", script)

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


class StartupClassifierScriptTests(TestCase):
    # TestCase: build_liquidsoap_script's host_aircheck path (unused
    # here, host_aircheck=False throughout) would read AircheckConfig;
    # kept as TestCase for consistency with the other real-syntax class.
    """2026-08-06 post-deployment investigation, round 2: real, timed
    runs of the ACTUAL generated script (not a hand-written mockup) --
    same "live-verify Liquidsoap behavior, don't just assert on text"
    convention as BuildLiquidsoapScriptRealSyntaxTests above. Skipped
    cleanly if liquidsoap isn't on PATH.

    Every run substitutes ONLY the `input.alsa(...)` line for a
    synthetic source (sine()/blank()) -- everything else, including the
    exact startup classifier under test, is the real generated code.
    Encoder host/port are ALWAYS 127.0.0.1:1 (nothing listens there --
    an immediate, safe local connection-refused) and input_device is a
    unique per-test slug, so these runs can never reach the real
    Shoutcast server or collide with the live encoder's own state file
    at /run/isadoraair/liquidsoap_silence_airtap.json. State files are
    always cleaned up, including on failure."""

    SAFE_ENCODER_KWARGS = dict(host="127.0.0.1", port=1, mount="/1", station_name="t", genre="t", url="http://example.invalid")

    def _run(self, input_device, source_expr, generation, timeout=4):
        if shutil.which("liquidsoap") is None:
            self.skipTest("liquidsoap not installed on this box")
        state_path = em._audio_state_path(input_device)
        self.addCleanup(lambda: state_path.unlink(missing_ok=True))
        script = em.build_liquidsoap_script(input_device, [make_encoder(**self.SAFE_ENCODER_KWARGS)], generation=generation)
        alsa_line = f'source = input.alsa(buffer_size=1.0, device="{input_device}")'
        self.assertIn(alsa_line, script, "test fixture assumption broken -- can't safely substitute the source")
        script = script.replace(alsa_line, f"source = {source_expr}", 1)
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "test.liq"
            script_path.write_text(script, encoding="utf-8")
            try:
                subprocess.run(["liquidsoap", str(script_path)], capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                pass  # expected -- these scripts run forever until killed
        if not state_path.is_file():
            return None
        return json.loads(state_path.read_text(encoding="utf-8"))

    def test_startup_into_continuous_audio(self):
        state = self._run("classifiertest-a", "sine(440.0)", "gen-a", timeout=3)
        self.assertIsNotNone(state, "classifier never wrote a resolution")
        self.assertEqual(state["status"], "audio_ok")
        self.assertFalse(state["is_blank"])
        self.assertTrue(state["audio_observed"])
        self.assertEqual(state["generation"], "gen-a")

    def test_startup_into_continuous_silence(self):
        """The scenario this round of investigation was specifically
        about: without the classifier, this would stay "starting"/null
        forever (max_blank=20.0, far past any reasonable test/real
        startup-allowance window)."""
        state = self._run("classifiertest-b", "blank(duration=-1.0)", "gen-b", timeout=3)
        self.assertIsNotNone(state, "classifier never wrote a resolution")
        self.assertEqual(state["status"], "silent")
        self.assertTrue(state["is_blank"])
        self.assertFalse(state["audio_observed"])
        self.assertEqual(state["generation"], "gen-b")

    def test_silence_then_audio(self):
        """Classifier resolves the cold start to silent; a later GENUINE
        transition (blank.detect's own on_noise, not the classifier --
        classify_resolved is one-shot) then correctly fires."""
        state = self._run(
            "classifiertest-c",
            'sequence(merge=true, [blank(duration=2.0), sine(440.0)])',
            "gen-c", timeout=5,
        )
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "audio_ok")
        self.assertFalse(state["is_blank"])

    def test_audio_then_silence(self):
        state = self._run(
            "classifiertest-d",
            'sequence(merge=true, [sine(440.0, duration=2.0), blank(duration=10.0)])',
            "gen-d", timeout=5,
        )
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "silent")
        self.assertTrue(state["is_blank"])

    def test_callback_wins_race_against_classifier(self):
        """A genuine on_blank transition must supersede the classifier
        entirely -- structurally guaranteed by the classifier's own
        outer `if not real_transition_seen()` guard, checked before it
        ever measures or writes anything (see build_liquidsoap_script).
        max_blank=20.0 -> max_blank=0.05 for THIS test only (a timing
        knob): the real script's own production value makes a genuine
        on_blank transition take a full 20 real seconds to fire, far
        too slow for a unit test, without changing what's being
        tested -- whether a real transition wins the race, not how
        long blank.detect's own transition takes in production.

        Runs well past BOTH the shortened max_blank AND the classifier's
        own normal delay=1.0 window, then confirms the resolution
        timestamp reflects the EARLY on_blank transition, not a later
        classifier write -- if the classifier's outer guard were
        broken, its own (redundant, since the source genuinely is
        silent either way) write at ~1.0s would bump `since` well past
        the ~0.05s the real callback used."""
        if shutil.which("liquidsoap") is None:
            self.skipTest("liquidsoap not installed on this box")
        input_device = "classifiertest-e"
        state_path = em._audio_state_path(input_device)
        self.addCleanup(lambda: state_path.unlink(missing_ok=True))
        script = em.build_liquidsoap_script(input_device, [make_encoder(**self.SAFE_ENCODER_KWARGS)], generation="gen-e")
        alsa_line = f'source = input.alsa(buffer_size=1.0, device="{input_device}")'
        blank_detect_line = f'source = blank.detect(threshold={em.BLANK_DETECT_THRESHOLD_DB}, max_blank=20.0, min_noise=0.5, source)'
        self.assertIn(alsa_line, script)
        self.assertIn(blank_detect_line, script, "test fixture assumption broken -- can't safely shorten max_blank")
        script = script.replace(alsa_line, "source = blank(duration=-1.0)", 1)
        script = script.replace(blank_detect_line, blank_detect_line.replace("max_blank=20.0", "max_blank=0.05"), 1)
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "test.liq"
            script_path.write_text(script, encoding="utf-8")
            start = time.time()
            try:
                subprocess.run(["liquidsoap", str(script_path)], capture_output=True, text=True, timeout=2.5)
            except subprocess.TimeoutExpired:
                pass  # expected -- runs until killed, well past both windows
        elapsed = time.time() - start

        self.assertTrue(state_path.is_file(), "on_blank never resolved this")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "silent")
        self.assertTrue(state["is_blank"])
        # The resolution's own transition timestamp (`since`) must sit
        # close to script start, not close to when the run ended --
        # proves the EARLY on_blank write is what's in the file, not a
        # later classifier write bumping `since` forward.
        self.assertLess(state["since"] - state["started_at"], 0.5, "since is too far from started_at -- looks like the classifier wrote this, not the early on_blank callback")

    def test_old_generation_delayed_resolution_cannot_alter_current_state(self):
        """Simulates a lingering old-generation process (its classifier,
        its heartbeat, or in principle a genuine callback) still writing
        AFTER a newer generation has already taken over the SAME file --
        confirms the CAS guard now centralized inside write_state()
        itself (not just the classifier -- see that function's own
        comment) drops every such stale write rather than applying it.

        This test's own history is part of why the guard ended up
        centralized: an earlier version raced the injection against the
        classifier's own delay=1.0 and was genuinely flaky -- not
        because the classifier's guard was wrong, but because
        thread.run(every=X) with no explicit delay= defaults delay=0.0,
        so this script's 60s heartbeat fires almost immediately at
        startup too (confirmed empirically), and it had NO guard of its
        own before this write_state()-level fix. No special timing is
        needed anymore: injecting the newer generation at any point
        after the old process's own initial write, then letting it run
        for several seconds (long enough for its heartbeat AND its
        classifier to have each had multiple chances to write), is
        sufficient -- if the guard works, NOTHING from the old process
        can ever get through, regardless of exactly when."""
        if shutil.which("liquidsoap") is None:
            self.skipTest("liquidsoap not installed on this box")
        input_device = "classifiertest-f"
        state_path = em._audio_state_path(input_device)
        self.addCleanup(lambda: state_path.unlink(missing_ok=True))
        script = em.build_liquidsoap_script(input_device, [make_encoder(**self.SAFE_ENCODER_KWARGS)], generation="gen-f-OLD")
        alsa_line = f'source = input.alsa(buffer_size=1.0, device="{input_device}")'
        script = script.replace(alsa_line, "source = sine(440.0)", 1)  # continuous audio -- if the CAS guard fails, this would visibly write "audio_ok" under the OLD generation
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "test.liq"
            script_path.write_text(script, encoding="utf-8")
            proc = subprocess.Popen(["liquidsoap", str(script_path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                # Wait for the file to STABILIZE (no further changes for
                # a full second), not just first appear -- the old
                # process's own initial write is followed by an
                # ALMOST-IMMEDIATE heartbeat tick (thread.run(every=X)
                # with no explicit delay= defaults delay=0.0, confirmed
                # empirically -- see write_state()'s own comment).
                # Injecting into a genuinely quiet gap (both of the old
                # process's own early writes already complete) avoids a
                # narrow, real TOCTOU window this design does not fully
                # close: write_state()'s own read-check-then-write isn't
                # a true atomic compare-and-swap, so an external writer
                # landing in the microsecond gap between another
                # writer's check and its write could still slip through.
                # That residual is accepted -- it requires an already-
                # pathological precondition (an old process still alive
                # at all after a newer generation exists, which the real
                # deployment path structurally prevents) stacked with
                # adversarial sub-millisecond timing -- but this test
                # doesn't need to (and shouldn't try to) hit that exact
                # window to prove the guard does its job.
                last_seen = None
                stable_since = None
                deadline = time.time() + 10
                while time.time() < deadline:
                    if state_path.is_file():
                        current = state_path.read_text(encoding="utf-8")
                        if current != last_seen:
                            last_seen = current
                            stable_since = time.time()
                        elif time.time() - stable_since > 1.0:
                            break
                    time.sleep(0.02)
                else:
                    self.fail("old-generation process's state file never stabilized")
                self.assertEqual(json.loads(last_seen).get("generation"), "gen-f-OLD")

                em._atomic_write_json(state_path, {
                    "status": "starting", "is_blank": None, "generation": "gen-f-NEW", "timestamp": time.time(),
                })
                time.sleep(4.0)  # past several heartbeat ticks and several classifier ticks
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final["generation"], "gen-f-NEW", "the OLD generation's write_state() calls (heartbeat, classifier, or a real callback) must not have overwritten the newer generation's state")


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


class PostSpawnOverwriteRaceTests(EncoderManagerFixtureMixin, TransactionTestCase):
    """2026-08-05 post-deployment investigation: does _start_group()'s
    post-Popen() audio-state "invalidation" write (lines ~882-887)
    unconditionally clobber state the CHILD itself may have already
    published, for the SAME generation/pid, by the time that write
    executes? Popen() is mocked to simulate the absolute worst case --
    the child publishing its own state as a side effect of the Popen()
    call returning, before _start_group() reaches its own invalidation
    write -- modeling the child winning the race entirely.

    Real-world likelihood, per the accompanying investigation report:
    live observation during the 2026-08-05 deployment showed
    Liquidsoap's own 60s heartbeat (driven by ITS OWN in-memory
    last_status/last_is_blank refs -- see build_liquidsoap_script,
    never read back from this JSON file) still reporting "starting"/
    null a full 60+ seconds after a real launch. A race would
    self-heal at the very next heartbeat, since the heartbeat's source
    of truth lives in Liquidsoap's own process memory, untouched by
    this file ever being overwritten externally -- so the real
    incident was NOT this race. This test class exists because the
    CODE-LEVEL race is real and unconditional regardless of how
    reliably real-world timing has, so far, avoided triggering it --
    worth closing on its own merits."""

    def _popen_with_child_write(self, child_state):
        """Popen side_effect that spawns a normal fake proc AND
        immediately writes `child_state` to the audio-state file,
        using the REAL generation _start_group() just baked into the
        script file (recovered from the script text itself, not
        guessed) and the fake pid -- simulating the child publishing
        its own state before _start_group()'s own post-Popen code
        runs."""
        fake_pid = 424242

        def fake_popen(cmd, **kwargs):
            script_text = Path(cmd[1]).read_text(encoding="utf-8")
            gen_line = next(l for l in script_text.splitlines() if l.startswith("generation = "))
            generation = gen_line.split('"')[1]

            state = dict(child_state)
            state["pid"] = fake_pid
            state["generation"] = generation
            em._atomic_write_json(em._audio_state_path_for_slug("airtap"), state)

            proc = MagicMock()
            proc.pid = fake_pid
            proc.poll.return_value = None
            proc.returncode = None
            return proc

        return fake_popen

    def test_child_published_audio_ok_is_not_clobbered_back_to_starting(self):
        manager = em.EncoderManager()
        now = time.time()
        child_state = {
            "status": "audio_ok", "is_blank": False, "audio_observed": True,
            "input_device": "airtap", "started_at": now, "since": now, "timestamp": now,
        }
        with patch.object(em.subprocess, "Popen", side_effect=self._popen_with_child_write(child_state)):
            ok = manager._start_group("airtap", [make_encoder()])
        self.assertTrue(ok)

        audio_state = self.read_audio_state()
        self.assertEqual(audio_state["status"], "audio_ok")
        self.assertFalse(audio_state["is_blank"])
        self.assertTrue(audio_state["audio_observed"])

    def test_child_published_silent_is_not_clobbered_back_to_starting(self):
        manager = em.EncoderManager()
        now = time.time()
        child_state = {
            "status": "silent", "is_blank": True, "audio_observed": True,
            "input_device": "airtap", "started_at": now, "since": now, "timestamp": now,
        }
        with patch.object(em.subprocess, "Popen", side_effect=self._popen_with_child_write(child_state)):
            ok = manager._start_group("airtap", [make_encoder()])
        self.assertTrue(ok)

        audio_state = self.read_audio_state()
        self.assertEqual(audio_state["status"], "silent")
        self.assertTrue(audio_state["is_blank"])

    def test_child_published_newer_timestamp_for_same_generation_is_preserved(self):
        """Even independent of status/is_blank -- a state the child
        already wrote with a NEWER timestamp than the manager's own
        about-to-be-issued invalidation write must not be regressed to
        an older effective timestamp (which would falsely make freshly
        -published state look immediately stale to a probe)."""
        manager = em.EncoderManager()
        future_ts = time.time() + 5  # deliberately ahead of the manager's own launched_at
        child_state = {
            "status": "audio_ok", "is_blank": False, "audio_observed": True,
            "input_device": "airtap", "started_at": future_ts, "since": future_ts,
            "timestamp": future_ts,
        }
        with patch.object(em.subprocess, "Popen", side_effect=self._popen_with_child_write(child_state)):
            ok = manager._start_group("airtap", [make_encoder()])
        self.assertTrue(ok)

        audio_state = self.read_audio_state()
        self.assertGreaterEqual(audio_state["timestamp"], future_ts)


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

    def test_successful_popen_alone_does_not_reset_persisted_consecutive_failures(self):
        """Item 4 of the 2026-08-05 pre-deploy review: the sibling gap
        to the test above -- that one only checks the in-memory
        _retry_index (which schedule step a retry would use), not the
        persisted group-state field probe_encoder_group actually reads
        to decide crash-loop-critical. Build a REAL failure history via
        _handle_exit (the same path a dying child takes in production),
        then launch successfully, and confirm consecutive_failures in
        the state file the monitoring probe sees stays elevated -- only
        _check_stabilization (proven by test_resets_backoff_after_
        sustained_health) is allowed to clear it."""
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        manager._start_group("airtap", encoders)
        for _ in range(2):
            self.exit_current_child(manager, "airtap", returncode=1)
            manager._handle_exit("airtap", 1)
            manager._retry_at["airtap"] = 0
            manager._start_group("airtap", encoders)
        self.assertEqual(self.read_group_state()["consecutive_failures"], 2)

        # One more clean successful Popen() on top of that history --
        # the bug this test guards against would be _start_group's
        # success path zeroing the counter just because Popen() worked.
        manager._retry_at["airtap"] = 0
        ok = manager._start_group("airtap", encoders)
        self.assertTrue(ok)
        self.assertEqual(self.read_group_state()["consecutive_failures"], 2)


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

    def test_repeated_real_crashes_before_stabilization_increase_retry_delay(self):
        """Item 4 of the 2026-08-05 pre-deploy review: BackoffTests
        proves _schedule_retry's own progression in isolation, and the
        test above proves consecutive_failures accumulates -- but
        neither exercises the real _handle_exit -> _schedule_retry
        integration across actual repeated crash/relaunch cycles. This
        confirms next_retry_at spacing (what's actually persisted for
        the monitoring dashboard) grows in step with the real
        RETRY_BACKOFF_SECONDS schedule as each real crash is handled,
        not just that _schedule_retry does the right thing if called
        directly."""
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        manager._start_group("airtap", encoders)

        observed_delays = []
        for _ in range(3):
            before = time.time()
            self.exit_current_child(manager, "airtap", returncode=1)
            manager._handle_exit("airtap", 1)
            next_retry_at = self.read_group_state()["next_retry_at"]
            observed_delays.append(next_retry_at - before)
            manager._retry_at["airtap"] = 0  # force the next attempt to fire now
            manager._start_group("airtap", encoders)

        self.assertAlmostEqual(observed_delays[0], em.RETRY_BACKOFF_SECONDS[0], delta=2)
        self.assertAlmostEqual(observed_delays[1], em.RETRY_BACKOFF_SECONDS[1], delta=2)
        self.assertAlmostEqual(observed_delays[2], em.RETRY_BACKOFF_SECONDS[2], delta=2)
        self.assertLess(observed_delays[0], observed_delays[1])
        self.assertLess(observed_delays[1], observed_delays[2])

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

    def test_stop_does_not_touch_failure_bookkeeping_or_schedule_a_retry(self):
        """Item 4 of the 2026-08-05 pre-deploy review: stop() is a
        structurally separate code path from _handle_exit -- it never
        calls _handle_exit or _schedule_retry (confirmed by source
        inspection), so an intentional shutdown must never look like a
        crash to anything reading consecutive_failures/next_retry_at
        afterward. Removing the group-state file outright (the existing
        test_stop_marks_audio_state_dead_and_removes_group_state
        assertion) already makes this true for probe_encoder_group, but
        the in-memory bookkeeping stop() leaves behind matters too --
        it's what a subsequent, unrelated call to _check_health would
        see if the process is ever restarted with this manager object
        still resident."""
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        manager._start_group("airtap", encoders)
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)  # one real prior crash, for a non-trivial starting point
        manager._retry_at["airtap"] = 0
        manager._start_group("airtap", encoders)
        failures_before_stop = manager._group_meta("airtap")["consecutive_failures"]
        retry_index_before_stop = manager._retry_index.get("airtap")
        # Clear the harness's own manual override above so anything
        # found in _retry_at afterward is unambiguously stop()'s doing,
        # not leftover test setup -- _start_group itself never touches
        # this dict (confirmed by source inspection).
        manager._retry_at.pop("airtap", None)

        manager.stop()

        self.assertEqual(manager._group_meta("airtap")["consecutive_failures"], failures_before_stop)
        self.assertEqual(manager._retry_index.get("airtap"), retry_index_before_stop)
        self.assertNotIn("airtap", manager._retry_at)  # no retry was scheduled by stop()


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


# ---------------------------------------------------------------------
# _atomic_write_json -- pre-deploy safety review, 2026-08-05
# ---------------------------------------------------------------------
class AtomicWriteJsonTests(SimpleTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "state.json"

    def test_round_trips_valid_json(self):
        em._atomic_write_json(self.path, {"a": 1, "b": None, "c": False})
        self.assertEqual(json.loads(self.path.read_text()), {"a": 1, "b": None, "c": False})

    def test_no_temp_file_left_behind_after_success(self):
        em._atomic_write_json(self.path, {"a": 1})
        leftovers = list(Path(self._tmpdir.name).glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_uses_os_replace_not_copy(self):
        """Confirms the actual mechanism is a single atomic rename, not
        a read-modify-write or copy+delete sequence that could expose
        a partial file to a concurrent reader."""
        with patch.object(em.os, "replace", wraps=em.os.replace) as mock_replace:
            em._atomic_write_json(self.path, {"a": 1})
        mock_replace.assert_called_once()
        called_src, called_dst = mock_replace.call_args[0]
        self.assertEqual(str(called_dst), str(self.path))

    def test_reader_never_sees_a_half_written_document(self):
        """Simulates a reader landing exactly at the moment of the
        atomic rename by making os.replace itself the trigger point:
        before the call, only the OLD complete file (or nothing) is
        visible under the real name; after it returns, only the NEW
        complete file is. There is no observable window in between --
        write() only ever touches the .tmp path, never `self.path`."""
        em._atomic_write_json(self.path, {"version": "old", "complete": True})

        original_replace = em.os.replace
        seen_during_write = {}

        def spying_replace(src, dst):
            # Right before the swap: destination must still be the
            # fully-intact OLD document, never a partial new one.
            seen_during_write["before"] = json.loads(Path(dst).read_text())
            return original_replace(src, dst)

        with patch.object(em.os, "replace", side_effect=spying_replace):
            em._atomic_write_json(self.path, {"version": "new", "complete": True})

        self.assertEqual(seen_during_write["before"], {"version": "old", "complete": True})
        self.assertEqual(json.loads(self.path.read_text()), {"version": "new", "complete": True})

    def test_failed_write_does_not_destroy_last_complete_state(self):
        em._atomic_write_json(self.path, {"version": "good", "complete": True})
        good_bytes = self.path.read_bytes()

        with patch.object(em.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                em._atomic_write_json(self.path, {"version": "bad"})

        self.assertEqual(self.path.read_bytes(), good_bytes)

    def test_failed_write_cleans_up_its_own_temp_file(self):
        with patch.object(em.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                em._atomic_write_json(self.path, {"a": 1})
        leftovers = list(Path(self._tmpdir.name).glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_non_serializable_data_also_cleans_up_and_raises(self):
        """json.dumps raises TypeError, not OSError -- confirms the
        cleanup path catches both, not just I/O failures."""
        with self.assertRaises(TypeError):
            em._atomic_write_json(self.path, {"bad": object()})
        leftovers = list(Path(self._tmpdir.name).glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_written_file_is_readable_by_the_same_user(self):
        """Both isadoraair-encoders.service and isadoraair-monitoring.service
        run as the same User=/Group= (confirmed live in both unit
        files) -- so the only real requirement is that a plain,
        default-umask write is actually readable back, no special
        permission bits needed. Documents that fact rather than
        exercising cross-user permissions, which aren't meaningfully
        testable without actually running as two different users."""
        em._atomic_write_json(self.path, {"a": 1})
        # Readable and, per the default umask on a normal deployment,
        # not unusually restricted (owner read/write at minimum).
        mode = self.path.stat().st_mode
        self.assertTrue(mode & 0o400, "owner-read bit must be set")
        self.assertEqual(json.loads(self.path.read_text()), {"a": 1})


# ---------------------------------------------------------------------
# Deploy template -- 2026-08-06 post-deployment investigation, item 9
# ---------------------------------------------------------------------
class EncodersUnitTemplateTests(SimpleTestCase):
    """The missing wrapper log lines made the 2026-08-06 live incident
    meaningfully harder to diagnose (Liquidsoap's own subprocess output
    flushed promptly; the Python wrapper's print()/self._log() calls
    sat in a full stdout buffer for the entire session). Asserts the
    fix stays in the checked-in deploy template -- this does not touch
    the actually-installed unit file on this box, only the repo's
    source of truth for future deploys."""

    def test_pythonunbuffered_is_set(self):
        from django.conf import settings

        template = (settings.BASE_DIR / "deploy" / "isadoraair-encoders.service").read_text(encoding="utf-8")
        self.assertIn("PYTHONUNBUFFERED=1", template)
