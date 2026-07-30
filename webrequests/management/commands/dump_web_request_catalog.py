import json

from django.core.management.base import BaseCommand

from library.models import Track
from webrequests.models import WebRequestConfig


class Command(BaseCommand):
    """Dumps the full eligible-track catalog + availability grid as JSON,
    for the external catalog_sync.py script to push to the public site.
    Cross-venv pattern, same as dump_web_request_config -- this command
    is the only thing that touches the Django ORM; the external script
    just POSTs whatever comes out of stdout.

    Eligible = category-kind music AND ready2air. Matches exactly what
    the engine's fulfillment logic (once built) will accept as a valid
    request target -- the public site should never be able to search
    for something IsadoraAir would refuse to air."""

    help = "Dump the music/ready2air track catalog + availability grid as JSON for catalog_sync.py."

    def handle(self, *args, **options):
        cfg = WebRequestConfig.load()

        tracks = (
            Track.objects.filter(category__kind__code="music", ready2air=True)
            .select_related("artist", "album")
            .only("id", "title", "artist__name", "album__title", "duration_seconds")
        )

        payload = {
            "tracks": [
                {
                    "id": t.id,
                    "artist": t.artist.name,
                    "title": t.title,
                    "album": t.album.title if t.album_id else None,
                    "duration_seconds": t.duration_seconds,
                }
                for t in tracks.iterator()
            ],
            "availability_grid": [i in set(cfg.open_slots) for i in range(168)],
        }
        self.stdout.write(json.dumps(payload))
