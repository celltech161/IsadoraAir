import json
import sys

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from library.models import Track
from webrequests.models import SongRequest


class Command(BaseCommand):
    """Reads a JSON payload of still-pending requests (piped via stdin
    by the external requests_sync.py poller) and creates one local
    SongRequest per external_request_id we haven't already seen.
    Cross-venv pattern -- the poller has no Django installed, so all it
    does is HTTP GET the public site's pending-requests endpoint and
    pipe the response straight into this command.

    Already-known external_request_ids are silently skipped: once a
    request exists locally its lifecycle (pending -> ... -> terminal)
    is owned by refresh_song_request_statuses and the engine, not by
    repeated polls turning up the same still-open request."""

    help = "Ingest pending song requests polled from the public site (JSON via stdin)."

    def handle(self, *args, **options):
        payload = json.loads(sys.stdin.read())
        now = timezone.now()
        created = 0
        skipped = 0

        for item in payload.get("requests", []):
            external_id = item["id"]
            if SongRequest.objects.filter(external_request_id=external_id).exists():
                skipped += 1
                continue

            track = Track.objects.filter(
                id=item["track_id"], category__kind__code="music", ready2air=True,
            ).first()

            req = SongRequest.objects.create(
                external_request_id=external_id,
                track=track,
                requester_name=item.get("requester_name", ""),
                dedication_message=item.get("dedication_message", ""),
                submitted_at=parse_datetime(item["submitted_at"]),
            )
            if track is None:
                # Public site's own catalog sync is stale, or the track
                # went ineligible between catalog_sync and this poll --
                # resolve it immediately rather than waiting for the
                # next refresh_song_request_statuses cycle.
                req.status = "unavailable"
                req.resolved_at = now
                req.save(update_fields=["status", "resolved_at"])
            created += 1

        self.stdout.write(json.dumps({"created": created, "skipped": skipped}))
