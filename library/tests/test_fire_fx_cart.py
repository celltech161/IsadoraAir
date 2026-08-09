import json
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from library.models import FXCart


class FireFxCartCommandTests(TestCase):
    """Regression coverage for the fire_fx_cart bridge command
    (library/management/commands/fire_fx_cart.py), the internal process
    bridge the external weather-ingest venv's wx_alert_beep.py now
    shells out to instead of playing audio itself. No GStreamer
    playback is exercised here -- these tests only confirm the command
    validates the cart correctly and writes (or correctly fails to
    write) the same engine_cmd.json payload library.views.api_fx_fire
    writes for a browser-triggered fire.

    ENGINE_CMD_PATH is patched to a throwaway temp file for every test
    in this class -- this command is a real bridge to the SAME
    /run/isadoraair/engine_cmd.json the live playback engine polls
    every 500ms, and this box's engine is genuinely running in most
    dev/test contexts here. Never let a test in this class touch the
    real path."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.cmd_path = Path(self.tmpdir.name) / "engine_cmd.json"
        patcher = mock.patch(
            "library.management.commands.fire_fx_cart.ENGINE_CMD_PATH", self.cmd_path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_valid_enabled_cart_submits_expected_fx_fire_command(self):
        cart = FXCart.objects.create(name="Severe Wx Beep", filepath="/tmp/beep.wav")
        call_command("fire_fx_cart", str(cart.id))
        self.assertTrue(self.cmd_path.exists())
        self.assertEqual(
            json.loads(self.cmd_path.read_text()),
            {"command": "fx_fire", "cart_id": cart.id},
        )

    def test_nonexistent_cart_fails(self):
        with self.assertRaises(CommandError):
            call_command("fire_fx_cart", "999999")
        self.assertFalse(self.cmd_path.exists())

    def test_disabled_cart_fails(self):
        cart = FXCart.objects.create(name="Severe Wx Beep", filepath="/tmp/beep.wav", enabled=False)
        with self.assertRaises(CommandError):
            call_command("fire_fx_cart", str(cart.id))
        self.assertFalse(self.cmd_path.exists())

    def test_malformed_cart_id_fails(self):
        with self.assertRaises(CommandError):
            call_command("fire_fx_cart", "not-a-number")
        self.assertFalse(self.cmd_path.exists())

    def test_blank_cart_id_fails(self):
        with self.assertRaises(CommandError):
            call_command("fire_fx_cart", "")
        self.assertFalse(self.cmd_path.exists())

    def test_engine_command_write_failure_produces_command_failure(self):
        """A write failure (e.g. /run/isadoraair unmounted/missing) must
        raise CommandError -- not report success -- so a caller relying
        on the process exit status (wx_alert_beep.py) can tell submission
        genuinely failed and correctly withhold advancing its own
        last_played_iso timer."""
        cart = FXCart.objects.create(name="Severe Wx Beep", filepath="/tmp/beep.wav")
        bad_path = Path(self.tmpdir.name) / "nonexistent-subdir" / "engine_cmd.json"
        with mock.patch("library.management.commands.fire_fx_cart.ENGINE_CMD_PATH", bad_path):
            with self.assertRaises(CommandError):
                call_command("fire_fx_cart", str(cart.id))
        self.assertFalse(bad_path.exists())

    # Note: CommandError raised from handle() is exactly what Django's own
    # CLI entrypoint (run_from_argv, used by the real `python manage.py
    # fire_fx_cart ...` the external script subprocess.run()s) turns into
    # a nonzero process exit -- standard Django behavior, not re-verified
    # here via a raw ManagementUtility invocation: doing so inside a
    # TestCase trips run_from_argv's own `finally: connections.close_all()`,
    # which tears down the DB connection the surrounding test transaction
    # depends on and breaks every test that runs after it in the suite.
