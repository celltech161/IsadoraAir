import os

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone as dj_timezone

from library.models import Track
from monitoring.models import emit_event
from road_conditions.models import RoadConditionsConfiguration, RoadEvent
from road_conditions.report import (
    ReportBuildError, build_event_script, build_event_scripts,
    build_no_events_message, compose_report_script, compose_report_segments,
    feed_freshness, select_events,
)
from road_conditions.synthesis import (
    SynthesisError, retire_road_report, road_report_path, synthesize_road_report,
    write_road_report_text,
)
from road_conditions.voice import VoiceResolutionError, resolve_voice

# Arbitrary fixed two-integer pair for a command-wide Postgres advisory
# lock -- same idiom as sync_road_conditions.py's ROAD_CONDITIONS_LOCK_KEY
# and generate_dedication_intros.py's DEDICATION_LOCK_KEY. This command
# and sync_road_conditions.py intentionally do NOT share a lock key --
# they touch different data (RoadEvent rows vs. the KanDrive Track/file)
# and are meant to run on entirely different, decoupled schedules (see
# report.py and this command's own help text on "not tied to the 15-
# minute CARS polling cadence"), so there's no reason a sync in progress
# should block a generation run or vice versa. An overlapping firing of
# THIS command with itself just exits immediately.
GENERATE_ROAD_AUDIO_LOCK_KEY = (0x524F4144, 0x41554449)  # "ROAD"/"AUDI" as int32s, arbitrary


class Command(BaseCommand):
    help = (
        "Generate (or retire) the single, consolidated KanDrive road-report "
        "audio from currently in-scope RoadEvent rows. See road_conditions/"
        "report.py (event selection + text) and road_conditions/synthesis.py "
        "(Kokoro synthesis + Track lifecycle) for the actual logic -- this "
        "command is a thin CLI wrapper plus the overlap guard, matching "
        "sync_road_conditions.py's own shape."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help=(
                "Preview everything a real run would do -- selected events, "
                "the final script, the resolved voice, the intended category "
                "and output file, and what existing asset (if any) would be "
                "replaced or retired -- without writing anything: no Kokoro "
                "call, no DB write, no file write."
            ),
        )
        parser.add_argument(
            "--text-only", action="store_true",
            help=(
                "Print only the final composed report text (or the no-events "
                "message) for factual/stylistic review -- no voice, category, "
                "or filename detail. Implies --dry-run (never writes anything)."
            ),
        )
        parser.add_argument(
            "--force", action="store_true",
            help=(
                "Generate even though the source feed is stale, failed, or "
                "disabled (normally the command retires the existing report "
                "instead of regenerating from data that can no longer be "
                "trusted as current -- see feed_freshness() in report.py). "
                "For manual verification only; a real generation cycle "
                "should never need this."
            ),
        )
        parser.add_argument(
            "--voice", default=None,
            help=(
                "Override the weather-schedule-resolved voice slot (e.g. "
                "'day' or 'night') for this run only. Development/testing "
                "use -- production omits this so KanDrive automatically uses "
                "whichever voice weather's own schedule says for the moment "
                "generation actually runs. See road_conditions/voice.py."
            ),
        )
        parser.add_argument(
            "--event-id", default=None, dest="event_id",
            help=(
                "Preview just this one RoadEvent's own script (by its "
                "external_id, e.g. CARS5-12418) instead of running the full "
                "selection. Must be combined with --dry-run or --text-only -- "
                "KanDrive is always one consolidated report; this option "
                "never synthesizes audio for a single event on its own, "
                "which would silently produce a misleadingly partial report."
            ),
        )
        parser.add_argument(
            "--verbose", action="store_true",
            help="Print each selected event's id, category, priority, and route in addition to the summary.",
        )

    def handle(self, *args, **options):
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s, %s)", GENERATE_ROAD_AUDIO_LOCK_KEY)
            acquired = cur.fetchone()[0]
        if not acquired:
            self.stdout.write("Another generate_road_condition_audio is already running; exiting.")
            return
        try:
            self._run(options)
        finally:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", GENERATE_ROAD_AUDIO_LOCK_KEY)

    def _run(self, options):
        text_only = options["text_only"]
        dry_run = options["dry_run"] or text_only
        event_id = options["event_id"]
        now = dj_timezone.now()

        if event_id:
            if not dry_run:
                raise CommandError(
                    "--event-id must be combined with --dry-run or --text-only -- "
                    "KanDrive never synthesizes audio for a single event alone."
                )
            try:
                event = RoadEvent.objects.get(external_id=event_id)
            except RoadEvent.DoesNotExist:
                raise CommandError(f"No RoadEvent with external_id={event_id!r}.")
            self.stdout.write(build_event_script(event, now))
            return

        freshness = feed_freshness()
        if freshness != "fresh" and not options["force"]:
            self.stdout.write(self.style.WARNING(
                f"Feed freshness is {freshness!r}, not 'fresh' -- skipping generation "
                "(a report built from stale/unavailable/disabled source data must not "
                "keep airing as current). Use --force to generate anyway for manual "
                "verification."
            ))
            if dry_run:
                self.stdout.write("[DRY RUN] Would retire the existing KanDrive report, if any -- not regenerate.")
                return
            if retire_road_report():
                self.stdout.write("Retired the existing KanDrive report (pulled out of rotation eligibility).")
                emit_event(
                    category="road_conditions", level="warning", title="KanDrive report retired",
                    detail={"freshness": freshness},
                    dedupe_key=f"road_conditions|kandrive-retired|{freshness}",
                )
            else:
                self.stdout.write("No existing KanDrive report to retire.")
            return

        events = select_events(now)
        if options["verbose"]:
            for event in events:
                self.stdout.write(
                    f"  {event.external_id} category={event.headline_category!r} "
                    f"priority={event.priority} route={event.primary_route!r}"
                )

        try:
            if events:
                event_scripts = build_event_scripts(events, now)
                body = " ".join(event_scripts)
            else:
                event_scripts = []
                body = build_no_events_message()
        except ReportBuildError as exc:
            emit_event(
                category="road_conditions", level="error", title="KanDrive report text generation failed",
                detail={"error": str(exc)}, dedupe_key="road_conditions|kandrive-report-build-failed",
            )
            raise CommandError(str(exc))

        # Voice resolved BEFORE final composition (not after, as an
        # earlier revision of this command did) -- the final script can
        # now contain {announcer_name}, so --text-only needs the real
        # voice to print the exact script that would actually be spoken,
        # not just the un-framed report body. This does mean --text-only
        # now depends on voice resolution succeeding too, which is
        # correct: a preview that can't resolve the real voice can't
        # show the real final script either.
        try:
            slot, voice = resolve_voice(slot_override=options["voice"], now=now)
        except VoiceResolutionError as exc:
            raise CommandError(f"Voice resolution failed: {exc}")

        config = RoadConditionsConfiguration.load()
        # Full on-air name ("Claira Sky", not just "Claira") -- matches
        # how weather's own scripts identify the announcer (wx_forecast.py
        # splices in voice['signoff'], e.g. "I'm Claira Sky."), rather
        # than the short voice['name'] used for internal bookkeeping
        # (ID3 titles, log messages -- see this command's own success
        # message below, and voice.py's docstring on why `name` exists).
        # Falls back to the short name if a voice slot is ever missing
        # full_name (e.g. a future third slot not yet updated in
        # weather-ingest's lib/voices.py) rather than raising.
        announcer_name = voice.get("full_name") or voice["name"]
        text = compose_report_script(body, config, announcer_name=announcer_name)

        if text_only:
            self.stdout.write(text)
            return

        # Item transition sound -- only meaningful with 2+ events (no
        # boundary to insert one at with 0 or 1). A missing/unreadable
        # file degrades gracefully to plain synthesis (same shape as
        # the KNS show ingest script's own woosh handling) rather than
        # failing the whole cycle; a present-but-corrupt file instead
        # surfaces later as an ordinary SynthesisError from ffmpeg,
        # same as any other synthesis failure.
        segments = None
        transition_sound_path = None
        if len(events) >= 2 and config.transition_sound_enabled:
            candidate = (config.transition_sound_path or "").strip()
            if candidate and os.path.exists(candidate):
                transition_sound_path = candidate
                segments = compose_report_segments(event_scripts, config, announcer_name=announcer_name)
            else:
                self.stdout.write(self.style.WARNING(
                    f"Transition sound enabled but not found/readable at {candidate!r} -- "
                    "generating without it."
                ))

        final_path = road_report_path()

        if dry_run:
            self.stdout.write(f"[DRY RUN] Selected {len(events)} event(s).")
            self.stdout.write(f"[DRY RUN] Voice: slot={slot!r} model={voice['model']!r} name={voice['name']!r}")
            self.stdout.write("[DRY RUN] Intended category: KanDrive")
            self.stdout.write(f"[DRY RUN] Intended output file: {final_path}")
            if transition_sound_path:
                self.stdout.write(
                    f"[DRY RUN] Transition sound: {transition_sound_path} "
                    f"(between {len(segments)} items, {len(segments) - 1} insertion(s))"
                )
            existing = Track.objects.filter(filepath=str(final_path)).first()
            if existing:
                self.stdout.write(
                    f"[DRY RUN] Would replace existing Track id={existing.id} "
                    f"(currently ready2air={existing.ready2air})."
                )
            else:
                self.stdout.write("[DRY RUN] No existing KanDrive Track -- would create a new one.")
            self.stdout.write("[DRY RUN] Final script:")
            self.stdout.write(text)
            return

        try:
            track = synthesize_road_report(
                text, slot, voice, segments=segments, transition_sound_path=transition_sound_path,
            )
        except SynthesisError as exc:
            emit_event(
                category="road_conditions", level="error", title="KanDrive audio generation failed",
                detail={"error": str(exc)}, dedupe_key="road_conditions|kandrive-synthesis-failed",
            )
            raise CommandError(str(exc))

        # road_report.txt is written HERE, only now that
        # synthesize_road_report() has returned a Track without raising
        # -- i.e. the FLAC was written, the Track row was created/
        # updated, AND waveform analysis succeeded. This is deliberately
        # NOT inside synthesize_road_report() itself (see that
        # function's own docstring): writing it there, right after the
        # FLAC's own os.replace(), meant a LATER failure in that same
        # function (the Track DB write, or analysis) could still raise
        # SynthesisError while road_report.txt had already been
        # overwritten -- leaving the text file describing a generation
        # cycle that never actually completed successfully. Calling it
        # only here means the text file always describes a cycle that
        # completed successfully start to finish, matching the intended
        # "represents the last generation that completed successfully"
        # semantics exactly.
        try:
            write_road_report_text(text)
        except OSError as exc:
            raise CommandError(
                f"KanDrive audio generated successfully (track id={track.id}) but failed to "
                f"write the companion road_report.txt: {exc}"
            )

        self.stdout.write(
            f"Generated KanDrive report: track id={track.id}, {len(events)} event(s), "
            f"voice={voice['name']} ({slot}), duration={track.duration_seconds:.1f}s."
        )
