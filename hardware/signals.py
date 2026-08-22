"""Wire admin saves to the engine's live-reload IPC.

The engine polls /run/isadoraair/engine_cmd.json every 500 ms in
_check_commands; writing a JSON command there is a no-restart way to
push config changes. Worst-case latency from save to engine apply is
one poll cycle.
"""

import json
from pathlib import Path

from django.db import transaction
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
def reload_engine_on_audio_output_change(sender, instance, **kwargs):
    # [P0] 1.3C -- every AudioOutput row now needs SOME live-reload
    # signal, not just Studio Monitor: device_identity_kind/
    # device_identity (the automatic-recovery fields) live in the
    # running engine's OutputRecoverySlot objects, refreshed only when
    # asked -- without this, enabling "Automatic Recovery" in the admin
    # would silently do nothing until the next full engine restart.
    #
    # engine_cmd.json is a single-slot channel (see _write_engine_command
    # below/_check_commands) -- writing two commands for one save would
    # just have the second overwrite the first before the engine ever
    # reads it -- so exactly one is written per save, not two:
    #
    #   Studio Monitor: "reload_audio_output" (the pre-existing device-
    #     swap command) is Studio Monitor's ONE unified live-reload
    #     command -- it now ALSO refreshes every slot's recovery-identity
    #     fields AND reapplies AGC, all as part of the same command. An
    #     actual Studio Monitor identity change is an explicit branch-local
    #     live retarget; an unchanged identity is a hardware no-op. See
    #     engine.py's command handler and bounded output-slot lifecycle.
    #     (AGC reapply used to be
    #     a separate direct write from AudioOutputAdmin.save_model();
    #     that raced this signal for the same single-slot file and
    #     reliably clobbered this command -- see that module's save_model
    #     docstring.) Still the only named output with a live `device`-
    #     path swap at all (see _apply_audio_output_device's own
    #     docstring for why).
    #
    #   Every other named output (Stereotool Input today; any future
    #     output row): "reload_audio_output_recovery_config" -- refreshes
    #     recovery-identity fields only. A `device` (raw path) edit on
    #     one of these rows still requires a restart, unchanged from
    #     before this phase -- there is no live device-swap path for
    #     them at all, identity is the only thing now reloadable live.
    payload = {
        "command": (
            "reload_audio_output"
            if instance.name == "Studio Monitor"
            else "reload_audio_output_recovery_config"
        )
    }
    # Django admin wraps the whole change form in transaction.atomic().
    # Publishing from post_save itself lets the separately connected engine
    # consume this command before the new identity is committed. Defer the
    # atomic file write until the surrounding DB transaction commits; a
    # rollback then correctly publishes nothing.
    transaction.on_commit(lambda payload=payload: _write_engine_command(payload))
