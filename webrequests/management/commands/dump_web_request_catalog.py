import json

from django.core.management.base import BaseCommand

from webrequests.ingest import build_catalog_payload


class Command(BaseCommand):
    """Legacy cutover bridge that dumps the eligible catalog and grid.
    The native sync_web_request_catalog command now builds and pushes this
    in-process; keeping the dump avoids breaking an old helper before cutover.

    Eligible = category-kind music AND ready2air. Matches exactly what
    the engine's fulfillment logic (once built) will accept as a valid
    request target -- the public site should never be able to search
    for something IsadoraAir would refuse to air."""

    help = "Legacy bridge: dump the requestable catalog and availability grid as JSON."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(build_catalog_payload()))
