import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from library.models import FXCart

# Deliberately NOT imported from library.services.engine (which pulls in
# GStreamer/gi bindings at module scope) -- same reasoning library.views'
# api_fx_fire keeps its own inline Path("/run/isadoraair/engine_cmd.json")
# rather than importing the engine module. Every engine_cmd.json writer in
# this project inlines this literal rather than sharing a helper; matched
# here rather than introducing a one-off IPC abstraction.
ENGINE_CMD_PATH = Path("/run/isadoraair/engine_cmd.json")


class Command(BaseCommand):
    """Thin internal bridge: submits an existing, enabled FXCart to the
    IsadoraAir playback engine via its existing fx_fire engine command,
    for callers that can't reach Django directly -- currently the
    external weather-ingest venv's severe-weather alert beep
    (wx_alert_beep.py), invoked via subprocess using the same cross-venv
    pattern as sync_track_file/send_weather_notification/dump_weather_config.

    Mirrors library.views.api_fx_fire's own cart-lookup/validation shape
    (cart must exist and be enabled) without its HTTP surface or access
    control -- this is a process-local, same-box bridge for trusted
    callers, not a web API. Do NOT POST through /api/fx/ for this: that
    endpoint requires an authenticated session, which an unattended cron
    script has no way to hold.

    Retrigger mode, polyphony cap, and whether a sample is ultimately
    audible are enforced engine-side (single source of truth, same as
    every other fx_fire caller -- dashboard buttons, remote-DJ console,
    and this command all funnel through the identical engine-side
    _fx_fire). This command only confirms the cart exists/is enabled and
    that the engine command file was written; it implements no GStreamer
    playback, calls no ffmpeg, and touches no ALSA device itself."""

    help = "Fire an FXCart through the IsadoraAir engine's existing fx_fire command."

    def add_arguments(self, parser):
        parser.add_argument("cart_id", help="FXCart primary key.")

    def handle(self, *args, **options):
        raw_cart_id = options["cart_id"]
        try:
            cart_id = int(raw_cart_id)
        except (TypeError, ValueError):
            raise CommandError(f"cart_id must be an integer, got {raw_cart_id!r}")

        cart = FXCart.objects.filter(id=cart_id, enabled=True).only("id").first()
        if cart is None:
            raise CommandError(f"FXCart {cart_id} not found or disabled")

        try:
            ENGINE_CMD_PATH.write_text(
                json.dumps({"command": "fx_fire", "cart_id": cart_id}),
                encoding="utf-8",
            )
        except OSError as exc:
            raise CommandError(f"engine command dispatch failed: {exc}")

        self.stdout.write(f"Fired fx_fire for cart {cart_id}")
