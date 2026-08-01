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
    site -- all active ones (pending/no_slot_soon/scheduled), plus
    terminal ones resolved within STATUS_SAFETY_WINDOW -- as JSON for
    the external requests_sync.py script to push. Cross-venv pattern:
    this command is the only thing that touches the ORM; the script
    just POSTs stdout verbatim.

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
        cutoff = timezone.now() - STATUS_SAFETY_WINDOW
        requests = SongRequest.objects.filter(
            Q(status__in=SongRequest.ACTIVE_STATUSES) | Q(resolved_at__gte=cutoff)
        )

        updates = []
        for req in requests:
            updates.append({
                "external_request_id": req.external_request_id,
                "status": req.status,
                "estimated_play_time": req.estimated_play_time.isoformat() if req.estimated_play_time else None,
                "scheduled_at": req.scheduled_at.isoformat() if req.scheduled_at else None,
                "fulfilled_at": req.fulfilled_at.isoformat() if req.fulfilled_at else None,
                "status_updated_at": req.status_updated_at.isoformat(),
            })

        self.stdout.write(json.dumps({"updates": updates}))
