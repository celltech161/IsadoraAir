"""[P0] 1.3C pre-commit review -- hardware/signals.py's AudioOutput
post_save handler now writes ONE of two commands depending on which row
was saved (see that module's own updated docstring for why exactly one,
never two, given engine_cmd.json is a single-slot channel). No prior
test coverage of this signal existed at all; added here alongside this
phase's own change to it.

CMD_PATH is patched to a throwaway tempfile in every test -- this box IS
production (isadoraair-engine.service is live), and the real
/run/isadoraair/engine_cmd.json is that engine's actual IPC inbox. A
test must never write to it."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from hardware.models import AudioOutput


class AudioOutputReloadSignalTests(TestCase):
    def _save_and_read_command(self, name, device="plughw:9,0", **extra_fields):
        with tempfile.TemporaryDirectory() as tmp:
            cmd_path = Path(tmp) / "engine_cmd.json"
            with patch("hardware.signals.CMD_PATH", cmd_path):
                obj, _ = AudioOutput.objects.get_or_create(name=name, defaults={"device": device})
                for field, value in extra_fields.items():
                    setattr(obj, field, value)
                obj.save()
                self.assertTrue(cmd_path.is_file(), "post_save must have written a command")
                return json.loads(cmd_path.read_text(encoding="utf-8"))

    def test_studio_monitor_save_writes_reload_audio_output(self):
        payload = self._save_and_read_command("Studio Monitor")
        self.assertEqual(payload, {"command": "reload_audio_output"})

    def test_stereotool_input_save_writes_recovery_config_reload(self):
        payload = self._save_and_read_command("Stereotool Input")
        self.assertEqual(payload, {"command": "reload_audio_output_recovery_config"})

    def test_arbitrary_other_output_name_also_gets_recovery_config_reload(self):
        """Any future named output (not just the two the engine
        currently resolves by name) still gets SOME live-reload signal,
        not silence -- the engine-side handler is a safe no-op for a
        name it doesn't currently build a slot for."""
        payload = self._save_and_read_command("Some Future Output")
        self.assertEqual(payload, {"command": "reload_audio_output_recovery_config"})

    def test_identity_only_edit_still_fires_the_signal(self):
        """The whole point of this phase's fix: an edit that touches
        ONLY device_identity_kind/device_identity (not `device` at all)
        must still reach the engine -- this is exactly the case that
        was previously silently inert."""
        payload = self._save_and_read_command(
            "Stereotool Input", device_identity_kind="alsa_card_id", device_identity="CODEC")
        self.assertEqual(payload, {"command": "reload_audio_output_recovery_config"})
