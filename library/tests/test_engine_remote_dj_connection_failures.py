import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from django.test import SimpleTestCase, TestCase

import library.services.engine as eng_module
from library.services.remote_dj_connection import (
    FAILURE_DEPENDENCY_SESSION_BUILD,
    FAILURE_MEDIA_ROUTING,
    RemoteDJConnectionAttempt,
)
from library.tests.test_engine_runtime_commit import make_minimal_stand_in


ATTEMPT_ID = "A1_engine_attempt_12345"
OTHER_ATTEMPT_ID = "B2_engine_attempt_67890"


class RemoteDJGenerationBindingTests(SimpleTestCase):
    def setUp(self):
        Gst.init(None)
        self.engine = object.__new__(eng_module.PlaybackEngine)
        self.engine._remote_dj_server = MagicMock()
        self.current = self._session(OTHER_ATTEMPT_ID)
        self.engine.remote_dj_session = self.current

    @staticmethod
    def _session(attempt_id):
        session = eng_module.RemoteDJSession()
        session.connection_attempt = RemoteDJConnectionAttempt(
            attempt_id, 1_700_000_000_000
        )
        session.webrtc = MagicMock()
        return session

    def test_stale_browser_milestone_cannot_modify_current_attempt(self):
        self.engine._remote_dj_record_browser_milestone(
            ATTEMPT_ID, "ice_checking", 125.0
        )
        self.assertEqual(self.current.connection_attempt.milestones["browser"], {})

        self.engine._remote_dj_record_browser_milestone(
            OTHER_ATTEMPT_ID, "ice_checking", 130.0
        )
        self.assertEqual(
            self.current.connection_attempt.milestones["browser"]["ice_checking"],
            130.0,
        )

    def test_stale_answer_cannot_be_applied_to_current_webrtc(self):
        self.engine._remote_dj_handle_answer(ATTEMPT_ID, "stale-sdp")
        self.current.webrtc.emit.assert_not_called()
        self.assertNotIn(
            "answer_received",
            self.current.connection_attempt.milestones["server"],
        )

    def test_matching_answer_is_submitted_to_current_webrtc(self):
        sdp = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "t=0 0\r\n"
            "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        )
        self.engine._remote_dj_handle_answer(OTHER_ATTEMPT_ID, sdp)
        self.assertEqual(
            self.current.webrtc.emit.call_args.args[0],
            "set-remote-description",
        )
        milestones = self.current.connection_attempt.milestones["server"]
        self.assertIn("answer_received", milestones)
        self.assertIn("answer_submitted", milestones)

    def test_stale_ice_cannot_be_added_but_matching_ice_still_is(self):
        self.engine._remote_dj_handle_ice(
            ATTEMPT_ID, 0, "stale-candidate"
        )
        self.current.webrtc.emit.assert_not_called()

        self.engine._remote_dj_handle_ice(
            OTHER_ATTEMPT_ID, 1, "current-candidate"
        )
        self.current.webrtc.emit.assert_called_once_with(
            "add-ice-candidate", 1, "current-candidate"
        )

    def test_stale_signaling_disconnect_cannot_stop_current_session(self):
        self.engine._remote_dj_session_stop(ATTEMPT_ID)
        self.assertIs(self.engine.remote_dj_session, self.current)

    def test_stale_offer_promise_callbacks_cannot_touch_current_session(self):
        stale = self._session(ATTEMPT_ID)
        offer_promise = MagicMock()
        local_desc_promise = MagicMock()

        self.engine._remote_dj_on_offer_created(
            offer_promise, (stale, ATTEMPT_ID, stale.webrtc)
        )
        self.engine._remote_dj_on_local_desc_set(
            local_desc_promise,
            (stale, ATTEMPT_ID, stale.webrtc, "stale-offer"),
        )

        offer_promise.wait.assert_not_called()
        local_desc_promise.wait.assert_not_called()
        self.current.webrtc.emit.assert_not_called()
        self.engine._remote_dj_server.send_json_threadsafe.assert_not_called()

    def test_matching_local_description_callback_queues_offer(self):
        promise = MagicMock()
        self.engine._remote_dj_on_local_desc_set(
            promise,
            (
                self.current,
                OTHER_ATTEMPT_ID,
                self.current.webrtc,
                "current-offer",
            ),
        )

        promise.wait.assert_called_once()
        self.engine._remote_dj_server.send_json_threadsafe.assert_called_once_with(
            {"type": "offer", "sdp": "current-offer"}
        )
        self.assertIn(
            "offer_queued",
            self.current.connection_attempt.milestones["server"],
        )

    def test_stale_inbound_failure_cannot_fail_current_session(self):
        stale = self._session(ATTEMPT_ID)
        with patch.object(eng_module, "emit_event") as emit_event:
            self.engine._remote_dj_fail_bound_session(
                stale,
                ATTEMPT_ID,
                FAILURE_MEDIA_ROUTING,
                "stale inbound build failed",
            )

        self.assertIs(self.engine.remote_dj_session, self.current)
        self.assertIsNone(self.current.connection_attempt.failure)
        emit_event.assert_not_called()


class RemoteDJTransceiverBuildGuardTests(SimpleTestCase):
    def setUp(self):
        Gst.init(None)
        self.engine = object.__new__(eng_module.PlaybackEngine)
        self.session = eng_module.RemoteDJSession()
        self.session.webrtc = MagicMock()
        self.mon_pay = MagicMock()
        self.src_pad = MagicMock()
        self.mon_pay.get_static_pad.return_value = self.src_pad

    def test_request_pad_none_is_typed_session_build_failure(self):
        self.session.webrtc.request_pad_simple.return_value = None
        with self.assertRaisesRegex(
            eng_module.RemoteDJSessionBuildError, "outgoing_send_pad"
        ):
            self.engine._remote_dj_configure_transceivers(
                self.session, self.mon_pay
            )

    def test_missing_send_transceiver_is_typed_failure(self):
        send_pad = MagicMock()
        send_pad.get_property.return_value = None
        self.session.webrtc.request_pad_simple.return_value = send_pad
        self.src_pad.link.return_value = Gst.PadLinkReturn.OK

        with self.assertRaisesRegex(
            eng_module.RemoteDJSessionBuildError, "outgoing_transceiver"
        ):
            self.engine._remote_dj_configure_transceivers(
                self.session, self.mon_pay
            )

    def test_failed_outgoing_pad_link_is_typed_failure(self):
        send_pad = MagicMock()
        self.session.webrtc.request_pad_simple.return_value = send_pad
        self.src_pad.link.return_value = Gst.PadLinkReturn.WRONG_HIERARCHY

        with self.assertRaisesRegex(
            eng_module.RemoteDJSessionBuildError, "outgoing_rtp_pad_link"
        ):
            self.engine._remote_dj_configure_transceivers(
                self.session, self.mon_pay
            )

    def test_missing_receive_transceiver_is_typed_failure(self):
        send_pad = MagicMock()
        send_pad.get_property.return_value = SimpleNamespace(
            props=SimpleNamespace(direction=None)
        )
        self.session.webrtc.request_pad_simple.return_value = send_pad
        self.session.webrtc.emit.return_value = None
        self.src_pad.link.return_value = Gst.PadLinkReturn.OK

        with self.assertRaisesRegex(
            eng_module.RemoteDJSessionBuildError, "incoming_transceiver"
        ):
            self.engine._remote_dj_configure_transceivers(
                self.session, self.mon_pay
            )

    def test_required_element_none_is_typed_dependency_failure(self):
        with patch.object(
            eng_module.Gst.ElementFactory, "make", return_value=None
        ):
            with self.assertRaisesRegex(
                eng_module.RemoteDJSessionBuildError, "test_dependency"
            ):
                self.engine._remote_dj_require_element(
                    "missing-element", "test_dependency"
                )

    def test_pipeline_add_failure_is_typed_session_build_failure(self):
        self.engine.main_pipeline = MagicMock()
        self.engine.main_pipeline.add.return_value = False
        element = MagicMock()
        element.get_name.return_value = "partial-webrtc"

        with self.assertRaisesRegex(
            eng_module.RemoteDJSessionBuildError, "test_pipeline_add"
        ):
            self.engine._remote_dj_add_element(
                self.session, element, "test_pipeline_add"
            )
        self.assertEqual(self.session.elements, [])


class RemoteDJBuildRollbackTests(TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.diag_path = Path(self.tmp_dir.name) / "remote-dj.log"
        self.diag_patch = patch.object(eng_module, "DJ_DIAG_LOG", self.diag_path)
        self.diag_patch.start()
        self.addCleanup(self.diag_patch.stop)

    def _stand_in(self):
        selector = MagicMock()
        silence_pad = object()
        webrtc_pad = MagicMock()
        webrtc_pad.get_peer.return_value = None
        remote_gain = MagicMock()
        remote_gate = MagicMock()
        slot = eng_module.RemoteDJSlot(
            slot_id=0,
            selector=selector,
            silence_pad=silence_pad,
            webrtc_pad=webrtc_pad,
            remote_gain=remote_gain,
            remote_gate=remote_gate,
            master_mixer_pad=object(),
            silence_src=object(),
        )
        engine = object.__new__(eng_module.PlaybackEngine)
        engine.remote_dj_session = None
        engine._remote_dj_last_attempt = None
        engine.remote_dj_tee = MagicMock()
        engine.local_mic_tee = None
        engine.dj_slots = [slot]
        engine._dj_slot_available = MagicMock(return_value=slot)
        engine._remote_dj_server = MagicMock()
        engine.main_pipeline = MagicMock()
        engine._apply_talk_ducking = MagicMock()
        engine._apply_mic_mode_hold = MagicMock()
        return engine, slot

    def test_failed_pad_link_rolls_back_session_slot_and_gate(self):
        engine, slot = self._stand_in()
        send_pad = MagicMock()
        send_pad.get_property.return_value = SimpleNamespace(
            props=SimpleNamespace(direction=None)
        )
        source_pad = MagicMock()
        source_pad.link.return_value = Gst.PadLinkReturn.WRONG_HIERARCHY
        mon_pay = MagicMock()
        mon_pay.get_static_pad.return_value = source_pad

        def fail_build(session):
            session.webrtc = MagicMock()
            session.webrtc.request_pad_simple.return_value = send_pad
            engine._remote_dj_configure_transceivers(session, mon_pay)

        engine._remote_dj_build_session = fail_build
        with (
            patch.object(
                eng_module.RemoteDJAudioInput,
                "load",
                return_value=SimpleNamespace(gain_db=0.0),
            ),
            patch.object(eng_module, "emit_event") as emit_event,
        ):
            engine._remote_dj_session_start(
                ATTEMPT_ID, 1_700_000_000_000
            )

        self.assertIsNone(engine.remote_dj_session)
        self.assertIsNone(slot.session)
        slot.selector.set_property.assert_called_with(
            "active-pad", slot.silence_pad
        )
        self.assertNotIn(
            ("volume", 1.0),
            [call.args for call in slot.remote_gate.set_property.call_args_list],
        )
        self.assertEqual(
            engine._remote_dj_last_attempt.failure["class"],
            FAILURE_DEPENDENCY_SESSION_BUILD,
        )
        engine._remote_dj_server.disconnect_threadsafe.assert_called_once()
        emit_event.assert_called_once()

    def test_partial_elements_and_request_pad_are_reclaimed(self):
        engine, slot = self._stand_in()
        partial_element = MagicMock()
        partial_element.get_parent.return_value = engine.main_pipeline
        monitor_pad = MagicMock()
        monitor_pad.get_peer.return_value = None

        def fail_build(session):
            session.elements.append(partial_element)
            session.monitor_tee_pad = monitor_pad
            raise eng_module.RemoteDJSessionBuildError(
                "monitor_return_pad_link", "simulated failure"
            )

        engine._remote_dj_build_session = fail_build
        with (
            patch.object(
                eng_module.RemoteDJAudioInput,
                "load",
                return_value=SimpleNamespace(gain_db=0.0),
            ),
            patch.object(eng_module, "emit_event"),
        ):
            engine._remote_dj_session_start(
                ATTEMPT_ID, 1_700_000_000_000
            )

        partial_element.set_state.assert_called_once_with(Gst.State.NULL)
        engine.main_pipeline.remove.assert_called_once_with(partial_element)
        engine.remote_dj_tee.release_request_pad.assert_called_once_with(
            monitor_pad
        )
        self.assertIsNone(engine.remote_dj_session)
        self.assertIsNone(slot.session)

    def test_config_read_failure_after_slot_claim_uses_normal_rollback(self):
        engine, slot = self._stand_in()
        with (
            patch.object(
                eng_module.RemoteDJAudioInput,
                "load",
                side_effect=RuntimeError("configuration unavailable"),
            ),
            patch.object(eng_module, "emit_event") as emit_event,
        ):
            engine._remote_dj_session_start(
                ATTEMPT_ID, 1_700_000_000_000
            )

        self.assertIsNone(engine.remote_dj_session)
        self.assertIsNone(slot.session)
        slot.remote_gate.set_property.assert_called_once_with("volume", 0.0)
        slot.selector.set_property.assert_called_once_with(
            "active-pad", slot.silence_pad
        )
        emit_event.assert_called_once()


class RemoteDJEngineStateCompatibilityTests(SimpleTestCase):
    def test_existing_booleans_remain_and_connection_summary_is_additive(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        state_path = Path(tmp_dir.name) / "engine_state.json"
        engine = make_minimal_stand_in()
        session = eng_module.RemoteDJSession()
        session.connection_attempt = RemoteDJConnectionAttempt(
            ATTEMPT_ID, 1_700_000_000_000
        )
        session.connection_attempt.record("glib_session_start")
        engine.remote_dj_tee = object()
        engine.remote_dj_session = session

        with patch.object(eng_module, "STATE_PATH", state_path):
            engine._write_state()
        state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertIs(state["remote_dj_configured"], True)
        self.assertIs(state["remote_dj_connected"], True)
        self.assertIs(state["remote_dj_live"], False)
        summary = state["remote_dj_connection"]
        self.assertEqual(summary["attempt_id"], ATTEMPT_ID)
        self.assertEqual(summary["stage"], "glib_session_start")
        self.assertEqual(
            set(summary),
            {
                "attempt_id",
                "status",
                "stage",
                "started_at",
                "elapsed_ms",
                "milestones_ms",
                "failure",
            },
        )
        self.assertEqual(
            set(summary["milestones_ms"]), {"server", "browser"}
        )
