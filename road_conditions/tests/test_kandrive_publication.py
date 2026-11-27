"""1.24-B: content-only regeneration, published-audio provenance, the
Ready-to-Air rule and publication integrity.

Control paths run the real command against real files and rows, with only
TTS and ffmpeg faked (KanDriveSynthesisFixtureMixin). RealMediaPublicationTests
at the end uses real ffmpeg and real Track analysis; only the speech itself is
a generated tone. A "killed" publication is simulated by also disabling the
in-process recovery, since a signal would not run it."""
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import psycopg2
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone as dj_timezone

from isadoraair.tts.models import StationTTSVoice
from library.models import Category, CategoryKind, Track
from library.services import cue_markers
from monitoring.models import SystemEvent
from road_conditions import synthesis as synthesis_module
from road_conditions.models import RoadConditionsConfiguration, RoadEvent
from road_conditions.synthesis import (
    existing_report_is_healthy, hash_file_sha256, published_report_problem, read_provenance,
    road_report_path, road_report_pending_path, road_report_previous_path,
    road_report_provenance_path, road_report_text_path,
)
from road_conditions.tests.test_generate_road_condition_audio import CMD, make_fresh_config
from road_conditions.tests.test_kandrive_report import make_road_event
from road_conditions.tests.test_kandrive_synthesis import (
    KanDriveSynthesisFixtureMixin, ensure_default_weather_voice_personas,
)
from road_conditions.voice import VoiceResolutionError
from weather.models import WeatherConfig, WeatherVoicePersona

SKIPPED = "unchanged since last successful generation"


class SimulatedKill(BaseException):
    pass


def use_voice_all_day(slot):
    config = WeatherConfig.load()
    config.voice_schedule = [[slot, 0, 23]]
    config.save(update_fields=["voice_schedule"])


def generate(*args):
    out = StringIO()
    call_command("generate_road_condition_audio", *args, stdout=out)
    return out.getvalue()


def set_config(**fields):
    RoadConditionsConfiguration.load()
    RoadConditionsConfiguration.objects.update(**fields)


def track():
    return Track.objects.get(filepath=str(road_report_path()))


def analysis_writes_markers(row, next_start=250.0):
    """Analysis reaching the cue-marker authority, as the real one does."""
    result = cue_markers.apply_calculation(
        row.pk, {"next_start_seconds": next_start}, basis_revision=row.cue_marker_revision,
        source=cue_markers.SOURCE_ANALYSIS, media_replaced=True, duration=row.duration_seconds)
    assert result.ok, (result.error, result.conflict)
    assert Track.objects.get(pk=row.pk).cue_marker_revision != row.cue_marker_revision


def set_ready(value):
    """What an operator's Ready-to-Air change does to the row."""
    Track.objects.filter(filepath=str(road_report_path())).update(ready2air=value)


class PublishedReportMixin(KanDriveSynthesisFixtureMixin):
    """One road event, published once by the day voice."""

    def setUp(self):
        super().setUp()
        make_fresh_config()
        use_voice_all_day("day")
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        self._flac_payload = b"FLAC-ONE"
        generate()
        assert self._kokoro_call_count == 1

    def renders(self):
        return self._kokoro_call_count

    def provenance(self):
        record, problem = read_provenance()
        self.assertEqual(problem, "")
        return record

    def fingerprint(self):
        return RoadConditionsConfiguration.load().last_report_fingerprint

    def leftovers(self):
        return sorted(p.name for p in Path(self._tmpdir.name).iterdir()
                      if p.name.startswith(".") or p == road_report_pending_path())

    def assert_skips(self, *args):
        before = self.renders()
        out = generate(*args)
        self.assertEqual(self.renders(), before, out)
        self.assertIn(SKIPPED, out)
        return out

    def assert_renders(self, reason, *args):
        before = self.renders()
        self._flac_payload = f"FLAC-{before + 1}".encode()
        out = generate(*args)
        self.assertEqual(self.renders(), before + 1, out)
        self.assertIn(f"reason={reason}", out)
        return out

    def change_content(self, text="Both lanes are closed."):
        RoadEvent.objects.filter(external_id="A").update(description=text)


class VoiceChangesDoNotRegenerateTests(PublishedReportMixin, TransactionTestCase):
    def assert_unchanged_and_still_identified_as_claira(self):
        before = road_report_provenance_path().read_bytes()
        self.assert_skips()
        self.assertEqual(road_report_provenance_path().read_bytes(), before)
        record = self.provenance()
        self.assertEqual(record["announcer"], {"slot": "day", "name": "Claira", "full_name": "Claira Sky"})
        self.assertEqual(record["voice"]["provider_voice"], "af_jessica")
        self.assertIn("Claira Sky", road_report_text_path().read_text(encoding="utf-8"))
        self.assertEqual(track().title, "KanDrive Road Report (Claira)")
        self.assertTrue(track().ready2air)

    def test_every_hour_of_a_real_shift_schedule_skips(self):
        config = WeatherConfig.load()
        config.voice_schedule = [["day", 3, 8], ["night", 9, 14], ["day", 15, 20], ["night", 21, 2]]
        config.save(update_fields=["voice_schedule"])
        base = dj_timezone.localtime().replace(minute=2, second=0, microsecond=0)
        for hour in range(24):
            moment = base.replace(hour=hour)
            with patch("django.utils.timezone.now", return_value=moment):
                set_config(last_fetch_succeeded_at=moment)
                self.assert_skips()
        self.assertEqual(self.renders(), 1)

    def test_shift_to_the_other_announcer_skips(self):
        use_voice_all_day("night")
        self.assert_unchanged_and_still_identified_as_claira()

    def test_persona_rename_skips(self):
        WeatherVoicePersona.objects.filter(slot="day").update(display_name="Clare", full_name="Clare Skye")
        self.assert_unchanged_and_still_identified_as_claira()

    def test_voice_remap_and_model_change_skip(self):
        StationTTSVoice.objects.filter(name="Claira_Sky").update(provider_voice="af_bella", speed=1.1)
        self.assert_unchanged_and_still_identified_as_claira()

    def test_previously_used_voice_disabled_skips(self):
        StationTTSVoice.objects.filter(name="Claira_Sky").update(enabled=False)
        self.assert_unchanged_and_still_identified_as_claira()

    def test_previously_used_persona_removed_skips(self):
        use_voice_all_day("night")
        WeatherVoicePersona.objects.filter(slot="day").delete()
        self.assert_unchanged_and_still_identified_as_claira()

    def test_unchanged_report_never_resolves_a_voice(self):
        with patch(f"{CMD}.resolve_voice", side_effect=VoiceResolutionError("broken")) as resolve:
            self.assert_skips()
        resolve.assert_not_called()

    def test_changed_report_with_broken_voice_fails_and_keeps_the_old_report(self):
        self.change_content()
        old = self.fingerprint()
        with patch(f"{CMD}.resolve_voice", side_effect=VoiceResolutionError("broken")):
            with self.assertRaises(CommandError):
                generate()
        self.assertEqual(self.fingerprint(), old)
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-ONE")
        self.assertTrue(track().ready2air)

    def test_changed_report_is_rendered_with_the_current_voice(self):
        use_voice_all_day("night")
        self.change_content()
        self.assert_renders("content_changed")
        self.assertEqual(self.provenance()["announcer"]["name"], "Max")
        self.assertIn("{announcer_name}", RoadConditionsConfiguration.load().report_postamble)
        self.assertIn("Max Weatherly", road_report_text_path().read_text(encoding="utf-8"))
        self.assertIn("Max Weatherly", self.provenance()["script"])
        self.assertEqual(track().title, "KanDrive Road Report (Max)")


class ContentIdentityTests(PublishedReportMixin, TransactionTestCase):
    def test_new_changed_and_cleared_events_each_render_once(self):
        make_road_event(external_id="B", headline_category="roadwork", description="Bridge work.")
        self.assert_renders("content_changed")
        self.assert_skips()
        self.change_content()
        self.assert_renders("content_changed")
        self.assert_skips()
        RoadEvent.objects.filter(external_id="B").update(source_active=False)
        self.assert_renders("content_changed")
        RoadEvent.objects.update(source_active=False)
        self.assert_renders("content_changed")
        self.assertIn("not reporting any significant road conditions",
                      road_report_text_path().read_text(encoding="utf-8"))
        self.assert_skips()

    def test_priority_change_that_reorders_the_spoken_report_renders(self):
        make_road_event(external_id="B", headline_category="roadwork", description="Bridge work.", priority=3)
        make_road_event(external_id="C", headline_category="roadwork", description="Paving work.", priority=2)
        self.assert_renders("content_changed")
        RoadEvent.objects.filter(external_id="C").update(priority=4)
        self.assert_renders("content_changed")

    def test_priority_change_that_keeps_the_order_does_not_render(self):
        make_road_event(external_id="B", headline_category="roadwork", description="Bridge work.", priority=3)
        self.assert_renders("content_changed")
        RoadEvent.objects.filter(external_id="B").update(priority=4)
        self.assert_skips()

    def test_incidental_record_fields_and_whitespace_do_not_render(self):
        now = dj_timezone.now()
        RoadEvent.objects.update(source_update_time=now, source_next_update_time=now + timedelta(days=14),
                                 update_number=99, payload_checksum="f" * 64, last_seen_at=now,
                                 last_changed_at=now, description="  Road   closed.  ")
        config = RoadConditionsConfiguration.load()
        set_config(report_preamble=f"  {config.report_preamble}  ")
        self.assert_skips()

    def test_upstream_record_order_does_not_matter(self):
        make_road_event(external_id="B", headline_category="roadwork", description="Bridge work.")
        make_road_event(external_id="C", headline_category="roadwork", description="Paving work.")
        self.assert_renders("content_changed")
        rows = list(RoadEvent.objects.order_by("-external_id").values())
        RoadEvent.objects.all().delete()
        for row in rows:
            row.pop("id")
            RoadEvent.objects.create(**row)
        self.assert_skips()

    def test_template_and_transition_sound_changes_render(self):
        make_road_event(external_id="B", headline_category="roadwork", description="Bridge work.")
        self.assert_renders("content_changed")
        set_config(report_postamble="That is the report. I'm {announcer_name}.")
        self.assert_renders("content_changed")
        sound = Path(self._tmpdir.name) / "horn.bin"
        sound.write_bytes(b"horn-one")
        set_config(transition_sound_enabled=True, transition_sound_path=str(sound))
        before = self.renders()
        generate()
        self.assertEqual(self.renders(), before + 2)        # two segments, one report
        self.assertEqual(self.provenance()["rendering"]["transition"],
                         {"active": True, "sound_sha256": hash_file_sha256(sound), "segment_count": 2})
        self.assert_skips()
        sound.write_bytes(b"horn-two")
        out = generate()
        self.assertIn("reason=content_changed", out)


class ProvenanceTests(PublishedReportMixin, TransactionTestCase):
    def test_records_what_was_actually_published(self):
        record = self.provenance()
        script = road_report_text_path().read_text(encoding="utf-8")
        self.assertEqual(record["semantic_fingerprint"], self.fingerprint())
        self.assertEqual(record["script"], script)
        self.assertEqual(record["flac_sha256"], hash_file_sha256(road_report_path()))
        self.assertEqual(record["voice"], {"logical_voice_name": "Claira_Sky", "engine": "kokoro",
                                           "provider_voice": "af_jessica", "language": "en-us", "speed": 1.0})
        self.assertEqual(record["model"]["filename"], "kokoro-v1.0.onnx")
        self.assertEqual(len(record["model"]["contract_sha256"]), 64)
        self.assertEqual(record["rendering"]["audio_format_version"], 1)
        self.assertEqual(record["track"], {"id": track().id})
        self.assertTrue(record["published_at"])
        self.assertEqual(self.leftovers(), [])

    def test_each_damaged_artifact_has_a_defined_recovery_render(self):
        def edit_record(**changes):
            record = json.loads(road_report_provenance_path().read_text(encoding="utf-8"))
            record.update(changes)
            road_report_provenance_path().write_text(json.dumps(record), encoding="utf-8")

        cases = [
            (lambda: road_report_path().write_bytes(b"garbage"), "flac_mismatch"),
            (lambda: road_report_path().unlink(), "flac_missing"),
            (lambda: road_report_text_path().write_text("something else", encoding="utf-8"), "text_mismatch"),
            (lambda: road_report_text_path().unlink(), "text_missing"),
            (lambda: road_report_provenance_path().unlink(), "provenance_missing"),
            (lambda: road_report_provenance_path().write_text("{not json", encoding="utf-8"), "provenance_invalid"),
            (lambda: edit_record(provenance_version=99), "provenance_invalid"),
            (lambda: edit_record(flac_sha256="0" * 64), "flac_mismatch"),
            (lambda: edit_record(script="not what was published"), "text_mismatch"),
            (lambda: edit_record(semantic_fingerprint="0" * 64), "provenance_content_mismatch"),
            (lambda: edit_record(track={"id": 10**9}), "track_mismatch"),
            (lambda: Track.objects.filter(filepath=str(road_report_path())).delete(), "track_missing"),
            (lambda: road_report_pending_path().write_text("{}", encoding="utf-8"), "publication_incomplete"),
        ]
        for damage, reason in cases:
            with self.subTest(reason=reason):
                damage()
                self.assert_renders(reason)
                self.assert_skips()

    def test_regenerate_renders_an_unchanged_report_in_the_current_voice(self):
        use_voice_all_day("night")
        self.assert_renders("operator_regenerate", "--regenerate")
        self.assertEqual(self.provenance()["announcer"]["name"], "Max")

    def test_one_time_transition_from_the_version_one_fingerprint(self):
        # r0149 state: audio, Track, text and a v1 fingerprint; no sidecar.
        road_report_provenance_path().unlink()
        set_config(last_report_fingerprint="1" * 64)
        self.assert_renders("provenance_missing")
        use_voice_all_day("night")
        self.assert_skips()
        self.assertEqual(self.renders(), 2)

    def test_dry_run_and_text_only_write_nothing(self):
        self.change_content()
        def snapshot():
            return (sorted((p.name, p.read_bytes()) for p in Path(self._tmpdir.name).iterdir()),
                    Track.objects.filter(filepath=str(road_report_path())).values().get())
        before = snapshot()
        out = generate("--dry-run")
        self.assertIn("Would skip generation: no", out)
        self.assertIn("Render reason: content_changed", out)
        self.assertIn("Both lanes", generate("--text-only"))
        self.assertEqual(snapshot(), before)


class ExplicitVoiceCompatibilityTests(PublishedReportMixin, TransactionTestCase):
    """--voice behaves as in r0149: it renders when the published report is
    not already in that voice. Only the SCHEDULED voice is ignored."""

    def test_voice_matching_the_published_one_skips(self):
        use_voice_all_day("night")
        self.assert_skips("--voice", "day")

    def test_voice_differing_from_the_published_one_renders_once(self):
        self.assert_renders("explicit_voice", "--voice", "night")
        self.assertEqual(self.provenance()["announcer"]["name"], "Max")
        self.assert_skips("--voice", "night")

    def test_automatic_run_does_not_revert_a_manual_voice_render(self):
        self.assert_renders("explicit_voice", "--voice", "night")
        self.assert_skips()
        self.assertEqual(self.provenance()["announcer"]["name"], "Max")

    def test_remapped_voice_named_explicitly_renders(self):
        StationTTSVoice.objects.filter(name="Claira_Sky").update(provider_voice="af_bella")
        self.assert_skips()
        self.assert_renders("explicit_voice", "--voice", "day")

    def test_unknown_voice_slot_fails_without_changing_anything(self):
        with self.assertRaises(CommandError):
            generate("--voice", "nobody")
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-ONE")

    def test_regenerate_with_voice(self):
        self.assert_renders("operator_regenerate", "--regenerate", "--voice", "night")
        self.assertEqual(self.provenance()["announcer"]["slot"], "night")


class ReadyToAirPolicyTests(PublishedReportMixin, TransactionTestCase):
    """The workflow sets ready2air=True when a new report is published
    completely, and otherwise never writes it -- either way round."""

    OUTAGES = {
        "failed fetch": {"last_error": "CARS API returned HTTP 404"},
        "stale data": {"last_fetch_succeeded_at": "stale"},
        "feature disabled": {"enabled": False},
    }

    def outage(self, name):
        fields = dict(self.OUTAGES[name])
        if fields.get("last_fetch_succeeded_at") == "stale":
            fields["last_fetch_succeeded_at"] = dj_timezone.now() - timedelta(days=1)
        set_config(**fields)

    def recover(self):
        set_config(enabled=True, last_error="", last_fetch_succeeded_at=dj_timezone.now())

    def row(self):
        return Track.objects.filter(filepath=str(road_report_path())).values().get()

    def test_successful_new_publication_enables(self):
        set_ready(False)
        self.change_content()
        self.assert_renders("content_changed")
        self.assertTrue(track().ready2air)

    def test_explicit_regeneration_enables(self):
        set_ready(False)
        self.assert_renders("operator_regenerate", "--regenerate")
        self.assertTrue(track().ready2air)

    def test_unchanged_report_leaves_either_value(self):
        for value in (False, True):
            with self.subTest(ready=value):
                set_ready(value)
                before = self.row()
                use_voice_all_day("night")
                StationTTSVoice.objects.filter(name="Claira_Sky").update(provider_voice="af_bella")
                self.assert_skips()
                self.assertEqual(self.row(), before)

    def test_outages_change_neither_the_audio_nor_readiness(self):
        # Content has changed too: still nothing may happen during an outage.
        self.change_content()
        audio = road_report_path().read_bytes()
        for name in self.OUTAGES:
            for value in (True, False):
                with self.subTest(outage=name, ready=value):
                    self.recover()
                    set_ready(value)
                    self.outage(name)
                    before = (self.row(), road_report_provenance_path().read_bytes())
                    with patch(f"{CMD}.resolve_voice", side_effect=VoiceResolutionError("broken")):
                        generate()
                    self.assertEqual((self.row(), road_report_provenance_path().read_bytes()), before)
                    self.assertEqual(road_report_path().read_bytes(), audio)
                    self.assertEqual(self.renders(), 1)

    def test_recovery_with_unchanged_content_neither_renders_nor_enables(self):
        for name in self.OUTAGES:
            with self.subTest(outage=name):
                set_ready(False)
                self.outage(name)
                generate()
                self.recover()
                self.assert_skips()
                self.assertFalse(track().ready2air)

    def test_recovery_with_changed_content_publishes_and_enables(self):
        set_ready(False)
        self.outage("failed fetch")
        self.change_content()
        generate()
        self.assertFalse(track().ready2air)
        self.recover()
        self.assert_renders("content_changed")
        self.assertTrue(track().ready2air)

    def test_failed_voice_synthesis_validation_or_publication_keeps_readiness(self):
        failures = {
            "voice": lambda: patch(f"{CMD}.resolve_voice", side_effect=VoiceResolutionError("broken")),
            "synthesis": lambda: patch.object(
                synthesis_module, "synthesize_station_voice", side_effect=ValueError("tts")),
            "validation": lambda: patch.object(
                synthesis_module, "_validate_candidate", side_effect=ValueError("bad decode")),
            "publication": lambda: patch.object(synthesis_module, "_run_full_analysis", return_value=False),
        }
        self.change_content()
        for name, failing in failures.items():
            for value in (True, False):
                with self.subTest(failure=name, ready=value):
                    set_ready(value)
                    with failing(), self.assertRaises(CommandError):
                        generate()
                    self.assertEqual(track().ready2air, value)
                    self.assertEqual(road_report_path().read_bytes(), b"FLAC-ONE")

    def test_readiness_is_not_written_while_publishing(self):
        # Not used as a publication lock: both values survive until commit.
        for value in (False, True):
            with self.subTest(ready=value):
                set_ready(value)
                seen = []

                def observe(row):
                    seen.append(track().ready2air)
                    return True

                with patch.object(synthesis_module, "_run_full_analysis", side_effect=observe):
                    generate("--regenerate")
                self.assertEqual(seen, [value])
                self.assertTrue(track().ready2air)

    def test_no_readiness_machinery_creates_events_or_files(self):
        set_ready(False)
        self.assert_skips()
        self.assertFalse(SystemEvent.objects.filter(category="road_conditions").exists())
        self.assertEqual(sorted(p.name for p in Path(self._tmpdir.name).iterdir()),
                         ["road_report.flac", "road_report.json", "road_report.txt"])


class PublicationFailureTests(PublishedReportMixin, TransactionTestCase):
    def snapshot(self):
        row = track()
        return (road_report_path().read_bytes(), road_report_text_path().read_text(encoding="utf-8"),
                road_report_provenance_path().read_bytes(), row.title, row.duration_seconds,
                row.ready2air, row.cue_marker_revision, self.fingerprint())

    def fail_new_content(self, patcher):
        before = self.snapshot()
        original_fingerprint = self.fingerprint()
        use_voice_all_day("night")
        self.change_content()
        self._flac_payload = b"FLAC-NEW"
        with patcher:
            with self.assertRaises(CommandError):
                generate()
        use_voice_all_day("day")
        self.assertEqual(self.fingerprint(), original_fingerprint)
        return before

    def assert_previous_recording_restored(self, before):
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.leftovers(), [])
        # The old recording is genuinely whole again, so it verifies as itself.
        self.assertTrue(existing_report_is_healthy(self.fingerprint()))

    def revert_and_expect_the_restored_report_to_stand(self):
        self.change_content("Road closed.")
        self.assert_skips()
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-ONE")

    def test_each_failed_stage_restores_the_previous_recording(self):
        real_write = synthesis_module._write_json_atomically

        def fail_provenance(path, payload):
            if path == road_report_provenance_path():
                raise OSError("disk full")
            return real_write(path, payload)

        stages = {
            "track update": lambda: patch.object(
                Track.objects, "update_or_create", side_effect=RuntimeError("db down")),
            "analysis reports failure": lambda: patch.object(
                synthesis_module, "_run_full_analysis", return_value=False),
            "analysis raises": lambda: patch.object(
                synthesis_module, "_run_full_analysis", side_effect=RuntimeError("crash")),
            "script write": lambda: patch.object(
                synthesis_module, "write_road_report_text", side_effect=OSError("disk full")),
            "provenance write": lambda: patch.object(
                synthesis_module, "_write_json_atomically", side_effect=fail_provenance),
        }
        for name, patcher in stages.items():
            with self.subTest(stage=name):
                before = self.fail_new_content(patcher())
                self.assert_previous_recording_restored(before)
                self.revert_and_expect_the_restored_report_to_stand()
        self.assertEqual(self.renders(), 1 + len(stages))     # restores rendered nothing

    def test_new_content_is_retried_after_a_failure(self):
        self.fail_new_content(patch.object(synthesis_module, "_run_full_analysis", return_value=False))
        self.assert_renders("content_changed")
        self.assertIn("Both lanes", road_report_text_path().read_text(encoding="utf-8"))

    def test_failed_regenerate_of_unchanged_content_leaves_a_healthy_report(self):
        with patch.object(synthesis_module, "_run_full_analysis", return_value=False):
            with self.assertRaises(CommandError):
                generate("--regenerate")
        self.assert_skips()

    def test_candidate_failures_before_replacement_change_nothing(self):
        before = self.snapshot()
        self.change_content()
        for target, name in ((synthesis_module, "_validate_candidate"), (synthesis_module.os, "link")):
            with self.subTest(failing=name):
                with patch.object(target, name, side_effect=OSError("no space")):
                    with self.assertRaises(CommandError):
                        generate()
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.leftovers(), [])

    def test_analysis_that_changed_markers_is_rerun_on_the_restored_audio(self):
        calls = []

        def analysis(row):
            calls.append((road_report_path().read_bytes(), row.title, row.duration_seconds))
            if len(calls) == 1:                       # the new publication
                analysis_writes_markers(row, next_start=111.0)
                raise RuntimeError("crashed after the markers")
            analysis_writes_markers(row, next_start=250.0)  # the restore
            return True

        self.fail_new_content(patch.object(synthesis_module, "_run_full_analysis", side_effect=analysis))
        self.assertEqual([c[0] for c in calls], [b"FLAC-NEW", b"FLAC-ONE"])
        self.assertEqual(calls[1][1:], ("KanDrive Road Report (Claira)", 300.0))
        self.assertEqual(track().next_start_seconds, 250.0)
        self.assertEqual(self.renders(), 2)                  # no render to restore
        self.assertEqual(self.leftovers(), [])

    def test_untouched_markers_are_not_reanalyzed_during_restore(self):
        with patch.object(synthesis_module, "_run_full_analysis", return_value=False) as analysis:
            with self.assertRaises(CommandError):
                generate("--regenerate")
        self.assertEqual(analysis.call_count, 1)

    def test_restore_that_cannot_complete_stays_unhealthy_until_it_can(self):
        def breaks(row):
            if row.cue_marker_revision == first_revision:
                analysis_writes_markers(row)
            return False

        first_revision = track().cue_marker_revision
        self.fail_new_content(patch.object(synthesis_module, "_run_full_analysis", side_effect=breaks))
        self.assertTrue(road_report_pending_path().exists())
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-ONE")
        self.assertEqual(road_report_previous_path().read_bytes(), b"FLAC-ONE")
        self.assertEqual(published_report_problem(self.fingerprint()), "publication_incomplete")
        self.assertTrue(SystemEvent.objects.filter(category="road_conditions", level="error").exists())
        self.change_content("Road closed.")
        out = generate()                                      # analysis works again
        self.assertIn("Restored the previous road report", out)
        self.assertIn(SKIPPED, out)
        self.assertEqual(self.renders(), 2)

    def test_restore_analysis_that_raises_does_not_wedge_later_runs(self):
        first_revision = track().cue_marker_revision

        def breaks(row):
            if row.cue_marker_revision == first_revision:
                analysis_writes_markers(row)
                return False
            raise RuntimeError("analysis is broken")

        self.fail_new_content(patch.object(synthesis_module, "_run_full_analysis", side_effect=breaks))
        with patch.object(synthesis_module, "_run_full_analysis", side_effect=RuntimeError("still broken")):
            with self.assertRaises(CommandError):
                generate()                       # a normal reported failure, not a crash
        self.assertTrue(road_report_pending_path().exists())
        generate()
        self.assertEqual(published_report_problem(self.fingerprint()), "")
        self.assertEqual(self.leftovers(), [])

    def test_database_error_while_starting_is_an_ordinary_publication_failure(self):
        before = self.snapshot()
        self.change_content()
        with patch.object(synthesis_module, "_published_track", side_effect=RuntimeError("db down")), \
             patch(f"{CMD}.published_report_problem", return_value=""), \
             patch(f"{CMD}.recover_incomplete_publication", return_value=""):
            with self.assertRaises(CommandError) as caught:
                generate()
        self.assertIn("could not start", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_failed_publication_over_missing_audio_restores_columns_and_readiness(self):
        road_report_path().unlink()
        before = (track().title, track().duration_seconds, track().ready2air,
                  road_report_text_path().read_text(encoding="utf-8"))
        use_voice_all_day("night")
        with patch.object(synthesis_module, "_probe_duration", return_value=301.0), \
             patch.object(synthesis_module, "_run_full_analysis", return_value=False):
            with self.assertRaises(CommandError):
                generate()
        self.assertEqual((track().title, track().duration_seconds, track().ready2air,
                          road_report_text_path().read_text(encoding="utf-8")), before)
        self.assertFalse(road_report_path().exists())

    def test_unrelated_concurrent_track_updates_survive_success_and_failure(self):
        aired = dj_timezone.now()

        def engine_and_library(result):
            def analysis(row):
                Track.objects.filter(pk=row.pk).update(play_count=7, last_played_at=aired, energy="upbeat")
                return result
            return analysis

        for result in (False, True):
            with self.subTest(publication_succeeds=result):
                Track.objects.filter(pk=track().pk).update(play_count=0, energy="slow")
                self.change_content(f"Change {result}.")
                with patch.object(synthesis_module, "_run_full_analysis", side_effect=engine_and_library(result)):
                    if result:
                        generate()
                    else:
                        with self.assertRaises(CommandError):
                            generate()
                row = track()
                self.assertEqual((row.play_count, row.last_played_at, row.energy), (7, aired, "upbeat"))

    def test_readiness_changed_during_a_failing_publication_is_kept(self):
        def operator_unchecks(row):
            set_ready(False)
            return False

        self.fail_new_content(patch.object(synthesis_module, "_run_full_analysis", side_effect=operator_unchecks))
        self.assertFalse(track().ready2air)

    def test_failure_to_finish_a_committed_publication_is_completed_next_run(self):
        set_ready(False)
        use_voice_all_day("night")
        self.change_content()
        self._flac_payload = b"FLAC-NEW"
        with patch.object(synthesis_module, "_finish_publication", side_effect=RuntimeError("db down")):
            with self.assertRaises(CommandError):
                generate()
        self.assertFalse(track().ready2air)
        out = generate()
        self.assertIn("Completed an earlier publication", out)
        self.assertTrue(track().ready2air)
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-NEW")
        self.assertEqual(self.provenance()["announcer"]["name"], "Max")
        self.assertEqual(self.leftovers(), [])

    def test_first_ever_publication_failure_leaves_nothing_airable(self):
        Track.objects.filter(filepath=str(road_report_path())).delete()
        for path in Path(self._tmpdir.name).iterdir():
            path.unlink()
        set_config(last_report_fingerprint="")
        with patch.object(synthesis_module, "_run_full_analysis", return_value=False):
            with self.assertRaises(CommandError):
                generate()
        self.assertFalse(road_report_path().exists())
        self.assertFalse(track().ready2air)
        self.assertEqual(sorted(p.name for p in Path(self._tmpdir.name).iterdir()), [])
        generate()
        self.assertTrue(track().ready2air)
        self.assert_skips()

    def test_byte_identical_rerender_that_fails_is_not_mistaken_for_committed(self):
        # Deterministic speech can reproduce the published bytes exactly.
        before = self.snapshot()
        for value in (False, True):
            with self.subTest(ready=value):
                set_ready(value)
                self._flac_payload = b"FLAC-ONE"
                with patch.object(synthesis_module, "_run_full_analysis", return_value=False):
                    with self.assertRaises(CommandError):
                        generate("--regenerate")
                self.assertEqual(track().ready2air, value)
                self.assertEqual(self.leftovers(), [])
        set_ready(True)
        self.assertEqual(self.snapshot(), before)

    def test_byte_identical_rerender_that_is_killed_is_restored_not_completed(self):
        set_ready(False)
        self._flac_payload = b"FLAC-ONE"
        with patch.object(synthesis_module, "_recover_after_failure"), \
             patch.object(synthesis_module, "_run_full_analysis", side_effect=SimulatedKill()):
            with self.assertRaises(SimulatedKill):
                generate("--regenerate")
        out = generate()
        self.assertIn("Restored the previous road report", out)
        self.assertNotIn("Completed an earlier publication", out)
        self.assertFalse(track().ready2air)

    def test_row_is_not_locked_while_the_audio_is_analyzed(self):
        # The engine updates play_count on this row from its main loop
        # when the report starts to air; it must never wait on analysis.
        seen = []

        def engine_air_start(row):
            conn = psycopg2.connect(**connections["default"].get_connection_params())
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("SET lock_timeout = '1s'")
                    cur.execute("UPDATE library_track SET play_count = play_count + 1 WHERE id = %s", [row.pk])
                    seen.append(cur.rowcount)
            finally:
                conn.close()
            return True

        self.change_content()
        with patch.object(synthesis_module, "_run_full_analysis", side_effect=engine_air_start):
            generate()
        self.assertEqual(seen, [1])


class SourceChangesDuringSynthesisTests(PublishedReportMixin, TransactionTestCase):
    def during_synthesis(self, *actions):
        """Runs actions[n] while the n-th next render is being synthesized."""
        real = synthesis_module.synthesize_station_voice.side_effect
        pending = list(actions)

        def wrapped(*args, **kwargs):
            if pending:
                action = pending.pop(0)
                if action:
                    action()
            self._flac_payload = f"FLAC-RENDER-{self._kokoro_call_count + 1}".encode()
            return real(*args, **kwargs)

        synthesis_module.synthesize_station_voice.side_effect = wrapped

    def test_changed_content_discards_the_candidate_and_publishes_the_latest(self):
        self.change_content("First change.")
        self.during_synthesis(lambda: self.change_content("Second change."))
        out = generate()
        self.assertIn("Discarded the new audio before publishing", out)
        self.assertEqual(self.renders(), 3)
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-RENDER-3")
        self.assertIn("Second change.", road_report_text_path().read_text(encoding="utf-8"))
        self.assert_skips()

    def test_content_that_keeps_changing_is_left_for_the_next_retrieval(self):
        self.change_content("One.")
        self.during_synthesis(lambda: self.change_content("Two."), lambda: self.change_content("Three."))
        before = (road_report_provenance_path().read_bytes(), self.fingerprint())
        out = generate()
        self.assertIn("Leaving it for the next successful road data retrieval", out)
        self.assertEqual(self.renders(), 3)                     # two passes, never a third
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-ONE")
        self.assertEqual((road_report_provenance_path().read_bytes(), self.fingerprint()), before)
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(SystemEvent.objects.filter(title="KanDrive audio generation failed").exists())

    def test_feed_failing_during_synthesis_keeps_the_published_report(self):
        self.change_content()
        self.during_synthesis(lambda: set_config(last_error="HTTP 503"))
        out = generate()
        self.assertIn("no longer fresh", out)
        self.assertEqual((self.renders(), road_report_path().read_bytes()), (2, b"FLAC-ONE"))

    def test_events_clearing_during_synthesis_publishes_the_clear_report(self):
        self.change_content()
        self.during_synthesis(lambda: RoadEvent.objects.update(source_active=False))
        generate()
        self.assertIn("not reporting any significant", road_report_text_path().read_text(encoding="utf-8"))

    def test_announcer_shift_during_synthesis_is_not_a_content_change(self):
        self.change_content()
        self.during_synthesis(lambda: use_voice_all_day("night"))
        out = generate()
        self.assertNotIn("Discarded", out)
        self.assertEqual(self.provenance()["announcer"]["name"], "Claira")


class InterruptedPublicationTests(PublishedReportMixin, TransactionTestCase):
    def kill_during(self, target, name, **patch_kwargs):
        use_voice_all_day("night")
        self.change_content()
        self._flac_payload = b"FLAC-NEW"
        patch_kwargs.setdefault("side_effect", SimulatedKill())
        with patch.object(synthesis_module, "_recover_after_failure"), \
             patch.object(target, name, **patch_kwargs):
            with self.assertRaises(SimulatedKill):
                generate()
        use_voice_all_day("day")
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-NEW")
        self.assertTrue(road_report_pending_path().exists())
        self.assertEqual(published_report_problem(self.fingerprint()), "publication_incomplete")

    def revert_and_expect_restore_without_rendering(self):
        self.change_content("Road closed.")
        before = self.renders()
        out = self.assert_skips()
        self.assertIn("Restored the previous road report", out)
        self.assertEqual(self.renders(), before)
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-ONE")
        self.assertEqual(track().title, "KanDrive Road Report (Claira)")
        self.assertIn("Claira Sky", road_report_text_path().read_text(encoding="utf-8"))
        self.assertTrue(track().ready2air)
        self.assertEqual(self.leftovers(), [])

    def test_killed_before_the_track_update(self):
        self.kill_during(Track.objects, "update_or_create")
        self.revert_and_expect_restore_without_rendering()

    def test_killed_during_analysis_after_the_track_update(self):
        with patch.object(synthesis_module, "_probe_duration", return_value=301.0):
            self.kill_during(synthesis_module, "_run_full_analysis")
        self.assertEqual(track().duration_seconds, 301.0)
        self.revert_and_expect_restore_without_rendering()
        self.assertEqual(track().duration_seconds, 300.0)

    def test_killed_after_analysis_changed_the_markers(self):
        def analysis(row):
            analysis_writes_markers(row, next_start=111.0)
            return True

        with patch.object(synthesis_module, "_run_full_analysis", side_effect=analysis):
            self.kill_during(synthesis_module, "write_road_report_text")

        def restore_analysis(row):
            analysis_writes_markers(row, next_start=250.0)
            return True

        with patch.object(synthesis_module, "_run_full_analysis", side_effect=restore_analysis) as rerun:
            self.revert_and_expect_restore_without_rendering()
        self.assertEqual(rerun.call_count, 1)
        self.assertEqual(track().next_start_seconds, 250.0)

    def test_killed_between_the_script_and_the_provenance(self):
        real = synthesis_module._write_json_atomically

        def kill_at_provenance(path, payload):
            if path == road_report_provenance_path():
                raise SimulatedKill()
            return real(path, payload)

        self.kill_during(synthesis_module, "_write_json_atomically", side_effect=kill_at_provenance)
        self.revert_and_expect_restore_without_rendering()

    def test_killed_after_the_commit_is_completed(self):
        set_ready(False)
        self.kill_during(synthesis_module, "_finish_publication")
        out = generate()                       # content is still the new one
        self.assertIn("Completed an earlier publication", out)
        self.assertTrue(track().ready2air)
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-NEW")
        self.assertEqual(self.renders(), 2)     # completing it needed no second render
        self.assert_skips()

    def test_restore_then_render_of_the_new_content(self):
        self.kill_during(Track.objects, "update_or_create")
        out = self.assert_renders("content_changed")
        self.assertIn("Restored the previous road report", out)
        self.assert_skips()

    def test_restore_happens_even_while_the_feed_is_down(self):
        self.kill_during(Track.objects, "update_or_create")
        set_config(last_error="HTTP 503")
        self.assertIn("Restored the previous road report", generate())
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-ONE")

    def test_second_kill_keeps_the_last_complete_recording_as_the_way_back(self):
        self.kill_during(Track.objects, "update_or_create")
        with patch.object(synthesis_module, "recover_incomplete_publication", return_value="unrestorable"), \
             patch(f"{CMD}.recover_incomplete_publication", return_value=""):
            self._flac_payload = b"FLAC-NEWER"
            with patch.object(synthesis_module, "_recover_after_failure"), \
                 patch.object(Track.objects, "update_or_create", side_effect=SimulatedKill()):
                with self.assertRaises(SimulatedKill):
                    generate()
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-NEWER")
        self.assertEqual(road_report_previous_path().read_bytes(), b"FLAC-ONE")
        self.change_content("Road closed.")
        self.assert_skips()
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-ONE")

    def test_killed_first_publication_is_discarded(self):
        Track.objects.filter(filepath=str(road_report_path())).delete()
        for path in Path(self._tmpdir.name).iterdir():
            path.unlink()
        set_config(last_report_fingerprint="")
        with patch.object(synthesis_module, "_recover_after_failure"), \
             patch.object(synthesis_module, "_run_full_analysis", side_effect=SimulatedKill()):
            with self.assertRaises(SimulatedKill):
                generate()
        self.assertFalse(track().ready2air)
        self.assertIn("Removed unfinished audio", generate())
        self.assertTrue(track().ready2air)

    def test_audio_written_by_something_else_is_never_overwritten_by_a_restore(self):
        # e.g. killed, rolled back to r0149 which rendered, then upgraded.
        self.kill_during(Track.objects, "update_or_create")
        road_report_path().write_bytes(b"WRITTEN-BY-ANOTHER-RELEASE")
        self.change_content("Road closed.")
        with patch.object(synthesis_module, "synthesize_station_voice", side_effect=ValueError("tts down")):
            with self.assertRaises(CommandError):
                generate()
        self.assertEqual(road_report_path().read_bytes(), b"WRITTEN-BY-ANOTHER-RELEASE")
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(SystemEvent.objects.filter(title="KanDrive publication did not complete").exists())
        self.assert_renders("flac_mismatch")
        self.assert_skips()

    def test_dry_run_does_not_restore(self):
        self.kill_during(Track.objects, "update_or_create")
        self.assertIn("publication_incomplete", generate("--dry-run"))
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-NEW")
        self.assertTrue(road_report_pending_path().exists())

    def test_leftover_copy_after_a_complete_publication_is_removed(self):
        road_report_previous_path().write_bytes(b"leftover")
        self.assert_skips()
        self.assertFalse(road_report_previous_path().exists())

    def test_restart_with_a_valid_publication_skips(self):
        for _ in range(3):
            connections["default"].close()
            self.assert_skips()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
class RealMediaPublicationTests(TransactionTestCase):
    """Real ffmpeg encode, strict decode, Track analysis and waveform; only
    the speech is replaced by a short generated tone."""

    def setUp(self):
        kind, _ = CategoryKind.objects.get_or_create(code="spot", defaults={"name": "Spot"})
        Category.objects.get_or_create(code="KanDrive", defaults={"name": "KanDrive", "kind": kind})
        ensure_default_weather_voice_personas()
        make_fresh_config()
        use_voice_all_day("day")
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        waveforms = override_settings(WAVEFORMS_DIR=str(self.root / "waveforms"))
        waveforms.enable()
        self.addCleanup(waveforms.disable)
        for name, value in (("KANDRIVE_ROOT", self.root / "KanDrive"), ("TMP_AUDIO_ROOT", self.root / "tmp")):
            patcher = patch.object(synthesis_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tone = (440, 3)
        self.renders = 0

        def speech(text, *, voice, output_path, timeout_seconds=None, **kwargs):
            self.renders += 1
            frequency, seconds = self.tone
            subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", f"sine=frequency={frequency}:sample_rate=24000:duration={seconds}",
                 "-ac", "1", "-c:a", "pcm_s16le", str(output_path)], check=True, timeout=30)

        patcher = patch.object(synthesis_module, "synthesize_station_voice", side_effect=speech)
        patcher.start()
        self.addCleanup(patcher.stop)

    def state(self):
        row = track()
        waveform = json.loads(Path(row.waveform_path).read_text(encoding="utf-8"))
        return {
            "audio": hash_file_sha256(road_report_path()), "script": road_report_text_path().read_text(),
            "provenance": road_report_provenance_path().read_bytes(),
            "title": row.title, "duration": row.duration_seconds, "ready": row.ready2air,
            "cue_in": row.cue_in_seconds, "next_start": row.next_start_seconds,
            "waveform_identity": waveform["audio_identity"]["sha256"],
            "waveform_samples": (waveform["samples_left"], waveform["samples_right"]),
        }

    def change_to_a_longer_report(self):
        self.tone = (880, 6)
        RoadEvent.objects.update(description="Both lanes are closed.")

    def assert_whole_again(self, before):
        self.assertEqual(self.state(), before)
        record, problem = read_provenance()
        self.assertEqual(problem, "")
        self.assertEqual(published_report_problem(record["semantic_fingerprint"]), "")
        self.assertFalse(road_report_pending_path().exists())
        self.assertFalse(road_report_previous_path().exists())
        self.assertEqual(list((self.root / "waveforms").glob(".*previous*")), [])

    def test_publish_then_failure_after_real_analysis_restores_real_metadata(self):
        generate()
        before = self.state()
        self.assertTrue(before["ready"])
        self.assertIsNotNone(before["next_start"])
        self.assertTrue(str(track().waveform_path).startswith(str(self.root)))
        self.assertIn(SKIPPED, generate())
        self.change_to_a_longer_report()
        with patch.object(synthesis_module, "write_road_report_text", side_effect=OSError("disk full")):
            with self.assertRaises(CommandError):
                generate()
        self.assert_whole_again(before)
        self.assertEqual(self.renders, 2)
        # The next ordinary run completes, so the waveform lock was released.
        generate()
        after = self.state()
        self.assertNotEqual(after["audio"], before["audio"])
        self.assertNotEqual(after["next_start"], before["next_start"])
        self.assertNotEqual(after["waveform_identity"], before["waveform_identity"])
        self.assertIn(SKIPPED, generate())

    def test_kill_after_real_analysis_is_restored_without_speech_synthesis(self):
        generate()
        before = self.state()
        self.change_to_a_longer_report()
        with patch.object(synthesis_module, "_recover_after_failure"), \
             patch.object(synthesis_module, "write_road_report_text", side_effect=SimulatedKill()):
            with self.assertRaises(SimulatedKill):
                generate()
        self.assertNotEqual(track().next_start_seconds, before["next_start"])
        RoadEvent.objects.update(description="Road closed.")
        out = generate()
        self.assertIn("Restored the previous road report", out)
        self.assertEqual(self.renders, 2)
        self.assert_whole_again(before)

    def test_real_analysis_failure_restores(self):
        generate()
        before = self.state()
        self.change_to_a_longer_report()
        with patch("library.management.commands.analyze_tracks.decode_audio_to_pcm", return_value=b""):
            with self.assertRaises(CommandError):
                generate()
        self.assert_whole_again(before)

    def test_flipped_bytes_in_the_published_audio_are_detected(self):
        generate()
        data = bytearray(road_report_path().read_bytes())
        data[len(data) // 2] ^= 0xFF
        road_report_path().write_bytes(bytes(data))
        self.assertIn("reason=flac_mismatch", generate())
        self.assertEqual(self.renders, 2)

    def test_strict_decode_gate_rejects_truncated_and_non_audio_candidates(self):
        generate()
        good = road_report_path().read_bytes()
        duration = synthesis_module._probe_duration(road_report_path())
        synthesis_module._validate_candidate(road_report_path(), duration)
        for name, data in (("truncated", good[:len(good) // 2]), ("noise", b"not audio" * 1000)):
            with self.subTest(name=name):
                candidate = self.root / f"{name}.flac"
                candidate.write_bytes(data)
                with self.assertRaises(Exception):
                    synthesis_module._validate_candidate(candidate, duration)

    def test_truncated_encode_never_replaces_the_published_report(self):
        generate()
        good = road_report_path().read_bytes()
        real = synthesis_module._run_loudnorm_two_pass

        def truncate_after_encode(build, tmp_flac):
            real(build, tmp_flac)
            tmp_flac.write_bytes(tmp_flac.read_bytes()[:tmp_flac.stat().st_size // 2])

        with patch.object(synthesis_module, "_run_loudnorm_two_pass", side_effect=truncate_after_encode):
            with self.assertRaises(CommandError):
                generate("--regenerate")
        self.assertEqual(road_report_path().read_bytes(), good)
        self.assertIn(SKIPPED, generate())
