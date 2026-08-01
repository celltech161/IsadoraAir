from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from webrequests.models import SongRequest, WebRequestConfig


class Command(BaseCommand):
    help = (
        "One-time, review-first cleanup for SongRequest rows stuck as "
        "status=fulfilled with log_item IS NULL -- orphaned under the "
        "old fulfilled-means-assigned semantics, left untouched by the "
        "scheduled/fulfilled migration on purpose since this specific "
        "case is ambiguous: log_item can be NULL either because the "
        "assignment was genuinely lost, or because a perfectly "
        "successful, long-since-aired request's LogItem was later "
        "deleted by an unrelated cleanup (on_delete=SET_NULL) -- a "
        "migration can't tell those apart and shouldn't guess. Run "
        "manually once, post-migration, before re-enabling the "
        "feature. Reports every candidate row for review; only "
        "auto-requeues (to pending) the strong-evidence subset -- "
        "recent (submitted_at within expire_after_hours of now) AND "
        "whose old estimated_play_time was still in the future as of "
        "this run, meaning the old advisory ETA hadn't even elapsed "
        "yet, strongly suggesting the request never got the chance to "
        "air. Recent-but-ambiguous rows (past ETA) are reported only "
        "unless --requeue-all-recent is passed. Rows older than "
        "expire_after_hours are never auto-touched by either mode."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--requeue-all-recent",
            action="store_true",
            help="Also requeue recent-but-ambiguous rows (within expire_after_hours "
                 "of submitted_at, but whose old ETA has already passed) -- not just "
                 "the strong-evidence subset. Rows older than expire_after_hours are "
                 "still never touched.",
        )

    def handle(self, *args, **options):
        requeue_all_recent = options["requeue_all_recent"]
        cfg = WebRequestConfig.load()
        now = timezone.now()
        cutoff = now - timedelta(hours=cfg.expire_after_hours)

        candidates = (
            SongRequest.objects.filter(status="fulfilled", log_item__isnull=True)
            .select_related("track", "track__artist")
            .order_by("submitted_at")
        )

        strong_count = 0
        ambiguous_count = 0
        requeued_count = 0
        old_count = 0

        for req in candidates:
            track_label = (
                f"{req.track.artist.name if req.track.artist_id else '?'} - {req.track.title}"
                if req.track_id else "(track removed)"
            )
            age = now - req.submitted_at
            eta_label = req.estimated_play_time.isoformat() if req.estimated_play_time else "(none)"
            recent = req.submitted_at >= cutoff

            if not recent:
                old_count += 1
                self.stdout.write(
                    f"  OLD (never auto-touched): request={req.external_request_id} "
                    f"track={track_label} old_eta={eta_label} age={age}"
                )
                continue

            strong_evidence = req.estimated_play_time is not None and req.estimated_play_time > now
            if strong_evidence:
                bucket, will_requeue = "STRONG-EVIDENCE", True
                strong_count += 1
            else:
                bucket, will_requeue = "AMBIGUOUS", requeue_all_recent
                ambiguous_count += 1

            action = "requeuing to pending" if will_requeue else "reporting only -- use --requeue-all-recent to touch"
            self.stdout.write(
                f"  {bucket}: request={req.external_request_id} track={track_label} "
                f"old_eta={eta_label} age={age} -- {action}"
            )

            if will_requeue:
                requeue_now = timezone.now()
                requeued_count += SongRequest.objects.filter(
                    id=req.id, status="fulfilled", log_item__isnull=True,
                ).update(
                    status="pending", log_item=None, scheduled_at=None, fulfilled_at=None,
                    resolved_at=None, estimated_play_time=None, status_updated_at=requeue_now,
                )

        self.stdout.write(self.style.SUCCESS(
            f"Done. strong_evidence={strong_count} ambiguous={ambiguous_count} "
            f"requeued={requeued_count} old_untouched={old_count} "
            f"requeue_all_recent={requeue_all_recent}"
        ))
