import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, TestCase

from isadoraair.engine_commands import EngineCommandQueueFull
from library import views
from library.models import FXCart


class EngineCommandHttpProducerTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _post(self, path, payload):
        return self.factory.post(
            path, data=json.dumps(payload), content_type="application/json"
        )

    @patch("library.views.enqueue_engine_command")
    def test_library_seek_enqueues_exact_payload(self, enqueue):
        response = views.api_engine_seek(
            self._post("/api/engine/seek/", {"position": 12.5, "slot": "b"})
        )
        self.assertEqual(response.status_code, 200)
        enqueue.assert_called_once_with(
            {"command": "seek", "position": 12.5, "slot": "B"}
        )

    @patch("library.views.enqueue_engine_command")
    def test_deck_action_enqueues_exact_payload(self, enqueue):
        response = views.api_engine_deck_command(
            self._post("/api/engine/deck/a/", {"action": "pause"}), "a"
        )
        self.assertEqual(response.status_code, 200)
        enqueue.assert_called_once_with({"command": "deck_pause", "slot": "A"})

    @patch("library.views.enqueue_engine_command")
    def test_mic_ptt_enqueues_exact_payload(self, enqueue):
        response = views.api_engine_mic_ptt(
            self._post("/api/engine/mic-ptt/", {"active": True})
        )
        self.assertEqual(response.status_code, 200)
        enqueue.assert_called_once_with({"command": "mic_ptt", "active": True})

    @patch("library.views.enqueue_engine_command")
    def test_remote_dj_gate_enqueues_exact_payload(self, enqueue):
        response = views.api_engine_remote_dj_gate(
            self._post("/api/engine/remote-dj-gate/", {"active": False})
        )
        self.assertEqual(response.status_code, 200)
        enqueue.assert_called_once_with(
            {"command": "remote_dj_gate", "active": False}
        )

    @patch("library.views.enqueue_engine_command")
    def test_manual_mode_enqueues_exact_payload(self, enqueue):
        response = views.api_engine_manual_mode(
            self._post("/api/engine/manual-mode/", {"active": True})
        )
        self.assertEqual(response.status_code, 200)
        enqueue.assert_called_once_with(
            {"command": "set_manual_mode", "active": True}
        )

    @patch("library.views.enqueue_engine_command")
    def test_browser_fx_fire_enqueues_exact_payload(self, enqueue):
        cart = FXCart.objects.create(name="Test Cart", filepath="/tmp/test.wav")
        response = views.api_fx_fire(
            self._post("/api/fx/fire/", {"cart_id": cart.id})
        )
        self.assertEqual(response.status_code, 200)
        enqueue.assert_called_once_with({"command": "fx_fire", "cart_id": cart.id})

    @patch("library.views.enqueue_engine_command")
    def test_log_reload_path_enqueues_after_log_is_approved(self, enqueue):
        log = MagicMock(id=44)
        log.items.count.return_value = 3
        with patch("library.views.get_object_or_404", return_value=SimpleNamespace()), patch(
            "library.views._build_from_playlist", return_value=(log, None)
        ):
            response = views.api_playlist_play_now(
                self._post("/api/playlists/7/play-now/", {}), 7
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(log.status, "approved")
        log.save.assert_called_once_with(update_fields=["status"])
        enqueue.assert_called_once_with({"command": "reload_current_log"})

    @patch(
        "library.views.enqueue_engine_command",
        side_effect=EngineCommandQueueFull("engine command queue is full (256/256)"),
    )
    def test_ephemeral_http_failure_returns_truthful_503(self, enqueue):
        response = views.api_engine_mic_ptt(
            self._post("/api/engine/mic-ptt/", {"active": True})
        )
        self.assertEqual(response.status_code, 503)
        body = json.loads(response.content)
        self.assertNotIn("ok", body)
        self.assertIn("queue is full", body["error"])
