from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from isadoraair.engine_commands import EngineCommandQueueFull
from library.models import FXCart


class FireFxCartCommandTests(TestCase):
    """Regression coverage for the fire_fx_cart bridge command
    (library/management/commands/fire_fx_cart.py), the internal process
    bridge the in-tree weather_ingest venv's wx_alert_beep.py now
    shells out to instead of playing audio itself. No GStreamer
    playback is exercised here -- these tests only confirm the command
    validates the cart and submits the same queued payload
    library.views.api_fx_fire uses for a browser-triggered fire."""

    def setUp(self):
        patcher = mock.patch(
            "library.management.commands.fire_fx_cart.enqueue_engine_command"
        )
        self.enqueue = patcher.start()
        self.addCleanup(patcher.stop)

    def test_valid_enabled_cart_submits_expected_fx_fire_command(self):
        cart = FXCart.objects.create(name="Severe Wx Beep", filepath="/tmp/beep.wav")
        call_command("fire_fx_cart", str(cart.id))
        self.enqueue.assert_called_once_with(
            {"command": "fx_fire", "cart_id": cart.id}
        )

    def test_nonexistent_cart_fails(self):
        with self.assertRaises(CommandError):
            call_command("fire_fx_cart", "999999")
        self.enqueue.assert_not_called()

    def test_disabled_cart_fails(self):
        cart = FXCart.objects.create(name="Severe Wx Beep", filepath="/tmp/beep.wav", enabled=False)
        with self.assertRaises(CommandError):
            call_command("fire_fx_cart", str(cart.id))
        self.enqueue.assert_not_called()

    def test_malformed_cart_id_fails(self):
        with self.assertRaises(CommandError):
            call_command("fire_fx_cart", "not-a-number")
        self.enqueue.assert_not_called()

    def test_blank_cart_id_fails(self):
        with self.assertRaises(CommandError):
            call_command("fire_fx_cart", "")
        self.enqueue.assert_not_called()

    def test_engine_command_write_failure_produces_command_failure(self):
        """A write failure (e.g. /run/isadoraair unmounted/missing) must
        raise CommandError -- not report success -- so a caller relying
        on the process exit status (wx_alert_beep.py) can tell submission
        genuinely failed and correctly withhold advancing its own
        last_played_iso timer."""
        cart = FXCart.objects.create(name="Severe Wx Beep", filepath="/tmp/beep.wav")
        self.enqueue.side_effect = EngineCommandQueueFull("queue full")
        with self.assertRaises(CommandError):
            call_command("fire_fx_cart", str(cart.id))

    # Note: CommandError raised from handle() is exactly what Django's own
    # CLI entrypoint (run_from_argv, used by the real `python manage.py
    # fire_fx_cart ...` the external script subprocess.run()s) turns into
    # a nonzero process exit -- standard Django behavior, not re-verified
    # here via a raw ManagementUtility invocation: doing so inside a
    # TestCase trips run_from_argv's own `finally: connections.close_all()`,
    # which tears down the DB connection the surrounding test transaction
    # depends on and breaks every test that runs after it in the suite.
