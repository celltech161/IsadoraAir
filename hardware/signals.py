"""Wire committed admin saves to the engine's bounded live-reload queue."""

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from isadoraair.engine_commands import EngineCommandError, enqueue_engine_command

from .models import AudioOutput

log = logging.getLogger(__name__)


def _write_engine_command(payload):
    try:
        enqueue_engine_command(payload)
        return True
    except EngineCommandError as exc:
        # The database remains authoritative and the save is already
        # committed. Keep that valid save nonfatal, but make it explicit that
        # the running engine did not receive the requested live reload.
        log.error("Engine live-reload command was not queued: %s", exc)
        return False


@receiver(post_save, sender=AudioOutput)
def reload_engine_on_audio_output_change(sender, instance, update_fields=None, **kwargs):
    # [P0] 1.3C -- every AudioOutput row now needs SOME live-reload
    # signal, not just Studio Monitor: device_identity_kind/
    # device_identity (the automatic-recovery fields) live in the
    # running engine's OutputRecoverySlot objects, refreshed only when
    # asked -- without this, enabling "Automatic Recovery" in the admin
    # would silently do nothing until the next full engine restart.
    #
    # Exactly one unified command is still intentional for each save:
    #
    #   Studio Monitor: "reload_audio_output" (the pre-existing device-
    #     swap command) is Studio Monitor's ONE unified live-reload
    #     command -- it now ALSO refreshes every slot's recovery-identity
    #     fields AND reapplies AGC, all as part of the same command. An
    #     actual Studio Monitor identity change is an explicit branch-local
    #     live retarget; an unchanged identity is a hardware no-op. See
    #     engine.py's command handler and bounded output-slot lifecycle.
    #     (AGC reapply used to be
    #     a separate direct write from AudioOutputAdmin.save_model(); that
    #     historically raced this signal on the old single-slot transport --
    #     see that module's save_model docstring.) Still the only named output
    #     with a live `device`-
    #     path swap at all (see _apply_audio_output_device's own
    #     docstring for why).
    #
    #   Every other named output (Stereotool Input today; any future
    #     output row): "reload_audio_output_recovery_config" -- refreshes
    #     recovery-identity fields only. A `device` (raw path) edit on
    #     one of these rows still requires a restart, unchanged from
    #     before this phase -- there is no live device-swap path for
    #     them at all, identity is the only thing now reloadable live.
    # AudioOutputAdmin may persist its already-applied ALSA mixer-control
    # snapshot with a narrow second save. The primary model save has already
    # scheduled the one unified reload; do not turn that implementation detail
    # into a duplicate queued command now that the transport preserves every
    # publication rather than overwriting a single slot.
    if update_fields is not None and set(update_fields) == {"mixer_control_values"}:
        return

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
    # queue publication until the surrounding DB transaction commits; a
    # rollback then correctly publishes nothing.
    transaction.on_commit(lambda payload=payload: _write_engine_command(payload))
