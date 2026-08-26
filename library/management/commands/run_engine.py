import sys

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Start the IsadoraAir playback engine."

    def handle(self, *args, **options):
        from library.services.engine import (
            PlaybackEngine,
            POLICY_RESTART_EXIT_STATUS,
        )
        engine = PlaybackEngine()
        engine.start()
        if engine.restart_required:
            # [P0] 1.8 -- a poisoned deck slot cannot be reclaimed inside
            # this process; exit non-zero so systemd's
            # Restart=on-failure hands off to a fresh instance that
            # consumes the persisted anti-replay marker written by
            # PlaybackEngine._request_restart. See that method's
            # docstring and library/tests/test_engine_deck_lifecycle.py.
            sys.exit(POLICY_RESTART_EXIT_STATUS)
