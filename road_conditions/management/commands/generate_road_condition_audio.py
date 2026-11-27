import os

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone as dj_timezone

from library.models import Track
from monitoring.models import emit_event
from road_conditions.models import RoadConditionsConfiguration, RoadEvent
from road_conditions.report import (
    ANNOUNCER_PLACEHOLDER, ReportBuildError, build_event_script, build_event_scripts,
    build_no_events_message, compose_report_script, compose_report_segments,
    compose_semantic_script, compute_report_fingerprint, feed_freshness, select_events,
)
from road_conditions.synthesis import (
    HELD, READY, RETIRED_BY_FRESHNESS, SynthesisError, discard_retirement_record,
    hash_file_sha256, inspect_published_report, rearm_retired_report,
    recover_interrupted_publication, retire_road_report, road_report_path,
    synthesize_road_report,
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
        "(shared-TTS synthesis + Track lifecycle) for the actual logic -- this "
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
                "replaced or retired -- without writing anything: no TTS "
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
            "--regenerate", action="store_true",
            help=(
                "Rebuild the report, in the currently scheduled voice, even though its "
                "content fingerprint matches RoadConditionsConfiguration."
                "last_report_fingerprint and the published audio verifies -- i.e. "
                "bypass ONLY the fingerprint-based dedup skip (see report.py's "
                "compute_report_fingerprint()). Independent of --force: this does "
                "NOT bypass the stale/failed/disabled freshness gate below -- combine "
                "with --force for that. Normal production runs omit this; unchanged-"
                "report skipping is the default, intended behavior."
            ),
        )
        parser.add_argument(
            "--voice", default=None,
            help=(
                "Override the weather-schedule-resolved persona slot key for "
                "this run only. Any configured Weather Voice Persona is accepted. "
                "Development/testing use -- production omits this so KanDrive "
                "automatically uses whichever persona weather's schedule says when "
                "generation runs. See road_conditions/voice.py."
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

        config = RoadConditionsConfiguration.load()

        # Item transition sound -- only meaningful with 2+ events (no
        # boundary to insert one at with 0 or 1). A missing/unreadable
        # file degrades gracefully to plain synthesis (same shape as
        # the KNS show ingest script's own woosh handling) rather than
        # failing the whole cycle; a present-but-corrupt file instead
        # surfaces later as an ordinary SynthesisError from ffmpeg,
        # same as any other synthesis failure.
        transition_sound_path = None
        transition_sound_fingerprint = None
        if len(events) >= 2 and config.transition_sound_enabled:
            candidate = (config.transition_sound_path or "").strip()
            if candidate and os.path.exists(candidate):
                # Hashed on every run the feature is active for -- see
                # synthesis.hash_file_sha256() on why that is cheap
                # enough not to need caching. A file that vanishes or
                # cannot be read degrades like a missing one.
                try:
                    transition_sound_fingerprint = hash_file_sha256(candidate)
                    transition_sound_path = candidate
                except OSError as exc:
                    self.stdout.write(self.style.WARNING(
                        f"Transition sound became unreadable ({exc}) -- generating without it."
                    ))
            else:
                self.stdout.write(self.style.WARNING(
                    f"Transition sound enabled but not found/readable at {candidate!r} -- "
                    "generating without it."
                ))
        # What this run would ACTUALLY do, not merely what is configured.
        transition_active = bool(transition_sound_path)

        # The content identity: voice-neutral, computed after event
        # selection, text composition and transition-sound resolution --
        # everything that determines what the report says and how it is
        # assembled. No voice is resolved yet: an unchanged, verified
        # report must not depend on voice configuration at all.
        fingerprint = compute_report_fingerprint(
            compose_semantic_script(body, config),
            transition_active,
            transition_sound_fingerprint=transition_sound_fingerprint,
            segment_count=(
                len(compose_report_segments(event_scripts, config, announcer_name=ANNOUNCER_PLACEHOLDER))
                if transition_active else None
            ),
        )

        existing_fingerprint = config.last_report_fingerprint

        if not dry_run and recover_interrupted_publication():
            self.stdout.write(self.style.WARNING(
                "Restored the previous report audio: an earlier publication was interrupted "
                "before the Track was updated."
            ))
        # Verified against the published audio's own provenance. A
        # matching fingerprint alone is NEVER sufficient to skip.
        state = inspect_published_report(fingerprint)
        unchanged = (
            not options["regenerate"]
            and bool(existing_fingerprint) and existing_fingerprint == fingerprint
            and not state.problem
        )
        would_skip = unchanged and state.readiness in (READY, HELD)
        would_rearm = unchanged and state.readiness == RETIRED_BY_FRESHNESS and freshness == "fresh"
        reason = self._render_reason(options, state, existing_fingerprint, fingerprint)

        if text_only:
            _slot, _voice, text = self._compose_for_voice(options, now, body, config)
            self.stdout.write(text)
            return

        if dry_run:
            slot, voice, text = self._compose_for_voice(options, now, body, config)
            final_path = road_report_path()
            self.stdout.write(f"[DRY RUN] Selected {len(events)} event(s).")
            self.stdout.write(f"[DRY RUN] Voice: slot={slot!r} model={voice['model']!r} name={voice['name']!r}")
            self.stdout.write("[DRY RUN] Intended category: KanDrive")
            self.stdout.write(f"[DRY RUN] Intended output file: {final_path}")
            if transition_sound_path:
                count = len(compose_report_segments(event_scripts, config, announcer_name=voice["name"]))
                self.stdout.write(
                    f"[DRY RUN] Transition sound: {transition_sound_path} "
                    f"(between {count} items, {count - 1} insertion(s))"
                )
            existing = Track.objects.filter(filepath=str(final_path)).first()
            if existing:
                self.stdout.write(
                    f"[DRY RUN] Would replace existing Track id={existing.id} "
                    f"(currently ready2air={existing.ready2air})."
                )
            else:
                self.stdout.write("[DRY RUN] No existing KanDrive Track -- would create a new one.")
            self.stdout.write(f"[DRY RUN] Report fingerprint: {fingerprint}")
            self.stdout.write(f"[DRY RUN] Existing successful fingerprint: {existing_fingerprint or '(none)'}")
            self.stdout.write(f"[DRY RUN] Would skip generation: {'yes' if would_skip or would_rearm else 'no'}")
            if would_rearm:
                self.stdout.write("[DRY RUN] Would re-arm the retired report without rendering.")
            elif not would_skip:
                self.stdout.write(f"[DRY RUN] Render reason: {reason}")
            self.stdout.write("[DRY RUN] Final script:")
            self.stdout.write(text)
            return

        if would_skip:
            if state.readiness == READY:
                discard_retirement_record()
                self.stdout.write(
                    "KanDrive report unchanged since last successful generation; "
                    f"skipping synthesis and analysis. [fingerprint {fingerprint[:12]}]"
                )
            else:
                self.stdout.write(
                    "KanDrive report unchanged since last successful generation but held off air "
                    "(ready2air was cleared outside road-data freshness); leaving it held, "
                    f"skipping synthesis. [fingerprint {fingerprint[:12]}]"
                )
            return

        if would_rearm:
            if rearm_retired_report(fingerprint):
                self.stdout.write(
                    "KanDrive report re-armed without synthesis: road data is fresh again, content "
                    "is unchanged and the published audio verifies. "
                    f"[fingerprint {fingerprint[:12]}]"
                )
                return
            reason = "retired_report_not_rearmable"

        self.stdout.write(
            f"KanDrive render needed: reason={reason} fingerprint={fingerprint[:12]} "
            f"previous={existing_fingerprint[:12] or 'none'}"
        )

        # Voice resolved only now that audio is really going to be made.
        # Its name is spoken, so it is resolved before final composition.
        slot, voice, text = self._compose_for_voice(options, now, body, config)
        segments = (
            compose_report_segments(event_scripts, config, announcer_name=self._announcer_name(voice))
            if transition_active else None
        )

        try:
            # Publishes the audio, Track, waveform, road_report.txt and
            # provenance together; see synthesis._publish_report().
            track = synthesize_road_report(
                text, slot, voice, segments=segments, transition_sound_path=transition_sound_path,
                semantic_fingerprint=fingerprint,
            )
        except SynthesisError as exc:
            emit_event(
                category="road_conditions", level="error", title="KanDrive audio generation failed",
                detail={"error": str(exc)}, dedupe_key="road_conditions|kandrive-synthesis-failed",
            )
            raise CommandError(str(exc))

        # Fingerprint/timestamp persisted ONLY here, after the whole
        # publication has succeeded, so they can never come to describe a
        # cycle that did not complete.
        config.last_report_fingerprint = fingerprint
        config.last_report_generated_at = dj_timezone.now()
        try:
            config.save(update_fields=["last_report_fingerprint", "last_report_generated_at"])
        except Exception as exc:
            # The audio, text and provenance ARE correct and live at this
            # point -- only the change-detection bookkeeping for the NEXT
            # run failed to persist. Surfaced as a real CommandError
            # rather than claiming full success; the worst consequence is
            # one unnecessary regeneration, never wrong on-air audio.
            raise CommandError(
                f"KanDrive audio generated successfully (track id={track.id}) and "
                f"published, but failed to persist the generation fingerprint: {exc}"
            )

        if not track.ready2air:
            self.stdout.write(self.style.WARNING(
                "The new KanDrive report was published but left held off air: ready2air had been "
                "cleared outside road-data freshness."
            ))
            emit_event(
                category="road_conditions", level="warning",
                title="New KanDrive report published but held off air",
                detail={"track_id": track.id}, dedupe_key="road_conditions|kandrive-held",
            )
        self.stdout.write(
            f"Generated KanDrive report: track id={track.id}, {len(events)} event(s), "
            f"voice={voice['name']} ({slot}), duration={track.duration_seconds:.1f}s."
        )

    @staticmethod
    def _render_reason(options, state, existing_fingerprint, fingerprint):
        """Why a render is needed, for the journal."""
        if options["regenerate"]:
            return "operator_regenerate"
        if state.problem in ("provenance_missing", "provenance_invalid"):
            return state.problem
        if existing_fingerprint != fingerprint:
            return "content_changed" if existing_fingerprint else "no_previous_report"
        if state.problem:
            return state.problem
        return f"readiness_{state.readiness}"

    @staticmethod
    def _announcer_name(voice):
        # Full on-air name ("Claira Sky", not just "Claira") -- matches
        # how weather's own scripts identify the announcer (wx_forecast.py
        # splices in voice['signoff'], e.g. "I'm Claira Sky."), rather
        # than the short voice['name'] used for internal bookkeeping.
        # Falls back to the short name if a voice slot is ever missing
        # full_name.
        return voice.get("full_name") or voice["name"]

    def _compose_for_voice(self, options, now, body, config):
        try:
            slot, voice = resolve_voice(slot_override=options["voice"], now=now)
        except VoiceResolutionError as exc:
            raise CommandError(f"Voice resolution failed: {exc}")
        text = compose_report_script(body, config, announcer_name=self._announcer_name(voice))
        return slot, voice, text
