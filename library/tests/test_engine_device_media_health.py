"""HW-04/05/06 live audio-device media-health status regression tests.

Hardware-free coverage of the third status axis: actual media-flow evidence.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from library.services import audio_recovery
from library.services import engine as eng_module


class MicMediaHealthStateTests(SimpleTestCase):
    def _engine(self):
        obj = object.__new__(eng_module.PlaybackEngine)
        obj._mic_slot = SimpleNamespace(
            snapshot=lambda: {
                "state": audio_recovery.SlotState.OK.value,
                "generation": 0,
            }
        )
        obj._mic_hw_bin = object()
        obj._mic_pending_hw_bin = None
        obj._mic_buf_count = 0
        obj._mic_media_baseline_count = 0
        obj._mic_media_observation_started_at = 100.0
        obj._mic_next_retry_at = None
        obj._mic_device_present = None
        obj._mic_identity_kind = "alsa_card_id"
        obj._mic_identity = "TESTMIC"
        obj._mic_legacy_device = "plughw:99,0"
        obj._mic_last_error = None
        obj._mic_last_state_change_at = None
        return obj

    def test_cold_start_mic_ok_can_coexist_with_unverified_then_not_flowing(self):
        obj = self._engine()
        obj.mic_ok = True
        obj._mic_buffer_age_ms = lambda: None

        with patch.object(eng_module.time, "monotonic", return_value=100.2):
            health = obj._mic_media_health_state()
        self.assertTrue(obj.mic_ok)
        self.assertEqual(health["status"], eng_module.LIVE_MEDIA_HEALTH_UNVERIFIED)

        with patch.object(eng_module.time, "monotonic", return_value=101.2):
            health = obj._mic_media_health_state()
        self.assertTrue(obj.mic_ok)
        self.assertEqual(health["status"], eng_module.LIVE_MEDIA_HEALTH_NOT_FLOWING)

    def test_fresh_current_generation_buffer_is_flowing_even_when_ptt_is_off(self):
        obj = self._engine()
        obj.mic_ok = True
        obj.mic_live = False
        obj._mic_buf_count = 4
        obj._mic_media_baseline_count = 3
        obj._mic_buffer_age_ms = lambda: 25

        with patch.object(eng_module.time, "monotonic", return_value=101.5):
            health = obj._mic_media_health_state()

        self.assertEqual(health["status"], eng_module.LIVE_MEDIA_HEALTH_FLOWING)
        self.assertEqual(health["buffers_observed_this_generation"], 1)
        self.assertFalse(obj.mic_live)

    def test_stale_hardware_buffer_is_not_flowing(self):
        obj = self._engine()
        obj._mic_buf_count = 1
        obj._mic_buffer_age_ms = lambda: eng_module.MIC_MEDIA_HEALTH_STALE_MS + 1

        with patch.object(eng_module.time, "monotonic", return_value=102.0):
            health = obj._mic_media_health_state()

        self.assertEqual(health["status"], eng_module.LIVE_MEDIA_HEALTH_NOT_FLOWING)

    def test_mic_recovery_payload_exports_media_health_separately_from_ok(self):
        obj = self._engine()
        obj.mic_ok = True
        expected = {
            "status": eng_module.LIVE_MEDIA_HEALTH_NOT_FLOWING,
            "buffer_age_ms": None,
            "buffers_observed_this_generation": 0,
        }
        obj._mic_media_health_state = lambda: expected

        with patch.object(
            eng_module.audio_recovery,
            "resolve_runtime_device",
            return_value="plughw:CARD=TESTMIC,DEV=0",
        ):
            payload = obj._mic_recovery_state()

        self.assertTrue(obj.mic_ok)
        self.assertEqual(payload["state"], audio_recovery.SlotState.OK.value)
        self.assertEqual(payload["media_health"], expected)


class OutputMediaHealthStateTests(SimpleTestCase):
    def _slot(self):
        sink = object()
        coordinator = SimpleNamespace(
            snapshot=lambda: {
                "state": audio_recovery.SlotState.OK.value,
                "generation": 0,
                "operation_state": "NONE",
            }
        )
        return SimpleNamespace(
            name="Studio Monitor",
            kind="studio_monitor",
            coordinator=coordinator,
            current_sink=sink,
            media_observation_sink=sink,
            media_observation_started_at=100.0,
            media_last_rendered=None,
            media_last_progress_at=None,
            next_retry_at=None,
            device_present=None,
            legacy_device="plughw:99,0",
            identity_kind="alsa_card_id",
            identity="TESTOUT",
            recovery_attempt=0,
            last_error=None,
            last_state_change_at=None,
        )

    def test_stagnant_render_counter_becomes_not_flowing_while_recovery_stays_ok(self):
        obj = object.__new__(eng_module.PlaybackEngine)
        slot = self._slot()

        with patch.object(eng_module, "_output_sink_rendered_count", return_value=0):
            with patch.object(eng_module.time, "monotonic", return_value=100.0):
                first = obj._output_media_health_state(slot)
            with patch.object(eng_module.time, "monotonic", return_value=101.1):
                later = obj._output_media_health_state(slot)

        self.assertEqual(first["status"], eng_module.LIVE_MEDIA_HEALTH_UNVERIFIED)
        self.assertEqual(later["status"], eng_module.LIVE_MEDIA_HEALTH_NOT_FLOWING)
        self.assertEqual(slot.coordinator.snapshot()["state"], audio_recovery.SlotState.OK.value)

    def test_advancing_render_counter_is_flowing(self):
        obj = object.__new__(eng_module.PlaybackEngine)
        slot = self._slot()

        with patch.object(eng_module, "_output_sink_rendered_count", side_effect=[10, 12]):
            with patch.object(eng_module.time, "monotonic", return_value=100.0):
                obj._output_media_health_state(slot)
            with patch.object(eng_module.time, "monotonic", return_value=100.2):
                health = obj._output_media_health_state(slot)

        self.assertEqual(health["status"], eng_module.LIVE_MEDIA_HEALTH_FLOWING)
        self.assertEqual(health["rendered_count"], 12)

    def test_output_recovery_payload_can_report_ok_but_not_flowing(self):
        obj = object.__new__(eng_module.PlaybackEngine)
        slot = self._slot()
        sink = MagicMock()
        sink.get_property.return_value = "plughw:CARD=TESTOUT,DEV=0"
        slot.current_sink = sink
        slot.media_observation_sink = sink
        obj._output_slots = {"studio_monitor": slot}

        expected = {
            "status": eng_module.LIVE_MEDIA_HEALTH_NOT_FLOWING,
            "rendered_count": 0,
            "last_progress_age_s": 1.5,
        }
        obj._output_media_health_state = lambda candidate: expected

        with patch.object(
            eng_module.audio_recovery,
            "resolve_runtime_device",
            return_value="plughw:CARD=TESTOUT,DEV=0",
        ):
            payload = obj._output_recovery_state()["studio_monitor"]

        self.assertEqual(payload["state"], audio_recovery.SlotState.OK.value)
        self.assertEqual(payload["media_health"], expected)


class DashboardMicHealthPresentationTests(SimpleTestCase):
    def test_dashboard_distinguishes_unverified_and_not_flowing_capture(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "library"
            / "dashboard.html"
        ).read_text()
        self.assertIn("micHealth === 'NOT_FLOWING'", template)
        self.assertIn("micHealth === 'UNVERIFIED'", template)
        self.assertIn("Studio Mic: CHECKING", template)
        self.assertIn("Studio Mic: LIVE (CHECKING)", template)
        self.assertIn("Studio Mic: LIVE (ERROR)", template)
