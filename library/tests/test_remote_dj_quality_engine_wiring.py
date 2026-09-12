"""P1 1.5 Pass B2 -- engine-side wiring of the sustained quality sampler
and its feed into RemoteDJQualityTracker.

Django TestCase (not SimpleTestCase): _remote_dj_begin_recovery reads
RemoteDJConfig.load() and both it and _remote_dj_session_stop call
monitoring.models.emit_event, which touch the real database. Same
setUp shape as test_remote_dj_reconnect_recovery.py's
RemoteDJReconnectRecoveryTests -- a real "volume" element for the gate
(so get_property reflects real set_property calls), GLib.timeout_add
patched to CAPTURE rather than really schedule.
"""
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstWebRTC

from django.test import TestCase

import library.services.engine as eng_module
from library.models import RemoteDJConfig
from library.services.remote_dj_connection import RemoteDJConnectionAttempt
from library.services.remote_dj_stats import sanitize_browser_stats_payload

ATTEMPT_A = "B2_attempt_generation_A0000000"
ATTEMPT_B = "B2_attempt_generation_B0000000"


def _stats_report(packets_received=100, packets_lost=0, jitter=0.010,
                   dtls_state="connected", pair_id="pair-1"):
    return {
        "in-audio": {
            "id": "in-audio", "type": "inbound-rtp", "kind": "audio",
            "transport-id": "audio-transport", "packets-received": packets_received,
            "packets-lost": packets_lost, "jitter": jitter, "bytes-received": 1000,
        },
        "audio-transport": {
            "id": "audio-transport", "type": "transport",
            "selected-candidate-pair-id": pair_id,
            "dtls-state": dtls_state, "dtls-role": "server",
        },
        pair_id: {
            "id": pair_id, "type": "candidate-pair",
            "local-candidate-id": "local", "remote-candidate-id": "remote",
        },
        "local": {"id": "local", "type": "local-candidate", "candidate-type": "srflx", "protocol": "udp"},
        "remote": {"id": "remote", "type": "remote-candidate", "candidate-type": "prflx", "protocol": "udp"},
    }


class RemoteDJQualitySamplerTests(TestCase):
    def setUp(self):
        Gst.init(None)
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

        talk_ducking_patcher = patch.object(self.engine, "_apply_talk_ducking")
        talk_ducking_patcher.start()
        self.addCleanup(talk_ducking_patcher.stop)

        remote_gate = Gst.ElementFactory.make("volume", None)
        remote_gate.set_property("volume", 0.0)
        self.slot = eng_module.RemoteDJSlot(
            slot_id=0, selector=MagicMock(), silence_pad=MagicMock(),
            webrtc_pad=MagicMock(), remote_gain=MagicMock(),
            remote_gate=remote_gate, master_mixer_pad=MagicMock(),
            silence_src=MagicMock(),
        )
        self.engine.dj_slots = [self.slot]

        self.attempt_id = ATTEMPT_A
        self.session = eng_module.RemoteDJSession()
        self.session.connection_attempt = RemoteDJConnectionAttempt(
            self.attempt_id, int(time.time() * 1000)
        )
        self.session.webrtc = MagicMock()
        self.session.slot_id = 0
        self.slot.session = self.session
        self.engine.remote_dj_session = self.session
        self.session.connection_attempt.record("peer_connected")

        cfg = RemoteDJConfig.load()
        cfg.enabled = True
        cfg.reconnect_grace_seconds = 10
        cfg.save()

        self._captured_timers = []

        def _fake_timeout_add(interval_ms, callback, *extra_args):
            # _remote_dj_schedule_stats_burst calls GLib.timeout_add with
            # an extra positional `trigger` string on top of `context`;
            # the quality sampler and B1/B1.1 timers pass only `context`.
            # Store whatever positional args were actually given so each
            # test can unpack the shape it expects.
            context = extra_args[0] if extra_args else None
            self._captured_timers.append((interval_ms, callback, context, extra_args))
            return len(self._captured_timers) + 1000  # unique nonzero id

        timeout_add_patcher = patch.object(
            eng_module.GLib, "timeout_add", side_effect=_fake_timeout_add
        )
        timeout_add_patcher.start()
        self.addCleanup(timeout_add_patcher.stop)

    def _ice_state_context(self, session=None, attempt_id=None):
        session = session or self.session
        attempt_id = attempt_id or session.connection_attempt.attempt_id
        return (session, attempt_id, session.webrtc)

    def _fire_ice_connected(self, session=None, attempt_id=None, nick="connected"):
        session = session or self.session
        session.webrtc.props.ice_connection_state = SimpleNamespace(value_nick=nick)
        with patch.object(self.engine, "_remote_dj_request_stats"):
            self.engine._remote_dj_on_ice_connection_state(
                session.webrtc, None, self._ice_state_context(session, attempt_id)
            )

    # -- #1: sustained sampler starts only for an active connected attempt --

    def test_quality_sampler_starts_on_ice_connected(self):
        self._fire_ice_connected()
        quality_timers = [t for t in self._captured_timers if t[0] == 1000]
        self.assertEqual(len(quality_timers), 1)
        self.assertTrue(self.session.quality_sampler_started)
        self.assertNotEqual(self.session.quality_sampler_source_id, 0)

    def test_quality_sampler_does_not_start_twice(self):
        self._fire_ice_connected(nick="connected")
        self._fire_ice_connected(nick="completed")
        quality_timers = [t for t in self._captured_timers if t[0] == 1000]
        self.assertEqual(len(quality_timers), 1)

    # -- #2: sampler stops on teardown --

    def test_quality_sampler_source_cleared_on_session_stop(self):
        self._fire_ice_connected()
        self.assertNotEqual(self.session.quality_sampler_source_id, 0)
        self.engine._remote_dj_session_stop()
        self.assertEqual(self.session.quality_sampler_source_id, 0)

    # -- #3: a stale attempt's quality tick after replacement is a no-op --

    def test_stale_quality_tick_after_session_replaced_is_harmless(self):
        self._fire_ice_connected()
        _interval, callback, context, _extra = next(t for t in self._captured_timers if t[0] == 1000)

        self.engine._remote_dj_session_stop()
        session_b = eng_module.RemoteDJSession()
        session_b.connection_attempt = RemoteDJConnectionAttempt(ATTEMPT_B, int(time.time() * 1000))
        session_b.webrtc = MagicMock()
        session_b.slot_id = 0
        self.slot.session = session_b
        self.engine.remote_dj_session = session_b

        result = callback(context)  # A's stale tick fires late
        self.assertFalse(result)  # must return False (stop), not True
        # B's own tracker/session must be untouched.
        self.assertIs(self.engine.remote_dj_session, session_b)

    # -- #4: uplink sample fed from _remote_dj_on_stats_ready --

    def test_uplink_sample_fed_from_stats_reply(self):
        promise = MagicMock()
        promise.get_reply.return_value = _stats_report(packets_received=100, packets_lost=0, jitter=0.010)
        self.engine._remote_dj_on_stats_ready(
            promise, (self.session, self.attempt_id, self.session.webrtc, "quality_sample"),
        )
        promise2 = MagicMock()
        promise2.get_reply.return_value = _stats_report(packets_received=200, packets_lost=1, jitter=0.010)
        self.engine._remote_dj_on_stats_ready(
            promise2, (self.session, self.attempt_id, self.session.webrtc, "quality_sample"),
        )
        snap = self.session.quality.snapshot(
            attempt_status=self.session.connection_attempt.status, now=time.monotonic()
        )
        self.assertNotEqual(snap["remote_mic"], "initializing")

    # -- #5: downlink sample fed from _remote_dj_record_browser_stats --

    def test_downlink_sample_fed_from_browser_stats(self):
        payload1 = sanitize_browser_stats_payload({
            "ice_state": "connected", "rtt_ms": 20.0,
            "inbound": {"packets_received": 100, "packets_lost": 0, "jitter_ms": 8.0,
                        "concealed_samples": 0, "concealment_events": 0},
        })
        payload2 = sanitize_browser_stats_payload({
            "ice_state": "connected", "rtt_ms": 22.0,
            "inbound": {"packets_received": 200, "packets_lost": 1, "jitter_ms": 9.0,
                        "concealed_samples": 0, "concealment_events": 0},
        })
        self.engine._remote_dj_record_browser_stats(self.attempt_id, payload1, 100.0)
        self.engine._remote_dj_record_browser_stats(self.attempt_id, payload2, 1100.0)
        snap = self.session.quality.snapshot(
            attempt_status=self.session.connection_attempt.status, now=time.monotonic()
        )
        self.assertNotEqual(snap["monitor_return"], "initializing")

    # -- #10/generation safety: stale browser sample from A cannot feed B's tracker --

    def test_stale_browser_sample_from_a_does_not_feed_bs_tracker(self):
        session_b = eng_module.RemoteDJSession()
        session_b.connection_attempt = RemoteDJConnectionAttempt(ATTEMPT_B, int(time.time() * 1000))
        session_b.webrtc = MagicMock()
        session_b.slot_id = 0
        self.slot.session = session_b
        self.engine.remote_dj_session = session_b

        payload = sanitize_browser_stats_payload({
            "ice_state": "connected", "rtt_ms": 20.0,
            "inbound": {"packets_received": 100, "packets_lost": 0, "jitter_ms": 8.0},
        })
        # A's own (stale) attempt_id, now that B occupies the slot.
        self.engine._remote_dj_record_browser_stats(self.attempt_id, payload, 100.0)
        snap_b = session_b.quality.snapshot(attempt_status="connected", now=time.monotonic())
        self.assertEqual(snap_b["monitor_return"], "initializing")

    # -- #6: quality feed skipped while Reconnecting --

    def test_uplink_feed_skipped_while_reconnecting(self):
        self.session.connection_attempt.status = "reconnecting"
        promise = MagicMock()
        promise.get_reply.return_value = _stats_report(packets_received=100, packets_lost=0)
        self.engine._remote_dj_on_stats_ready(
            promise, (self.session, self.attempt_id, self.session.webrtc, "quality_sample"),
        )
        snap = self.session.quality.snapshot(attempt_status="reconnecting", now=time.monotonic())
        self.assertEqual(snap["overall"], "reconnecting")

    def test_downlink_feed_skipped_while_reconnecting(self):
        self.session.connection_attempt.status = "reconnecting"
        payload = sanitize_browser_stats_payload({
            "ice_state": "connected", "rtt_ms": 20.0,
            "inbound": {"packets_received": 100, "packets_lost": 0, "jitter_ms": 8.0},
        })
        self.engine._remote_dj_record_browser_stats(self.attempt_id, payload, 100.0)
        # Feed must have been skipped -- internal downlink history is empty.
        self.assertEqual(len(self.session.quality._downlink), 0)

    # -- #7/#31: entering Reconnecting resets the tracker --

    def test_begin_recovery_resets_quality_tracker(self):
        for i in range(3):
            self.session.quality.note_downlink_sample(
                time.monotonic(), packets_received=100 * (i + 1), packets_lost=0, jitter_ms=8.0,
            )
        self.session.gate_desired = False
        self.engine._remote_dj_begin_recovery(self.session, reason="transport_disconnected")
        self.assertEqual(len(self.session.quality._downlink), 0)

    # -- #8/#28/#29/#30: B2 never touches gate/Manual/B1 recovery timers --

    def test_quality_sample_tick_never_calls_gate_or_manual_or_recovery(self):
        self._fire_ice_connected()
        _interval, callback, context, _extra = next(t for t in self._captured_timers if t[0] == 1000)
        with (
            patch.object(self.engine, "_remote_dj_set_gate") as gate_mock,
            patch.object(self.engine, "_apply_mic_mode_hold") as manual_mock,
            patch.object(self.engine, "_remote_dj_begin_recovery") as begin_mock,
            patch.object(self.engine, "_remote_dj_on_recovery_deadline") as deadline_mock,
        ):
            callback(context)
        gate_mock.assert_not_called()
        manual_mock.assert_not_called()
        begin_mock.assert_not_called()
        deadline_mock.assert_not_called()

    def test_browser_stats_feed_never_touches_gate_or_manual(self):
        payload = sanitize_browser_stats_payload({
            "ice_state": "connected", "rtt_ms": 20.0,
            "inbound": {"packets_received": 100, "packets_lost": 0, "jitter_ms": 8.0},
        })
        with (
            patch.object(self.engine, "_remote_dj_set_gate") as gate_mock,
            patch.object(self.engine, "_apply_mic_mode_hold") as manual_mock,
        ):
            self.engine._remote_dj_record_browser_stats(self.attempt_id, payload, 100.0)
        gate_mock.assert_not_called()
        manual_mock.assert_not_called()

    # -- #9/#35: compact quality state serializes correctly into engine state --

    def test_quality_state_accessor_returns_none_without_session(self):
        self.engine.remote_dj_session = None
        self.assertIsNone(self.engine._remote_dj_quality_state())

    def test_quality_state_accessor_is_json_serializable(self):
        for i in range(3):
            self.session.quality.note_downlink_sample(
                time.monotonic(), packets_received=100 * (i + 1), packets_lost=0,
                jitter_ms=8.0, concealed_samples=0, concealment_events=0, rtt_ms=20.0,
            )
        state = self.engine._remote_dj_quality_state()
        self.assertIsInstance(state, dict)
        json.dumps(state)  # must not raise
        self.assertIn("overall", state)


class RemoteDJQualityDashboardTemplateContractTests(TestCase):
    """Lightweight text-contract check on the browser-side sustained
    sampler and UI indicator -- same pattern as
    test_remote_dj_transport_observability.py's own template contract
    tests. #36-#39 (UI renders each state) are exercised via this
    static/deterministic contract rather than a browser automation
    framework, per the task's explicit non-goal on introducing one."""

    def setUp(self):
        from pathlib import Path
        self.template = (
            Path(__file__).parents[1] / "templates/library/dashboard.html"
        ).read_text(encoding="utf-8")

    def test_sustained_sampler_present_and_reuses_existing_collector(self):
        self.assertIn("function rdjStartSustainedQualitySampling", self.template)
        self.assertIn("setInterval(", self.template)
        self.assertIn("rdjCollectStats(pc, ws, attemptId)", self.template)

    def test_sustained_sampler_stopped_on_disconnect(self):
        self.assertIn("clearInterval(rdjQualityIntervalId)", self.template)

    def test_quality_render_function_maps_all_four_states(self):
        self.assertIn("function renderRemoteDjQuality", self.template)
        for level in ("good", "fair", "poor", "reconnecting"):
            self.assertIn(f"{level}:", self.template.split("const labels")[1][:200])

    def test_quality_badge_never_labeled_signal_strength(self):
        self.assertNotIn("Signal Strength", self.template)
        self.assertIn("Remote Link:", self.template)
