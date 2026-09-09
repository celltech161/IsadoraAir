from django.core.management.base import BaseCommand
from django.db import connection

from webrequests.models import SongRequest
from webrequests.services import DedicationSynthesisOutcome, synthesize_dedication_intro

# Arbitrary fixed two-integer pair for the command-wide Postgres advisory
# lock -- same idiom as log_builder.py's _advisory_lock_for_hour, just a
# single fixed key here rather than one keyed per (date, hour), since
# this command's job is small enough that serializing the WHOLE run
# against itself (rather than per-request) is simpler and sufficient:
# expected volume is low, and a command-wide lock means an overlapping
# firing (e.g. a slow run still finishing when the next timer fires)
# just exits immediately instead of racing itself and risking the
# destructive-delete scenario two workers redundantly attaching then
# cleaning up the SAME Track row could otherwise produce.
DEDICATION_LOCK_KEY = (0x44454449, 0x43415449)  # "DEDI"/"CATI" as int32s, arbitrary


class Command(BaseCommand):
    """Synthesizes spoken dedication intros for scheduled web requests,
    on its own timer -- deliberately kept OUT of
    refresh_song_request_statuses, which must stay fast and reliable
    (stranded-request detection, self-heal, expiry, scheduling, ETA
    refresh all run there every ~20s). TTS plus ffmpeg can take
    tens of seconds worst case; bolting that onto the reconciliation
    command would risk delaying exactly the recovery logic that matters
    most.

    Winner (earliest-submitted collapsed request) is determined per
    distinct log_item_id BEFORE checking whether synthesis is still
    needed -- filtering intro_track__isnull=True first would let the
    winner's own row drop out of the query the moment it's synthesized,
    making a collapsed duplicate look like "the first row" on the next
    cycle and get a redundant intro of its own.

    The five-per-run cap below counts only real synthesis attempts
    (DedicationSynthesisOutcome.SYNTHESIZED/FAILED) -- a deterministic
    text-policy suppression (POLICY_SUPPRESSED) never touches shared
    TTS/ffmpeg at all and does not consume a slot, so several
    permanently-suppressed winners ordered ahead of a valid one cannot
    starve it out of a run. See DedicationSynthesisOutcome's docstring
    in webrequests/services.py."""

    help = "Synthesize spoken dedication intros with the configured logical TTS voice."

    def handle(self, *args, **options):
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s, %s)", DEDICATION_LOCK_KEY)
            acquired = cur.fetchone()[0]
        if not acquired:
            self.stdout.write("Another instance is already running; exiting.")
            return
        try:
            self._run()
        finally:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", DEDICATION_LOCK_KEY)

    def _run(self):
        scheduled = (
            SongRequest.objects.filter(
                status="scheduled", log_item__isnull=False,
                # Don't synthesize for a song that's already airing/aired --
                # a real, if brief, window exists where played_at is set but
                # status hasn't been promoted to fulfilled yet (self-heal
                # hasn't run, or mark_song_requests_aired itself hasn't
                # completed). Any intro built in that window can never be
                # spliced (the LogItem is already past _next_queue_item) --
                # pure wasted synthesis.
                log_item__played_at__isnull=True,
            )
            .select_related("log_item", "track", "track__artist")
            .order_by("log_item__scheduled_time", "submitted_at", "id")
        )

        seen_log_item_ids = set()
        processed = 0
        synthesized = 0
        suppressed = 0
        for req in scheduled:
            if req.log_item_id in seen_log_item_ids:
                continue
            seen_log_item_ids.add(req.log_item_id)  # permanent winner for this LogItem
            if req.intro_track_id is not None:
                continue  # already synthesized on a prior cycle
            if processed >= 5:  # cap per run -- a sudden burst drains across several cycles
                break
            outcome = synthesize_dedication_intro(req)
            if outcome is DedicationSynthesisOutcome.POLICY_SUPPRESSED:
                # A deterministic template/input-length violation, caught
                # before any shared-TTS/ffmpeg work began -- NOT a real
                # attempt. Deliberately does not count against the
                # five-per-run cap (see DedicationSynthesisOutcome's own
                # docstring): otherwise, several permanently-suppressed
                # requests ordered ahead of a valid one could starve it
                # out of every run indefinitely. `continue`, not `break`,
                # so scanning keeps going past it in this same run.
                suppressed += 1
                continue
            processed += 1
            if outcome is DedicationSynthesisOutcome.SYNTHESIZED:
                synthesized += 1

        self.stdout.write(
            f"Checked {len(seen_log_item_ids)} slot(s), attempted {processed}, "
            f"synthesized {synthesized}, suppressed {suppressed}."
        )
