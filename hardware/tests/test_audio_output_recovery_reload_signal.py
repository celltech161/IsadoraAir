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
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from hardware.admin import AudioOutputAdmin
from hardware.models import AudioOutput


class AudioOutputReloadSignalTests(TestCase):
    def _save_and_read_command(self, name, device="plughw:9,0", **extra_fields):
        with tempfile.TemporaryDirectory() as tmp:
            cmd_path = Path(tmp) / "engine_cmd.json"
            with patch("hardware.signals.CMD_PATH", cmd_path), \
                 self.captureOnCommitCallbacks(execute=True):
                obj, _ = AudioOutput.objects.get_or_create(name=name, defaults={"device": device})
                for field, value in extra_fields.items():
                    setattr(obj, field, value)
                obj.save()
            self.assertTrue(cmd_path.is_file(), "committed save must write a command")
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


    def test_command_is_published_only_after_transaction_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd_path = Path(tmp) / "engine_cmd.json"
            with patch("hardware.signals.CMD_PATH", cmd_path):
                with self.captureOnCommitCallbacks(execute=False) as callbacks:
                    obj, _ = AudioOutput.objects.get_or_create(name="Studio Monitor")
                    obj.device_identity_kind = "alsa_card_id"
                    obj.device_identity = "CODEC"
                    obj.save()
                    self.assertFalse(cmd_path.exists())
                self.assertGreaterEqual(len(callbacks), 1)
                for callback in callbacks:
                    callback()
            self.assertEqual(
                json.loads(cmd_path.read_text(encoding="utf-8")),
                {"command": "reload_audio_output"})

    def test_rolled_back_save_publishes_no_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd_path = Path(tmp) / "engine_cmd.json"
            with patch("hardware.signals.CMD_PATH", cmd_path), \
                 self.captureOnCommitCallbacks(execute=False) as callbacks:
                obj, _ = AudioOutput.objects.get_or_create(name="Studio Monitor")
                obj.device_identity_kind = "alsa_card_id"
                obj.device_identity = "CODEC"
                obj.save()
            self.assertGreaterEqual(len(callbacks), 1)
            self.assertFalse(cmd_path.exists())


class AudioOutputAdminSaveModelIntegrationTests(TestCase):
    """Integration-bug regression coverage: a real production save via
    the Django Admin went through AudioOutputAdmin.save_model(), not
    just the post_save signal in isolation -- and save_model() used to
    write a SECOND, separate "reload_agc_config" command directly to
    engine_cmd.json, AFTER super().save_model() (which fires the
    post_save signal above, writing "reload_audio_output") had already
    run. Both writers targeted the same single-slot file, so the
    admin's later write reliably clobbered the signal's -- an identity
    edit's "reload_audio_output" command never reached the engine; only
    the AGC reapply did. The tests above, which only ever call
    obj.save() directly, could never have caught this -- they never
    exercise save_model() at all. These do.

    CMD_PATH is patched to a throwaway tempfile in every test -- same
    production-safety requirement as above. amixer/alsactl subprocess
    calls (triggered by mixer-control changes) are mocked in the one
    test that exercises them -- this must never touch real hardware,
    on this box least of all."""

    def _save_via_admin(self, obj, form=None, mock_subprocess=False):
        admin_instance = AudioOutputAdmin(AudioOutput, None)
        with tempfile.TemporaryDirectory() as tmp:
            cmd_path = Path(tmp) / "engine_cmd.json"
            with patch("hardware.signals.CMD_PATH", cmd_path), \
                 self.captureOnCommitCallbacks(execute=True):
                if mock_subprocess:
                    with patch("hardware.admin.subprocess.run") as mock_run:
                        mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
                        admin_instance.save_model(request=None, obj=obj, form=form, change=True)
                else:
                    admin_instance.save_model(request=None, obj=obj, form=form, change=True)
            self.assertTrue(cmd_path.is_file(), "save_model must have produced a command")
            return json.loads(cmd_path.read_text(encoding="utf-8"))

    def test_studio_monitor_identity_save_via_admin_writes_reload_audio_output(self):
        """The exact production scenario reported: device=plughw:2,0,
        device_identity_kind=alsa_card_id, device_identity=PCH. The
        final command presented to the engine must be
        "reload_audio_output", never "reload_agc_config"."""
        obj, _ = AudioOutput.objects.get_or_create(
            name="Studio Monitor", defaults={"device": "plughw:2,0"})
        obj.device = "plughw:2,0"
        obj.device_identity_kind = "alsa_card_id"
        obj.device_identity = "PCH"

        payload = self._save_via_admin(obj)

        self.assertEqual(payload, {"command": "reload_audio_output"})
        self.assertNotEqual(payload.get("command"), "reload_agc_config")
        obj.refresh_from_db()
        self.assertEqual(obj.device_identity_kind, "alsa_card_id")
        self.assertEqual(obj.device_identity, "PCH")

    def test_studio_monitor_agc_only_save_via_admin_still_writes_reload_audio_output(self):
        """An AGC-only edit (no identity/device touched at all) must
        still land on "reload_audio_output" -- Studio Monitor has
        exactly ONE live-reload command, covering identity + device +
        AGC together, regardless of which specific fields this
        particular save changed."""
        obj, _ = AudioOutput.objects.get_or_create(
            name="Studio Monitor", defaults={"device": "plughw:2,0"})
        obj.device = "plughw:2,0"
        obj.agc_enabled = True
        obj.agc_ratio = 4.0
        obj.agc_threshold = 0.8
        obj.agc_makeup_gain_db = 3.0

        payload = self._save_via_admin(obj)

        self.assertEqual(payload, {"command": "reload_audio_output"})
        obj.refresh_from_db()
        self.assertTrue(obj.agc_enabled)
        self.assertEqual(obj.agc_ratio, 4.0)

    def test_stereotool_input_save_via_admin_still_writes_recovery_config_reload(self):
        """Regression guard: the removed second writer in save_model()
        was Studio-Monitor-only to begin with, so Stereotool Input's
        behavior through the real admin path shouldn't have changed --
        confirming that explicitly, via save_model() rather than the
        bare-signal test above."""
        obj, _ = AudioOutput.objects.get_or_create(
            name="Stereotool Input", defaults={"device": "plughw:0,0"})
        obj.device_identity_kind = "alsa_card_id"
        obj.device_identity = "Loopback"

        payload = self._save_via_admin(obj)

        self.assertEqual(payload, {"command": "reload_audio_output_recovery_config"})

    def test_studio_monitor_save_with_mixer_change_leaves_no_conflicting_command(self):
        """_apply_mixer_form_changes may itself call
        obj.save(update_fields=["mixer_control_values"]), which re-fires
        AudioOutput's post_save signal a SECOND time within the same
        admin save. Both writes are "reload_audio_output" for Studio
        Monitor -- same command, same single-slot file -- so the file's
        final content is still exactly that command, never a different,
        conflicting one."""
        obj, _ = AudioOutput.objects.get_or_create(
            name="Studio Monitor", defaults={"device": "plughw:2,0"})
        # This name is seeded blank-device by a data migration
        # (library/migrations/0008_audioinput_audiooutput.py's
        # seed_defaults) -- get_or_create's `defaults` only apply on
        # actual creation, so the row this test gets back may already
        # exist with device="". Set it explicitly: _parse_card_number
        # (and therefore the whole mixer-apply path this test is
        # exercising) needs a real card number to not short-circuit.
        obj.device = "plughw:2,0"
        obj.mixer_control_values = {}

        control = {"control_id": "Master", "label": "Master",
                   "has_enum": False, "has_switch": False, "has_volume": True}
        form = SimpleNamespace(_mixer_control_map={"mixer_0": control}, cleaned_data={"mixer_0": 55})

        payload = self._save_via_admin(obj, form=form, mock_subprocess=True)

        self.assertEqual(payload, {"command": "reload_audio_output"})
        obj.refresh_from_db()
        self.assertEqual(obj.mixer_control_values, {"Master": 55})
