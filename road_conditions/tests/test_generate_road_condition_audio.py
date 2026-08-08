"""generate_road_condition_audio management command tests -- CLI wiring,
output format, exit codes, the advisory-lock overlap guard, and the
freshness gate's generate-vs-retire branching. report.select_events()/
build_full_report()/synthesis.synthesize_road_report()/retire_road_report()
are exercised through light mocking at the command's own import points
(their own real behavior is covered exhaustively in
test_kandrive_report.py and test_kandrive_synthesis.py) -- no live
Kokoro or CARS API calls anywhere here."""
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
from road_conditions.synthesis import SynthesisError, road_report_text_path
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
        with patch(f"{CMD}.build_full_report", side_effect=ReportBuildError("Failed to build script for event 'A': boom")):
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

        self._kokoro_should_fail = True
        with self.assertRaises(CommandError):
            call_command("generate_road_condition_audio", "--voice", "day", stdout=StringIO())

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
