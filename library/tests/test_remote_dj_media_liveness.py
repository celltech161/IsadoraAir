"""P1 1.5 Pass B1.1 -- engine-authoritative inbound media liveness.

The product clock is deterministic in the state-machine tests: GLib timers
are captured and invoked directly, and time.monotonic is a fake.  The final
test uses a finite, hardware-free audiotestsrc -> level -> fakesink pipeline
to prove that the selected ongoing-media hook reports repeated digital-
silence buffers and ceases when upstream reaches EOS.
"""
import math
import time
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstWebRTC

from django.test import SimpleTestCase, TestCase

import library.services.engine as eng_module
from library.models import RemoteDJConfig
from library.services.remote_dj_connection import RemoteDJConnectionAttempt


Gst.init(None)

ATTEMPT_A = "B1_1_media_generation_A0000000"
ATTEMPT_B = "B1_1_media_generation_B0000000"


class RemoteDJMediaLivenessTests(TestCase):
    def setUp(self):
        self.clock = 100.0
        self.clock_patcher = patch.object(
            eng_module.time, "monotonic", side_effect=lambda: self.clock
        )
        self.clock_patcher.start()
        self.addCleanup(self.clock_patcher.stop)

        self.engine = object.__new__(eng_module.PlaybackEngine)
        self.engine._remote_dj_server = MagicMock()
        self.engine.manual_mode = False
        self.engine._manual_from_mic = False
        self.engine.mic_live = False
        self.engine._manual_hold_pending = False
        self.engine._next_triggered = False
        self.engine.remote_dj_tee = MagicMock()
        self.engine.local_mic_tee = None
        self.engine._remote_dj_last_attempt = None
        self.engine.main_pipeline = MagicMock()
        self.engine.output_level = object()
        self.engine._apply_talk_ducking = MagicMock()

        gate = Gst.ElementFactory.make("volume", None)
        gate.set_property("volume", 0.0)
        self.slot = eng_module.RemoteDJSlot(
            slot_id=0,
            selector=MagicMock(),
            silence_pad=MagicMock(),
            webrtc_pad=MagicMock(),
            remote_gain=MagicMock(),
            remote_gate=gate,
            master_mixer_pad=MagicMock(),
            silence_src=MagicMock(),
        )
        self.engine.dj_slots = [self.slot]
        self.session = self._new_session(ATTEMPT_A)
        self._install(self.session)

        cfg = RemoteDJConfig.load()
        cfg.enabled = True
        cfg.reconnect_grace_seconds = 10
        cfg.save()

        self.timers = []

        def fake_timeout_add(interval_ms, callback, context):
            source_id = 1000 + len(self.timers)
            self.timers.append((source_id, interval_ms, callback, context))
            return source_id

        self.timeout_patcher = patch.object(
            eng_module.GLib, "timeout_add", side_effect=fake_timeout_add
        )
        self.timeout_patcher.start()
        self.addCleanup(self.timeout_patcher.stop)

    def _new_session(self, attempt_id):
        session = eng_module.RemoteDJSession()
        session.connection_attempt = RemoteDJConnectionAttempt(
            attempt_id, int(time.time() * 1000)
        )
        session.connection_attempt.record("peer_connected")
        session.webrtc = MagicMock()
        session.slot_id = 0
        session.dj_level = object()
        return session

    def _install(self, session):
        self.slot.session = session
        self.engine.remote_dj_session = session

    def _arrive(self, advance=0.0, session=None):
        self.clock += advance
        return self.engine._remote_dj_note_media_arrival(
            session or self.session
        )

    def _arm(self):
        self._arrive()
        self.assertEqual(self.timers[0][1], eng_module.REMOTE_DJ_MEDIA_WATCHDOG_INTERVAL_MS)

    def _watchdog(self):
        _source, _interval, callback, context = self.timers[0]
        return callback(context)

    def _deadline(self):
        timer = next(timer for timer in self.timers if timer[1] == 10000)
        return timer[2](timer[3])

    def _fire_native(self, session, state):
        session.webrtc.props.connection_state = state
        self.engine._remote_dj_on_connection_state(
            session.webrtc,
            None,
            (session, session.connection_attempt.attempt_id, session.webrtc),
        )

    def _expire_media(self):
        self._arm()
        self.clock += eng_module.REMOTE_DJ_MEDIA_LIVENESS_TIMEOUT_S
        self._watchdog()

    def test_watchdog_does_not_arm_before_first_usable_media(self):
        self.assertEqual(self.session.media_watchdog_source_id, 0)
        self.assertIsNone(self.session.last_media_monotonic)
        self.assertEqual(self.timers, [])

    def test_first_usable_media_arms_monotonic_watchdog(self):
        self._arm()
        self.assertEqual(self.session.last_media_monotonic, 100.0)
        self.assertEqual(self.session.media_watchdog_source_id, 1000)

    def test_ongoing_media_refreshes_liveness(self):
        self._arm()
        self._arrive(0.1)
        self.assertEqual(self.session.last_media_monotonic, 100.1)
        self.assertEqual(len(self.timers), 1)

    def test_silent_level_message_refreshes_liveness(self):
        self._arm()
        self.clock += 0.1
        structure = MagicMock()
        structure.get_name.return_value = "level"
        structure.get_value.side_effect = lambda _name: [-math.inf, -math.inf]
        message = MagicMock()
        message.get_structure.return_value = structure
        message.src = self.session.dj_level
        self.engine._on_element_message(None, message)
        self.assertEqual(self.session.last_media_monotonic, 100.1)
        self.assertTrue(all(math.isinf(v) for v in self.session.dj_level_sample["rms"]))
        self.assertEqual(self.session.connection_attempt.status, "connected")

    def test_stale_or_zero_amplitude_sample_alone_cannot_trigger_recovery(self):
        self._arm()
        self.session.dj_level_sample = {"ts": 0.0, "rms": [-math.inf]}
        self.clock += eng_module.REMOTE_DJ_MEDIA_LIVENESS_TIMEOUT_S - 0.01
        self._watchdog()
        self.assertEqual(self.session.connection_attempt.status, "connected")

    def test_missing_media_beyond_threshold_enters_reconnecting(self):
        self._expire_media()
        self.assertEqual(self.session.connection_attempt.status, "reconnecting")
        self.assertEqual(self.session.recovery_reason, "media_liveness_timeout")

    def test_media_timeout_force_mutes_physical_gate_and_preserves_desire(self):
        self.session.gate_desired = True
        self.slot.remote_gate.set_property("volume", 1.0)
        self._expire_media()
        self.assertEqual(self.slot.remote_gate.get_property("volume"), 0.0)
        self.assertTrue(self.session.gate_desired)

    def test_media_timeout_starts_existing_ten_second_grace(self):
        self._expire_media()
        deadlines = [timer for timer in self.timers if timer[1] == 10000]
        self.assertEqual(len(deadlines), 1)
        self.assertEqual(self.session.recovery_deadline_source_id, deadlines[0][0])

    def test_one_straggler_does_not_recover_session(self):
        self._expire_media()
        self._arrive(0.1)
        self.assertEqual(self.session.connection_attempt.status, "reconnecting")
        self.assertEqual(self.session.media_recovery_observations, 1)

    def test_two_normal_cadence_media_observations_recover_same_session(self):
        self._expire_media()
        self._arrive(0.1)
        self._arrive(0.1)
        self.assertIs(self.engine.remote_dj_session, self.session)
        self.assertEqual(self.session.connection_attempt.status, "connected")
        self.assertEqual(self.session.recovery_deadline_source_id, 0)

    def test_desired_gate_restores_after_media_recovery(self):
        self.session.gate_desired = True
        self.slot.remote_gate.set_property("volume", 1.0)
        self._expire_media()
        self._arrive(0.1)
        self._arrive(0.1)
        self.assertEqual(self.slot.remote_gate.get_property("volume"), 1.0)

    def test_undesired_gate_remains_muted_after_media_recovery(self):
        self._expire_media()
        self._arrive(0.1)
        self._arrive(0.1)
        self.assertEqual(self.slot.remote_gate.get_property("volume"), 0.0)

    def test_ptt_on_during_reconnecting_changes_desire_but_not_physical_gate(self):
        self._expire_media()
        self.engine._remote_dj_set_gate(True)
        self.assertTrue(self.session.gate_desired)
        self.assertEqual(self.slot.remote_gate.get_property("volume"), 0.0)

    def test_ptt_on_during_reconnecting_does_not_release_mic_owned_manual(self):
        self.engine.manual_mode = True
        self.engine._manual_from_mic = True
        self._expire_media()
        self.engine._remote_dj_set_gate(True)
        self.assertTrue(self.engine.manual_mode)
        self.assertTrue(self.engine._manual_from_mic)

    def test_new_desired_gate_during_recovery_claims_manual_when_media_recovers(self):
        self._expire_media()
        self.engine._remote_dj_set_gate(True)
        self._arrive(0.1)
        self._arrive(0.1)
        self.assertEqual(self.slot.remote_gate.get_property("volume"), 1.0)
        self.assertTrue(self.engine.manual_mode)
        self.assertTrue(self.engine._manual_from_mic)

    def test_repeated_watchdog_ticks_do_not_duplicate_grace_timer(self):
        self._expire_media()
        before = len([timer for timer in self.timers if timer[1] == 10000])
        self.clock += 2.0
        self._watchdog()
        self._watchdog()
        after = len([timer for timer in self.timers if timer[1] == 10000])
        self.assertEqual((before, after), (1, 1))

    def test_brief_media_gap_below_threshold_does_not_reconnect(self):
        self._arm()
        self.clock += eng_module.REMOTE_DJ_MEDIA_LIVENESS_TIMEOUT_S - 0.001
        self._watchdog()
        self.assertEqual(self.session.connection_attempt.status, "connected")

    def test_marginal_isolated_arrivals_do_not_flap_or_multiply_timers(self):
        self._expire_media()
        self._arrive(0.1)
        self._arrive(eng_module.REMOTE_DJ_MEDIA_RECOVERY_CONFIRM_WINDOW_S + 0.1)
        self.assertEqual(self.session.connection_attempt.status, "reconnecting")
        self.assertEqual(self.session.media_recovery_observations, 1)
        self.assertEqual(len([timer for timer in self.timers if timer[1] == 10000]), 1)

    def test_liveness_grace_expiry_finalizes_and_releases_slot(self):
        self._expire_media()
        self._deadline()
        self.assertIsNone(self.engine.remote_dj_session)
        self.assertIsNone(self.slot.session)
        self.assertEqual(self.session.connection_attempt.status, "failed")

    def test_native_failed_after_liveness_finalization_is_harmless(self):
        self._expire_media()
        self._deadline()
        self._fire_native(self.session, GstWebRTC.WebRTCPeerConnectionState.FAILED)
        self.assertIsNone(self.engine.remote_dj_session)

    def test_signaling_disconnect_after_liveness_finalization_is_harmless(self):
        self._expire_media()
        self._deadline()
        self.assertFalse(self.engine._remote_dj_session_stop(ATTEMPT_A))

    def test_stale_watchdog_from_attempt_a_cannot_affect_attempt_b(self):
        self._arm()
        callback, context = self.timers[0][2], self.timers[0][3]
        session_b = self._new_session(ATTEMPT_B)
        self._install(session_b)
        self.clock += 10.0
        self.assertFalse(callback(context))
        self.assertEqual(session_b.connection_attempt.status, "connected")

    def test_stale_media_callback_from_a_cannot_recover_attempt_b(self):
        self._expire_media()
        session_b = self._new_session(ATTEMPT_B)
        session_b.connection_attempt.mark_reconnecting()
        session_b.recovery_reason = "media_liveness_timeout"
        self._install(session_b)
        self.assertFalse(self._arrive(0.1, session=self.session))
        self.assertFalse(self._arrive(0.1, session=self.session))
        self.assertEqual(session_b.connection_attempt.status, "reconnecting")

    def test_native_disconnected_path_still_enters_shared_recovery(self):
        self._fire_native(self.session, GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED)
        self.assertEqual(self.session.connection_attempt.status, "reconnecting")
        self.assertEqual(self.session.recovery_reason, "transport_disconnected")

    def test_native_failed_remains_immediately_terminal(self):
        self._fire_native(self.session, GstWebRTC.WebRTCPeerConnectionState.FAILED)
        self.assertIsNone(self.engine.remote_dj_session)

    def test_native_closed_remains_immediately_terminal(self):
        self._fire_native(self.session, GstWebRTC.WebRTCPeerConnectionState.CLOSED)
        self.assertIsNone(self.engine.remote_dj_session)

    def test_explicit_disconnect_cancels_media_watchdog(self):
        self._arm()
        self.engine._remote_dj_session_stop()
        self.assertEqual(self.session.media_watchdog_source_id, 0)
        self.assertIsNone(self.engine.remote_dj_session)

    def test_media_liveness_event_records_reason_and_age(self):
        with patch.object(eng_module, "emit_event") as mocked_emit:
            self._expire_media()
        reconnect = next(
            call for call in mocked_emit.call_args_list
            if call.kwargs.get("title") == "Remote DJ entered Reconnecting"
        )
        self.assertEqual(reconnect.kwargs["detail"]["trigger_reason"], "media_liveness_timeout")
        self.assertGreaterEqual(
            reconnect.kwargs["detail"]["last_media_age_seconds"],
            eng_module.REMOTE_DJ_MEDIA_LIVENESS_TIMEOUT_S,
        )


class RemoteDJMediaLivenessGStreamerEvidenceTests(SimpleTestCase):
    def test_silent_decoded_media_reports_repeatedly_then_stops_at_eos(self):
        pipeline = Gst.Pipeline.new(None)
        src = Gst.ElementFactory.make("audiotestsrc", None)
        src.set_property("wave", "silence")
        src.set_property("volume", 0.0)
        src.set_property("num-buffers", 30)
        src.set_property("samplesperbuffer", 960)
        convert = Gst.ElementFactory.make("audioconvert", None)
        resample = Gst.ElementFactory.make("audioresample", None)
        caps = Gst.ElementFactory.make("capsfilter", None)
        caps.set_property(
            "caps", Gst.Caps.from_string("audio/x-raw,rate=48000,channels=1")
        )
        encoder = Gst.ElementFactory.make("opusenc", None)
        encoder.set_property("frame-size", 20)
        pay = Gst.ElementFactory.make("rtpopuspay", None)
        depay = Gst.ElementFactory.make("rtpopusdepay", None)
        decoder = Gst.ElementFactory.make("opusdec", None)
        level = Gst.ElementFactory.make("level", None)
        level.set_property("interval", 20_000_000)
        level.set_property("post-messages", True)
        sink = Gst.ElementFactory.make("fakesink", None)
        elements = (
            src,
            convert,
            resample,
            caps,
            encoder,
            pay,
            depay,
            decoder,
            level,
            sink,
        )
        for element in elements:
            pipeline.add(element)
        for upstream, downstream in zip(elements, elements[1:]):
            self.assertTrue(upstream.link(downstream))
        self.addCleanup(pipeline.set_state, Gst.State.NULL)

        engine = object.__new__(eng_module.PlaybackEngine)
        session = eng_module.RemoteDJSession()
        session.connection_attempt = RemoteDJConnectionAttempt(
            "B1_1_hardware_free_silence000", int(time.time() * 1000)
        )
        session.connection_attempt.record("peer_connected")
        session.webrtc = object()
        session.dj_level = level
        engine.remote_dj_session = session
        engine.output_level = object()

        bus = pipeline.get_bus()
        level_count = 0
        with patch.object(eng_module.GLib, "timeout_add", return_value=777), patch.object(
            eng_module, "emit_event"
        ):
            pipeline.set_state(Gst.State.PLAYING)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                message = bus.timed_pop_filtered(
                    int(0.25 * Gst.SECOND),
                    Gst.MessageType.ELEMENT | Gst.MessageType.EOS | Gst.MessageType.ERROR,
                )
                if message is None:
                    continue
                if message.type == Gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    self.fail(f"hardware-free liveness pipeline failed: {error}; {debug}")
                if message.type == Gst.MessageType.EOS:
                    break
                structure = message.get_structure()
                if structure is not None and structure.get_name() == "level" and message.src is level:
                    engine._on_element_message(None, message)
                    level_count += 1
            else:
                self.fail("hardware-free liveness pipeline did not reach EOS")

            self.assertGreaterEqual(level_count, 2)
            self.assertEqual(session.media_watchdog_source_id, 777)
            self.assertEqual(session.connection_attempt.status, "connected")
            # The encoded source samples are exactly zero.  Opus may
            # decode that to a finite very-low noise floor, which is the
            # point: liveness follows buffer arrival, never this value.
            self.assertEqual(src.get_property("volume"), 0.0)
            self.assertTrue(session.dj_level_sample["rms"])
            self.assertIsNone(
                bus.timed_pop_filtered(
                    int(0.1 * Gst.SECOND), Gst.MessageType.ELEMENT
                )
            )
