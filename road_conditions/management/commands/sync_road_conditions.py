import math
from datetime import datetime, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from monitoring.models import emit_event
from road_conditions.alerts import evaluate_feed
from road_conditions.api import CarsApiError
from road_conditions.models import RoadConditionsConfiguration
from road_conditions.services import sync_events
from road_conditions.trigger import request_generation

# Arbitrary fixed two-integer pair for a command-wide Postgres advisory
# lock -- same idiom as generate_dedication_intros.py's DEDICATION_LOCK_KEY
# (see webrequests/management/commands/generate_dedication_intros.py) --
# an overlapping firing (a slow ~8MB fetch still in flight when the next
# timer tick fires) just exits immediately rather than running two syncs
# against the same RoadEvent rows concurrently.
ROAD_CONDITIONS_LOCK_KEY = (0x524F4144, 0x434F4E44)  # "ROAD"/"COND" as int32s, arbitrary


# Retrieval slots are anchored to the clock, this many minutes past the hour,
# so they stay clear of the top-of-hour forecast renders and the
# quarter-hour temperature renders instead of drifting across them. The
# spacing is still poll_cadence_minutes. With 30 that is :07 and :37; in
# general the minutes are 7 modulo gcd(cadence, 60), so only a cadence
# sharing no factor with 60 ever reaches every minute.
POLL_ANCHOR_MINUTE = 7


def _slot_start(now, cadence_minutes):
    """The start of the retrieval slot `now` falls in."""
    period = max(1, cadence_minutes) * 60
    anchor = POLL_ANCHOR_MINUTE * 60
    start = math.floor((now.timestamp() - anchor) / period) * period + anchor
    return datetime.fromtimestamp(start, tz=dt_timezone.utc)


def _due(config, now=None):
    """(due, next_slot_start). Due once in each slot: when nothing has been
    attempted since the slot began (immediately after downtime, or if there
    has never been an attempt) -- the application-level check the systemd
    timer itself does NOT perform (see the timer file's own comment: it
    fires far more often than the real cadence on purpose, so a
    poll_cadence_minutes edit here takes effect within about a minute, with
    no timer file edit or systemd reload). The retrieval starts on the
    first tick after the slot begins."""
    now = now or timezone.now()
    start = _slot_start(now, config.poll_cadence_minutes)
    next_start = datetime.fromtimestamp(
        start.timestamp() + max(1, config.poll_cadence_minutes) * 60, tz=dt_timezone.utc)
    last = config.last_fetch_attempted_at
    return last is None or last < start, next_start


class Command(BaseCommand):
    """Fetches KDOT CARS road events and upserts them into RoadEvent.
    Safe to run manually at any time -- --dry-run reports what would
    happen without writing anything. See road_conditions/services.py
    for the actual fetch/filter/upsert logic; this command is a thin
    CLI wrapper plus the overlap guard."""

    help = "Sync road/construction events from the Kansas DOT CARS API into RoadEvent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be created/updated/deactivated without writing anything.",
        )
        parser.add_argument(
            "--force-full", action="store_true",
            help=(
                "Run even if Road Conditions Configuration is disabled, "
                "AND even if the last sync was too recent per "
                "poll_cadence_minutes. Useful for a one-off connectivity/"
                "credentials check, or manual verification after an admin "
                "change. (There is no incremental/partial fetch mode to "
                "override -- the CARS API always returns its complete "
                "current event set, so this does not change what's "
                "fetched, only whether the disabled/not-yet-due guards "
                "are honored.)"
            ),
        )
        parser.add_argument(
            "--verbose", action="store_true",
            help="Print each kept event's id, classification, and route in addition to the summary.",
        )
        parser.add_argument(
            "--event-type", action="append", default=None, dest="event_type",
            help=(
                "Restrict to this CARS API eventClassification (repeatable) "
                "-- NOT the same thing as an event's own headline_category. "
                "Overrides Road Conditions Configuration's own list for "
                "this run only. Real values: truckersReports, "
                "roadReports, winterDriving, weatherWarningsAreaEvents, "
                "constructionReports. Using this flag marks the run "
                "'narrowed' -- it will never deactivate stale events or "
                "change any existing row's in_scope, since it deliberately "
                "only looked at part of the feed."
            ),
        )
        parser.add_argument(
            "--county", default=None,
            help=(
                "Restrict to this one county (exact match against the "
                "event's 'counties' list, e.g. 'Ottawa'). Overrides Road "
                "Conditions Configuration's county list for this run "
                "only. Also marks the run 'narrowed' -- see --event-type."
            ),
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help=(
                "Only consider the first N fetched events (post-fetch, "
                "pre-filter -- there is no server-side limit/pagination "
                "to request instead). For development/troubleshooting. "
                "Also marks the run 'narrowed' -- see --event-type. "
                "Deactivation and in_scope changes are disabled for this "
                "entire run, not just for events past the limit -- a "
                "truncated fetch can't prove ANY event outside its own "
                "first N is still (or is no longer) in the complete "
                "source, so nothing gets deactivated or re-scoped."
            ),
        )

    def handle(self, *args, **options):
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s, %s)", ROAD_CONDITIONS_LOCK_KEY)
            acquired = cur.fetchone()[0]
        if not acquired:
            self.stdout.write("Another sync_road_conditions is already running; exiting.")
            return
        try:
            try:
                self._run(options)
            finally:
                # Every real invocation, including "not due yet" and failed
                # ones: this is what notices the data going stale.
                if not options["dry_run"]:
                    evaluate_feed()
        finally:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", ROAD_CONDITIONS_LOCK_KEY)

    def _run(self, options):
        config = RoadConditionsConfiguration.load()
        if not config.enabled and not options["force_full"]:
            self.stdout.write("Road Conditions Configuration is disabled; nothing to do. (Use --force-full to run anyway.)")
            return

        if not options["force_full"]:
            due, next_start = _due(config)
            if not due:
                self.stdout.write(
                    f"Not due yet -- next retrieval slot {timezone.localtime(next_start):%H:%M}, "
                    f"poll_cadence_minutes is {config.poll_cadence_minutes}. "
                    "(Use --force-full to run anyway.)"
                )
                return
        # Deliberately nothing written to RoadConditionsSyncRun/
        # RoadConditionsConfiguration.last_fetch_* for a disabled- or
        # not-due skip, matching each other -- sync_events() is simply
        # never called, so there's no misleading "failed" or even
        # "skipped" run in the history for a case where nothing was
        # actually attempted. The systemd timer fires far more often
        # than poll_cadence_minutes (see deploy/isadoraair-sync-road-
        # conditions.timer) specifically so a cadence change here takes
        # effect within about a minute without needing a timer edit/
        # systemd reload -- same idiom as OGRemoteConfig.poll_interval_minutes
        # (see ogremote-poll.timer).

        dry_run = options["dry_run"]
        narrowed = bool(options["event_type"] or options["county"] or options["limit"] is not None)
        if narrowed:
            self.stdout.write(self.style.WARNING(
                "Narrowed run (--event-type/--county/--limit given): the source set this run "
                "sees is intentionally incomplete. Deactivation and in_scope re-scoping are "
                "both disabled for the whole run -- no existing RoadEvent will be marked gone "
                "or out of coverage based on this run's results."
            ))

        try:
            result = sync_events(
                config,
                dry_run=dry_run,
                event_classifications_filter=options["event_type"],
                county_filter=options["county"],
                limit=options["limit"],
            )
        except CarsApiError as exc:
            raise CommandError(f"Road conditions sync failed: {exc}")

        if options["verbose"] and result["relevant_count"]:
            self.stdout.write(f"(kept {result['relevant_count']} of {result['fetched_count']} fetched events -- see RoadEvent admin for details)")

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            f"{prefix}Fetched: {result['fetched_count']}\n"
            f"{prefix}Relevant: {result['relevant_count']}\n"
            f"{prefix}Created: {result['created_count']}\n"
            f"{prefix}Updated: {result['updated_count']}\n"
            f"{prefix}Unchanged: {result['unchanged_count']}\n"
            f"{prefix}Deactivated: {result['deactivated_count']}\n"
            f"{prefix}Errors: {result['error_count']}"
        )
        if result["outcome"] == "partial":
            self.stdout.write(self.style.WARNING(
                f"Partial sync: {result['error_count']} record(s) failed to parse and were skipped "
                "(the fetch itself succeeded -- this is not a total failure)."
            ))

        # Reached only after a real retrieval has been committed. Never for
        # a skipped, failed, dry or narrowed run. The generator runs
        # separately under systemd, so this returns at once and its outcome
        # cannot change this run's result.
        if not dry_run and not narrowed:
            if request_generation():
                self.stdout.write("Requested a road audio generation check.")
            else:
                self.stdout.write(self.style.WARNING(
                    "The sync succeeded, but the road audio generation check could not be requested."
                ))
                emit_event(
                    category="road_conditions", level="warning",
                    title="Road audio generation could not be requested",
                    detail={}, dedupe_key="road_conditions|generation-request-failed",
                )
