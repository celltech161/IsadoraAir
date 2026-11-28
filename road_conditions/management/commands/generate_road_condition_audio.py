import os
from typing import NamedTuple

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
    ReportSuperseded, SynthesisError, hash_file_sha256, published_report_problem,
    read_provenance, recover_incomplete_publication, road_report_path, road_report_pending_path,
    synthesize_road_report,
)
from road_conditions import trigger
from road_conditions.voice import VoiceResolutionError, resolve_voice

# A separate advisory lock serializes generation, publication and recovery.
# The sync command can commit newer road data while a report is rendering.
GENERATE_ROAD_AUDIO_LOCK_KEY = (0x524F4144, 0x41554449)  # "ROAD"/"AUDI" as int32s, arbitrary

# A render whose content changed while it was being made is redone at once
# (the change's own trigger may have arrived while this run was busy), but
# only this many times in one run.
MAX_RENDER_PASSES = 2


class CurrentReport(NamedTuple):
    """What the report would say now, before any voice is chosen."""
    events: list
    event_scripts: list
    body: str
    config: RoadConditionsConfiguration
    transition_sound_path: str | None
    fingerprint: str


class Command(BaseCommand):
    help = (
        "Generate the single, consolidated KanDrive road-report audio from "
        "currently in-scope RoadEvent rows, when its content has changed. See road_conditions/"
        "report.py (event selection + text) and road_conditions/synthesis.py "
        "(shared-TTS synthesis + Track lifecycle) for the actual logic -- this "
        "command is a thin CLI wrapper plus the overlap guard, matching "
        "sync_road_conditions.py's own shape."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dispatched", action="store_true",
            help="Handle only pending work from the road synchronization service.",
        )
        parser.add_argument(
            "--recover-only", action="store_true",
            help="Settle a pending local publication without checking upstream freshness or synthesizing.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help=(
                "Preview everything a real run would do -- selected events, "
                "the final script, the resolved voice, the intended category "
                "and output file, and what existing asset (if any) would be "
                "replaced -- without writing anything: no TTS "
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
                "disabled (normally the command leaves the existing report as it "
                "is rather than rendering from data that can no longer be "
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
                "Renders if the published report is not already in this voice. "
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
            if options["dispatched"]:
                self._run_dispatched(options)
            else:
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

        if not dry_run and self._settle_incomplete_publication() == "unrestorable":
            raise CommandError("Road publication recovery is pending")
        if options["recover_only"]:
            return

        if text_only or dry_run:
            self._preview(options, now, text_only)
            return

        self._render_current_report(options)

    def _run_dispatched(self, options):
        mode = trigger.dispatch_mode()
        if mode not in ("generate", "recover"):
            return
        if road_report_pending_path().exists():
            if not trigger.recovery_due(claim=True):
                return
            if self._settle_incomplete_publication() == "unrestorable":
                raise CommandError("Road publication recovery is pending")
        if mode == "recover" or options["recover_only"]:
            return
        config = RoadConditionsConfiguration.load()
        if feed_freshness(config) != "fresh":
            return
        ticket = trigger.pending_sync(config)
        if ticket is None:
            return
        try:
            completed = self._render_current_report(options)
        except CommandError:
            # A real generation attempt failed. A future successful sync may
            # retry it. Pending publication recovery has its own retry budget.
            if feed_freshness() == "fresh":
                trigger.acknowledge_sync(ticket)
            raise
        if completed:
            trigger.acknowledge_sync(ticket)

    def _render_current_report(self, options):
        for render_pass in range(1, MAX_RENDER_PASSES + 1):
            try:
                return self._check_and_render(options, dj_timezone.now())
            except ReportSuperseded as exc:
                self.stdout.write(self.style.WARNING(
                    f"Discarded the new audio before publishing: {exc}."
                    + (" Checking again." if render_pass < MAX_RENDER_PASSES else
                       " Leaving it for the next successful road data retrieval.")
                ))

    def _settle_incomplete_publication(self):
        """Needs no road data and no speech synthesis, so it runs first."""
        try:
            outcome = recover_incomplete_publication()
        except Exception as exc:
            self.stderr.write(f"Could not settle the pending road publication: {exc}")
            outcome = "unrestorable"
        if not outcome:
            return
        message = {
            "completed": "Completed an earlier publication that had committed.",
            "restored": "Restored the previous road report: an earlier publication did not complete.",
            "discarded": "Removed unfinished audio from an earlier first publication.",
            "abandoned": "Dropped the record of an earlier unfinished publication: the audio on disk "
                         "has been replaced since, and is not being overwritten.",
            "unrestorable": "An earlier publication could not be settled. Recovery is still pending.",
        }[outcome]
        self.stdout.write(self.style.WARNING(message))
        if outcome != "completed":
            emit_event(
                category="road_conditions", level="warning" if outcome != "unrestorable" else "error",
                title="KanDrive publication did not complete", detail={"outcome": outcome},
                dedupe_key=f"road_conditions|kandrive-publication-{outcome}",
            )
        return outcome

    def _freshness_allows(self, options, *, preview=False):
        """False, having said so, when road data is not fresh. Nothing is
        changed in that case: not the audio, and not Ready-to-Air."""
        freshness = feed_freshness()
        if freshness == "fresh" or options["force"]:
            return True
        self.stdout.write(self.style.WARNING(
            f"Feed freshness is {freshness!r}, not 'fresh' -- skipping generation "
            "(a report must not be rendered from stale, unavailable or disabled source "
            "data). The last published report is left as it is: audio and Ready-to-Air "
            "unchanged. Use --force to generate anyway for manual verification."
        ))
        if preview:
            self.stdout.write("[DRY RUN] Would change nothing.")
        return False

    def _build_report(self, now, options, *, quiet=False):
        """Event selection, voice-neutral text and transition sound --
        everything that determines what the report says and how it is
        assembled. No voice is resolved here: an unchanged, verified
        report must not depend on voice configuration at all."""
        events = select_events(now)
        if options["verbose"] and not quiet:
            for event in events:
                self.stdout.write(
                    f"  {event.external_id} category={event.headline_category!r} "
                    f"priority={event.priority} route={event.primary_route!r}"
                )
        if events:
            event_scripts = build_event_scripts(events, now)
            body = " ".join(event_scripts)
        else:
            event_scripts = []
            body = build_no_events_message()

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
            problem = f"Transition sound enabled but not found/readable at {candidate!r} -- generating without it."
            if candidate and os.path.exists(candidate):
                # Hashed on every run the feature is active for -- see
                # synthesis.hash_file_sha256() on why that is cheap
                # enough not to need caching.
                try:
                    transition_sound_fingerprint = hash_file_sha256(candidate)
                    transition_sound_path = candidate
                except OSError as exc:
                    problem = f"Transition sound became unreadable ({exc}) -- generating without it."
            if not transition_sound_path and not quiet:
                self.stdout.write(self.style.WARNING(problem))
        # What this run would ACTUALLY do, not merely what is configured.
        transition_active = bool(transition_sound_path)

        fingerprint = compute_report_fingerprint(
            compose_semantic_script(body, config),
            transition_active,
            transition_sound_fingerprint=transition_sound_fingerprint,
            segment_count=(
                len(compose_report_segments(event_scripts, config, announcer_name=ANNOUNCER_PLACEHOLDER))
                if transition_active else None
            ),
        )
        return CurrentReport(events, event_scripts, body, config, transition_sound_path, fingerprint)

    def _build_report_or_fail(self, now, options):
        try:
            return self._build_report(now, options)
        except ReportBuildError as exc:
            emit_event(
                category="road_conditions", level="error", title="KanDrive report text generation failed",
                detail={"error": str(exc)}, dedupe_key="road_conditions|kandrive-report-build-failed",
            )
            raise CommandError(str(exc))

    def _render_reason(self, options, now, report):
        """Why audio must be rendered, or "" if the published report stands.
        A matching fingerprint is never enough: the published audio must
        verify too. Ready-to-Air plays no part."""
        if options["regenerate"]:
            return "operator_regenerate"
        problem = published_report_problem(report.fingerprint)
        if problem in ("publication_incomplete", "provenance_missing", "provenance_invalid"):
            return problem
        existing = report.config.last_report_fingerprint
        if existing != report.fingerprint:
            return "content_changed" if existing else "no_previous_report"
        if problem:
            return problem
        # An explicit --voice asks for the report in that voice, as it
        # always has; the scheduled voice never does.
        if options["voice"]:
            slot, voice, text = self._compose_for_voice(options, now, report.body, report.config)
            if not self._published_in_voice(slot, voice, text):
                return "explicit_voice"
        return ""

    @staticmethod
    def _published_in_voice(slot, voice, text):
        record, _problem = read_provenance()
        try:
            return (record["script"] == text and record["announcer"]["slot"] == slot
                    and record["voice"]["engine"] == voice.get("engine")
                    and record["voice"]["provider_voice"] == voice.get("model"))
        except (KeyError, TypeError):
            return False

    def _preview(self, options, now, text_only):
        """--dry-run / --text-only: reads only."""
        if not self._freshness_allows(options, preview=True):
            return
        report = self._build_report_or_fail(now, options)
        reason = self._render_reason(options, now, report)
        slot, voice, text = self._compose_for_voice(options, now, report.body, report.config)
        if text_only:
            self.stdout.write(text)
            return
        final_path = road_report_path()
        self.stdout.write(f"[DRY RUN] Selected {len(report.events)} event(s).")
        self.stdout.write(f"[DRY RUN] Voice: slot={slot!r} model={voice['model']!r} name={voice['name']!r}")
        self.stdout.write("[DRY RUN] Intended category: KanDrive")
        self.stdout.write(f"[DRY RUN] Intended output file: {final_path}")
        if report.transition_sound_path:
            count = len(compose_report_segments(report.event_scripts, report.config, announcer_name=voice["name"]))
            self.stdout.write(
                f"[DRY RUN] Transition sound: {report.transition_sound_path} "
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
        self.stdout.write(f"[DRY RUN] Report fingerprint: {report.fingerprint}")
        self.stdout.write(
            f"[DRY RUN] Existing successful fingerprint: {report.config.last_report_fingerprint or '(none)'}")
        self.stdout.write(f"[DRY RUN] Would skip generation: {'no' if reason else 'yes'}")
        if reason:
            self.stdout.write(f"[DRY RUN] Render reason: {reason}")
        self.stdout.write("[DRY RUN] Final script:")
        self.stdout.write(text)

    def _check_and_render(self, options, now):
        if not self._freshness_allows(options):
            return False
        report = self._build_report_or_fail(now, options)
        reason = self._render_reason(options, now, report)
        if not reason:
            self.stdout.write(
                "KanDrive report unchanged since last successful generation; "
                f"skipping synthesis and analysis. [fingerprint {report.fingerprint[:12]}]"
            )
            return True

        config = report.config
        self.stdout.write(
            f"KanDrive render needed: reason={reason} fingerprint={report.fingerprint[:12]} "
            f"previous={config.last_report_fingerprint[:12] or 'none'}"
        )

        # Voice resolved only now that audio is really going to be made.
        # Its name is spoken, so it is resolved before final composition.
        slot, voice, text = self._compose_for_voice(options, now, report.body, config)
        segments = (
            compose_report_segments(report.event_scripts, config, announcer_name=self._announcer_name(voice))
            if report.transition_sound_path else None
        )

        def superseded():
            """Re-reads the authoritative data just before publication."""
            if not options["force"] and feed_freshness() != "fresh":
                return "the road data is no longer fresh"
            try:
                current = self._build_report(dj_timezone.now(), options, quiet=True)
            except ReportBuildError:
                return "the report can no longer be built"
            if current.fingerprint != report.fingerprint:
                return "the report content changed while it was being rendered"
            return ""

        try:
            # Publishes the audio, Track, waveform, road_report.txt and
            # provenance together; see synthesis._publish_report().
            track = synthesize_road_report(
                text, slot, voice, segments=segments,
                transition_sound_path=report.transition_sound_path,
                semantic_fingerprint=report.fingerprint, superseded=superseded,
            )
        except ReportSuperseded:
            raise
        except SynthesisError as exc:
            emit_event(
                category="road_conditions", level="error", title="KanDrive audio generation failed",
                detail={"error": str(exc)}, dedupe_key="road_conditions|kandrive-synthesis-failed",
            )
            raise CommandError(str(exc))

        self.stdout.write(
            f"Generated KanDrive report: track id={track.id}, {len(report.events)} event(s), "
            f"voice={voice['name']} ({slot}), duration={track.duration_seconds:.1f}s."
        )
        return True

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
