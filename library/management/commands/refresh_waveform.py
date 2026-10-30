"""Refresh Waveform Only for one Track (roadmap 1.21, r0123 Package B).

Regenerates the Track's waveform cache from its current audio file as a trusted version-2 cache
and changes nothing else (library.services.waveform_generation.refresh). One Track per run; there
is deliberately no "all Tracks" mode -- installing r0123 never regenerates caches by itself.

    manage.py refresh_waveform TRACK_ID [--json]

``--background`` with the lock descriptors is how the Refresh Waveform Only endpoint runs a
long-form Track in a detached process; it is not meant to be typed.
"""
import json
import os

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Regenerate one Track's waveform cache from its audio, changing nothing else."

    def add_arguments(self, parser):
        parser.add_argument("track_id", type=int)
        parser.add_argument("--json", action="store_true", help="Machine-readable result.")
        parser.add_argument("--background", action="store_true", help=argparse_suppress())
        parser.add_argument("--operation-lock-fd", type=int, help=argparse_suppress())
        parser.add_argument("--slot-lock-fd", type=int, help=argparse_suppress())

    def handle(self, *args, **options):
        from library.services import waveform_generation
        from monitoring.models import emit_event

        if options["background"]:
            os.nice(10)              # long-form decoding yields to the station's real-time work
        try:
            result = waveform_generation.refresh(options["track_id"], operation_lock_fd=options["operation_lock_fd"])
        finally:
            if options["slot_lock_fd"] is not None:
                os.close(options["slot_lock_fd"])
        if options["background"]:
            emit_event(category="library", level="info" if result.ok else "warning",
                       title="Waveform refreshed" if result.ok else "Waveform refresh did not complete",
                       detail=result.to_dict(), dedupe_key=f"library|waveform-refresh|{result.track_id}|{result.status}")
        if options["json"]:
            self.stdout.write(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            self.stdout.write(f"Track {result.track_id}: {result.status} -- {result.detail}")
        if not result.ok:
            raise CommandError(f"waveform not refreshed: {result.status}")


def argparse_suppress():
    import argparse
    return argparse.SUPPRESS
