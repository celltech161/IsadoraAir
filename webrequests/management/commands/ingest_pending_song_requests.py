import json
import sys

from django.core.management.base import BaseCommand
from webrequests.ingest import ingest_pending_items


class Command(BaseCommand):
    """Legacy cutover bridge: reads still-pending requests from stdin
    and creates one local
    SongRequest per external_request_id we haven't already seen.
    The native ingest_web_requests command now performs this in-process; this
    command remains temporarily useful while transitioning an older install.

    Already-known external_request_ids are silently skipped: once a
    request exists locally its lifecycle (pending -> ... -> terminal)
    is owned by refresh_song_request_statuses and the engine, not by
    repeated polls turning up the same still-open request."""

    help = "Legacy bridge: ingest pending public-site requests from JSON on stdin."

    def handle(self, *args, **options):
        payload = json.loads(sys.stdin.read())
        self.stdout.write(json.dumps(ingest_pending_items(payload.get("requests", []))))
