"""Wire admin saves to the engine's live-reload IPC.

The engine polls /run/isadoraair/engine_cmd.json every 500 ms in
_check_commands; writing a JSON command there is a no-restart way to
push config changes. Worst-case latency from save to engine apply is
one poll cycle.
"""

import json
from pathlib import Path

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import AudioOutput

CMD_PATH = Path("/run/isadoraair/engine_cmd.json")


def _write_engine_command(payload):
    tmp = CMD_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.rename(CMD_PATH)
    except OSError:
        # Engine isn't running, or /run/isadoraair isn't created yet
        # (RuntimeDirectory= only exists while the unit is active).
        # Next engine startup will read the latest DB value anyway.
        pass


@receiver(post_save, sender=AudioOutput)
def reload_engine_on_studio_monitor_change(sender, instance, **kwargs):
    # Only the Studio Monitor row currently drives engine output.
    # Other named outputs (cue, headphones, ...) will need their own
    # reload commands as they get wired.
    if instance.name != "Studio Monitor":
        return
    _write_engine_command({"command": "reload_audio_output"})
