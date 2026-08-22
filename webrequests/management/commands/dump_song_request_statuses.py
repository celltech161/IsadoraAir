import json

from django.core.management.base import BaseCommand
from webrequests.ingest import build_status_payload


class Command(BaseCommand):
    """Dumps every SongRequest that still needs reporting to the public
    site -- all active ones (pending/no_slot_soon/scheduled), plus
    recently resolved terminal ones. Retained as a legacy cutover bridge;
    ingest_web_requests now builds and pushes the same payload in-process.

    Every update carries the COMPLETE field set, with explicit nulls
    for anything not applicable rather than omitted keys -- a full
    state-replacement contract, agreed with the public site, so a
    backward status move (e.g. scheduled -> pending after an
    hour-rollover reconciliation) can't leave a stale ETA or air
    timestamp displayed from the request's previous state. status_
    updated_at is the version/ordering token the site uses to reject an
    out-of-order or delayed push."""

    help = "Dump SongRequest statuses needing a push to the public site's status endpoint."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps({"updates": build_status_payload()}))
