"""generate_road_condition_audio management command tests -- CLI wiring,
output format, exit codes, the advisory-lock overlap guard, and the
freshness gate's generate-vs-retire branching. report.select_events()/
build_event_scripts()/synthesis.synthesize_road_report()/retire_road_report()
are exercised through light mocking at the command's own import points
(their own real behavior is covered exhaustively in
test_kandrive_report.py and test_kandrive_synthesis.py) -- no live
Kokoro or CARS API calls anywhere here."""
import tempfile
from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

import psycopg2
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone as dj_timezone

from library.models import Track
from road_conditions.management.commands.generate_road_condition_audio import (
    GENERATE_ROAD_AUDIO_LOCK_KEY,
)
from road_conditions.management.commands.sync_road_conditions import ROAD_CONDITIONS_LOCK_KEY
from road_conditions.models import RoadConditionsConfiguration, RoadEvent
from road_conditions.report import ReportBuildError
from road_conditions.synthesis import SynthesisError, road_report_path, road_report_text_path
from road_conditions.tests.test_kandrive_report import make_road_event
from road_conditions.tests.test_kandrive_synthesis import KanDriveSynthesisFixtureMixin

CMD = "road_conditions.management.commands.generate_road_condition_audio"


def fake_track(id=999, duration_seconds=180.0):
    """synthesize_road_report always returns a real Track on success
    (or raises) -- a stand-in with just the attributes the command's
    own success-message formatting touches (.id, .duration_seconds)."""
    track = MagicMock()
    track.id = id
    track.duration_seconds = duration_seconds
    return track


def make_fresh_config():
    config = RoadConditionsConfiguration.load()
    config.enabled = True
    config.last_error = ""
    config.last_fetch_succeeded_at = dj_timezone.now()
    config.stale_data_threshold_minutes = 60
    config.save()
    return config


class LockKeyIsolationTests(TestCase):
    def test_generate_audio_lock_key_differs_from_sync_lock_key(self):
        """The two commands must never contend for the same lock -- a
        sync in progress must not block generation, and vice versa
        (they touch different data and run on different schedules)."""
        self.assertNotEqual(GENERATE_ROAD_AUDIO_LOCK_KEY, ROAD_CONDITIONS_LOCK_KEY)


class AdvisoryLockOverlapTests(TestCase):
    def test_exits_quietly_when_another_instance_holds_the_lock(self):
        params = connections["default"].get_connection_params()
        blocking_conn = psycopg2.connect(**params)
        try:
            with blocking_conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s, %s)", GENERATE_ROAD_AUDIO_LOCK_KEY)
                self.assertTrue(cur.fetchone()[0], "test setup failed to acquire its own lock")

            with patch(f"{CMD}.select_events") as mock_select:
                out = StringIO()
                call_command("generate_road_condition_audio", "--text-only", stdout=out)
                mock_select.assert_not_called()
                self.assertIn("already running", out.getvalue())
        finally:
            with blocking_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", GENERATE_ROAD_AUDIO_LOCK_KEY)
            blocking_conn.close()

    def test_lock_released_even_when_synthesis_raises(self):
        make_fresh_config()
        make_road_event(external_id="A")
        with patch(f"{CMD}.synthesize_road_report", side_effect=SynthesisError("boom")):
            with self.assertRaises(CommandError):
                call_command("generate_road_condition_audio", stdout=StringIO())

        with patch(f"{CMD}.select_events", return_value=[]) as mock_select:
            call_command("generate_road_condition_audio", "--text-only", stdout=StringIO())
            mock_select.assert_called_once()  # would not be reached if the lock leaked


class DryRunZeroWritesTests(TestCase):
    def setUp(self):
        make_fresh_config()

    def test_dry_run_creates_no_track_and_calls_no_kokoro(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        with patch(f"{CMD}.synthesize_road_report") as mock_synth:
            out = StringIO()
            call_command("generate_road_condition_audio", "--dry-run", stdout=out)
            mock_synth.assert_not_called()
        self.assertEqual(Track.objects.filter(category__code="KanDrive").count(), 0)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertIn("Road closed.", out.getvalue())

    def test_text_only_creates_no_track_and_calls_no_kokoro(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        with patch(f"{CMD}.synthesize_road_report") as mock_synth:
            out = StringIO()
            call_command("generate_road_condition_audio", "--text-only", stdout=out)
            mock_synth.assert_not_called()
        self.assertEqual(Track.objects.filter(category__code="KanDrive").count(), 0)
        self.assertIn("Road closed.", out.getvalue())

    def test_dry_run_shows_voice_category_and_intended_filename(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        with patch(f"{CMD}.synthesize_road_report") as mock_synth:
            out = StringIO()
            call_command("generate_road_condition_audio", "--dry-run", "--voice", "day", stdout=out)
            mock_synth.assert_not_called()
        output = out.getvalue()
        self.assertIn("Claira", output)
        self.assertIn("KanDrive", output)
        self.assertIn("road_report.flac", output)

    def test_text_only_implies_dry_run_even_without_the_flag(self):
        make_road_event(external_id="A")
        with patch(f"{CMD}.synthesize_road_report") as mock_synth:
            call_command("generate_road_condition_audio", "--text-only", stdout=StringIO())
            mock_synth.assert_not_called()


class NoCurrentEventBehaviorTests(TestCase):
    def setUp(self):
        make_fresh_config()

    def test_zero_events_with_fresh_feed_speaks_no_advisory_message(self):
        with patch(f"{CMD}.synthesize_road_report", return_value=fake_track()) as mock_synth:
            call_command("generate_road_condition_audio", stdout=StringIO())
            mock_synth.assert_called_once()
            spoken_text = mock_synth.call_args[0][0]
        self.assertIn("not reporting any significant road conditions", spoken_text)

    def test_zero_events_text_only_prints_no_advisory_message(self):
        out = StringIO()
        call_command("generate_road_condition_audio", "--text-only", stdout=out)
        self.assertIn("not reporting any significant road conditions", out.getvalue())


class StaleFailedFeedBehaviorTests(TestCase):
    def test_stale_feed_retires_instead_of_generating(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.last_error = ""
        config.last_fetch_succeeded_at = dj_timezone.now() - timedelta(days=5)
        config.stale_data_threshold_minutes = 60
        config.save()

        with patch(f"{CMD}.retire_road_report", return_value=True) as mock_retire, \
             patch(f"{CMD}.synthesize_road_report") as mock_synth:
            out = StringIO()
            call_command("generate_road_condition_audio", stdout=out)
            mock_retire.assert_called_once()
            mock_synth.assert_not_called()
        self.assertIn("stale", out.getvalue().lower())

    def test_failed_feed_retires_instead_of_generating(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.last_error = "CARS API timed out"
        config.last_fetch_succeeded_at = dj_timezone.now()
        config.save()

        with patch(f"{CMD}.retire_road_report", return_value=True) as mock_retire, \
             patch(f"{CMD}.synthesize_road_report") as mock_synth:
            out = StringIO()
            call_command("generate_road_condition_audio", stdout=out)
            mock_retire.assert_called_once()
            mock_synth.assert_not_called()
        self.assertIn("failed", out.getvalue().lower())

    def test_disabled_config_retires_instead_of_generating(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = False
        config.save()

        with patch(f"{CMD}.retire_road_report", return_value=False) as mock_retire, \
             patch(f"{CMD}.synthesize_road_report") as mock_synth:
            out = StringIO()
            call_command("generate_road_condition_audio", stdout=out)
            mock_retire.assert_called_once()
            mock_synth.assert_not_called()
        self.assertIn("disabled", out.getvalue().lower())

    def test_force_bypasses_the_freshness_gate(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.last_error = "boom"
        config.save()
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")

        with patch(f"{CMD}.retire_road_report") as mock_retire, \
             patch(f"{CMD}.synthesize_road_report", return_value=fake_track()) as mock_synth:
            call_command("generate_road_condition_audio", "--force", stdout=StringIO())
            mock_retire.assert_not_called()
            mock_synth.assert_called_once()

    def test_dry_run_on_stale_feed_does_not_call_retire(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.last_error = ""
        config.last_fetch_succeeded_at = dj_timezone.now() - timedelta(days=5)
        config.stale_data_threshold_minutes = 60
        config.save()

        with patch(f"{CMD}.retire_road_report") as mock_retire:
            call_command("generate_road_condition_audio", "--dry-run", stdout=StringIO())
            mock_retire.assert_not_called()


class VoiceSelectionTests(TestCase):
    def setUp(self):
        make_fresh_config()
        make_road_event(external_id="A")

    def test_voice_override_passed_through(self):
        with patch(f"{CMD}.resolve_voice", return_value=("day", {"engine": "kokoro", "model": "af_jessica", "name": "Claira"})) as mock_resolve, \
             patch(f"{CMD}.synthesize_road_report", return_value=fake_track()):
            call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
            mock_resolve.assert_called_once()
            self.assertEqual(mock_resolve.call_args.kwargs.get("slot_override"), "day")

    def test_production_default_passes_no_override(self):
        with patch(f"{CMD}.resolve_voice", return_value=("night", {"engine": "kokoro", "model": "am_liam", "name": "Max"})) as mock_resolve, \
             patch(f"{CMD}.synthesize_road_report", return_value=fake_track()):
            call_command("generate_road_condition_audio", stdout=StringIO())
            self.assertIsNone(mock_resolve.call_args.kwargs.get("slot_override"))

    def test_resolved_voice_is_passed_to_synthesis(self):
        voice = {"engine": "kokoro", "model": "am_liam", "name": "Max"}
        with patch(f"{CMD}.resolve_voice", return_value=("night", voice)), \
             patch(f"{CMD}.synthesize_road_report", return_value=fake_track()) as mock_synth:
            call_command("generate_road_condition_audio", stdout=StringIO())
            called_slot, called_voice = mock_synth.call_args[0][1], mock_synth.call_args[0][2]
            self.assertEqual(called_slot, "night")
            self.assertEqual(called_voice, voice)


class EventIdOptionTests(TestCase):
    def setUp(self):
        make_fresh_config()
        make_road_event(external_id="CARS5-EXAMPLE", description="Specific event text.")

    def test_event_id_without_dry_run_or_text_only_raises(self):
        with self.assertRaises(CommandError):
            call_command("generate_road_condition_audio", "--event-id", "CARS5-EXAMPLE", stdout=StringIO())

    def test_event_id_with_text_only_prints_just_that_events_script(self):
        out = StringIO()
        call_command("generate_road_condition_audio", "--event-id", "CARS5-EXAMPLE", "--text-only", stdout=out)
        self.assertIn("Specific event text.", out.getvalue())

    def test_unknown_event_id_raises_clear_error(self):
        with self.assertRaises(CommandError):
            call_command("generate_road_condition_audio", "--event-id", "NOPE", "--text-only", stdout=StringIO())

    def test_event_id_never_calls_synthesis(self):
        with patch(f"{CMD}.synthesize_road_report") as mock_synth:
            call_command("generate_road_condition_audio", "--event-id", "CARS5-EXAMPLE", "--dry-run", stdout=StringIO())
            mock_synth.assert_not_called()


class FailureReportingTests(TestCase):
    def setUp(self):
        make_fresh_config()
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")

    def test_report_build_error_becomes_command_error_naming_the_event(self):
        # The command builds the report body via build_event_scripts()
        # directly now (not build_full_report(), which it no longer
        # imports at all -- see the command's own comment on avoiding
        # redundant per-event script recomputation for the transition-
        # sound path).
        with patch(f"{CMD}.build_event_scripts", side_effect=ReportBuildError("Failed to build script for event 'A': boom")):
            with self.assertRaises(CommandError) as ctx:
                call_command("generate_road_condition_audio", stdout=StringIO())
        self.assertIn("'A'", str(ctx.exception))

    def test_synthesis_error_becomes_command_error(self):
        with patch(f"{CMD}.synthesize_road_report", side_effect=SynthesisError("kokoro exploded")):
            with self.assertRaises(CommandError) as ctx:
                call_command("generate_road_condition_audio", stdout=StringIO())
        self.assertIn("kokoro exploded", str(ctx.exception))

    def test_voice_resolution_error_becomes_command_error(self):
        from road_conditions.voice import VoiceResolutionError
        with patch(f"{CMD}.resolve_voice", side_effect=VoiceResolutionError("weather-ingest missing")):
            with self.assertRaises(CommandError):
                call_command("generate_road_condition_audio", stdout=StringIO())

    def test_generation_failure_does_not_affect_weather_config(self):
        """Weather/road generation failure isolation: this command's
        own failure must never touch WeatherConfig or any weather-side
        state -- they are entirely separate systems (weather-ingest is
        an external, non-Django project; this command only ever READS
        WeatherConfig.voice_schedule via road_conditions/voice.py)."""
        from weather.models import WeatherConfig
        before = WeatherConfig.load().voice_schedule
        with patch(f"{CMD}.synthesize_road_report", side_effect=SynthesisError("boom")):
            with self.assertRaises(CommandError):
                call_command("generate_road_condition_audio", stdout=StringIO())
        after = WeatherConfig.load().voice_schedule
        self.assertEqual(before, after)


class VerboseOptionTests(TestCase):
    def setUp(self):
        make_fresh_config()

    def test_verbose_lists_selected_events(self):
        make_road_event(external_id="CARS5-VERBOSE-TEST", headline_category="closure", priority=7)
        with patch(f"{CMD}.synthesize_road_report", return_value=fake_track()):
            out = StringIO()
            call_command("generate_road_condition_audio", "--verbose", stdout=out)
        self.assertIn("CARS5-VERBOSE-TEST", out.getvalue())
        self.assertIn("closure", out.getvalue())


class ReportFramingTests(TestCase):
    """compose_report_script() integration with the command's own
    reordered _run() -- voice is now resolved BEFORE final composition
    (see generate_road_condition_audio.py), so --text-only/--dry-run
    print the exact final script (preamble + body + postamble, with
    {announcer_name} resolved), not just the unframed report body."""

    def setUp(self):
        make_fresh_config()

    def test_normal_generation_passes_fully_framed_text_to_synthesis(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        config = RoadConditionsConfiguration.load()
        config.report_preamble = "PREAMBLE MARKER."
        config.report_postamble = "POSTAMBLE MARKER, I'm {announcer_name}."
        config.save()
        with patch(f"{CMD}.resolve_voice", return_value=("day", {"engine": "kokoro", "model": "af_jessica", "name": "Claira"})), \
             patch(f"{CMD}.synthesize_road_report", return_value=fake_track()) as mock_synth:
            call_command("generate_road_condition_audio", stdout=StringIO())
            spoken_text = mock_synth.call_args[0][0]
        self.assertTrue(spoken_text.startswith("PREAMBLE MARKER."))
        self.assertIn("Road closed.", spoken_text)
        self.assertTrue(spoken_text.endswith("POSTAMBLE MARKER, I'm Claira."))

    def test_day_voice_yields_correct_announcer_name(self):
        # Full on-air name ("Claira Sky"), not just the short voice['name']
        # ("Claira") used for internal bookkeeping (ID3 titles, log lines)
        # -- see voice.py's shared VOICES dict (full_name) and this
        # command's own comment on why. Uses the REAL, unmocked
        # resolve_voice() -- this test environment has live access to
        # weather-ingest's shared voice module (see other tests in this
        # file, e.g. DryRunZeroWritesTests, that already rely on this).
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        config = RoadConditionsConfiguration.load()
        config.report_preamble = ""
        config.report_postamble = "I'm {announcer_name}."
        config.save()
        out = StringIO()
        call_command("generate_road_condition_audio", "--text-only", "--voice", "day", stdout=out)
        self.assertIn("I'm Claira Sky.", out.getvalue())

    def test_night_voice_yields_correct_announcer_name(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        config = RoadConditionsConfiguration.load()
        config.report_preamble = ""
        config.report_postamble = "I'm {announcer_name}."
        config.save()
        out = StringIO()
        call_command("generate_road_condition_audio", "--text-only", "--voice", "night", stdout=out)
        self.assertIn("I'm Max Weatherly.", out.getvalue())

    def test_announcer_name_falls_back_to_short_name_when_full_name_missing(self):
        # Defensive fallback (voice.get("full_name") or voice["name"])
        # for a hypothetical voice slot that hasn't been given a
        # full_name yet in weather-ingest's shared VOICES dict --
        # must not raise, must not print a literal "None".
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        config = RoadConditionsConfiguration.load()
        config.report_preamble = ""
        config.report_postamble = "I'm {announcer_name}."
        config.save()
        with patch(f"{CMD}.resolve_voice", return_value=("day", {"engine": "kokoro", "model": "af_jessica", "name": "Claira"})):
            out = StringIO()
            call_command("generate_road_condition_audio", "--text-only", "--voice", "day", stdout=out)
        self.assertIn("I'm Claira.", out.getvalue())

    def test_text_only_prints_fully_resolved_script(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        config = RoadConditionsConfiguration.load()
        config.report_preamble = "PREAMBLE MARKER."
        config.save()
        out = StringIO()
        call_command("generate_road_condition_audio", "--text-only", "--voice", "day", stdout=out)
        output = out.getvalue()
        self.assertIn("PREAMBLE MARKER.", output)
        self.assertIn("Road closed.", output)

    def test_dry_run_displays_fully_resolved_script(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        config = RoadConditionsConfiguration.load()
        config.report_preamble = "PREAMBLE MARKER."
        config.save()
        with patch(f"{CMD}.synthesize_road_report") as mock_synth:
            out = StringIO()
            call_command("generate_road_condition_audio", "--dry-run", "--voice", "day", stdout=out)
            mock_synth.assert_not_called()
        self.assertIn("PREAMBLE MARKER.", out.getvalue())

    def test_fresh_zero_event_generation_receives_framing(self):
        config = RoadConditionsConfiguration.load()
        config.report_preamble = "PREAMBLE MARKER."
        config.report_postamble = "POSTAMBLE MARKER."
        config.save()
        out = StringIO()
        call_command("generate_road_condition_audio", "--text-only", stdout=out)
        output = out.getvalue()
        self.assertIn("PREAMBLE MARKER.", output)
        self.assertIn("not reporting any significant road conditions", output)
        self.assertIn("POSTAMBLE MARKER.", output)

    def test_event_id_remains_unframed(self):
        make_road_event(external_id="CARS5-FRAME-TEST", description="Specific event text.")
        config = RoadConditionsConfiguration.load()
        config.report_preamble = "PREAMBLE MARKER."
        config.report_postamble = "POSTAMBLE MARKER."
        config.save()
        out = StringIO()
        call_command("generate_road_condition_audio", "--event-id", "CARS5-FRAME-TEST", "--text-only", stdout=out)
        output = out.getvalue()
        self.assertIn("Specific event text.", output)
        self.assertNotIn("PREAMBLE MARKER.", output)
        self.assertNotIn("POSTAMBLE MARKER.", output)

    def test_text_only_voice_resolution_failure_becomes_command_error(self):
        # A direct consequence of resolving the voice before checking
        # text_only now (see the command's own comment on this
        # reordering) -- a preview that can't resolve the real voice
        # can't show the real final script either.
        make_road_event(external_id="A", description="Road closed.")
        from road_conditions.voice import VoiceResolutionError
        with patch(f"{CMD}.resolve_voice", side_effect=VoiceResolutionError("weather-ingest missing")):
            with self.assertRaises(CommandError):
                call_command("generate_road_condition_audio", "--text-only", stdout=StringIO())


class TextFilePersistenceCommandTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    """End-to-end command-level checks (real synthesize_road_report,
    fake Kokoro/ffmpeg subprocess calls via KanDriveSynthesisFixtureMixin)
    that road_report.txt is only ever written on a genuinely successful,
    real generation -- never on a preview/individual-event/retirement
    path, and never replaced by a failed run."""

    def setUp(self):
        super().setUp()
        make_fresh_config()

    def test_successful_generation_writes_exact_final_script(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertTrue(road_report_text_path().exists())
        written = road_report_text_path().read_text(encoding="utf-8")
        self.assertIn("Road closed.", written)
        self.assertIn("Claira", written)  # from the default postamble's {announcer_name}
        self.assertNotIn("{announcer_name}", written)

    def test_successful_generation_writes_exact_string_passed_to_synthesis(self):
        # Not just "contains the right substrings" (the test above) --
        # byte-for-byte the SAME string synthesize_road_report() was
        # actually called with, proving the command doesn't recompute
        # or diverge the text between the synthesis call and the
        # text-file write.
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        from road_conditions.synthesis import synthesize_road_report as real_synthesize_road_report
        with patch(f"{CMD}.synthesize_road_report", wraps=real_synthesize_road_report) as spy_synth:
            call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
            passed_text = spy_synth.call_args[0][0]
        self.assertEqual(road_report_text_path().read_text(encoding="utf-8"), passed_text)

    def test_text_only_writes_no_text_file(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--text-only", stdout=StringIO())
        self.assertFalse(road_report_text_path().exists())

    def test_dry_run_writes_no_text_file(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--dry-run", stdout=StringIO())
        self.assertFalse(road_report_text_path().exists())

    def test_event_id_writes_no_text_file(self):
        make_road_event(external_id="CARS5-NOFILE", description="Specific event text.")
        call_command("generate_road_condition_audio", "--event-id", "CARS5-NOFILE", "--text-only", stdout=StringIO())
        self.assertFalse(road_report_text_path().exists())

    def test_stale_feed_does_not_write_or_replace_text_file(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        good_text = road_report_text_path().read_text(encoding="utf-8")

        config = RoadConditionsConfiguration.load()
        config.last_error = ""
        config.last_fetch_succeeded_at = dj_timezone.now() - timedelta(days=5)
        config.stale_data_threshold_minutes = 60
        config.save()

        call_command("generate_road_condition_audio", stdout=StringIO())  # retires instead of generating
        self.assertEqual(road_report_text_path().read_text(encoding="utf-8"), good_text)

    def test_disabled_feed_does_not_write_or_replace_text_file(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        good_text = road_report_text_path().read_text(encoding="utf-8")

        config = RoadConditionsConfiguration.load()
        config.enabled = False
        config.save()

        call_command("generate_road_condition_audio", stdout=StringIO())
        self.assertEqual(road_report_text_path().read_text(encoding="utf-8"), good_text)

    def test_synthesis_failure_leaves_previous_text_file_untouched(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        good_text = road_report_text_path().read_text(encoding="utf-8")

        # --regenerate forces past the fingerprint dedup skip -- without
        # it, this second call would now be a genuinely unchanged report
        # (same event, same config) and would correctly be SKIPPED
        # before ever reaching Kokoro (see FingerprintSkipBehaviorTests),
        # never exercising the failure path this test means to check.
        self._kokoro_should_fail = True
        with self.assertRaises(CommandError):
            call_command("generate_road_condition_audio", "--voice", "day", "--regenerate", stdout=StringIO())

        self.assertEqual(road_report_text_path().read_text(encoding="utf-8"), good_text)

    def test_waveform_analysis_failure_leaves_previous_text_file_untouched(self):
        # The exact scenario this hardening round exists to fix: the
        # FLAC and Track row ARE successfully written before analysis
        # runs, but synthesize_road_report() still raises SynthesisError
        # when analysis fails -- road_report.txt (written by the
        # command only after a full, un-raised success) must remain
        # whatever it was before this run, not get overwritten with
        # text describing a cycle that never actually completed.
        from road_conditions import synthesis as synthesis_module
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        good_text = road_report_text_path().read_text(encoding="utf-8")

        # Change the config so a second, failing run would produce
        # VISIBLY DIFFERENT text if (incorrectly) written -- otherwise
        # this test couldn't distinguish "correctly untouched" from "a
        # regression happened to write byte-identical text again".
        config = RoadConditionsConfiguration.load()
        config.report_preamble = "DIFFERENT PREAMBLE FOR THE FAILING RUN."
        config.save()

        with patch.object(synthesis_module, "_run_full_analysis", return_value=False):
            with self.assertRaises(CommandError):
                call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())

        current_text = road_report_text_path().read_text(encoding="utf-8")
        self.assertEqual(current_text, good_text)
        self.assertNotIn("DIFFERENT PREAMBLE FOR THE FAILING RUN.", current_text)


class TransitionSoundDecisionTests(TestCase):
    """Unit-level: mocked synthesize_road_report, confirming the command
    correctly decides WHAT to pass for segments/transition_sound_path
    (or None/None) under every combination of enabled/disabled, file
    present/missing, and event count."""

    def setUp(self):
        make_fresh_config()

    def test_disabled_by_default_passes_none(self):
        make_road_event(external_id="A", headline_category="closure", description="First.")
        make_road_event(external_id="B", headline_category="roadwork", description="Second.")
        with patch(f"{CMD}.synthesize_road_report", return_value=fake_track()) as mock_synth:
            call_command("generate_road_condition_audio", stdout=StringIO())
        self.assertIsNone(mock_synth.call_args.kwargs.get("segments"))
        self.assertIsNone(mock_synth.call_args.kwargs.get("transition_sound_path"))

    def test_enabled_with_missing_file_falls_back_with_warning(self):
        config = RoadConditionsConfiguration.load()
        config.transition_sound_enabled = True
        config.transition_sound_path = "/nonexistent/path/woosh.wav"
        config.save()
        make_road_event(external_id="A", headline_category="closure", description="First.")
        make_road_event(external_id="B", headline_category="roadwork", description="Second.")
        with patch(f"{CMD}.synthesize_road_report", return_value=fake_track()) as mock_synth:
            out = StringIO()
            call_command("generate_road_condition_audio", stdout=out)
        self.assertIsNone(mock_synth.call_args.kwargs.get("segments"))
        self.assertIsNone(mock_synth.call_args.kwargs.get("transition_sound_path"))
        self.assertIn("not found/readable", out.getvalue())

    def test_enabled_with_real_file_and_two_events_passes_segments(self):
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            config = RoadConditionsConfiguration.load()
            config.transition_sound_enabled = True
            config.transition_sound_path = f.name
            config.save()
            make_road_event(external_id="A", headline_category="closure", description="First.")
            make_road_event(external_id="B", headline_category="roadwork", description="Second.")
            with patch(f"{CMD}.synthesize_road_report", return_value=fake_track()) as mock_synth:
                call_command("generate_road_condition_audio", stdout=StringIO())
            self.assertEqual(mock_synth.call_args.kwargs.get("transition_sound_path"), f.name)
            segments = mock_synth.call_args.kwargs.get("segments")
            self.assertEqual(len(segments), 2)

    def test_enabled_with_one_event_passes_none(self):
        # No boundary to insert a transition at with only one item.
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            config = RoadConditionsConfiguration.load()
            config.transition_sound_enabled = True
            config.transition_sound_path = f.name
            config.save()
            make_road_event(external_id="A", headline_category="closure", description="Only event.")
            with patch(f"{CMD}.synthesize_road_report", return_value=fake_track()) as mock_synth:
                call_command("generate_road_condition_audio", stdout=StringIO())
            self.assertIsNone(mock_synth.call_args.kwargs.get("segments"))
            self.assertIsNone(mock_synth.call_args.kwargs.get("transition_sound_path"))

    def test_enabled_with_zero_events_passes_none(self):
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            config = RoadConditionsConfiguration.load()
            config.transition_sound_enabled = True
            config.transition_sound_path = f.name
            config.save()
            with patch(f"{CMD}.synthesize_road_report", return_value=fake_track()) as mock_synth:
                call_command("generate_road_condition_audio", stdout=StringIO())
            self.assertIsNone(mock_synth.call_args.kwargs.get("segments"))

    def test_dry_run_shows_transition_sound_line_when_applicable(self):
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            config = RoadConditionsConfiguration.load()
            config.transition_sound_enabled = True
            config.transition_sound_path = f.name
            config.save()
            make_road_event(external_id="A", headline_category="closure", description="First.")
            make_road_event(external_id="B", headline_category="roadwork", description="Second.")
            with patch(f"{CMD}.synthesize_road_report") as mock_synth:
                out = StringIO()
                call_command("generate_road_condition_audio", "--dry-run", "--voice", "day", stdout=out)
                mock_synth.assert_not_called()
            self.assertIn("Transition sound:", out.getvalue())
            self.assertIn(f.name, out.getvalue())

    def test_dry_run_omits_transition_sound_line_when_not_applicable(self):
        make_road_event(external_id="A", headline_category="closure", description="Only event.")
        with patch(f"{CMD}.synthesize_road_report") as mock_synth:
            out = StringIO()
            call_command("generate_road_condition_audio", "--dry-run", "--voice", "day", stdout=out)
            mock_synth.assert_not_called()
        self.assertNotIn("Transition sound:", out.getvalue())


class TransitionSoundEndToEndCommandTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    """Real synthesize_road_report, fake Kokoro/ffmpeg subprocess calls
    (via KanDriveSynthesisFixtureMixin) -- proves the whole chain
    (config -> command decision -> report.py segment building ->
    synthesis.py per-segment Kokoro calls) actually wires together, not
    just that the command passes the right mocked arguments."""

    def setUp(self):
        super().setUp()
        make_fresh_config()

    def test_enabled_with_two_events_makes_multiple_kokoro_calls(self):
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            config = RoadConditionsConfiguration.load()
            config.transition_sound_enabled = True
            config.transition_sound_path = f.name
            config.save()
            make_road_event(external_id="A", headline_category="closure", description="First event.")
            make_road_event(external_id="B", headline_category="roadwork", description="Second event.")
            call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 2)
        self.assertTrue(road_report_path().exists())

    def test_disabled_makes_a_single_kokoro_call_even_with_two_events(self):
        make_road_event(external_id="A", headline_category="closure", description="First event.")
        make_road_event(external_id="B", headline_category="roadwork", description="Second event.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 1)

    def test_enabled_with_missing_file_makes_a_single_kokoro_call(self):
        config = RoadConditionsConfiguration.load()
        config.transition_sound_enabled = True
        config.transition_sound_path = "/nonexistent/woosh.wav"
        config.save()
        make_road_event(external_id="A", headline_category="closure", description="First event.")
        make_road_event(external_id="B", headline_category="roadwork", description="Second event.")
        out = StringIO()
        call_command("generate_road_condition_audio", "--voice", "day", stdout=out)
        self.assertEqual(self._kokoro_call_count, 1)
        self.assertIn("not found/readable", out.getvalue())

    def test_enabled_with_three_events_makes_three_kokoro_calls(self):
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            config = RoadConditionsConfiguration.load()
            config.transition_sound_enabled = True
            config.transition_sound_path = f.name
            config.save()
            make_road_event(external_id="A", headline_category="closure", description="Event one.")
            make_road_event(external_id="B", headline_category="roadwork", description="Event two.")
            make_road_event(external_id="C", headline_category="warning", description="Event three.")
            call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 3)


# ---------------------------------------------------------------------
# Change-detection / fingerprinting -- generate_road_condition_audio
# skips the expensive Kokoro/ffmpeg/analysis pipeline when the report
# is genuinely unchanged AND the existing artifacts are still healthy.
# All real synthesize_road_report() calls below (Kokoro/ffmpeg faked
# only at the subprocess boundary via KanDriveSynthesisFixtureMixin),
# not mocked at the command's own import point -- the skip decision
# reads real filesystem/DB state (FLAC/Track/road_report.txt), so a
# genuine first generation is needed before a second call can
# meaningfully prove the skip (or forced-regeneration) behavior.
# ---------------------------------------------------------------------

class FingerprintSkipBehaviorTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        make_fresh_config()

    def test_first_successful_generation_synthesizes_and_stores_fingerprint_and_time(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        config = RoadConditionsConfiguration.load()
        self.assertEqual(config.last_report_fingerprint, "")
        self.assertIsNone(config.last_report_generated_at)

        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())

        self.assertEqual(self._kokoro_call_count, 1)
        config.refresh_from_db()
        self.assertEqual(len(config.last_report_fingerprint), 64)
        self.assertIsNotNone(config.last_report_generated_at)

    def test_second_identical_generation_skips_synthesis(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 1)

        with patch(f"{CMD}.synthesize_road_report") as mock_synth:
            out = StringIO()
            call_command("generate_road_condition_audio", "--voice", "day", stdout=out)
            mock_synth.assert_not_called()
        self.assertIn("unchanged since last successful generation", out.getvalue())

    def test_skipped_run_does_not_update_last_report_generated_at(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        config = RoadConditionsConfiguration.load()
        first_timestamp = config.last_report_generated_at
        first_fingerprint = config.last_report_fingerprint

        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        config.refresh_from_db()
        self.assertEqual(config.last_report_generated_at, first_timestamp)
        self.assertEqual(config.last_report_fingerprint, first_fingerprint)

    def test_skipped_run_does_not_rewrite_road_report_text(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())

        with patch(f"{CMD}.write_road_report_text") as mock_write:
            call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
            mock_write.assert_not_called()

    def test_skipped_run_does_not_run_track_analysis(self):
        # synthesize_road_report is what calls _run_full_analysis --
        # already proven not-called above, but this asserts the
        # analysis step specifically, in case that internal wiring
        # ever changes independently of the outer synthesis call.
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())

        from road_conditions import synthesis as synthesis_module
        with patch.object(synthesis_module, "_run_full_analysis") as mock_analysis:
            call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
            mock_analysis.assert_not_called()

    def test_skipped_run_exits_successfully(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        # A second, skipped call must not raise -- a successful no-op,
        # not an error (nothing here is wrapped in assertRaises).
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())


class ArtifactHealthGuardTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    """A matching fingerprint alone is NOT enough to skip -- every
    listed artifact-health condition must independently force
    regeneration even though the fingerprint would otherwise match."""

    def setUp(self):
        super().setUp()
        make_fresh_config()
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")

    def _generate_once(self):
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())

    def test_missing_flac_forces_regeneration(self):
        self._generate_once()
        road_report_path().unlink()

        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 2)
        self.assertTrue(road_report_path().exists())

    def test_missing_track_forces_regeneration(self):
        self._generate_once()
        Track.objects.filter(filepath=str(road_report_path())).delete()

        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 2)
        self.assertTrue(Track.objects.filter(filepath=str(road_report_path())).exists())

    def test_ready2air_false_forces_regeneration(self):
        self._generate_once()
        Track.objects.filter(filepath=str(road_report_path())).update(ready2air=False)

        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 2)
        self.assertTrue(Track.objects.get(filepath=str(road_report_path())).ready2air)

    def test_missing_text_file_forces_regeneration(self):
        self._generate_once()
        road_report_text_path().unlink()

        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 2)
        self.assertTrue(road_report_text_path().exists())

    def test_mismatched_text_file_forces_regeneration(self):
        self._generate_once()
        road_report_text_path().write_text("Some tampered/mismatched content.", encoding="utf-8")

        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 2)


class RetirementRecoveryRegressionTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    """The exact scenario called out in the fingerprinting task:

      12:00 report generated (fingerprint=ABC, Track ready2air=True)
      12:30 source feed fails -- report retired (ready2air=False)
      13:00 source recovers -- road conditions themselves never
            changed, so the fingerprint is STILL ABC

    The 13:00 run MUST regenerate/reactivate the report rather than
    skip merely because the hash matches."""

    def test_stale_retirement_then_fresh_recovery_regenerates_not_skips(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        config = make_fresh_config()

        # 12:00 -- real generation.
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 1)
        fingerprint_after_first_generation = RoadConditionsConfiguration.load().last_report_fingerprint
        self.assertNotEqual(fingerprint_after_first_generation, "")

        # 12:30 -- feed goes stale; report retired. Reload first --
        # `config` above is stale in memory (its own
        # last_report_fingerprint is still "" from before the 12:00
        # generation wrote it) -- a plain .save() with the old in-memory
        # object would silently blow away the fingerprint the real
        # generation just persisted, which would invalidate this whole
        # regression test.
        config = RoadConditionsConfiguration.load()
        config.last_error = ""
        config.last_fetch_succeeded_at = dj_timezone.now() - timedelta(days=5)
        config.stale_data_threshold_minutes = 60
        config.save()
        call_command("generate_road_condition_audio", stdout=StringIO())  # retires, does not generate
        self.assertEqual(self._kokoro_call_count, 1)  # still just the one real call
        self.assertFalse(Track.objects.get(filepath=str(road_report_path())).ready2air)
        # Retirement never touches the fingerprint -- it's still ABC.
        self.assertEqual(RoadConditionsConfiguration.load().last_report_fingerprint, fingerprint_after_first_generation)

        # 13:00 -- feed recovers; the underlying RoadEvent never
        # changed, so a naive fingerprint-only check would still match.
        config = RoadConditionsConfiguration.load()
        config.last_error = ""
        config.last_fetch_succeeded_at = dj_timezone.now()
        config.save()
        out = StringIO()
        call_command("generate_road_condition_audio", "--voice", "day", stdout=out)

        self.assertEqual(self._kokoro_call_count, 2)  # regenerated, NOT skipped
        self.assertNotIn("unchanged since last successful generation", out.getvalue())
        self.assertTrue(Track.objects.get(filepath=str(road_report_path())).ready2air)


class FailureLifecycleFingerprintTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        make_fresh_config()
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")

    def test_synthesis_failure_does_not_update_fingerprint_or_time(self):
        self._kokoro_should_fail = True
        with self.assertRaises(CommandError):
            call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        config = RoadConditionsConfiguration.load()
        self.assertEqual(config.last_report_fingerprint, "")
        self.assertIsNone(config.last_report_generated_at)

    def test_analysis_failure_does_not_update_fingerprint_or_time(self):
        from road_conditions import synthesis as synthesis_module
        with patch.object(synthesis_module, "_run_full_analysis", return_value=False):
            with self.assertRaises(CommandError):
                call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        config = RoadConditionsConfiguration.load()
        self.assertEqual(config.last_report_fingerprint, "")
        self.assertIsNone(config.last_report_generated_at)

    def test_text_file_write_failure_does_not_update_fingerprint_or_time(self):
        with patch(f"{CMD}.write_road_report_text", side_effect=OSError("disk full")):
            with self.assertRaises(CommandError):
                call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        config = RoadConditionsConfiguration.load()
        self.assertEqual(config.last_report_fingerprint, "")
        self.assertIsNone(config.last_report_generated_at)

    def test_previous_successful_fingerprint_remains_intact_after_failed_newer_attempt(self):
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        good_config = RoadConditionsConfiguration.load()
        good_fingerprint = good_config.last_report_fingerprint
        good_time = good_config.last_report_generated_at

        # A DIFFERENT preamble so a coincidental identical fingerprint
        # can't mask a regression -- same idiom this file already uses
        # for the text-file equivalent (see
        # test_waveform_analysis_failure_leaves_previous_text_file_untouched).
        config = RoadConditionsConfiguration.load()
        config.report_preamble = "A DIFFERENT PREAMBLE FOR THE FAILING RUN."
        config.save()

        self._kokoro_should_fail = True
        with self.assertRaises(CommandError):
            call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())

        config.refresh_from_db()
        self.assertEqual(config.last_report_fingerprint, good_fingerprint)
        self.assertEqual(config.last_report_generated_at, good_time)


class RegenerateAndForceCliOptionTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        make_fresh_config()
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")

    def test_regenerate_forces_synthesis_when_fingerprint_matches(self):
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 1)

        out = StringIO()
        call_command("generate_road_condition_audio", "--voice", "day", "--regenerate", stdout=out)
        self.assertEqual(self._kokoro_call_count, 2)  # regenerated, not skipped
        self.assertNotIn("unchanged since last successful generation", out.getvalue())

    def test_regenerate_does_not_bypass_freshness_safety(self):
        config = RoadConditionsConfiguration.load()
        config.last_error = "boom"  # feed failed
        config.save()

        out = StringIO()
        call_command("generate_road_condition_audio", "--regenerate", stdout=out)
        self.assertEqual(self._kokoro_call_count, 0)  # never reached synthesis -- retired instead
        self.assertIn("failed", out.getvalue().lower())

    def test_force_alone_still_skips_an_unchanged_healthy_report(self):
        # --force only bypasses the freshness gate -- fingerprint dedup
        # behaves normally unless --regenerate is ALSO given.
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 1)

        out = StringIO()
        call_command("generate_road_condition_audio", "--voice", "day", "--force", stdout=out)
        self.assertEqual(self._kokoro_call_count, 1)  # still skipped
        self.assertIn("unchanged since last successful generation", out.getvalue())

    def test_force_and_regenerate_combo_bypasses_both(self):
        config = RoadConditionsConfiguration.load()
        config.last_error = "boom"  # feed failed -- never recovers in this test
        config.save()

        call_command("generate_road_condition_audio", "--voice", "day", "--force", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 1)

        out = StringIO()
        call_command("generate_road_condition_audio", "--voice", "day", "--force", "--regenerate", stdout=out)
        self.assertEqual(self._kokoro_call_count, 2)  # bypassed BOTH freshness and fingerprint dedup
        self.assertNotIn("unchanged since last successful generation", out.getvalue())


class DryRunFingerprintReportingTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        make_fresh_config()
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")

    def test_dry_run_reports_fingerprint_lines(self):
        out = StringIO()
        call_command("generate_road_condition_audio", "--dry-run", "--voice", "day", stdout=out)
        output = out.getvalue()
        self.assertIn("[DRY RUN] Report fingerprint:", output)
        self.assertIn("[DRY RUN] Existing successful fingerprint:", output)
        self.assertIn("[DRY RUN] Would skip generation:", output)
        self.assertEqual(self._kokoro_call_count, 0)

    def test_dry_run_would_skip_no_when_nothing_generated_yet(self):
        out = StringIO()
        call_command("generate_road_condition_audio", "--dry-run", "--voice", "day", stdout=out)
        self.assertIn("Would skip generation: no", out.getvalue())
        self.assertIn("Existing successful fingerprint: (none)", out.getvalue())

    def test_dry_run_would_skip_yes_when_unchanged_and_healthy(self):
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        out = StringIO()
        call_command("generate_road_condition_audio", "--dry-run", "--voice", "day", stdout=out)
        self.assertIn("Would skip generation: yes", out.getvalue())
        self.assertEqual(self._kokoro_call_count, 1)  # dry run never actually synthesizes

    def test_dry_run_would_skip_no_when_changed(self):
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        config = RoadConditionsConfiguration.load()
        config.report_preamble = "A CHANGED PREAMBLE."
        config.save()

        out = StringIO()
        call_command("generate_road_condition_audio", "--dry-run", "--voice", "day", stdout=out)
        self.assertIn("Would skip generation: no", out.getvalue())

    def test_dry_run_would_skip_no_when_regenerate_passed(self):
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        out = StringIO()
        call_command("generate_road_condition_audio", "--dry-run", "--voice", "day", "--regenerate", stdout=out)
        self.assertIn("Would skip generation: no", out.getvalue())

    def test_dry_run_never_writes_config_fingerprint_fields(self):
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        good_config = RoadConditionsConfiguration.load()
        fingerprint_before = good_config.last_report_fingerprint
        time_before = good_config.last_report_generated_at

        call_command("generate_road_condition_audio", "--dry-run", "--voice", "day", stdout=StringIO())

        config = RoadConditionsConfiguration.load()
        self.assertEqual(config.last_report_fingerprint, fingerprint_before)
        self.assertEqual(config.last_report_generated_at, time_before)


class TextOnlyIgnoresFingerprintDedupTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        make_fresh_config()

    def test_text_only_still_prints_full_text_even_when_report_is_unchanged(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())

        out = StringIO()
        call_command("generate_road_condition_audio", "--text-only", "--voice", "day", stdout=out)
        self.assertIn("Road closed.", out.getvalue())
        self.assertNotIn("unchanged since last successful generation", out.getvalue())

    def test_text_only_never_updates_fingerprint_state(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        fingerprint_before = RoadConditionsConfiguration.load().last_report_fingerprint

        call_command("generate_road_condition_audio", "--text-only", "--voice", "day", stdout=StringIO())
        self.assertEqual(RoadConditionsConfiguration.load().last_report_fingerprint, fingerprint_before)


class EventIdOutsideFingerprintTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        make_fresh_config()

    def test_event_id_never_touches_fingerprint_state(self):
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        fingerprint_before = RoadConditionsConfiguration.load().last_report_fingerprint

        call_command("generate_road_condition_audio", "--event-id", "A", "--text-only", stdout=StringIO())
        self.assertEqual(RoadConditionsConfiguration.load().last_report_fingerprint, fingerprint_before)


class FramingAndVoiceChangeForcesRegenerationTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        make_fresh_config()
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")

    def test_editing_preamble_forces_regeneration(self):
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 1)

        config = RoadConditionsConfiguration.load()
        config.report_preamble = "A BRAND NEW PREAMBLE."
        config.save()

        out = StringIO()
        call_command("generate_road_condition_audio", "--voice", "day", stdout=out)
        self.assertEqual(self._kokoro_call_count, 2)
        self.assertNotIn("unchanged since last successful generation", out.getvalue())

    def test_editing_postamble_forces_regeneration(self):
        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 1)

        config = RoadConditionsConfiguration.load()
        config.report_postamble = "A BRAND NEW POSTAMBLE."
        config.save()

        call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 2)

    def test_voice_model_change_forces_regeneration(self):
        # Same slot/name, different model -- the model behind an
        # announcer name changing must still force regeneration (see
        # report.compute_report_fingerprint()'s own docstring).
        voice_v1 = {"engine": "kokoro", "model": "af_jessica", "name": "Claira", "full_name": "Claira Sky"}
        voice_v2 = {"engine": "kokoro", "model": "af_jessica_v2", "name": "Claira", "full_name": "Claira Sky"}

        with patch(f"{CMD}.resolve_voice", return_value=("day", voice_v1)):
            call_command("generate_road_condition_audio", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 1)

        with patch(f"{CMD}.resolve_voice", return_value=("day", voice_v2)):
            out = StringIO()
            call_command("generate_road_condition_audio", stdout=out)
        self.assertEqual(self._kokoro_call_count, 2)
        self.assertNotIn("unchanged since last successful generation", out.getvalue())


class PlannedEventTransitionFingerprintTests(KanDriveSynthesisFixtureMixin, TransactionTestCase):
    """report.py's own planned/future-event wording (see
    report._planned_prefix()) can change the final composed text purely
    because wall-clock time crosses an event's start_time -- even
    though KDOT's own record (and therefore RoadEvent.payload_checksum)
    never changed. The fingerprint is based on the actual generated
    final text, so this must force regeneration rather than being
    optimized away by a source-level checksum."""

    def test_same_stored_event_before_and_after_start_time_forces_regeneration(self):
        reference = dj_timezone.now()
        start_time = reference + timedelta(hours=2)
        make_road_event(
            external_id="CARS5-PLANNED", headline_category="roadwork",
            description="Lane closure ahead.", start_time=start_time,
        )
        config = make_fresh_config()
        # Tolerate the multi-hour mocked time jump below without the
        # feed itself looking stale -- unrelated to what this test
        # actually checks.
        config.stale_data_threshold_minutes = 24 * 60
        config.save()

        with patch("django.utils.timezone.now", return_value=reference):
            call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 1)
        text_before = road_report_text_path().read_text(encoding="utf-8")
        self.assertIn("Beginning", text_before)  # planned-work wording
        fingerprint_before = RoadConditionsConfiguration.load().last_report_fingerprint

        after_start = start_time + timedelta(hours=1)
        with patch("django.utils.timezone.now", return_value=after_start):
            out = StringIO()
            call_command("generate_road_condition_audio", "--voice", "day", stdout=out)

        self.assertEqual(self._kokoro_call_count, 2)  # regenerated, NOT skipped
        self.assertNotIn("unchanged since last successful generation", out.getvalue())
        text_after = road_report_text_path().read_text(encoding="utf-8")
        self.assertNotIn("Beginning", text_after)  # no longer planned -- now in effect
        self.assertNotEqual(RoadConditionsConfiguration.load().last_report_fingerprint, fingerprint_before)
