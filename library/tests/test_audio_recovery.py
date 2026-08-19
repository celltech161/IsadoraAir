"""[P0] 1.3B2 -- pure-module tests for library/services/audio_recovery.py.
No GStreamer, no Django DB, no hardware -- plain unittest against the
classifier, SlotCoordinator, backoff, and ALSA-cards parsing/identity
resolution. See test_engine_mic_recovery.py for the GStreamer-integrated
half (topology, EOS quarantine, engine-level state transitions)."""
import time
import unittest

from django.test import SimpleTestCase

from library.services import audio_recovery as ar


class ClassifyAudioDeviceErrorTests(SimpleTestCase):
    """Item 1/2: real captured signatures classify correctly; unrelated
    errors do not trigger recovery."""

    def test_disconnected_capture_error_is_device_lost(self):
        # Actual captured text, scratchpad/audio_recovery/hotplug_harness/
        # logs -- alsasrc mid-stream disconnect.
        err = ("gst-resource-error-quark: Error recording from audio device. "
               "The device has been disconnected. (9)")
        self.assertEqual(ar.classify_audio_device_error(err), "device_lost")

    def test_disconnected_output_error_is_device_lost(self):
        # Same signature shape on the output side (alsasink) -- confirms
        # the classifier isn't accidentally input-specific in its text
        # matching (even though only input recovery is wired to it here).
        err = ("gst-resource-error-quark: Error outputting to audio device. "
               "The device has been disconnected. (10)")
        self.assertEqual(ar.classify_audio_device_error(err), "device_lost")

    def test_could_not_open_device_absent_is_device_lost_via_debug_text(self):
        # Actual captured text, Harness E -- the message itself does NOT
        # contain "disconnected"/"no such device"; only the debug string
        # does. A classifier checking only the message text would
        # misclassify this as "unknown" -- regression-guards the fix.
        err = "gst-resource-error-quark: Could not open audio device for recording. (5)"
        debug = ("../ext/alsa/gstalsasrc.c(794): gst_alsasrc_open (): "
                 "/GstPipeline:x/GstAlsaSrc:src:\nRecording open error on "
                 "device 'plughw:CARD=NoSuchCardExists,DEV=0': No such device")
        self.assertEqual(ar.classify_audio_device_error(err, debug), "device_lost")

    def test_message_only_no_debug_still_works(self):
        self.assertEqual(
            ar.classify_audio_device_error(
                "gst-resource-error-quark: Error recording from audio device. "
                "The device has been disconnected. (9)", ""),
            "device_lost")

    def test_immediate_followup_stream_error_is_unknown_not_device_lost(self):
        # Actual captured text -- the "Internal data stream error"
        # message that ALWAYS immediately follows a real disconnect.
        # Must NOT independently trigger a second recovery cycle.
        err = "gst-stream-error-quark: Internal data stream error. (1)"
        debug = ("../libs/gst/base/gstbasesrc.c(3187): gst_base_src_loop (): "
                 "streaming stopped, reason error (-5)")
        self.assertEqual(ar.classify_audio_device_error(err, debug), "unknown")

    def test_busy_is_transient(self):
        err = "gst-resource-error-quark: Could not open audio device. (5)"
        debug = "snd_pcm_open: Device or resource busy"
        self.assertEqual(ar.classify_audio_device_error(err, debug), "transient")

    def test_totally_unrelated_error_is_unknown(self):
        self.assertEqual(
            ar.classify_audio_device_error("gst-core-error-quark: some codec problem"),
            "unknown")

    def test_case_insensitive(self):
        self.assertEqual(
            ar.classify_audio_device_error("Device Has Been DISCONNECTED"),
            "device_lost")


class BackoffTests(SimpleTestCase):
    def test_schedule_matches_locked_sequence_without_jitter(self):
        expected = [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
        got = [ar.compute_backoff_seconds(i, jitter=False) for i in range(1, 7)]
        self.assertEqual(got, expected)

    def test_caps_at_30_beyond_the_explicit_schedule(self):
        for attempt in (7, 8, 50, 1000):
            self.assertEqual(ar.compute_backoff_seconds(attempt, jitter=False), 30.0)

    def test_attempt_zero_or_negative_treated_as_first_step(self):
        self.assertEqual(ar.compute_backoff_seconds(0, jitter=False), 1.0)
        self.assertEqual(ar.compute_backoff_seconds(-5, jitter=False), 1.0)

    def test_jitter_stays_within_declared_fraction(self):
        import random
        rng = random.Random(42)
        for _ in range(200):
            val = ar.compute_backoff_seconds(3, jitter=True, rng=rng)  # base 4.0
            self.assertGreaterEqual(val, 4.0 * 0.85 - 0.001)
            self.assertLessEqual(val, 4.0 * 1.15 + 0.001)


class AlsaCardsParsingTests(SimpleTestCase):
    """Item 21-ish (identity resolver correctness) -- pure parsing, no
    file IO, using real captured /proc/asound/cards text from this
    session's own production-safety checks."""

    SAMPLE = (
        " 0 [Loopback       ]: Loopback - Loopback\n"
        "                      Loopback 1\n"
        " 1 [D10s           ]: USB-Audio - D10s\n"
        "                      Topping D10s at usb-0000:00:14.0-9, high speed\n"
        " 2 [PCH            ]: HDA-Intel - HDA Intel PCH\n"
        "                      HDA Intel PCH at 0x4000100000 irq 149\n"
        " 5 [CODEC          ]: USB-Audio - USB Audio CODEC\n"
        "                      Burr-Brown from TI USB Audio CODEC at "
        "usb-0000:00:14.0-10, full speed\n"
    )

    def test_parses_all_short_ids_stripped_of_padding(self):
        cards = ar.parse_alsa_cards(self.SAMPLE)
        self.assertEqual(cards, {"Loopback": 0, "D10s": 1, "PCH": 2, "CODEC": 5})

    def test_empty_text_returns_empty(self):
        self.assertEqual(ar.parse_alsa_cards(""), {})

    def test_malformed_lines_ignored(self):
        self.assertEqual(ar.parse_alsa_cards("garbage\nmore garbage\n"), {})

    def test_identity_present(self):
        cards = ar.parse_alsa_cards(self.SAMPLE)
        self.assertTrue(ar.alsa_card_identity_present("CODEC", cards))
        self.assertFalse(ar.alsa_card_identity_present("NOPE", cards))
        self.assertFalse(ar.alsa_card_identity_present("", cards))

    def test_read_alsa_cards_present_never_raises_on_missing_file(self):
        # Points at a path that (almost certainly) doesn't exist --
        # exercises the "unreadable /proc/asound/cards degrades to empty,
        # doesn't crash" contract without needing to mock Path globally.
        import library.services.audio_recovery as mod
        original = mod._ALSA_CARDS_PATH
        try:
            from pathlib import Path
            mod._ALSA_CARDS_PATH = Path("/nonexistent/definitely/not/here")
            self.assertEqual(mod.read_alsa_cards_present(), {})
        finally:
            mod._ALSA_CARDS_PATH = original


class ResolveRuntimeDeviceTests(SimpleTestCase):
    def test_alsa_card_id_uses_native_card_name_syntax(self):
        self.assertEqual(
            ar.resolve_runtime_device("alsa_card_id", "PCH", "plughw:2,0"),
            "plughw:CARD=PCH,DEV=0")

    def test_blank_identity_kind_falls_back_to_legacy(self):
        self.assertEqual(
            ar.resolve_runtime_device("", "", "plughw:2,0"),
            "plughw:2,0")

    def test_alsa_card_id_kind_but_no_identity_value_falls_back_to_legacy(self):
        self.assertEqual(
            ar.resolve_runtime_device("alsa_card_id", "", "plughw:2,0"),
            "plughw:2,0")

    def test_unrecognized_kind_falls_back_to_legacy(self):
        self.assertEqual(
            ar.resolve_runtime_device("future_kind_not_yet_supported", "x", "plughw:2,0"),
            "plughw:2,0")

    def test_no_legacy_and_no_identity_returns_none(self):
        self.assertIsNone(ar.resolve_runtime_device("", "", ""))


class SlotCoordinatorCoreTests(SimpleTestCase):
    """Item 3/4/14/15: coalescing, stale-generation rejection, no
    unbounded worker creation, restart-required/abandoned behavior.
    Reuses the exact synthetic-worker technique proven in
    scratchpad/audio_recovery/hotplug_harness/test_slot_guard.py."""

    def _wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_mark_degraded_first_call_true_then_false(self):
        slot = ar.SlotCoordinator("mic")
        self.assertTrue(slot.mark_degraded())
        self.assertFalse(slot.mark_degraded())
        self.assertEqual(slot.state, ar.SlotState.DEGRADED)

    def test_request_recovery_ignored_while_ok(self):
        slot = ar.SlotCoordinator("mic")
        self.assertEqual(slot.request_recovery(lambda: True), "ignored_ok")

    def test_successful_worker_transitions_to_ok(self):
        slot = ar.SlotCoordinator("mic")
        slot.mark_degraded()
        result = slot.request_recovery(lambda: True)
        self.assertEqual(result, "dispatched")
        self.assertTrue(self._wait_until(lambda: slot.state == ar.SlotState.OK))

    def test_failed_worker_transitions_to_degraded(self):
        slot = ar.SlotCoordinator("mic")
        slot.mark_degraded()
        slot.request_recovery(lambda: False)
        self.assertTrue(self._wait_until(lambda: slot.snapshot()["operation_state"] == "RETURNED"))
        self.assertEqual(slot.state, ar.SlotState.DEGRADED)

    def test_coalesces_repeated_requests_while_in_flight(self):
        slot = ar.SlotCoordinator("mic")
        slot.mark_degraded()
        gate = {"release": False}

        def slow_worker():
            while not gate["release"]:
                time.sleep(0.005)
            return True

        first = slot.request_recovery(slow_worker)
        second = slot.request_recovery(slow_worker)  # while still in flight
        self.assertEqual(first, "dispatched")
        self.assertEqual(second, "coalesced")
        gate["release"] = True
        self.assertTrue(self._wait_until(lambda: slot.state == ar.SlotState.OK))
        # Only ONE worker thread was ever created for the coalesced pair --
        # generation only advanced by 1, not 2.
        self.assertEqual(slot.generation, 1)

    def test_stale_late_completion_does_not_alter_newer_generation(self):
        """Item 4: a late-resolving op for generation N must not affect
        state once generation N+1 is already current."""
        slot = ar.SlotCoordinator("mic", timeout_s=0.15)
        slot.mark_degraded()
        gate = {"release": False}

        def hanging_worker():
            while not gate["release"]:
                time.sleep(0.01)
            return True  # resolves AFTER abandonment

        slot.request_recovery(hanging_worker)
        # Let the watchdog abandon it.
        self.assertTrue(self._wait_until(
            lambda: (slot.tick() or True) and slot.state == ar.SlotState.RESTART_REQUIRED,
            timeout=2.0))
        gen_at_abandonment = slot.generation
        # Now let the real (leaked) worker thread finally finish.
        gate["release"] = True
        time.sleep(0.3)
        # State must NOT have been dragged back to OK by the late result.
        self.assertEqual(slot.state, ar.SlotState.RESTART_REQUIRED)
        self.assertEqual(slot.generation, gen_at_abandonment)

    def test_restart_required_ignores_automatic_retry(self):
        slot = ar.SlotCoordinator("mic", timeout_s=0.1)
        slot.mark_degraded()
        slot.request_recovery(lambda: (time.sleep(5), True)[1])
        self.assertTrue(self._wait_until(lambda: (slot.tick() or True) and
                                          slot.state == ar.SlotState.RESTART_REQUIRED))
        self.assertEqual(slot.request_recovery(lambda: True), "ignored_restart_required")

    def test_retry_now_bumps_generation_after_restart_required(self):
        slot = ar.SlotCoordinator("mic", timeout_s=0.1)
        slot.mark_degraded()
        slot.request_recovery(lambda: (time.sleep(5), True)[1])
        self.assertTrue(self._wait_until(lambda: (slot.tick() or True) and
                                          slot.state == ar.SlotState.RESTART_REQUIRED))
        gen_before = slot.generation
        result = slot.retry_now(lambda: True)
        self.assertEqual(result, "dispatched")
        self.assertEqual(slot.generation, gen_before + 1)

    def test_no_unbounded_worker_creation_under_repeated_coalesced_notifications(self):
        """Item 14: many repeated 'failure' notifications while one
        operation is in flight must dispatch exactly one worker."""
        slot = ar.SlotCoordinator("mic")
        slot.mark_degraded()
        gate = {"release": False}
        dispatch_count = {"n": 0}

        def slow_worker():
            dispatch_count["n"] += 1
            while not gate["release"]:
                time.sleep(0.005)
            return True

        results = [slot.request_recovery(slow_worker) for _ in range(25)]
        gate["release"] = True
        self.assertTrue(self._wait_until(lambda: slot.state == ar.SlotState.OK))
        self.assertEqual(dispatch_count["n"], 1)
        self.assertEqual(results.count("dispatched"), 1)
        self.assertEqual(results.count("coalesced"), 24)

    def test_worker_exception_treated_as_failure_not_coordinator_crash(self):
        slot = ar.SlotCoordinator("mic")
        slot.mark_degraded()

        def raising_worker():
            raise RuntimeError("boom")

        slot.request_recovery(raising_worker)
        self.assertTrue(self._wait_until(lambda: slot.state == ar.SlotState.DEGRADED and
                                          slot.snapshot()["operation_state"] == "RETURNED"))
