import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from webrequests.models import SongRequest

# How long a terminal status keeps getting reported after resolved_at,
# instead of only once -- so a single dropped push from requests_sync.py
# can't leave the public site showing a stale status forever. Generous
# on purpose: cheap to over-report a handful of already-resolved rows,
# expensive for a requester to be told "pending" after their song aired.
STATUS_SAFETY_WINDOW = timedelta(hours=1)


class Command(BaseCommand):
    """Dumps every SongRequest that still needs reporting to the public
    site -- all non-terminal ones, plus terminal ones resolved within
    STATUS_SAFETY_WINDOW -- as JSON for the external requests_sync.py
    script to push. Cross-venv pattern: this command is the only thing
    that touches the ORM; the script just POSTs stdout verbatim."""

    help = "Dump SongRequest statuses needing a push to the public site's status endpoint."

    def handle(self, *args, **options):
        cutoff = timezone.now() - STATUS_SAFETY_WINDOW
        requests = SongRequest.objects.filter(
            Q(status__in=SongRequest.NON_TERMINAL_STATUSES) | Q(resolved_at__gte=cutoff)
        )

        payload = {
            "statuses": [
                {
                    "id": req.external_request_id,
                    "status": req.status,
                    "estimated_play_time": (
                        req.estimated_play_time.isoformat() if req.estimated_play_time else None
                    ),
                }
                for req in requests
            ]
        }
        self.stdout.write(json.dumps(payload))
